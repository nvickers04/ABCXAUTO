"""Paper lab playbook â€” Grok's notebook; live only follows a promote.

Notebook is not executable, not a wake clock, not a standing order.
Clerk validates writes against gates (floors / live / sleeve) like self_tune.
Paper researches established structures, journals what beat model cost, and does those more.
Live never copies paper fills. It may take new risk only after a promoted
snapshot exists (scorecard beating + Grok marked ready). Operator still must
connect live TWS (7496) with the confirm phrase. Two processes, two client ids.

The saved book is a TYPE tree: trunk = sendable ORDER_EXAMPLES keys, branches =
strategies under a type. Tool order on a card is a recipe, not a clerk gate.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_LAB = _REPO_ROOT / "playbook_lab.json"
_DEFAULT_LIVE = _REPO_ROOT / "playbook_live.json"
_MAX_INSTRUCTIONS = 16000
_LEDGER_CAP = 12
_PATCH_KEYS = (
    "instructions",
    "types",
    "catalog",
    "mode",
    "ready_to_promote",
)
_MAX_STRATEGIES_PER_TYPE = 12
_STRATEGY_FIELDS = ("name", "when_on", "tool_order", "ticket_shape", "invalidation", "note")
# Not catalog trunks: knobs, plus defined_risk_only rejects.
_SKIP_PLAYBOOK_TYPES = frozenset({
    "set_risk",
    "self_tune",
    "ratio_spread",
    "jade_lizard",
})
# Trunk = these sendable ORDER_EXAMPLES keys. Never invert.
PLAYBOOK_TYPE_KEYS = (
    "market_bracket",
    "bracket",
    "trailing_stop",
    "modify_stop",
    "modify_target",
    "cancel_order",
    "close_option",
    "buy_option",
    "vertical_spread",
    "calendar_spread",
    "diagonal_spread",
    "butterfly",
    "iron_butterfly",
    "iron_condor",
    "straddle",
    "strangle",
    "protective_put",
    "collar",
    "covered_call",
    "cash_secured_put",
)
_TYPE_META_KEYS = frozenset({
    "mode",
    "ready_to_promote",
    "instructions",
    "types",
    "catalog",
    "strategies",
    "defined_risk",
    "open_shape",
    "close_tp_sl",
    "default_tool_recipe",
})
_HARD_SHAPE = frozenset({"unknown_type", "ticker_list", "diary", "shape"})
# Ceremony leftovers. Must not linger via save_lab merging **prev.
_DEAD_LAB_KEYS = (
    "do_more",
    "stop_doing",
    "basis",
    "evidence",
    "research_tools",
    "diary",
    "nap",
    "naps",
    "wake_at",
    "wake_in_s",
    "wake_if",
    "ticker_list",
    "tickers",
)
# Notebook is not self_tune. Same class of knobs self_tune rejects or clamps â€”
# write_lab_playbook must not loosen floors, switch live, or set a dollar sleeve.
_GATE_FORBIDDEN: dict[str, str] = {
    "trading_mode": "live remains gated â€” notebook cannot switch mode",
    "live_confirm": "live remains gated â€” notebook cannot switch mode",
    "sizing_floors": "operator-only â€” notebook cannot flip sizing floors",
    "trading_budget_usd": "size and risk are % of NetLiq â€” no dollar sleeve",
    "risk_posture": "risk_posture is locked â€” notebook cannot retune",
    "daily_loss_limit_pct": "knobs are self_tune â€” notebook cannot retune risk",
    "max_position_pct": "knobs are self_tune â€” notebook cannot retune risk",
    "max_risk_per_trade_pct": "knobs are self_tune â€” notebook cannot retune risk",
    "max_peak_drawdown_pct": "knobs are self_tune â€” notebook cannot retune risk",
    "max_option_premium_pct": "knobs are self_tune â€” notebook cannot retune risk",
    "max_open_positions": "knobs are self_tune â€” notebook cannot retune risk",
    "risk_gates_enabled": "immutable floor â€” notebook cannot disable",
    "auto_panic_on_breach": "immutable floor â€” notebook cannot disable",
    "defined_risk_only": "immutable floor â€” notebook cannot disable",
    "cash_only": "immutable floor â€” notebook cannot disable",
}
# GATES: N% / floor N% NL is clerk law. Notebook may restate it only when
# sizing_floors is ON and N is the live max_risk_per_trade_pct knob.
_GATES_HDR = re.compile(r"\bGATES\b[^:\n]{0,48}:", re.IGNORECASE)
_FLOOR_NL = re.compile(r"\bfloor\s+(\d+(?:\.\d+)?)\s*%\s*NL\b", re.IGNORECASE)
_PCT = re.compile(r"(\d+(?:\.\d+)?)\s*%")
_TYPE_HDR = re.compile(r"^TYPE\s+(\S+)", re.IGNORECASE)
_STRATEGY_HDR = re.compile(r"^STRATEGY\s+(.+)$", re.IGNORECASE)
_FIELD_LINE = re.compile(
    r"^(defined_risk|open_shape|close_tp_sl|default_tool_recipe|name|when_on|"
    r"tool_order|ticket_shape|invalidation|note)\s*[:=]\s*(.*)$",
    re.IGNORECASE,
)
_DIARY_OR_CLOCK = re.compile(
    r"\b(nap|naps|napping|diary|wake_at|wake_in_s|wake_in\b|set_wake|"
    r"park until|no new risk until)\b",
    re.IGNORECASE,
)
_TICKER_TOKEN = re.compile(r"^[A-Z]{1,5}$")
_STALE_H_DEFAULT = 1.0
_CARD_WINDOWS = ("15m", "1h", "4h")


def stale_hours() -> float:
    import os

    raw = (os.environ.get("ABCXAUTO_PLAYBOOK_STALE_H") or "").strip()
    if not raw:
        return _STALE_H_DEFAULT
    try:
        return max(0.25, float(raw))
    except ValueError:
        return _STALE_H_DEFAULT


def _lab_path() -> Path:
    import os

    raw = (os.environ.get("ABCXAUTO_PLAYBOOK_LAB_PATH") or "").strip()
    return Path(raw) if raw else _DEFAULT_LAB


def _live_path() -> Path:
    import os

    raw = (os.environ.get("ABCXAUTO_PLAYBOOK_LIVE_PATH") or "").strip()
    return Path(raw) if raw else _DEFAULT_LIVE


def _read(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except Exception:
        logger.debug("playbook read failed path=%s", path, exc_info=True)
        return {}


def _write(path: Path, state: dict[str, Any]) -> None:
    try:
        path.write_text(json.dumps(state, indent=2, default=str) + "\n", encoding="utf-8")
    except Exception:
        logger.exception("playbook write failed path=%s", path)


def _drop_dead_lab_keys(state: dict[str, Any]) -> dict[str, Any]:
    out = dict(state)
    for key in _DEAD_LAB_KEYS:
        out.pop(key, None)
    for key in _GATE_FORBIDDEN:
        out.pop(key, None)
    out.pop("risk", None)
    out.pop("universe", None)
    out.pop("rejected", None)
    return out


def load_lab() -> dict[str, Any]:
    return _drop_dead_lab_keys(_read(_lab_path()))


def load_live() -> dict[str, Any]:
    return _drop_dead_lab_keys(_read(_live_path()))


def is_paper() -> bool:
    try:
        from abcxauto.config import get_config

        return bool(get_config().is_paper)
    except Exception:
        return True


def _field(raw: dict[str, Any], prev: dict[str, Any], key: str, default: str = "") -> str:
    if key in raw:
        return str(raw.get(key) or default)
    return str(prev.get(key) or default)


def playbook_type_keys() -> tuple[str, ...]:
    """Sendable ORDER_EXAMPLES keys the notebook may use as trunks."""
    from abcxauto.order_examples import NOT_TICKETS, ORDER_EXAMPLES

    skip = _SKIP_PLAYBOOK_TYPES | NOT_TICKETS
    return tuple(k for k in PLAYBOOK_TYPE_KEYS if k in ORDER_EXAMPLES and k not in skip)


def _close_tp_sl(name: str) -> str:
    from abcxauto.order_examples import COMBO_BAG_CLOSE

    if name in COMBO_BAG_CLOSE:
        return "same strategy + closing_position + limit_price (one BAG)"
    if name == "buy_option":
        return "close_option (prefer conId)"
    if name in ("market_bracket", "bracket"):
        return "child stop + target; modify_stop / modify_target / cancel_order"
    if name in ("trailing_stop", "trailing_stop_limit"):
        return "trail is the stop; cancel_order or market/limit/stop close"
    if name == "oca":
        return "protection legs; modify_stop / modify_target / cancel_order"
    if name in ("modify_stop", "modify_target", "cancel_order", "close_option"):
        return "this type is the close"
    if name in ("market_order", "limit_order", "stop_order", "stop_limit"):
        return "stock close (closing_position)"
    if name in ("protective_put", "collar", "covered_call", "cash_secured_put"):
        return "close stock+option legs; cancel_order / close_option as needed"
    if name == "roll_option":
        return "roll of an existing option"
    return "cancel_order or market/limit/stop close (closing_position)"


def _empty_stanza(name: str = "") -> dict[str, Any]:
    from abcxauto.order_examples import ORDER_EXAMPLES

    params = ORDER_EXAMPLES.get(name) or {}
    return {
        "defined_risk": True,
        "open_shape": ", ".join(str(k) for k in params.keys()),
        "close_tp_sl": _close_tp_sl(name) if name else "",
        "strategies": [],
    }


def empty_type_catalog() -> dict[str, Any]:
    """One stanza per allowed sendable type, strategies=[]. No tickers."""
    return {name: _empty_stanza(name) for name in playbook_type_keys()}


def _as_bool(val: Any, default: bool | None = None) -> bool | None:
    if val is None:
        return default
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    s = str(val).strip().lower()
    if s in ("1", "true", "yes", "y"):
        return True
    if s in ("0", "false", "no", "n", ""):
        return False
    return default


def _norm_recipe(raw: Any) -> list[str]:
    """Optional tool recipe. Stored, never gated."""
    if isinstance(raw, str):
        parts = [p.strip() for p in re.split(r"[,;]+", raw) if p.strip()]
        return parts[:16]
    if isinstance(raw, (list, tuple)):
        return [str(x).strip() for x in raw if str(x).strip()][:16]
    return []


def _norm_strategy(raw: Any) -> dict[str, str] | None:
    if isinstance(raw, str):
        name = raw.strip()
        if not name:
            return None
        raw = {"name": name}
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name") or "").strip()
    if not name:
        return None
    tool_order = raw.get("tool_order") or raw.get("tool_recipe") or raw.get("default_tool_recipe")
    if isinstance(tool_order, (list, tuple)):
        tool_order = ", ".join(str(x).strip() for x in tool_order if str(x).strip())
    else:
        tool_order = str(tool_order or "").strip()
    return {
        "name": name[:120],
        "when_on": str(raw.get("when_on") or "").strip()[:800],
        "tool_order": tool_order[:400],
        "ticket_shape": str(raw.get("ticket_shape") or "").strip()[:800],
        "invalidation": str(raw.get("invalidation") or "").strip()[:800],
        "note": str(raw.get("note") or raw.get("notes") or "").strip()[:800],
    }


def _norm_strategies(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in raw:
        row = _norm_strategy(item)
        if not row:
            continue
        key = row["name"].lower()
        if key in seen:
            out = [r for r in out if r["name"].lower() != key]
        else:
            seen.add(key)
        out.append(row)
        if len(out) >= _MAX_STRATEGIES_PER_TYPE:
            break
    return out


def _merge_strategies(
    prev: list[dict[str, str]],
    incoming: list[dict[str, str]],
) -> list[dict[str, str]]:
    by_name: dict[str, dict[str, str]] = {}
    order: list[str] = []
    for row in prev:
        key = row["name"].lower()
        by_name[key] = row
        order.append(key)
    for row in incoming:
        key = row["name"].lower()
        by_name[key] = row
        if key not in order:
            order.append(key)
    merged = [by_name[k] for k in order if k in by_name]
    return merged[:_MAX_STRATEGIES_PER_TYPE]


def _floors_and_knob() -> tuple[bool, float]:
    """Live clerk flag + max_risk_per_trade_pct. Fail closed: floors off."""
    try:
        from abcxauto.config import get_config
        from abcxauto.risk_gates import sizing_floors_active

        cfg = get_config()
        knob = float(getattr(cfg, "max_risk_per_trade_pct", 0) or 0)
        return bool(sizing_floors_active(cfg)), knob
    except Exception:
        return False, 0.0


def _gate_pcts_on_line(line: str) -> list[float]:
    """Percents claimed as GATES: N% or floor N% NL on one line."""
    pcts: list[float] = []
    if _GATES_HDR.search(line):
        pcts.extend(float(m.group(1)) for m in _PCT.finditer(line))
    pcts.extend(float(m.group(1)) for m in _FLOOR_NL.finditer(line))
    return pcts


def _invented_pct_gate_line(line: str, floors_on: bool, knob: float) -> bool:
    """True when this line invents a % gate the clerk is not enforcing."""
    pcts = _gate_pcts_on_line(line)
    if not pcts:
        return False
    if floors_on and knob > 0 and all(abs(n - knob) < 1e-6 for n in pcts):
        return False
    return True


def _has_invented_pct_gate(text: str) -> bool:
    floors_on, knob = _floors_and_knob()
    return any(_invented_pct_gate_line(line, floors_on, knob) for line in text.splitlines())


def _strip_invented_pct_gate_lines(text: str) -> str:
    """Drop GATES: N% / floor N% NL lines unless floors ON and N is the live knob."""
    floors_on, knob = _floors_and_knob()
    kept = [
        line
        for line in text.splitlines()
        if not _invented_pct_gate_line(line, floors_on, knob)
    ]
    return "\n".join(kept)


def _walk_text(obj: Any) -> str:
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        return "\n".join(_walk_text(v) for v in obj.values())
    if isinstance(obj, (list, tuple)):
        return "\n".join(_walk_text(v) for v in obj)
    return ""


def _norm_type_row(row: Any, *, prev: dict[str, Any] | None = None) -> dict[str, Any]:
    prev = prev if isinstance(prev, dict) else {}
    src = row if isinstance(row, dict) else {}
    out: dict[str, Any] = {}
    if "defined_risk" in src:
        dr = _as_bool(src.get("defined_risk"))
        if dr is not None:
            out["defined_risk"] = dr
    elif "defined_risk" in prev:
        out["defined_risk"] = bool(prev.get("defined_risk"))
    open_s = src["open_shape"] if "open_shape" in src else prev.get("open_shape")
    close_s = src["close_tp_sl"] if "close_tp_sl" in src else prev.get("close_tp_sl")
    out["open_shape"] = str(open_s or "")[:800]
    out["close_tp_sl"] = str(close_s or "")[:800]
    if "default_tool_recipe" in src:
        rec = _norm_recipe(src.get("default_tool_recipe"))
        if rec:
            out["default_tool_recipe"] = rec
    elif prev.get("default_tool_recipe"):
        rec = _norm_recipe(prev.get("default_tool_recipe"))
        if rec:
            out["default_tool_recipe"] = rec
    if "strategies" in src:
        if src.get("strategies") == []:
            out["strategies"] = []
        else:
            incoming_s = _norm_strategies(src.get("strategies"))
            out["strategies"] = _merge_strategies(
                _norm_strategies(prev.get("strategies")), incoming_s
            )
    else:
        out["strategies"] = _norm_strategies(prev.get("strategies"))
    return out


def _strip_gates_from_types(types: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, row in types.items():
        if not isinstance(row, dict):
            continue
        stanza = dict(row)
        for key in ("open_shape", "close_tp_sl"):
            stanza[key] = _strip_invented_pct_gate_lines(str(stanza.get(key) or ""))
        rec = stanza.get("default_tool_recipe")
        if isinstance(rec, str):
            cleaned = _norm_recipe(_strip_invented_pct_gate_lines(rec))
            if cleaned:
                stanza["default_tool_recipe"] = cleaned
            else:
                stanza.pop("default_tool_recipe", None)
        cleaned: list[dict[str, str]] = []
        for strat in stanza.get("strategies") or []:
            if not isinstance(strat, dict):
                continue
            item = dict(strat)
            for key in _STRATEGY_FIELDS:
                item[key] = _strip_invented_pct_gate_lines(str(item.get(key) or ""))
            if item.get("name"):
                cleaned.append(item)
        stanza["strategies"] = cleaned
        out[name] = stanza
    return out


def render_playbook_tree(types: dict[str, Any] | None) -> str:
    """Readable tree: TYPE trunks, then child strategies."""
    if not isinstance(types, dict) or not types:
        return ""
    lines: list[str] = []
    for name in playbook_type_keys():
        row = types.get(name)
        if not isinstance(row, dict):
            continue
        if "defined_risk" in row:
            dr = "yes" if row.get("defined_risk") else "no"
            lines.append(f"TYPE {name}  defined_risk={dr}")
        else:
            lines.append(f"TYPE {name}")
        open_s = str(row.get("open_shape") or "").strip()
        close_s = str(row.get("close_tp_sl") or "").strip()
        recipe = row.get("default_tool_recipe")
        if open_s:
            lines.append(f"  open: {open_s}")
        if close_s:
            lines.append(f"  close: {close_s}")
        if recipe:
            if isinstance(recipe, (list, tuple)):
                rec_s = ", ".join(str(x) for x in recipe if str(x).strip())
            else:
                rec_s = str(recipe).strip()
            if rec_s:
                lines.append(f"  recipe: {rec_s}")
        strats = [
            s for s in (row.get("strategies") or [])
            if isinstance(s, dict) and str(s.get("name") or "").strip()
        ]
        if not strats:
            lines.append("  strategies: []")
        else:
            for strat in strats:
                nm = str(strat.get("name") or "").strip()
                lines.append(f"  - {nm}")
                for key in ("when_on", "tool_order", "ticket_shape", "invalidation", "note"):
                    val = str(strat.get(key) or "").strip()
                    if val:
                        lines.append(f"      {key}: {val}")
    return "\n".join(lines)


def notebook_text(state: dict[str, Any] | None) -> str:
    """Rendered TYPE tree when types exist; else leftover instructions."""
    blob = state if isinstance(state, dict) else {}
    types = blob.get("types")
    if isinstance(types, dict) and types:
        tree = render_playbook_tree(types)
        if tree:
            return tree
    return str(blob.get("instructions") or "").strip()


def _has_book(state: dict[str, Any] | None) -> bool:
    return bool(notebook_text(state))


def _unknown_type_keys(blob: dict[str, Any]) -> list[str]:
    allowed = set(playbook_type_keys())
    return sorted(
        k for k in blob
        if k not in allowed and k not in _TYPE_META_KEYS
    )


def _parse_structured_text(text: str) -> dict[str, Any] | None:
    types: dict[str, Any] = {}
    cur_type: str | None = None
    cur_strat: dict[str, str] | None = None

    def _flush_strat() -> None:
        nonlocal cur_strat
        if cur_type and cur_strat and cur_strat.get("name"):
            types.setdefault(cur_type, {"strategies": []})
            types[cur_type].setdefault("strategies", []).append(dict(cur_strat))
        cur_strat = None

    found_type = False
    for raw_line in text.splitlines():
        s = raw_line.strip()
        if not s:
            continue
        m = _TYPE_HDR.match(s)
        if m:
            _flush_strat()
            cur_type = m.group(1).strip().rstrip(":").strip()
            types.setdefault(cur_type, {"strategies": []})
            cur_strat = None
            found_type = True
            continue
        m = _STRATEGY_HDR.match(s)
        if m:
            _flush_strat()
            cur_strat = {
                "name": m.group(1).strip().rstrip(":").strip(),
                "when_on": "",
                "tool_order": "",
                "ticket_shape": "",
                "invalidation": "",
                "note": "",
            }
            continue
        m = _FIELD_LINE.match(s)
        if not m:
            continue
        field = m.group(1).lower()
        val = m.group(2).strip()
        if field in ("defined_risk", "open_shape", "close_tp_sl", "default_tool_recipe"):
            if cur_type and cur_strat is None:
                types.setdefault(cur_type, {"strategies": []})
                if field == "defined_risk":
                    types[cur_type][field] = val.lower() in ("1", "true", "yes", "y")
                elif field == "default_tool_recipe":
                    types[cur_type][field] = _norm_recipe(val)
                else:
                    types[cur_type][field] = val
            continue
        if cur_strat is not None:
            cur_strat[field] = val
    _flush_strat()
    if not found_type:
        return None
    return types


def _coerce_types_blob(blob: Any) -> dict[str, Any] | None:
    if blob is True:
        return {}
    if isinstance(blob, str):
        text = blob.strip()
        if not text:
            return None
        if text.lower() in ("catalog", "type catalog", "types"):
            return {}
        if text[:1] in "{[":
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                inner = parsed.get("types")
                if isinstance(inner, dict):
                    return inner
                if (
                    parsed == {}
                    or any(k in playbook_type_keys() for k in parsed)
                    or _looks_like_type_map(parsed)
                ):
                    return parsed
                return None
        return _parse_structured_text(text)
    if isinstance(blob, dict):
        inner = blob.get("types")
        if isinstance(inner, dict) and (
            inner == {}
            or any(k in playbook_type_keys() for k in inner)
            or _looks_like_type_map(inner)
        ):
            return inner
        if any(k in playbook_type_keys() for k in blob) or blob == {} or _looks_like_type_map(blob):
            return blob
    return None


def _looks_like_type_map(blob: dict[str, Any]) -> bool:
    if not blob:
        return False
    stanza = {"strategies", "defined_risk", "open_shape", "close_tp_sl"}
    values = list(blob.values())
    if not all(isinstance(v, dict) for v in values):
        return False
    return any(stanza & set(v) for v in values)


def _extract_types(raw: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
    """Incoming types dict, or None if omitted. Error is unknown_type / unstructured."""
    for key in ("types", "catalog"):
        if key not in raw:
            continue
        blob = raw.get(key)
        if blob is None or blob == "":
            continue
        parsed = _coerce_types_blob(blob)
        if parsed is not None:
            if _unknown_type_keys(parsed):
                return None, "unknown_type"
            return parsed, ""
        return None, "unstructured"
    inst = raw.get("instructions")
    if isinstance(inst, dict):
        parsed = _coerce_types_blob(inst)
        if parsed is not None:
            if _unknown_type_keys(parsed):
                return None, "unknown_type"
            return parsed, ""
        return None, "unstructured"
    if isinstance(inst, str) and inst.strip():
        parsed = _coerce_types_blob(inst)
        if parsed is not None:
            if _unknown_type_keys(parsed):
                return None, "unknown_type"
            return parsed, ""
        return None, "unstructured"
    return None, ""


def _is_ticker_list(text: str) -> bool:
    """Whole book is a ticker list, not mixed English (NO/NEW/UNTIL are not names)."""
    if re.search(r"\bTYPE\b", text, re.IGNORECASE):
        return False
    if _DIARY_OR_CLOCK.search(text):
        return False
    cleaned = re.sub(r"[.]+", " ", text).strip()
    tokens = [t for t in re.split(r"[\s,;|/]+", cleaned.upper()) if t]
    if len(tokens) < 2:
        return False
    if not all(_TICKER_TOKEN.match(t) for t in tokens):
        return False
    if "," in text or ";" in text:
        return True
    letters = [c for c in text if c.isalpha()]
    return bool(letters) and all(c.isupper() for c in letters)


def book_shape_rejects(raw: Any) -> dict[str, str]:
    """Reject diary / ticker lists / unknown send keys as the whole book."""
    if not isinstance(raw, dict):
        return {}
    incoming, err = _extract_types(raw)
    if err == "unknown_type":
        blob = raw.get("types") if "types" in raw else raw.get("catalog")
        if blob is None:
            blob = raw.get("instructions")
        parsed = blob if isinstance(blob, dict) else _coerce_types_blob(blob)
        bad = _unknown_type_keys(parsed) if isinstance(parsed, dict) else []
        if not bad and isinstance(blob, str):
            parsed_text = _parse_structured_text(blob)
            bad = _unknown_type_keys(parsed_text) if parsed_text else []
        label = ", ".join(bad) if bad else "unknown"
        return {"unknown_type": f"do not add unknown types ({label})"}
    if incoming is not None:
        return {}
    inst = str(raw.get("instructions") or "").strip()
    if not inst:
        return {}
    if _has_invented_pct_gate(inst) and not [
        ln for ln in inst.splitlines()
        if ln.strip() and not _invented_pct_gate_line(ln, *_floors_and_knob())
    ]:
        return {}
    if _DIARY_OR_CLOCK.search(inst):
        return {"diary": "notebook is a TYPE tree, not a diary/nap/wake clock"}
    if _is_ticker_list(inst):
        return {"ticker_list": "tickers are picked in the look, not stored as the book"}
    if err == "unstructured":
        return {"shape": "notebook is a TYPE tree, not a diary"}
    return {}


def _merge_type_catalog(
    prev: dict[str, Any],
    incoming: dict[str, Any],
) -> dict[str, Any] | None:
    allowed = playbook_type_keys()
    allowed_set = set(allowed)
    if _unknown_type_keys(incoming):
        return None
    out = empty_type_catalog()
    prev_types = prev.get("types") if isinstance(prev.get("types"), dict) else {}
    for name in allowed:
        clerk = out[name]
        prev_row = prev_types.get(name) if isinstance(prev_types.get(name), dict) else {}
        merged = _norm_type_row(prev_row, prev=clerk)
        merged["defined_risk"] = clerk["defined_risk"]
        merged["open_shape"] = clerk["open_shape"]
        merged["close_tp_sl"] = clerk["close_tp_sl"]
        out[name] = merged
    if incoming == {}:
        return _strip_gates_from_types(out)
    for name, row in incoming.items():
        if name not in allowed_set:
            continue
        clerk = out[name]
        merged = _norm_type_row(row, prev=out.get(name))
        merged["defined_risk"] = clerk["defined_risk"]
        merged["open_shape"] = clerk["open_shape"]
        merged["close_tp_sl"] = clerk["close_tp_sl"]
        out[name] = merged
    return _strip_gates_from_types(out)


def gate_rejects(raw: Any) -> dict[str, str]:
    """Reject floors / live / sleeve knobs on a notebook write. Notes stay notes."""
    if not isinstance(raw, dict):
        return {}
    rejected: dict[str, str] = {}
    for key, reason in _GATE_FORBIDDEN.items():
        if key in raw:
            rejected[key] = reason
    # Nested self_tune-shaped blobs are not notebook fields either.
    for nest in ("risk", "universe"):
        blob = raw.get(nest)
        if nest in raw and isinstance(blob, dict):
            rejected[nest] = "knobs are self_tune â€” notebook cannot retune"
            for key, reason in _GATE_FORBIDDEN.items():
                if key in blob:
                    rejected[key] = reason
    inst = str(raw.get("instructions") or "")
    types_text = _walk_text(raw.get("types")) if isinstance(raw.get("types"), dict) else ""
    catalog_text = _walk_text(raw.get("catalog")) if isinstance(raw.get("catalog"), dict) else ""
    if (
        (inst and _has_invented_pct_gate(inst))
        or (types_text and _has_invented_pct_gate(types_text))
        or (catalog_text and _has_invented_pct_gate(catalog_text))
    ):
        rejected["invented_pct_gate"] = "notebook cannot invent a % gate"
    return rejected


def clamp_update(raw: Any) -> dict[str, Any] | None:
    """Full rewrite or patch. Omitted fields keep the previous lab text.

    Gate knobs (floors / live / sleeve) are never stored â€” see gate_rejects.
    Invented GATES: N% / floor N% NL lines are stripped unless floors are ON
    and N equals the live max_risk_per_trade_pct knob.
    The book is a TYPE tree (sendable keys), not a diary.
    """
    if not isinstance(raw, dict):
        return None
    if not any(k in raw for k in _PATCH_KEYS):
        return None
    if _HARD_SHAPE.intersection(book_shape_rejects(raw)):
        return None
    prev = load_lab()
    incoming, err = _extract_types(raw)
    if err:
        return None
    types: dict[str, Any] | None
    if incoming is not None:
        types = _merge_type_catalog(prev, incoming)
        if types is None:
            return None
        instructions = render_playbook_tree(types)
        instructions = _strip_invented_pct_gate_lines(instructions)
    else:
        types = prev.get("types") if isinstance(prev.get("types"), dict) else {}
        if types:
            instructions = render_playbook_tree(types)
        else:
            instructions = _field(raw, prev, "instructions")
            if "instructions" in raw:
                instructions = _strip_invented_pct_gate_lines(instructions)
    instructions = instructions.strip()[:_MAX_INSTRUCTIONS]
    if not instructions and not types:
        return None
    mode = _field(raw, prev, "mode", "explore").strip().lower()
    if mode not in ("explore", "exploit"):
        mode = "explore"
    ready = raw["ready_to_promote"] if "ready_to_promote" in raw else prev.get("ready_to_promote")
    out: dict[str, Any] = {
        "mode": mode,
        "instructions": instructions,
        "ready_to_promote": bool(ready),
    }
    if types:
        out["types"] = types
    return out


def grounding_error(
    raw: dict[str, Any] | None,
    *,
    tool_trace: list[str] | None = None,
) -> str:
    """Shape only. The notebook is Grok's; clerk does not demand a constitution."""
    if not isinstance(raw, dict):
        return "write_lab_playbook needs a notebook object"
    return ""


