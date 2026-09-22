"""Live Grok thinking stream — think/say tokens as they arrive.

Headless prints to stdout (ASCII). ProEngine binds so the UI can show the same buffer.
A short tail file lets Cursor review the stream without the window.
One append-only file per ET day under data/state/think_session/YYYY-MM-DD.txt
keeps the paid look: spoken [say], tool names, tool args when present, a short
desk line for large tool JSON (chat.append still gets the full paid blob).
Reasoning (kind think) is omitted from the day file and think_live — listeners
still get the original tokens. Bounces append a run banner; begin_run never
wipes it. think_tail.txt stays an 8kb overwrite; think_live stays a 24kb RAM
window.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

Listener = Callable[[str, str], None]

_lock = threading.Lock()
_listeners: list[Listener] = []
_engine: Any = None
_speaker = ""  # last banner in the live stream ("grok" or "")
_STATE_DIR = Path(__file__).resolve().parents[1] / "data" / "state"
_THINK_TAIL_DEFAULT = _STATE_DIR / "think_tail.txt"
_THINK_PREV_DEFAULT = _STATE_DIR / "think_prev.txt"
_THINK_SESSION_DEFAULT = _STATE_DIR / "think_session"
_LAST_TURN_DEFAULT = _STATE_DIR / "last_turn.json"
_DESK_BRIEF_DEFAULT = _STATE_DIR / "desk_brief.json"
_RUN_DEFAULT = _STATE_DIR / "run.json"


class _EnvPath(os.PathLike):
    """Path that re-reads an optional ABCXAUTO_* env var on every use.

    ``from think_stream import LAST_TURN_PATH`` binds this object, not a
    frozen Path, so a later setenv still wins. Empty env is today's
    data/state file.
    """

    __slots__ = ("_env_name", "_default")

    def __init__(self, env_name: str, default: Path) -> None:
        object.__setattr__(self, "_env_name", env_name)
        object.__setattr__(self, "_default", Path(default))

    def _resolve(self) -> Path:
        raw = (os.environ.get(self._env_name) or "").strip()
        return Path(raw) if raw else self._default

    def __fspath__(self) -> str:
        return os.fspath(self._resolve())

    def __str__(self) -> str:
        return str(self._resolve())

    def __repr__(self) -> str:
        return repr(self._resolve())

    def __eq__(self, other: object) -> bool:
        resolved = self._resolve()
        if isinstance(other, _EnvPath):
            return resolved == other._resolve()
        return resolved == other

    def __hash__(self) -> int:
        return hash(self._resolve())

    def __truediv__(self, other: object):
        return self._resolve() / other

    def __rtruediv__(self, other: object):
        return other / self._resolve()

    def __getattr__(self, name: str):
        return getattr(self._resolve(), name)


THINK_TAIL_PATH = _EnvPath("ABCXAUTO_THINK_TAIL_PATH", _THINK_TAIL_DEFAULT)
THINK_PREV_PATH = _EnvPath("ABCXAUTO_THINK_PREV_PATH", _THINK_PREV_DEFAULT)
THINK_SESSION_DIR = _EnvPath("ABCXAUTO_THINK_SESSION_DIR", _THINK_SESSION_DEFAULT)
LAST_TURN_PATH = _EnvPath("ABCXAUTO_LAST_TURN_PATH", _LAST_TURN_DEFAULT)
DESK_BRIEF_PATH = _EnvPath("ABCXAUTO_DESK_BRIEF_PATH", _DESK_BRIEF_DEFAULT)
RUN_PATH = _EnvPath("ABCXAUTO_RUN_PATH", _RUN_DEFAULT)


def _state_path(env_name: str, default: Path) -> Path:
    """Optional ABCXAUTO_* redirect. Empty env keeps today's data/state path."""
    raw = (os.environ.get(env_name) or "").strip()
    return Path(raw) if raw else default


def think_tail_path() -> Path:
    return _state_path("ABCXAUTO_THINK_TAIL_PATH", _THINK_TAIL_DEFAULT)


def think_prev_path() -> Path:
    return _state_path("ABCXAUTO_THINK_PREV_PATH", _THINK_PREV_DEFAULT)


def think_session_dir() -> Path:
    return _state_path("ABCXAUTO_THINK_SESSION_DIR", _THINK_SESSION_DEFAULT)


def last_turn_path() -> Path:
    return _state_path("ABCXAUTO_LAST_TURN_PATH", _LAST_TURN_DEFAULT)


def run_path() -> Path:
    return _state_path("ABCXAUTO_RUN_PATH", _RUN_DEFAULT)


_TAIL_MIN_INTERVAL = 2.0
_last_tail_write = 0.0
_run: dict[str, Any] = {}

logger = logging.getLogger(__name__)

_ASCII_PUNCT = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u2013": "-",
        "\u2014": "-",
        "\u2212": "-",
        "\u2026": "...",
        "\u00a0": " ",
        "\u2265": ">=",
        "\u2264": "<=",
        "\u00b1": "+/-",
    }
)


def ascii_text(text: str) -> str:
    """Windows consoles are often cp1252 — never emit non-ASCII to stdout.

    Common punctuation is mapped to ASCII so a curly apostrophe does not
    paint as '?' (that looked like Grok answering a question mark).
    """
    t = (text or "").translate(_ASCII_PUNCT)
    return t.encode("ascii", "replace").decode("ascii")


def subscribe(fn: Listener) -> None:
    with _lock:
        if fn not in _listeners:
            _listeners.append(fn)


def unsubscribe(fn: Listener) -> None:
    with _lock:
        if fn in _listeners:
            _listeners.remove(fn)


def bind_engine(engine: Any | None) -> None:
    """One ProEngine at a time; think_live buffer updates without flooding the UI queue."""
    global _engine
    _engine = engine


def reset_speaker() -> None:
    """New look / new run starts with no banner so the next emit names who spoke."""
    global _speaker
    with _lock:
        _speaker = ""


_TOOL_CHIP_RE = re.compile(r"^\[([a-z][a-z0-9_]*)(?:\s|=|\])", re.I | re.M)


def empty_grok_segment(buf: str) -> bool:
    """True when the last ``--- GROK ---`` has no [say] after it.

    [think] tokens are not a spoken checkpoint. A [say] with content sits.
    """
    text = str(buf or "")
    last = text.rfind("--- GROK")
    if last < 0:
        return False
    return _grok_segment_empty(text[last:])


def empty_or_junk_grok_tip(buf: str) -> bool:
    """Last ``--- GROK ---`` is silent, think-only, empty, or a junk ``?`` say.

    Wall-clock recover uses the tip content, not ``stream_round``'s stop
    code. A [say] with real words still sits. A tool chip in this segment
    means clerk results are still landing.
    """
    text = str(buf or "")
    last = text.rfind("--- GROK")
    if last < 0:
        return False
    seg = text[last:]
    if _grok_segment_empty(seg):
        return True
    return _grok_segment_junk_say(seg)


