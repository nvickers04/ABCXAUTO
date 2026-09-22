"""Think-stream parsing and strip helpers. Reads text, never rewrites it."""
from __future__ import annotations

from typing import Any

from abcxauto.desktop.tokens import (
    AMBER,
    GREEN,
    MUTED,
    STREAM_ALARM,
    STREAM_POKE,
    STREAM_TAIL_CHARS,
    STREAM_WARN,
    TEXT,
    _IN_FLIGHT_MARKERS,
)

def format_stream_poke(kind: str, detail: str = "") -> str:
    """Visible poke chip for the think stream.

    Unprotected keeps ``[unprotected]`` and appends the event payload (symbols)
    when present. Other poke kinds stay marker-only. Halt/send are untouched.
    """
    k = str(kind or "").strip().lower()
    marker = f"[{k}]" if k else "[]"
    payload = str(detail or "").strip()
    if k == "unprotected" and payload:
        return f"{marker} {payload}"
    return marker


def stream_line_kind(line: str) -> str:
    """Marker class for one raw stream line. Reads the text, never edits it."""
    s = (line or "").strip()
    if not s:
        return "blank"
    if (
        s.startswith("--- GROK")
        or s.startswith("--- CLERK")
        or s.startswith("=== run")
    ):
        return "banner"
    if s == "[think]":
        return "think"
    if s == "[say]":
        return "say"
    if s == "[clerk]":
        return "clerk"
    # Exact chip or chip + payload (e.g. [unprotected] AAPL STK).
    if any(s == poke or s.startswith(f"{poke} ") for poke in STREAM_POKE):
        return "poke"
    if any(frag in s for frag in STREAM_ALARM):
        return "alarm"
    if any(frag in s for frag in STREAM_WARN):
        return "warn"
    if s.startswith("[") and s.endswith("]"):
        if "= already have it" in s:
            return "cached"
        return "send" if s == "[send]" else "tool"
    if s.startswith("hits=") and " src=" in s:
        return "scan"
    if s.startswith("{"):
        # Compact tool dumps from emit(). Chips are [book], not {…}.
        return "json"
    return "prose"


def stream_scan_hits_n(line: str) -> int | None:
    """hits=N from a scan trophy line. Old quoted= and new deepest= both work."""
    raw = (line or "").strip()
    if not raw.startswith("hits="):
        return None
    token = raw[5:].split(None, 1)[0]
    try:
        return int(token)
    except (TypeError, ValueError):
        return None


def stream_view_lines(body: str) -> list[str]:
    """What the think pane paints from think_session_text.

    JSON object dumps stay on the ET keep-file (Copy stream / disk). The pane
    keeps [think]/[say]/[tool] chips, banners, and prose so the look is
    readable. Consecutive JSON object lines collapse to one muted stub.
    """
    out: list[str] = []
    json_chars = 0
    json_n = 0

    def _flush_json() -> None:
        nonlocal json_chars, json_n
        if not json_n:
            return
        if json_n == 1:
            out.append(f"{{json {json_chars:,} chars}}")
        else:
            out.append(f"{{json {json_n} lines, {json_chars:,} chars}}")
        json_chars = 0
        json_n = 0

    for raw in (body or "").splitlines():
        kind = stream_line_kind(raw)
        if kind == "blank":
            continue
        if kind == "json":
            json_n += 1
            json_chars += len(raw.strip())
            continue
        _flush_json()
        out.append(raw)
    _flush_json()
    return out


def current_look_text(buf: str) -> str:
    """The live look: from the last GROK banner, not a clerk speaker."""
    text = buf or ""
    grok = text.rfind("--- GROK")
    return text[grok:] if grok >= 0 else text


def think_tail_tool_chips(buf: str) -> list[str]:
    """Tool names from the readable tail's [chip] lines, not last_turn.tool_trace.

    Each model step paints ``--- GROK ---``, so the last banner is often an
    open [think] with [book]/[scan]/… still above it. Counting only
    current_look_text then paints 0 tools on a live look.
    """
    names: list[str] = []
    for raw in (buf or "")[-STREAM_TAIL_CHARS:].splitlines():
        if stream_line_kind(raw) != "tool":
            continue
        inner = raw.strip()[1:-1].strip()
        if inner:
            names.append(inner.split()[0])
    return names


def think_tail_last_marker(buf: str) -> str:
    """Last stream marker in the tail. Prose keeps the marker it follows."""
    last = ""
    for raw in (buf or "").splitlines():
        kind = stream_line_kind(raw)
        if kind in (
            "think",
            "say",
            "tool",
            "banner",
            "cached",
            "send",
            "warn",
            "alarm",
            "poke",
        ):
            last = kind
    return last