def _score_snap(scorecard: dict[str, Any] | None) -> dict[str, Any]:
    sc = scorecard if isinstance(scorecard, dict) else {}
    return {
        "beating_model": sc.get("beating_model"),
        "edge_usd": sc.get("edge_usd"),
        "book_return_pct": sc.get("book_return_pct"),
        "model_cost_usd": sc.get("model_cost_usd"),
    }


def _outcome_card(card: dict[str, Any] | None) -> dict[str, Any]:
    """Ledger row is the score of a card, not the notes."""
    out = dict(card) if isinstance(card, dict) else {}
    out.pop("instructions", None)
    out.pop("types", None)
    for key in _DEAD_LAB_KEYS:
        out.pop(key, None)
    return out


def _ledger_card(state: dict[str, Any], scorecard: dict[str, Any] | None = None) -> dict[str, Any]:
    sc = scorecard if isinstance(scorecard, dict) else (
        state.get("paper_score") if isinstance(state.get("paper_score"), dict) else {}
    )
    lots = [str(x) for x in (state.get("lots_at_write") or [])][:16]
    return {
        "revision": state.get("revision"),
        "written_at": state.get("written_at"),
        "mode": state.get("mode"),
        "ready_to_promote": bool(state.get("ready_to_promote")),
        "beating_model": sc.get("beating_model"),
        "edge_usd": sc.get("edge_usd"),
        "book_return_pct": sc.get("book_return_pct"),
        "lots_at_write": lots,
    }