def empty_grok_after_tools(buf: str) -> bool:
    """Hung empty GROK after tools and/or send returned. Not a sit clock.

    Typical keepfile::

        --- GROK ---
        [book]
        [quote]
        --- GROK ---

        --- GROK ---
        [option_quote]
        --- GROK ---

        --- GROK ---
        [send]
        --- GROK ---

        --- GROK ---
        [option_quote]
        --- GROK ---
        [think]
        IV on the 765C

    The last banner has no [say]. Tools or send already landed. Worker still up.
    Fat option_quote JSON can wipe chips from the 24kb think_live window —
    engine recover then uses tool_trace + empty last banner, not this helper.
    """
    text = str(buf or "")
    last = text.rfind("--- GROK")
    if last < 0:
        return False
    if not _grok_segment_empty(text[last:]):
        return False
    before = text[:last]
    prev = before.rfind("--- GROK")
    look_before = before[prev:] if prev >= 0 else before
    return bool(_TOOL_CHIP_RE.search(look_before))


def _grok_segment_empty(seg: str) -> bool:
    """True when this GROK banner has no [say] content and no tool chip.

    [think] prose is not a checkpoint. A [say] with content sits. A tool
    chip in this segment means clerk results are still landing, not hung.
    """
    lines = [ln.strip() for ln in str(seg or "").splitlines()]
    if lines and lines[0].startswith("--- GROK"):
        lines = lines[1:]
    in_say = False
    for ln in lines:
        if not ln:
            continue
        if ln.startswith("--- GROK"):
            continue
        if ln.startswith("[stream "):
            continue
        if ln == "[think]":
            in_say = False
            continue
        if ln == "[say]":
            in_say = True
            continue
        if _TOOL_CHIP_RE.match(ln):
            return False
        if in_say:
            return False
    return True


def _grok_segment_junk_say(seg: str) -> bool:
    """True when this GROK banner's [say] is empty or a lone '?'."""
    lines = [ln.strip() for ln in str(seg or "").splitlines()]
    if lines and lines[0].startswith("--- GROK"):
        lines = lines[1:]
    said: list[str] = []
    in_say = False
    for ln in lines:
        if not ln:
            continue
        if ln.startswith("--- GROK"):
            continue
        if ln.startswith("[stream "):
            continue
        if ln == "[think]":
            in_say = False
            continue
        if ln == "[say]":
            in_say = True
            continue
        if _TOOL_CHIP_RE.match(ln):
            return False
        if in_say:
            said.append(ln)
    raw = "\n".join(said).strip()
    return (not raw) or raw == "?"


# Glass/day-file budget for tool results. Short chips and args stay verbatim;
# large JSON becomes one desk line so a live look cannot flood the pane.
_TOOL_GLASS_KEEP = 240
_TOOL_GLASS_MAX = 200


def _try_json_payload(text: str) -> Any | None:
    """Parse a tool blob that is a JSON object or array. Else None."""
    raw = (text or "").strip()
    if not raw or raw[0] not in "{[":
        return None
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    if isinstance(payload, (dict, list)):
        return payload
    return None


def _short_tool_error(err: Any) -> str:
    """One short error line. Drop gRPC debug_error_string noise."""
    s = ascii_text(str(err or "")).strip()
    if not s:
        return "error"
    cut = re.split(r"debug_error_string", s, maxsplit=1, flags=re.I)[0]
    cut = cut.strip(" ,;:\n\t")
    # Common grpc Status trailing junk after the human message.
    cut = re.sub(r"\s*[,{]\s*$", "", cut).strip()
    if not cut:
        cut = "error"
    if len(cut) > _TOOL_GLASS_MAX:
        cut = cut[: _TOOL_GLASS_MAX - 3].rstrip() + "..."
    return cut if cut.lower().startswith("error") else f"error: {cut}"


def _fmt_px(val: Any) -> str:
    try:
        px = float(val)
    except (TypeError, ValueError):
        return ""
    if px != px:  # NaN
        return ""
    if abs(px) >= 100:
        return f"{px:.2f}".rstrip("0").rstrip(".")
    text = f"{px:.4f}".rstrip("0").rstrip(".")
    return text or "0"


def _quote_desk_line(row: dict[str, Any]) -> str | None:
    sym = str(row.get("symbol") or "").upper().strip()
    if not sym:
        return None
    if row.get("last") is None and row.get("bid") is None and row.get("ask") is None:
        if row.get("mid") is None:
            return None
    bits = [sym]
    last = row.get("last")
    if last is None:
        last = row.get("mid")
    last_s = _fmt_px(last)
    if last_s:
        bits.append(f"last={last_s}")
    bid_s = _fmt_px(row.get("bid"))
    ask_s = _fmt_px(row.get("ask"))
    if bid_s or ask_s:
        bits.append(f"bid={bid_s or '-'}/{ask_s or '-'}")
    return " ".join(bits) if len(bits) > 1 else None


def _candle_series_line(row: dict[str, Any]) -> str | None:
    bars = row.get("bars")
    if not isinstance(bars, list):
        return None
    sym = str(row.get("symbol") or "").upper().strip() or "?"
    res = str(row.get("resolution") or row.get("barSize") or "").strip()
    n = len(bars)
    bits = [sym]
    if res:
        bits.append(res)
    bits.append(f"bars={n}")
    last = bars[-1] if bars and isinstance(bars[-1], dict) else {}
    if isinstance(last, dict) and last:
        t = last.get("t_iso") or last.get("t") or last.get("time") or last.get("date")
        c = last.get("c") if last.get("c") is not None else last.get("close")
        c_s = _fmt_px(c)
        if t not in (None, ""):
            bits.append(f"last={t}")
        if c_s:
            bits.append(f"close={c_s}")
    return " ".join(bits)