def think_tail_in_flight(buf: str) -> bool:
    """True when the tail is still inside a look (open think, tool, or new banner)."""
    return think_tail_last_marker(current_look_text(buf)) in _IN_FLIGHT_MARKERS


def _say_is_real(text: str) -> bool:
    t = " ".join((text or "").split())
    return bool(t) and t not in {"?", "—", "-", ".", "…"}


def think_tail_last_say(buf: str) -> str:
    """Last real assistant [say] in the tail. Junk '?' does not wipe an earlier say."""
    found: list[str] = []
    lines = (buf or "").splitlines()
    i = 0
    while i < len(lines):
        if lines[i].strip() != "[say]":
            i += 1
            continue
        i += 1
        parts: list[str] = []
        while i < len(lines):
            kind = stream_line_kind(lines[i])
            if kind not in ("prose", "blank"):
                break
            bit = lines[i].strip()
            if bit:
                parts.append(bit)
            i += 1
        text = " ".join(parts).strip()
        if _say_is_real(text):
            found.append(text)
    return found[-1] if found else ""


def last_card_send_label(rows: list[dict[str, Any]] | None = None) -> str:
    """Last named send row when the caller already has it. Persist is gone."""
    if rows is None:
        return ""
    if not rows:
        return ""
    row = rows[-1] if isinstance(rows[-1], dict) else {}
    card = str(row.get("card") or "").strip()
    if not card:
        return ""
    symbol = str(row.get("symbol") or "").strip()
    return f"{symbol} · {card}" if symbol else card


def grok_sub_state(
    *,
    running: bool,
    status: str = "",
    fail_streak: int = 0,
    parked: bool = False,
    tail_moved: bool = False,
    tail_live: bool = False,
    paused: bool = False,
    session_capped: bool = False,
) -> str:
    """One process, one state: looking | sat | idle | paused | look failed | off.

    Cap-idle and overnight park are idle, not sat. A leftover open think after
    the cap is not live. Paused is operator stop. Sat is only between looks.
    """
    if paused:
        return "paused"
    if not running:
        return "off"
    st = (status or "").lower()
    # Session cap / park beat a leftover open think. That look is not live.
    if session_capped or st == "idle" or parked or st == "parked":
        return "idle"
    looking = (
        st.startswith("thinking")
        or st.startswith("grok")
        or bool(tail_moved)
        or bool(tail_live)
    )
    if looking:
        return "looking"
    if int(fail_streak or 0) > 0:
        return "look failed"
    return "sat"


def grok_sub_color(state: str) -> str:
    if state == "looking":
        return GREEN
    if state in ("look failed", "idle", "paused"):
        return AMBER
    if state == "sat":
        return TEXT
    return MUTED


def format_token_count(n: int | float | None) -> str:
    """Compact billed-token count for the strip (2.533M/2.5M)."""
    try:
        count = int(n or 0)
    except (TypeError, ValueError):
        return "0"
    if abs(count) >= 1_000_000:
        text = f"{count / 1_000_000:.3f}".rstrip("0").rstrip(".")
        return f"{text}M"
    if abs(count) >= 1000:
        text = f"{count / 1000:.1f}".rstrip("0").rstrip(".")
        return f"{text}k"
    return str(count)


def session_cap_idle_line(used: dict | None = None) -> str:
    """Which cap tripped, with the numbers. Never sat."""
    row = used if isinstance(used, dict) else {}
    why = str(row.get("why") or "").strip().lower()
    try:
        looks = int(row.get("looks") or 0)
        look_cap = int(row.get("look_cap") or 0)
    except (TypeError, ValueError):
        looks, look_cap = 0, 0
    try:
        tokens = int(row.get("tokens") or 0)
        token_cap = int(row.get("token_cap") or 0)
    except (TypeError, ValueError):
        tokens, token_cap = 0, 0
    look_hit = why in ("looks", "looks+tokens") or (look_cap > 0 and looks >= look_cap)
    token_hit = why in ("tokens", "looks+tokens") or (
        token_cap > 0 and tokens >= token_cap
    )
    bits: list[str] = []
    if look_hit:
        bits.append(f"look cap {looks}/{look_cap}")
    if token_hit:
        bits.append(
            f"token cap {format_token_count(tokens)}/{format_token_count(token_cap)}"
        )
    if not bits:
        return "session cap — idle"
    return f"{' · '.join(bits)} — idle"