def _compact_card(card: dict[str, Any] | None) -> dict[str, Any]:
    c = card if isinstance(card, dict) else {}
    return {
        "revision": c.get("revision"),
        "written_at": c.get("written_at"),
        "mode": c.get("mode"),
        "ready_to_promote": c.get("ready_to_promote"),
        "beating_model": c.get("beating_model"),
        "edge_usd": c.get("edge_usd"),
        "book_return_pct": c.get("book_return_pct"),
        "closed_edge": c.get("closed_edge"),
        "closed_beating": c.get("closed_beating"),
        "closed_at": c.get("closed_at"),
    }


def _close_card(
    card: dict[str, Any],
    scorecard: dict[str, Any] | None,
    now: str,
) -> dict[str, Any]:
    sc = _score_snap(scorecard)
    out = _outcome_card(card)
    out["closed_at"] = now
    out["closed_edge"] = sc.get("edge_usd")
    out["closed_beating"] = sc.get("beating_model")
    out["closed_return_pct"] = sc.get("book_return_pct")
    return out


def ensure_ledger(lab: dict[str, Any] | None) -> list[dict[str, Any]]:
    """In-memory ledger. Seed from the current blob if the file is still flat."""
    state = lab if isinstance(lab, dict) else {}
    rows = [_outcome_card(r) for r in (state.get("ledger") or []) if isinstance(r, dict)]
    if rows:
        return rows[-_LEDGER_CAP:]
    if state.get("instructions") or state.get("revision") or state.get("types"):
        return [_ledger_card(state, state.get("paper_score"))]
    return []