def _scan_symbols(payload: dict[str, Any]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()

    def _add(raw: Any) -> None:
        sym = str(raw or "").upper().strip()
        if sym and sym not in seen:
            seen.add(sym)
            out.append(sym)

    for raw in payload.get("symbols") or []:
        _add(raw)
    for key in ("hits", "rows"):
        for row in payload.get(key) or []:
            if isinstance(row, dict):
                _add(row.get("symbol"))
            else:
                _add(row)
    return out


def _notes_cards_line(payload: dict[str, Any]) -> str | None:
    """ok/error + id for notes/cards tool results — not the whole card body."""
    note = payload.get("note") if isinstance(payload.get("note"), dict) else None
    card = payload.get("card") if isinstance(payload.get("card"), dict) else None
    body = note or card
    store = str(payload.get("store") or "").strip().lower()
    has_notes_shape = bool(
        body
        or store in ("notes", "cards")
        or isinstance(payload.get("notes"), list)
        or isinstance(payload.get("cards"), list)
        or (payload.get("id") not in (None, "") and "ok" in payload)
    )
    if not has_notes_shape:
        return None
    nid = ""
    if body and body.get("id") not in (None, ""):
        nid = str(body.get("id"))
    elif payload.get("id") not in (None, ""):
        nid = str(payload.get("id"))
    elif isinstance(payload.get("ids"), list) and payload["ids"]:
        nid = ",".join(str(x) for x in payload["ids"][:4] if str(x).strip())
    err = payload.get("error")
    if err not in (None, "") or payload.get("ok") is False:
        bits = [_short_tool_error(err or "failed")]
        if nid:
            bits.append(f"id={nid}")
        return " ".join(bits)
    bits = ["ok"]
    if store:
        bits.append(store)
    if nid:
        bits.append(f"id={nid}")
    n = payload.get("n")
    if n is None and isinstance(payload.get("notes"), list):
        n = len(payload["notes"])
    if n is None and isinstance(payload.get("cards"), list):
        n = len(payload["cards"])
    if n is not None:
        bits.append(f"n={n}")
    return " ".join(bits)


def _is_notes_cards_payload(payload: dict[str, Any]) -> bool:
    store = str(payload.get("store") or "").strip().lower()
    if store in ("notes", "cards"):
        return True
    if isinstance(payload.get("note"), dict) or isinstance(payload.get("card"), dict):
        return True
    if isinstance(payload.get("notes"), list) or isinstance(payload.get("cards"), list):
        return True
    return False


def _summarize_tool_json(payload: Any) -> str:
    """One desk fact from a large tool JSON object/array."""
    if isinstance(payload, list):
        if not payload:
            return "[]"
        if all(isinstance(x, dict) for x in payload):
            if any(isinstance(x.get("bars"), list) for x in payload):
                lines = [_candle_series_line(x) for x in payload[:5]]
                clean = [ln for ln in lines if ln]
                if clean:
                    return "; ".join(clean)
            quotes = [_quote_desk_line(x) for x in payload[:8]]
            clean_q = [ln for ln in quotes if ln]
            if clean_q:
                return "; ".join(clean_q)
        raw = json.dumps(payload, default=str, separators=(",", ":"))
        return raw if len(raw) <= _TOOL_GLASS_MAX else raw[: _TOOL_GLASS_MAX - 3] + "..."

    if not isinstance(payload, dict):
        return str(payload)[:_TOOL_GLASS_MAX]

    # notes/cards first among structured shapes when clearly that tool —
    # still surface ok/error + id (not the card body).
    if _is_notes_cards_payload(payload):
        notes = _notes_cards_line(payload)
        if notes:
            return notes

    err = payload.get("error")
    if err not in (None, ""):
        return _short_tool_error(err)

    alloc = payload.get("allocation_line")
    if isinstance(alloc, str) and alloc.strip():
        return ascii_text(alloc.strip())

    # Candles before quote: a series row can also carry symbol/last metrics.
    if isinstance(payload.get("bars"), list):
        line = _candle_series_line(payload)
        if line:
            return line
    series = payload.get("series")
    if isinstance(series, list) and series:
        lines = [
            _candle_series_line(x)
            for x in series[:5]
            if isinstance(x, dict)
        ]
        clean = [ln for ln in lines if ln]
        if clean:
            return "; ".join(clean)

    if isinstance(payload.get("quotes"), list):
        lines = [
            _quote_desk_line(x)
            for x in payload["quotes"][:8]
            if isinstance(x, dict)
        ]
        clean = [ln for ln in lines if ln]
        if clean:
            return "; ".join(clean)
    qline = _quote_desk_line(payload)
    if qline and not payload.get("hits") and not payload.get("rows"):
        return qline

    # Scan: arena/scan_code + symbols, no hit objects.
    if (
        payload.get("arena") not in (None, "")
        or payload.get("scan_code") not in (None, "")
        or payload.get("hits") is not None
        or (
            isinstance(payload.get("rows"), list)
            and (
                payload.get("symbols") is not None
                or any(
                    isinstance(r, dict) and r.get("symbol")
                    for r in (payload.get("rows") or [])[:3]
                )
            )
        )
    ):
        bits: list[str] = []
        arena = str(payload.get("arena") or "").strip()
        code = str(payload.get("scan_code") or "").strip()
        if arena or code:
            bits.append("/".join(p for p in (arena, code) if p))
        syms = _scan_symbols(payload)
        if syms:
            bits.append(" ".join(syms[:24]))
        elif payload.get("empty"):
            bits.append("empty")
        if bits:
            return " ".join(bits)

    notes = _notes_cards_line(payload)
    if notes:
        return notes

    try:
        raw = json.dumps(payload, default=str, separators=(",", ":"))
    except (TypeError, ValueError):
        raw = str(payload)
    raw = ascii_text(raw)
    if len(raw) <= _TOOL_GLASS_MAX:
        return raw
    return raw[: _TOOL_GLASS_MAX - 3].rstrip() + "..."


def _tool_glass_text(text: str) -> str:
    """Glass/day-file text for one tool emit. Large JSON → one desk line."""
    raw = text or ""
    stripped = raw.strip()
    if not stripped:
        return ascii_text(raw)
    # Short chips, args, and hits= lines stay as emitted.
    if len(stripped) <= _TOOL_GLASS_KEEP:
        return ascii_text(raw)
    payload = _try_json_payload(stripped)
    if payload is not None:
        line = _summarize_tool_json(payload).strip()
        if not line:
            line = stripped[:_TOOL_GLASS_MAX]
        line = ascii_text(line)
        if len(line) > _TOOL_GLASS_MAX:
            line = line[: _TOOL_GLASS_MAX - 3].rstrip() + "..."
        nl = "\n" if raw.endswith("\n") or "\n" in raw else ""
        # Leading newline from brain (`\n[book]\n` style) is for chips only;
        # summarized JSON results are one trailing-newline desk line.
        return f"{line}\n" if (nl or raw.endswith("\n")) else line
    # Large non-JSON tool text: keep a one-line cap.
    one = ascii_text(stripped.replace("\n", " "))
    if len(one) > _TOOL_GLASS_MAX:
        one = one[: _TOOL_GLASS_MAX - 3].rstrip() + "..."
    return one + ("\n" if raw.endswith("\n") else "")


def _paint(kind: str, text: str) -> str:
    """Turn one emit into stream text. Tool results stay unmarked; no clerk speaker."""
    global _speaker
    if kind == "think":
        return ascii_text(text)
    if kind == "stage":
        label = ascii_text(text).strip().upper()
        _speaker = "grok"
        if label in ("", "GROK", "JUDGE", "ACT", "CLERK"):
            return "\n--- GROK ---\n"
        return f"\n--- GROK {label} ---\n"
    if kind == "stage_end":
        return "\n"
    if kind == "tool":
        return _tool_glass_text(text)
    return ascii_text(text)


def emit(kind: str, text: str) -> None:
    if kind not in ("stage", "stage_end") and not text:
        return
    with _lock:
        fns = list(_listeners)
        eng = _engine
        piece = _paint(kind, text)
    # Day file is emit() itself — not a ProEngine side effect. Friday's
    # keep-file appeared at first Pro paint; headless / pre-Pro looks never
    # landed. Glass RAM / think_tail still require a bound engine.
    if piece:
        _append_think_session(piece)
    if eng is not None and piece:
        try:
            # Say and think must reach think_tail even inside the 2s throttle.
            # Tool JSON can wait; spoken text must not stall the glass.
            _append_engine_piece(
                eng, piece, force_tail=kind in ("say", "think")
            )
        except Exception:
            logger.debug("think_stream engine append failed", exc_info=True)
    for fn in fns:
        try:
            fn(kind, text, piece)
        except TypeError:
            try:
                fn(kind, text)
            except Exception:
                logger.debug("think_stream listener failed", exc_info=True)
        except Exception:
            logger.debug("think_stream listener failed", exc_info=True)


def keep(text: str) -> None:
    """Append to today's session keep-file. Does not touch RAM or the glass tail.

    The run banner uses this so a bounce is grep-able before Pro binds.
    Paid tool JSON goes through ``emit()`` so headless looks still land.
    """
    if not text:
        return
    piece = text if text.endswith("\n") else f"{text}\n"
    _append_think_session(piece)


def _append_engine_piece(
    eng: Any, piece: str, *, force_tail: bool = False
) -> None:
    s = getattr(eng, "state", None)
    if s is None or not piece:
        return
    cur = getattr(s, "think_live", "") or ""
    s.think_live = (cur + piece)[-24000:]
    _write_think_tail(s.think_live, force=force_tail)


def _append_engine(eng: Any, kind: str, text: str) -> None:
    piece = _paint(kind, text)
    if not piece:
        return
    _append_engine_piece(eng, piece, force_tail=kind == "say")


def _et_session_day() -> str:
    return datetime.now(ZoneInfo("America/New_York")).date().isoformat()


# Operator wall clock on the run banner. Session *filename* stays ET.
_DESK_TZ = ZoneInfo("America/Chicago")


def _git_sha_short() -> str:
    """Cheap HEAD for the bounce banner. Missing git is ``?``, not a crash."""
    root = Path(__file__).resolve().parents[1]
    try:
        import subprocess

        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(root),
            capture_output=True,
            text=True,
            timeout=1.0,
            check=False,
        )
        sha = (out.stdout or "").strip()
        if 4 <= len(sha) <= 40 and all(c in "0123456789abcdefABCDEF" for c in sha):
            return sha[:12]
    except Exception:
        logger.debug("git sha for run banner failed", exc_info=True)
    try:
        head = (root / ".git" / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref:"):
            ref = head.split(":", 1)[1].strip()
            raw = (root / ".git" / ref).read_text(encoding="utf-8").strip()
        else:
            raw = head
        if raw and all(c in "0123456789abcdefABCDEF" for c in raw[:40]):
            return raw[:12]
    except OSError:
        pass
    return "?"


def _run_banner_line(
    *,
    pid: int | None = None,
    sha: str | None = None,
    now: datetime | None = None,
) -> str:
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=timezone.utc)
    stamp = clock.astimezone(_DESK_TZ).strftime("%Y-%m-%d %H:%M")
    pid_n = os.getpid() if pid is None else int(pid)
    token = (sha if sha is not None else _git_sha_short()) or "?"
    return f"=== run {stamp} CT pid={pid_n} sha={token} ===\n"