def revision_card(revision: int, lab: dict[str, Any] | None = None) -> dict[str, Any] | None:
    state = lab if isinstance(lab, dict) else load_lab()
    want = int(revision)
    for row in reversed(ensure_ledger(state)):
        try:
            if int(row.get("revision") or 0) == want:
                return _outcome_card(row)
        except (TypeError, ValueError):
            continue
    try:
        if int(state.get("revision") or 0) == want:
            return _ledger_card(state, state.get("paper_score"))
    except (TypeError, ValueError):
        return None
    return None


def save_lab(update: dict[str, Any], *, scorecard: dict[str, Any] | None = None) -> dict[str, Any]:
    prev = load_lab()
    now = datetime.now(timezone.utc).isoformat()
    rev = int(prev.get("revision") or 0) + 1
    ledger = ensure_ledger(prev)
    if ledger and scorecard:
        ledger[-1] = _close_card(ledger[-1], scorecard, now)
    lots_at = update.get("lots_at_write")
    if not lots_at:
        try:
            from abcxauto.think_stream import LAST_TURN_PATH, _read_json

            lots_at = list((_read_json(LAST_TURN_PATH) or {}).get("open_lots") or [])
        except Exception:
            lots_at = list(prev.get("lots_at_write") or [])
    state = _drop_dead_lab_keys({
        **prev,
        **update,
        "revision": rev,
        "written_at": now,
        "promoted": False,
        "lots_at_write": [str(x) for x in (lots_at or [])][:32],
    })
    if scorecard:
        state["paper_score"] = _score_snap(scorecard)
    ledger.append(_ledger_card(state, state.get("paper_score")))
    state["ledger"] = ledger[-_LEDGER_CAP:]
    _write(_lab_path(), state)
    return state