def _keep_run_banner() -> None:
    """One line in today's ET keep-file so a bounce is grep-able. Never a new file."""
    line = _run_banner_line()
    try:
        path = _think_session_path()
        if path.is_file() and path.stat().st_size:
            keep("\n" + line)
            return
    except OSError:
        pass
    keep(line)


def _think_session_path() -> Path:
    return think_session_dir() / f"{_et_session_day()}.txt"


_session_read_cache: dict[str, Any] = {
    "path": "",
    "mtime": None,
    "size": None,
    "text": "",
}


def _read_think_session_file() -> str:
    """Today's append-only stream. Empty if the file is missing or unreadable."""
    path = _think_session_path()
    try:
        if not path.is_file():
            _session_read_cache.update(path=str(path), mtime=None, size=None, text="")
            return ""
        st = path.stat()
        ident = str(path)
        if (
            _session_read_cache.get("path") == ident
            and _session_read_cache.get("mtime") == st.st_mtime_ns
            and _session_read_cache.get("size") == st.st_size
        ):
            return str(_session_read_cache.get("text") or "")
        text = path.read_text(encoding="utf-8")
        _session_read_cache.update(
            path=ident, mtime=st.st_mtime_ns, size=st.st_size, text=text
        )
        return text
    except OSError:
        logger.debug("think_session read failed", exc_info=True)
        return str(_session_read_cache.get("text") or "")


def _read_think_tail_file() -> str:
    try:
        if think_tail_path().is_file():
            return think_tail_path().read_text(encoding="utf-8")
    except OSError:
        logger.debug("think_tail read failed", exc_info=True)
    return ""


def _join_session_and_live(session: str, live: str) -> str:
    """Keep the day's file and any live chars that have not landed yet."""
    if not session:
        return live
    if not live:
        return session
    if session.endswith(live):
        return session
    chunk = 512
    while chunk > 0:
        if len(session) >= chunk:
            fp = session[-chunk:]
            at = live.find(fp)
            if at != -1:
                return session + live[at + chunk :]
        chunk //= 2
    if live in session:
        return session
    return session + live


def think_session_text(live: str = "") -> str:
    """Full day's stream for the Pro pane and Copy stream.

    Prefers today's append-only think_session file so a look older than the
    24k RAM window / 8kb glass tail is still readable. Falls back to the live
    buffer, then think_tail.txt, so a missing file never crashes the glass.
    """
    session = _read_think_session_file()
    buf = live or ""
    if session:
        return _join_session_and_live(session, buf)
    if buf:
        return buf
    return _read_think_tail_file()


def _append_think_session(piece: str) -> None:
    """Append to today's ET file. begin_run must not truncate this file."""
    if not piece:
        return
    try:
        think_session_dir().mkdir(parents=True, exist_ok=True)
        with _think_session_path().open("a", encoding="utf-8") as fh:
            fh.write(piece)
    except OSError:
        logger.debug("think_session append failed", exc_info=True)


def _write_think_tail(buf: str, *, force: bool = False) -> None:
    global _last_tail_write
    now = time.monotonic()
    if not force and now - _last_tail_write < _TAIL_MIN_INTERVAL:
        return
    _last_tail_write = now
    try:
        think_tail_path().parent.mkdir(parents=True, exist_ok=True)
        think_tail_path().write_text(buf[-8000:], encoding="utf-8")
    except OSError:
        logger.debug("think_tail write failed", exc_info=True)


def flush_think_tail() -> None:
    """Write the live buffer now so a kill does not lose the last chunks."""
    eng = _engine
    buf = ""
    if eng is not None:
        try:
            buf = str(getattr(getattr(eng, "state", None), "think_live", "") or "")
        except Exception:
            buf = ""
    if not buf and think_tail_path().is_file():
        return
    _write_think_tail(buf or (think_tail_path().read_text(encoding="utf-8") if think_tail_path().is_file() else ""), force=True)


def _archive_think_tail() -> None:
    try:
        if not think_tail_path().is_file():
            return
        text = think_tail_path().read_text(encoding="utf-8")
        if not text.strip():
            return
        think_prev_path().parent.mkdir(parents=True, exist_ok=True)
        think_prev_path().write_text(text, encoding="utf-8")
    except OSError:
        logger.debug("think_prev archive failed", exc_info=True)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        import psutil

        return bool(psutil.pid_exists(pid))
    except Exception:
        pass
    if os.name == "nt":
        try:
            import ctypes

            handle = ctypes.windll.kernel32.OpenProcess(0x00100000, 0, int(pid))
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return False
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {}


def _compact_scan_hits(raw: Any) -> dict[str, Any]:
    """Keep the last screen's triage rows. Symbols-only scan_fetched is not enough."""
    if not isinstance(raw, dict) or not raw:
        return {}
    rows: list[dict[str, Any]] = []
    for r in list(raw.get("rows") or [])[:16]:
        if not isinstance(r, dict):
            continue
        sym = str(r.get("symbol") or "").upper().strip()
        if not sym:
            continue
        item: dict[str, Any] = {"symbol": sym}
        if "skip_class" in r:
            item["skip_class"] = r.get("skip_class") or ""
        else:
            try:
                from abcxauto.universe import scan_skip_class

                item["skip_class"] = scan_skip_class(r)
            except Exception:
                item["skip_class"] = ""
        for key in (
            "rank",
            "screen",
            "scan_code",
            "metric_name",
            "metric_value",
            "gap_pct",
            "last",
            "volume",
            "market_cap",
            "stock_type",
            "source",
            "open",
            "close",
            "change_pct",
            "open_gap_pct",
            "gap%",
            "distance",
            "bid",
            "ask",
            "spread",
            "spread_pct",
            "quote_source",
        ):
            if r.get(key) not in (None, ""):
                item[key] = r[key]
        ibkr = r.get("ibkr") if isinstance(r.get("ibkr"), dict) else None
        if ibkr:
            live: dict[str, Any] = {}
            for key in ("last", "bid", "ask", "asof_iso", "freshness", "source"):
                if ibkr.get(key) not in (None, ""):
                    live[key] = ibkr[key]
            if live:
                item["ibkr"] = live
        mda = r.get("mda") if isinstance(r.get("mda"), dict) else None
        if mda:
            delayed: dict[str, Any] = {}
            for key in (
                "mda_last",
                "sma20",
                "dist20",
                "ret5",
                "freshness",
                "source",
                "asof_iso",
            ):
                if mda.get(key) not in (None, ""):
                    delayed[key] = mda[key]
            news = mda.get("news")
            if isinstance(news, list) and news:
                delayed["news_n"] = len(news)
            if delayed:
                item["mda"] = delayed
        rows.append(item)
    if not rows and not raw.get("scan_code") and not raw.get("arena"):
        return {}
    return {
        "source": raw.get("source"),
        "arena": raw.get("arena"),
        "scan_code": raw.get("scan_code"),
        "ranked": bool(raw.get("ranked")),
        "rank_meaning": raw.get("rank_meaning") or "",
        "quoted": raw.get("quoted"),
        "rows": rows,
    }


def _compact_live_quotes(raw: Any) -> dict[str, float]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, float] = {}
    for key, val in list(raw.items())[:16]:
        name = str(key or "").upper().strip()
        raw_px = val.get("last") if isinstance(val, dict) else val
        if raw_px is None and isinstance(val, dict):
            raw_px = val.get("mid")
        try:
            px = float(raw_px)
        except (TypeError, ValueError):
            continue
        if name and px > 0:
            out[name] = px
    return out


def _signed_open_gap(row: dict[str, Any] | None) -> float:
    """Signed gap% / open_gap_pct. Missing metric is 0."""
    try:
        from abcxauto.opportunity_scan import row_gap_pct
    except Exception:
        row_gap_pct = None  # type: ignore[assignment]
    if row_gap_pct is not None:
        gap = row_gap_pct(row)
        return float(gap) if gap is not None else 0.0
    if not isinstance(row, dict):
        return 0.0
    raw = row.get("gap%")
    if raw is None:
        raw = row.get("open_gap_pct")
    if raw is None:
        return 0.0
    try:
        return float(raw)
    except (TypeError, ValueError):
        return 0.0


def _open_gap_mag(row: dict[str, Any] | None) -> float:
    """|gap%| (or open_gap_pct). Missing gap sorts last."""
    try:
        from abcxauto.opportunity_scan import row_gap_pct
    except Exception:
        row_gap_pct = None  # type: ignore[assignment]
    if row_gap_pct is not None:
        gap = row_gap_pct(row)
        return abs(float(gap)) if gap is not None else -1.0
    if not isinstance(row, dict):
        return -1.0
    raw = row.get("gap%")
    if raw is None:
        raw = row.get("open_gap_pct")
    if raw is None:
        return -1.0
    try:
        return abs(float(raw))
    except (TypeError, ValueError):
        return -1.0


def sort_scan_rows(rows: list[Any] | None) -> list[dict[str, Any]]:
    """Biggest |gap| first. Levered / micro sort after so they are not the lead."""
    clean = [r for r in (rows or []) if isinstance(r, dict)]
    try:
        from abcxauto.universe import scan_skip_class
    except Exception:
        return sorted(clean, key=_open_gap_mag, reverse=True)

    def _key(row: dict[str, Any]) -> tuple[int, float]:
        return (1 if scan_skip_class(row) else 0, -_open_gap_mag(row))

    return sorted(clean, key=_key)


def merge_scan_hits(prior: Any, incoming: Any) -> dict[str, Any]:
    """Union this look's screens. Last-scan-wins used to hide the gap row."""
    new = _compact_scan_hits(incoming)
    old = _compact_scan_hits(prior)
    if not old:
        return {**new, "rows": sort_scan_rows(new.get("rows"))} if new else new
    if not new:
        return {**old, "rows": sort_scan_rows(old.get("rows"))}
    by: dict[str, dict[str, Any]] = {}
    for r in list(old.get("rows") or []) + list(new.get("rows") or []):
        if not isinstance(r, dict):
            continue
        sym = str(r.get("symbol") or "").upper().strip()
        if not sym:
            continue
        prev = by.get(sym)
        if prev is None:
            by[sym] = dict(r)
            continue
        keep = dict(prev)
        if _open_gap_mag(r) > _open_gap_mag(prev):
            keep.update({k: v for k, v in r.items() if v not in (None, "")})
        else:
            for k, v in r.items():
                if keep.get(k) in (None, "") and v not in (None, ""):
                    keep[k] = v
        by[sym] = keep
    rows = sort_scan_rows(list(by.values()))
    new_best = max((_open_gap_mag(r) for r in (new.get("rows") or [])), default=-1.0)
    old_best = max((_open_gap_mag(r) for r in (old.get("rows") or [])), default=-1.0)
    meta = new if new_best >= old_best else old
    return {
        "source": meta.get("source") or new.get("source"),
        "arena": meta.get("arena") or new.get("arena"),
        "scan_code": meta.get("scan_code") or new.get("scan_code"),
        "ranked": bool(meta.get("ranked") if meta.get("ranked") is not None else new.get("ranked")),
        "rank_meaning": meta.get("rank_meaning") or new.get("rank_meaning") or "",
        "quoted": new.get("quoted") if new.get("quoted") not in (None, "") else old.get("quoted"),
        "rows": rows[:16],
    }


def current_run() -> dict[str, Any]:
    if _run:
        return dict(_run)
    return _read_json(run_path())