def maybe_promote(*, scorecard: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Copy lab â†’ live snapshot only when paper is beating the model bill."""
    lab = load_lab()
    if not _has_book(lab):
        return None
    if not lab.get("ready_to_promote"):
        return None
    sc = scorecard or lab.get("paper_score") or {}
    if sc.get("beating_model") is not True:
        return None
    now = datetime.now(timezone.utc).isoformat()
    live = {
        **lab,
        "promoted": True,
        "promoted_at": now,
        "promoted_revision": lab.get("revision"),
        "source": "paper_lab",
        "note": "live follows this snapshot; does not copy paper fills",
    }
    _write(_live_path(), live)
    lab["promoted"] = True
    lab["promoted_at"] = now
    _write(_lab_path(), lab)
    return live


def live_has_promoted() -> bool:
    live = load_live()
    return bool(live.get("promoted") and _has_book(live))


def live_new_risk_allowed() -> bool:
    """Paper may take new risk. Live needs a promoted playbook."""
    if is_paper():
        return True
    return live_has_promoted()


def _reject_note(rejected: dict[str, str]) -> str:
    if "unknown_type" in rejected:
        return rejected["unknown_type"]
    if "invented_pct_gate" in rejected and set(rejected) <= {"invented_pct_gate"}:
        return "notebook cannot invent a % gate"
    if "ticker_list" in rejected:
        return rejected["ticker_list"]
    if "diary" in rejected:
        return rejected["diary"]
    if "shape" in rejected:
        return rejected["shape"]
    if "invented_pct_gate" in rejected:
        return "notebook cannot invent a % gate"
    return "notebook cannot loosen gates"


def apply_from_judgment(judgment: dict[str, Any] | None) -> dict[str, Any] | None:
    """Paper: persist Grok's notebook. Live: ignore writes.

    Gate knobs in the payload are rejected (not applied). Notes still save.
    Diary / ticker-list / unknown-type writes do not save.
    """
    if not judgment or not is_paper():
        return None
    raw = judgment.get("lab_playbook") or judgment.get("playbook")
    rejected = dict(gate_rejects(raw))
    rejected.update(book_shape_rejects(raw))
    if _HARD_SHAPE.intersection(rejected):
        return {
            "status": "rejected",
            "rejected": rejected,
            "note": _reject_note(rejected),
        }
    update = clamp_update(raw)
    if not update:
        if rejected:
            note = "notebook cannot loosen gates"
            if "invented_pct_gate" in rejected:
                note = "notebook cannot invent a % gate"
            return {
                "status": "rejected",
                "rejected": rejected,
                "note": _reject_note(rejected),
            }
        return None
    score = None
    try:
        from abcxauto.scorecard import compute_scorecard

        score = compute_scorecard()
    except Exception:
        score = None
    state = save_lab(update, scorecard=score)
    maybe_promote(scorecard=score)
    if rejected:
        out = dict(state)
        out["rejected"] = rejected
        if "invented_pct_gate" in rejected:
            out["note"] = "notes saved; invented % gate lines stripped"
        else:
            out["note"] = "notes saved; gate knobs ignored"
        return out
    return state


def playbook_is_stale(
    lab: dict[str, Any] | None = None,
    *,
    now: datetime | None = None,
) -> bool:
    """True when a standing card is older than the lab rewrite cadence."""
    state = lab if isinstance(lab, dict) else load_lab()
    if not _has_book(state):
        return False
    age = playbook_age_hours(state, now=now)
    return age is not None and age >= stale_hours()


def _window_edges(scorecard: dict[str, Any] | None) -> dict[str, Any]:
    wins = (scorecard or {}).get("windows") if isinstance(scorecard, dict) else None
    if not isinstance(wins, dict):
        wins = {}
    out: dict[str, Any] = {}
    for label in _CARD_WINDOWS:
        row = wins.get(label) if isinstance(wins.get(label), dict) else {}
        out[f"win_{label}"] = row.get("edge_usd")
        out[f"win_{label}_beat"] = row.get("beating_model")
    return out


def _since_write_score(
    lab: dict[str, Any],
    scorecard: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Book vs model since this card was written. Inception hole stays on the scorecard."""
    written = str(lab.get("written_at") or "")
    now_nl = (scorecard or {}).get("net_liquidation") if isinstance(scorecard, dict) else None
    empty = {"since_write_edge": None, "since_write_pnl": None, "since_write_cost": None}
    if not written:
        return empty
    try:
        from abcxauto.memory import get_journal

        journal = get_journal()
    except Exception:
        return empty
    start_nl, _start_ts = None, None
    try:
        if hasattr(journal, "nav_at_or_before"):
            start_nl, _start_ts = journal.nav_at_or_before(written)
    except Exception:
        start_nl = None
    usage = {}
    try:
        if hasattr(journal, "model_usage_since"):
            usage = dict(journal.model_usage_since(written) or {})
    except Exception:
        usage = {}
    cost = float(usage.get("cost_usd") or 0.0)
    try:
        now_f = float(now_nl) if now_nl is not None else None
        start_f = float(start_nl) if start_nl is not None else None
    except (TypeError, ValueError):
        return {**empty, "since_write_cost": cost}
    if now_f is None or start_f is None or start_f <= 0:
        return {**empty, "since_write_cost": cost}
    pnl = now_f - start_f
    return {
        "since_write_edge": pnl - cost,
        "since_write_pnl": pnl,
        "since_write_cost": cost,
    }


def playbook_age_hours(
    lab: dict[str, Any] | None = None,
    *,
    now: datetime | None = None,
) -> float | None:
    raw = str((lab or {}).get("written_at") or "")
    if not raw:
        return None
    try:
        written = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if written.tzinfo is None:
        written = written.replace(tzinfo=timezone.utc)
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=timezone.utc)
    return max(0.0, (clock - written).total_seconds() / 3600.0)


def playbook_glance(scorecard: dict[str, Any] | None = None) -> dict[str, Any]:
    """Score since the last write. Not the notebook text â€” Grok asks playbook() for that."""
    facts = playbook_facts(scorecard)
    return {
        "revision": facts.get("revision"),
        "age_h": facts.get("age_h"),
        "since_write_edge": facts.get("since_write_edge"),
        "now_edge": facts.get("now_edge"),
        "win_4h": facts.get("win_4h"),
        "lots_at_write": list(facts.get("lots_at_write") or [])[:16],
    }


def playbook_facts(scorecard: dict[str, Any] | None = None) -> dict[str, Any]:
    """Forest vs the last write: age and score at write vs now. No lecture."""
    from abcxauto.world_state import pct_of_nl

    lab = load_lab()
    inst = notebook_text(lab)
    at_write = lab.get("paper_score") if isinstance(lab.get("paper_score"), dict) else {}
    now_sc = scorecard if isinstance(scorecard, dict) else {}
    age = playbook_age_hours(lab)
    ledger = [_compact_card(r) for r in ensure_ledger(lab)]
    since = _since_write_score(lab, now_sc)
    nl = now_sc.get("net_liquidation")
    at_edge = at_write.get("edge_usd")
    now_edge = now_sc.get("edge_usd")
    since_edge = since.get("since_write_edge")
    facts = {
        "revision": lab.get("revision"),
        "mode": lab.get("mode") or None,
        "has_instructions": bool(inst),
        "ready_to_promote": bool(lab.get("ready_to_promote")) if inst else None,
        "age_h": round(age, 1) if age is not None else None,
        "at_write_edge": at_edge,
        "at_write_edge_pct_of_nl": pct_of_nl(at_edge, nl),
        "at_write_beating": at_write.get("beating_model"),
        "now_edge": now_edge,
        "now_edge_pct_of_nl": pct_of_nl(now_edge, nl),
        "now_beating": now_sc.get("beating_model"),
        "since_write_edge": since_edge,
        "since_write_edge_pct_of_nl": pct_of_nl(since_edge, nl),
        "since_write_pnl": since.get("since_write_pnl"),
        "lots_at_write": [str(x) for x in (lab.get("lots_at_write") or [])][:16],
        "ledger": ledger[-8:],
    }
    facts.update(_window_edges(now_sc))
    halt = _clerk_halt_slice(now_sc)
    facts.update(halt)
    if isinstance(halt, dict):
        facts["halt_trips_at_pct_of_nl"] = pct_of_nl(halt.get("halt_trips_at_usd"), nl)
        facts["ibkr_day_vs_halt_pct_of_nl"] = pct_of_nl(halt.get("ibkr_day_vs_halt"), nl)
    return facts