def mark_review_stale(*, archive_tail: bool = False) -> None:
    """Mark last_turn dead. Keep the think tail so a mid-turn kill is readable."""
    flush_think_tail()
    prev = _read_json(last_turn_path())
    run = current_run()
    payload = {
        "stale": True,
        "previous_run_id": prev.get("run_id") or run.get("run_id") or "",
        "previous_strat": prev.get("strat") or "",
        "open_lots": list(prev.get("open_lots") or []),
        "net_liquidation": prev.get("net_liquidation"),
        "flat": prev.get("flat"),
        "session": prev.get("session") or {},
        "ibkr_connected": prev.get("ibkr_connected"),
        "mix": prev.get("mix") if isinstance(prev.get("mix"), dict) else {},
        "rationale": (prev.get("rationale") or "")[:1200],
        "tool_trace": list(prev.get("tool_trace") or []),
        "sends": prev.get("sends") or 0,
        "send_calls": prev.get("send_calls") or 0,
        "scan_fetched": list(prev.get("scan_fetched") or []),
        "scan_hits": _compact_scan_hits(prev.get("scan_hits")),
        "candle_source": prev.get("candle_source") or "none",
    }
    try:
        last_turn_path().parent.mkdir(parents=True, exist_ok=True)
        last_turn_path().write_text(json.dumps(payload, indent=2), encoding="utf-8")
    except OSError:
        logger.debug("last_turn stale mark failed", exc_info=True)
    if archive_tail:
        _archive_think_tail()
        try:
            if think_tail_path().is_file():
                think_tail_path().write_text("", encoding="utf-8")
        except OSError:
            logger.debug("think_tail clear failed", exc_info=True)


def _last_turn_is_this_hunt(prev: dict[str, Any] | None) -> bool:
    """A completed look from this hunt. Overnight and mid-turn kills are not."""
    if not isinstance(prev, dict) or not prev:
        return False
    if prev.get("stale"):
        return False
    if str(prev.get("strat") or "") == "in_progress":
        return False
    return last_look_is_fresh(prev)