def _clerk_halt_slice(scorecard: dict[str, Any] | None) -> dict[str, Any]:
    from abcxauto.book import clerk_halt_facts

    nl = None
    day = None
    if isinstance(scorecard, dict):
        nl = scorecard.get("net_liquidation")
        day = scorecard.get("ibkr_daily_pnl")
        if day is None:
            day = scorecard.get("daily_pnl")
    if nl is None or day is None:
        try:
            from abcxauto.memory import get_journal

            perf = get_journal().account_performance() or {}
            if nl is None:
                nl = perf.get("net_liquidation")
            if day is None:
                day = perf.get("daily_pnl")
        except Exception:
            pass
    return clerk_halt_facts(nl, day)


def format_ledger_line(facts: dict[str, Any] | None) -> str:
    rows = (facts or {}).get("ledger") if isinstance(facts, dict) else None
    if not isinstance(rows, list) or not rows:
        return ""
    bits = []
    for row in rows[-4:]:
        if not isinstance(row, dict) or row.get("revision") is None:
            continue
        bit = f"r{row.get('revision')}:{row.get('edge_usd')}"
        if row.get("closed_edge") is not None:
            bit += f">{row.get('closed_edge')}"
        bits.append(bit)
    return " ".join(bits)


def _live_scorecard(lab: dict[str, Any] | None = None) -> dict[str, Any]:
    """Current scorecard. The stamp on disk is at_write, not now."""
    try:
        from abcxauto.scorecard import compute_scorecard

        sc = compute_scorecard()
        if isinstance(sc, dict) and sc:
            return sc
    except Exception:
        pass
    state = lab if isinstance(lab, dict) else {}
    stamp = state.get("paper_score")
    return stamp if isinstance(stamp, dict) else {}


def format_block() -> str:
    """Compact forest for book/wake. Full prose is the playbook tool."""
    paper = is_paper()
    lab = load_lab()
    live = load_live()
    if paper:
        inst = notebook_text(lab)
        if not inst:
            return "LAB PLAYBOOK: none. write_lab_playbook to set; playbook tool for full text.\n"
        live_sc = _live_scorecard(lab)
        facts = playbook_facts(live_sc)
        ledger = format_ledger_line(facts)
        lots = facts.get("lots_at_write") or []
        lots_s = ",".join(str(x) for x in lots[:8]) if lots else "none"
        return (
            "LAB PLAYBOOK:\n"
            f"- rev={lab.get('revision')} mode={lab.get('mode') or 'explore'} "
            f"promoted={bool(lab.get('promoted'))}\n"
            f"- since_write={facts.get('since_write_edge')} "
            f"now_edge={facts.get('now_edge')} "
            f"4h={facts.get('win_4h')} age_h={facts.get('age_h')}\n"
            f"- lots_at_write={lots_s}\n"
            f"- ledger: {ledger or 'none'}\n"
            "- notebook: playbook tool; send is the book\n"
        )
    inst = notebook_text(live)
    if not inst:
        return "LIVE: no promoted paper playbook. New risk blocked until promote (code).\n"
    return (
        "LIVE PLAYBOOK (promoted snapshot):\n"
        f"- promoted_revision={live.get('promoted_revision')} "
        f"promoted_at={live.get('promoted_at')}\n"
        "- notebook: playbook tool\n"
    )


def clear_lab(*, reason: str = "") -> dict[str, Any]:
    """Operator wipe. Grok starts a new notebook; no standing essay."""
    now = datetime.now(timezone.utc).isoformat()
    state = {
        "mode": "explore",
        "instructions": "",
        "types": {},
        "ready_to_promote": False,
        "promoted": False,
        "revision": 0,
        "written_at": now,
        "cleared_at": now,
        "cleared_reason": str(reason or "")[:240],
        "ledger": [],
        "paper_score": {},
    }
    _write(_lab_path(), state)
    return state


def playbook_payload(revision: Any = None, *, full: bool = False) -> dict[str, Any]:
    """Notebook plus score since write. full is accepted and ignored â€” the notes are the tool."""
    paper = is_paper()
    lab = load_lab() if paper else load_live()
    live_sc = _live_scorecard(lab) if paper else (
        lab.get("paper_score") if isinstance(lab.get("paper_score"), dict) else {}
    )
    facts = playbook_facts(live_sc)
    inst = notebook_text(lab)
    types = lab.get("types") if isinstance(lab.get("types"), dict) else {}
    current: dict[str, Any] = {
        "revision": lab.get("revision") or lab.get("promoted_revision"),
        "mode": lab.get("mode"),
        "ready_to_promote": bool(lab.get("ready_to_promote")),
        "promoted": bool(lab.get("promoted")),
        "written_at": lab.get("written_at") or lab.get("promoted_at"),
        "paper_score": lab.get("paper_score") or {},
        "types": types,
        "instructions": inst,
        "instructions_n": len(inst),
    }
    out: dict[str, Any] = {
        "scope": "lab" if paper else "live",
        "score": {
            "revision": facts.get("revision"),
            "age_h": facts.get("age_h"),
            "at_write_edge": facts.get("at_write_edge"),
            "now_edge": facts.get("now_edge"),
            "since_write_edge": facts.get("since_write_edge"),
            "since_write_pnl": facts.get("since_write_pnl"),
            "lots_at_write": list(facts.get("lots_at_write") or []),
            "clerk_halted": facts.get("clerk_halted"),
            "halt_kind": facts.get("halt_kind"),
            "halt_reason": facts.get("halt_reason"),
            "daily_loss_limit_pct": facts.get("daily_loss_limit_pct"),
            "halt_trips_at_usd": facts.get("halt_trips_at_usd"),
            "halt_trips_at_pct_of_nl": facts.get("halt_trips_at_pct_of_nl"),
            "ibkr_day_vs_halt": facts.get("ibkr_day_vs_halt"),
            "ibkr_day_vs_halt_pct_of_nl": facts.get("ibkr_day_vs_halt_pct_of_nl"),
            "now_edge_pct_of_nl": facts.get("now_edge_pct_of_nl"),
            "since_write_edge_pct_of_nl": facts.get("since_write_edge_pct_of_nl"),
        },
        "current": current,
        "facts": facts,
        "ledger": [_compact_card(r) for r in ensure_ledger(lab)],
    }
    if revision in (None, ""):
        return out
    try:
        want = int(revision)
    except (TypeError, ValueError):
        out["error"] = "revision must be an int"
        return out
    card = revision_card(want, lab)
    if card is None:
        out["error"] = f"revision {want} not in ledger"
        return out
    out["revision"] = _outcome_card(card)
    return out