def begin_run() -> dict[str, Any]:
    """Stamp a new process identity. Call after killing leftovers.

    A fresh completed look stays on disk so a reload does not wipe
    the tape the next wake needs. Overnight and killed mid-turn still stale.
    Today's think_session file is one ET day — bounces append a run banner;
    this must not truncate or replace that file.
    """
    global _run
    prev = _read_json(last_turn_path())
    if _last_turn_is_this_hunt(prev):
        _archive_think_tail()
        try:
            if think_tail_path().is_file():
                think_tail_path().write_text("", encoding="utf-8")
        except OSError:
            logger.debug("think_tail clear failed", exc_info=True)
    else:
        mark_review_stale(archive_tail=True)
    reset_speaker()
    try:
        from abcxauto.park_clock import ensure_next_look

        # Overnight / after-close only. RTH and premarket write no sit clock.
        ensure_next_look(previous_set_at="")
    except Exception:
        logger.debug("grok_wake seed on begin_run failed", exc_info=True)
    _run = {
        "run_id": uuid.uuid4().hex,
        "pid": os.getpid(),
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        from abcxauto.config import launch_model_knobs

        _run.update(launch_model_knobs(reload=True))
    except Exception:
        logger.debug("launch model knobs on begin_run failed", exc_info=True)
    try:
        run_path().parent.mkdir(parents=True, exist_ok=True)
        run_path().write_text(json.dumps(_run, indent=2), encoding="utf-8")
    except OSError:
        logger.debug("run.json write failed", exc_info=True)
    _keep_run_banner()
    return dict(_run)


_SESSION_KEEP = (
    "date",
    "today",
    "open",
    "high",
    "low",
    "last",
    "n",
    "vs_open",
    "vs_low",
    "above_open",
    "above_low",
    "bid",
    "ask",
    "spread",
    "spread_pct",
    "prior_close",
    "gap_pts",
    "gap_pct",
    "open_gap_pct",
    "retrace_30",
    "retrace_50",
    "size",
    "ticket",
    "print",
    "source",
)


def _refresh_session_today(rng: dict[str, Any]) -> dict[str, Any]:
    out = {key: rng[key] for key in _SESSION_KEEP if key in rng}
    day = str(out.get("date") or "")
    if day:
        try:
            from abcxauto.opportunity_scan import _et_calendar_day

            out["today"] = day == _et_calendar_day()
        except Exception:
            pass
    return out


def _compact_session_range(raw: Any) -> dict[str, Any]:
    """Keep today's opening-low tape only. A prior-day range is not a stop."""
    if not isinstance(raw, dict) or not raw:
        return {}
    out: dict[str, Any] = {}
    for sym, rng in raw.items():
        name = str(sym or "").upper().strip()
        if not name or not isinstance(rng, dict):
            continue
        row = _refresh_session_today(rng)
        if row.get("today") is False:
            continue
        if row.get("low") is None and row.get("open") is None:
            continue
        out[name] = row
        if len(out) >= 8:
            break
    return out


def _quotes_from_scan_hits(hits: Any) -> dict[str, float]:
    """IBKR lasts already printed on the last screen. Never MDA row.last."""
    blob = hits if isinstance(hits, dict) else {}
    out: dict[str, float] = {}
    for row in blob.get("rows") or []:
        if not isinstance(row, dict):
            continue
        sym = str(row.get("symbol") or "").upper().strip()
        ibkr = row.get("ibkr") if isinstance(row.get("ibkr"), dict) else {}
        raw = ibkr.get("last")
        try:
            px = float(raw)
        except (TypeError, ValueError):
            continue
        if sym and px > 0:
            out[sym] = px
    return out


def _seed_live_quotes_from_last(
    snap: dict[str, Any],
    data: dict[str, Any],
    hits: Any,
) -> None:
    qmap = snap.get("ibkr_live_quotes")
    if not isinstance(qmap, dict):
        qmap = {}
        snap["ibkr_live_quotes"] = qmap
    persisted = data.get("ibkr_live_quotes")
    if isinstance(persisted, dict):
        for key, raw in persisted.items():
            try:
                px = float(raw)
            except (TypeError, ValueError):
                continue
            name = str(key or "").upper().strip()
            if name and px > 0:
                qmap.setdefault(name, px)
    for name, px in _quotes_from_scan_hits(hits).items():
        qmap.setdefault(name, px)


def seed_snap_from_last_turn(snap: dict[str, Any] | None) -> None:
    """Carry last look's send geometry onto a fresh IBKR snap.

    This-look screens are not inherited: do not copy ``scan_hits`` or
    ``scan_at``. Grok fetches a screen if it wants one. Session range may
    copy when last_turn is fresh (open-hunt send geometry).
    ``snap()`` always stamps ``candle_source`` (``none`` until bars run).
    Copy a real last_turn source only when the live snap is blank or ``none``.
    Skip overwriting a live this-look bar source. Skip ``none`` as a source.
    """
    if not isinstance(snap, dict):
        return
    data = _read_json(last_turn_path())
    if not data:
        return
    existing = str(snap.get("candle_source") or "").strip()
    if existing in ("", "none"):
        src = str(data.get("candle_source") or "").strip()
        if src and src not in ("none",):
            snap["candle_source"] = src
    fresh = not data.get("stale") and last_look_is_fresh(data)
    if not snap.get("session_range") and fresh:
        rng = _compact_session_range(data.get("session_range"))
        if rng:
            snap["session_range"] = rng


def last_turn_is_live(payload: dict[str, Any] | None = None) -> bool:
    """True only if last_turn belongs to this live process."""
    data = payload if isinstance(payload, dict) else _read_json(last_turn_path())
    if not data or data.get("stale"):
        return False
    run = current_run()
    if not run or data.get("run_id") != run.get("run_id"):
        return False
    try:
        pid = int(run.get("pid") or 0)
    except (TypeError, ValueError):
        pid = 0
    if pid and not _pid_alive(pid):
        return False
    return True


def _desk_brief_path() -> Path:
    raw = (os.environ.get("ABCXAUTO_DESK_BRIEF_PATH") or "").strip()
    return Path(raw) if raw else _DESK_BRIEF_DEFAULT


def load_desk_brief() -> dict[str, Any]:
    """Last completed look. in_progress last_turn is not memory."""
    p = _desk_brief_path()
    if not p.is_file():
        data = _read_json(last_turn_path())
        if data.get("stale") or str(data.get("strat") or "") == "in_progress":
            return {}
        return data
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return {}


# Same-hunt window. Overnight / killed-desk briefs must not resume as next=.
LAST_LOOK_FRESH_S = 45 * 60
# A 30s–3m hunt re-wake must not re-pull every card screen. A 5m hunt rescans.
# A protected manage look can reuse the last tape longer — fill/unprotected still interrupt.
SCAN_REUSE_S = 180.0
SCAN_REUSE_MANAGE_S = 15 * 60.0


def _scan_hits_asof_age_s(
    row: dict[str, Any] | None,
    now: datetime | None = None,
) -> float | None:
    """Newest IBKR quote asof on persisted scan rows. None if the tape has no clock."""
    hits = (row or {}).get("scan_hits") if isinstance(row, dict) else {}
    ages: list[float] = []
    for item in (hits.get("rows") or []) if isinstance(hits, dict) else []:
        if not isinstance(item, dict):
            continue
        ibkr = item.get("ibkr") if isinstance(item.get("ibkr"), dict) else {}
        # IBKR clocks only. MDA asof_iso is color, never a trigger.
        raw = str(ibkr.get("asof_iso") or "").strip()
        if not raw:
            continue
        age = last_look_age_s({"ts": raw}, now=now)
        if age is not None:
            ages.append(age)
    return min(ages) if ages else None


def scan_tape_age_s(
    row: dict[str, Any] | None,
    now: datetime | None = None,
) -> float | None:
    """Age of the last IBKR scan fetch. last_turn ts slides every look."""
    stamped = str((row or {}).get("scan_at") or "").strip()
    if stamped:
        return last_look_age_s({**(row or {}), "ts": stamped}, now=now)
    asof_age = _scan_hits_asof_age_s(row, now=now)
    if asof_age is not None:
        return asof_age
    return last_look_age_s(row, now=now)


def last_look_age_s(
    row: dict[str, Any] | None,
    now: datetime | None = None,
) -> float | None:
    ts = str((row or {}).get("ts") or "").strip()
    if not ts:
        return None
    try:
        stamped = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if stamped.tzinfo is None:
            stamped = stamped.replace(tzinfo=timezone.utc)
        clock = now or datetime.now(timezone.utc)
        if clock.tzinfo is None:
            clock = clock.replace(tzinfo=timezone.utc)
        return max(0.0, (clock - stamped.astimezone(timezone.utc)).total_seconds())
    except Exception:
        return None


def last_look_is_fresh(
    row: dict[str, Any] | None,
    *,
    now: datetime | None = None,
) -> bool:
    if not isinstance(row, dict) or not row:
        return False
    if "fresh" in row:
        return bool(row["fresh"])
    age = last_look_age_s(row, now)
    if age is None:
        return True
    return age <= LAST_LOOK_FRESH_S


def last_look_for_hunt(brief: dict[str, Any] | None = None) -> dict[str, Any]:
    """Last look only when it is still this hunt. Stale tape is not next=."""
    facts = last_look_facts() if brief is None else last_look_facts(brief)
    if not facts or facts.get("fresh") is False:
        return {}
    return facts


def last_look_wake_bit(brief: dict[str, Any] | None = None) -> str:
    """No leftover say / unused= / card homework on the next wake."""
    _ = brief
    return ""


def last_look_facts(brief: dict[str, Any] | None = None) -> dict[str, Any]:
    """Last completed look as structured facts for book(). Empty if none."""
    loaded = brief is None
    row = brief if isinstance(brief, dict) else load_desk_brief()
    if not isinstance(row, dict) or not row:
        return {}
    try:
        n = int(row.get("send_calls") if row.get("send_calls") is not None else row.get("sends") or 0)
    except (TypeError, ValueError):
        n = 0
    tools: list[str] = []
    for raw in list(row.get("tool_trace") or []):
        name = str(raw or "").strip()
        if name and name not in tools:
            tools.append(name)
        if len(tools) >= 8:
            break
    hits = _compact_scan_hits(row.get("scan_hits"))
    quotes = _compact_live_quotes(row.get("ibkr_live_quotes"))
    if not quotes:
        quotes = _quotes_from_scan_hits(hits)
    out: dict[str, Any] = {
        "send_calls": n,
        "tools": tools,
        "scan_hits": hits,
        "session_range": _compact_session_range(row.get("session_range")),
        "ibkr_live_quotes": quotes,
        "fresh": last_look_is_fresh(row),
    }
    if loaded:
        last = _read_json(last_turn_path())
        if last.get("stale"):
            out["fresh"] = False
    if out["fresh"] is False:
        return {
            "fresh": False,
            "send_calls": n,
            "tools": [],
            "scan_hits": {},
            "session_range": {},
            "ibkr_live_quotes": {},
        }
    # Leftover say is not the next job. Facts only.
    return out if (tools or hits or n) else {}


def last_turn_look_failed(out: dict[str, Any] | None) -> bool:
    """True when this persist payload is a junk/empty/dead look, not a completed turn.

    ``write_desk_brief`` skips ``strat=="in_progress"`` so the last completed
    look stays on the brief. ``write_last_turn`` skips a true empty / lone
    ``?`` look so it does not blank last_turn.json. A real say or send/fill
    is a finished look — leftover ``_failed`` / dead-stream stamps do not
    wipe it. Overnight / park still write.
    """
    if not isinstance(out, dict):
        return False
    if str(out.get("strat") or "") == "in_progress":
        return False
    if out.get("_parked") or out.get("parked"):
        return False
    try:
        if int(out.get("sends") or 0) > 0:
            return False
    except (TypeError, ValueError):
        pass
    rationale = str(out.get("rationale") or "").strip()
    if rationale and rationale != "?":
        return False
    if out.get("_stream_error") or out.get("stream_error"):
        return True
    return bool(out.get("_failed") or out.get("failed"))


def write_desk_brief(payload: dict[str, Any]) -> None:
    if str(payload.get("strat") or "") == "in_progress":
        return
    sends = payload.get("sends") or 0
    send_calls = payload.get("send_calls")
    if send_calls is None:
        send_calls = len(
            [t for t in (payload.get("tool_trace") or []) if str(t) == "send"]
        )
    row = {
        "strat": payload.get("strat"),
        "sends": sends,
        "send_calls": send_calls,
        "open_lots": list(payload.get("open_lots") or [])[:32],
        "net_liquidation": payload.get("net_liquidation"),
        "mix": payload.get("mix") if isinstance(payload.get("mix"), dict) else {},
        "rationale": (payload.get("rationale") or "")[:800],
        "tool_trace": list(payload.get("tool_trace") or [])[:16],
        "scan_hits": _compact_scan_hits(payload.get("scan_hits")),
        "session_range": _compact_session_range(payload.get("session_range")),
        "ibkr_live_quotes": _compact_live_quotes(payload.get("ibkr_live_quotes")),
        "ts": payload.get("ts") or datetime.now(timezone.utc).isoformat(),
    }
    p = _desk_brief_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(row, indent=2), encoding="utf-8")
    except OSError:
        logger.debug("desk_brief write failed", exc_info=True)


def _mix_of(out: dict[str, Any], world: dict[str, Any]) -> dict[str, Any]:
    for src in (out.get("mix"), world.get("mix")):
        if isinstance(src, dict) and src:
            return src
    try:
        from abcxauto.world_state import structure_mix

        return structure_mix(out.get("positions") or world.get("positions"))
    except Exception:
        return {}


def write_last_turn_after_send(
    *,
    strat: str,
    sends: int,
    positions: list[dict[str, Any]] | None,
    orders: list[dict[str, Any]] | None = None,
    rationale: str = "",
    tool_trace: list[str] | None = None,
    net_liquidation: float | None = None,
    reality_pulse: dict[str, Any] | None = None,
    ibkr_live_last: Any = None,
    ibkr_live_quotes: dict[str, Any] | None = None,
    scan_hits: dict[str, Any] | None = None,
    session_range: dict[str, Any] | None = None,
) -> None:
    """Stamp last_turn from the live book right after a successful send.

    Do not wait for cycle persist — bounce must not see the pre-send snap.
    """
    from abcxauto.world_state import book_is_flat, lot_labels

    pos = list(positions or [])
    lots = lot_labels(pos)
    write_last_turn({
        "strat": strat,
        "sends": int(sends),
        "positions": pos,
        "open_lots": lots,
        "rationale": rationale,
        "tool_trace": list(tool_trace or []),
        "scan_hits": _compact_scan_hits(scan_hits),
        "session_range": _compact_session_range(session_range),
        "reality_pulse": reality_pulse or {},
        "ibkr_live_last": ibkr_live_last,
        "ibkr_live_quotes": dict(ibkr_live_quotes or {}),
        "world_state": {
            "flat": book_is_flat(pos, orders),
            "open_lots": lots,
            "positions": pos,
            "net_liquidation": net_liquidation,
        },
    })


def write_last_turn(out: dict[str, Any]) -> None:
    """Snapshot of the last Grok turn for Cursor review.

    Operator paint has no sit clock. Journal/logs keep the increment.
    A junk / empty / failed look is not a completed turn — keep the last
    real say/tools until a real look finishes. Overnight / park still write.
    """
    if last_turn_look_failed(out):
        return
    try:
        last_turn_path().parent.mkdir(parents=True, exist_ok=True)
        pulse = out.get("reality_pulse") or {}
        world = out.get("world_state") or {}
        run = current_run()
        gates = world.get("gates") if isinstance(world.get("gates"), dict) else {}
        fresh = pulse.get("data_freshness") if isinstance(pulse.get("data_freshness"), dict) else {}
        ibkr = pulse.get("ibkr_connected")
        if ibkr is None:
            ibkr = fresh.get("ibkr_connected")
        from abcxauto.world_state import lot_labels

        open_lots = list(out.get("open_lots") or world.get("open_lots") or [])
        if not open_lots:
            open_lots = lot_labels(out.get("positions") or world.get("positions"))
        unreliable = bool(
            out.get("book_unreliable") or gates.get("book_unreliable")
        )
        ibkr_down = ibkr is False or "ibkr_down" in str(out.get("validation") or "")
        prior = _read_json(last_turn_path())
        if (unreliable or ibkr_down) and not open_lots:
            open_lots = list(prior.get("open_lots") or [])
        nl = world.get("net_liquidation") or out.get("equity")
        try:
            nl_f = float(nl) if nl is not None else 0.0
        except (TypeError, ValueError):
            nl_f = 0.0
        if (unreliable or ibkr_down) and nl_f <= 0 and prior.get("net_liquidation"):
            nl = prior.get("net_liquidation")
            world = dict(world)
            world["net_liquidation"] = nl
        skip = str(out.get("validation") or out.get("skip_reason") or "")
        if skip.startswith("skipped_grok:"):
            skip = skip.split(":", 1)[-1].strip()
        elif out.get("strat") in ("skipped", "blocked"):
            skip = skip or str(out.get("strat") or "")
        else:
            skip = ""
        payload = {
            "strat": out.get("strat"),
            "rationale": (out.get("rationale") or "")[:1200],
            "validation": (out.get("validation") or "")[:400],
            "tool_trace": list(out.get("tool_trace") or []),
            "stage_error": out.get("stage_error") or "",
            "session": pulse.get("session") or world.get("session") or {},
            "scan_fetched": list(
                out.get("scan_fetched") or world.get("scan_fetched") or []
            ),
            "scan_hits": _compact_scan_hits(
                out.get("scan_hits") or world.get("scan_hits")
            ),
            "ibkr_connected": ibkr,
            "open_lots": open_lots,
            "book_unreliable": bool(
                out.get("book_unreliable") or gates.get("book_unreliable")
            ),
            "skip_reason": skip[:120],
            "f10_tripped": bool(out.get("f10_tripped")),
            "loop_halted": bool(out.get("loop_halted")),
            "model_cost_post_trip_USD": float(out.get("model_cost_post_trip_USD") or 0.0),
            "flat": world.get("flat"),
            "net_liquidation": world.get("net_liquidation") or out.get("equity") or nl,
            "mix": _mix_of(out, world),
            "sends": (
                int(out["sends"])
                if isinstance(out.get("sends"), int)
                else len([t for t in (out.get("tool_trace") or []) if str(t) == "send"])
            ),
            # sends counts every mutating tool, self_tune included. Only
            # send_calls answers "did a ticket reach the broker path".
            "send_calls": len(
                [t for t in (out.get("tool_trace") or []) if str(t) == "send"]
            ),
            "run_id": run.get("run_id") or "",
            "pid": run.get("pid"),
            "ts": datetime.now(timezone.utc).isoformat(),
            "stale": False,
            "ibkr_live_last": world.get("ibkr_live_last") or out.get("ibkr_live_last"),
            "ibkr_live_quotes": dict(
                world.get("ibkr_live_quotes") or out.get("ibkr_live_quotes") or {}
            ),
            "candle_source": (
                world.get("candle_source") or out.get("candle_source") or "none"
            ),
            "session_range": _compact_session_range(
                out.get("session_range")
                or world.get("session_range")
            ),
            "scan_screens": [
                str(x) for x in (out.get("scan_screens") or []) if str(x).strip()
            ][:8],
            "scan_calls": int(out.get("scan_calls") or 0),
            "scan_at": str(
                out.get("scan_at")
                or world.get("scan_at")
                or prior.get("scan_at")
                or ""
            ).strip(),
        }
        if str(payload.get("strat") or "") == "in_progress":
            brief = load_desk_brief()
            if brief.get("strat"):
                payload["previous_strat"] = brief.get("strat")
                payload["previous_sends"] = brief.get("sends") or 0
        last_turn_path().write_text(json.dumps(payload, indent=2), encoding="utf-8")
        write_desk_brief(payload)
    except OSError:
        logger.debug("last_turn write failed", exc_info=True)


def stdout_printer(kind: str, text: str, piece: str = "") -> None:
    t = piece if piece else _paint(kind, text)
    if not t:
        return
    sys.stdout.write(t)
    sys.stdout.flush()
