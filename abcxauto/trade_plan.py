"""ActiveTradePlan — durable open-trade lifecycle across cycles."""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_PATH = _REPO_ROOT / "active_trade_plan.json"


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


@dataclass
class ActiveTradePlan:
    symbol: str
    direction: str  # LONG | SHORT
    thesis: str = ""
    invalidation: str = ""
    entry_price: float | None = None
    stop_price: float | None = None
    target_price: float | None = None
    quantity: float | None = None
    max_hold_cycles: int = 20
    cycles_open: int = 0
    management: str = "move stop to BE after +0.5R; trail thereafter"
    status: str = "open"  # open | closed
    opened_at: str = field(default_factory=_utc_now)
    closed_at: str | None = None
    close_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ActiveTradePlan":
        known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in raw.items() if k in known})


def _path() -> Path:
    import os

    raw = os.environ.get("ABCXAUTO_TRADE_PLAN_PATH", "").strip()
    return Path(raw) if raw else _DEFAULT_PATH


def load_trade_plan(path: Path | None = None) -> Optional[ActiveTradePlan]:
    p = path or _path()
    if not p.is_file():
        return None
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("status") == "closed":
            return None
        return ActiveTradePlan.from_dict(raw)
    except Exception:
        logger.exception("load_trade_plan failed")
        return None


def save_trade_plan(plan: ActiveTradePlan, path: Path | None = None) -> Path:
    p = path or _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(plan.to_dict(), indent=2) + "\n", encoding="utf-8")
    return p


def clear_trade_plan(path: Path | None = None) -> None:
    p = path or _path()
    try:
        if p.is_file():
            p.unlink()
    except OSError:
        logger.exception("clear_trade_plan failed")


def close_trade_plan(reason: str = "", path: Path | None = None) -> None:
    plan = load_trade_plan(path)
    if not plan:
        clear_trade_plan(path)
        return
    plan.status = "closed"
    plan.closed_at = _utc_now()
    plan.close_reason = reason or "closed"
    # Persist closed snapshot then clear active
    p = path or _path()
    try:
        archive = p.with_name("last_closed_trade_plan.json")
        archive.write_text(json.dumps(plan.to_dict(), indent=2) + "\n", encoding="utf-8")
    except Exception:
        pass
    clear_trade_plan(path)


def bump_plan_cycle(path: Path | None = None) -> Optional[ActiveTradePlan]:
    plan = load_trade_plan(path)
    if not plan:
        return None
    plan.cycles_open = int(plan.cycles_open or 0) + 1
    if plan.max_hold_cycles and plan.cycles_open >= int(plan.max_hold_cycles):
        close_trade_plan("time_stop", path)
        return None
    save_trade_plan(plan, path)
    return plan


def plan_from_hunt_action(act: dict, thesis: str = "") -> Optional[ActiveTradePlan]:
    """Build a plan from a successful hunt bracket params."""
    params = (act or {}).get("params") or {}
    strat = str((act or {}).get("strategy") or (act or {}).get("action") or "").lower()
    if strat not in ("bracket", "market_bracket"):
        return None
    symbol = str(params.get("symbol") or "").upper()
    direction = str(params.get("direction") or "LONG").upper()
    if not symbol:
        return None
    try:
        stop = float(params["stop_price"]) if params.get("stop_price") is not None else None
    except (TypeError, ValueError):
        stop = None
    try:
        target = float(params["target_price"]) if params.get("target_price") is not None else None
    except (TypeError, ValueError):
        target = None
    try:
        qty = float(params["quantity"]) if params.get("quantity") is not None else None
    except (TypeError, ValueError):
        qty = None
    try:
        entry = float(params["entry_price"]) if params.get("entry_price") is not None else None
    except (TypeError, ValueError):
        entry = None
    return ActiveTradePlan(
        symbol=symbol,
        direction=direction if direction in ("LONG", "SHORT") else "LONG",
        thesis=(thesis or str((act or {}).get("rationale") or ""))[:500],
        invalidation=f"stop {stop}" if stop else "stop hit",
        entry_price=entry,
        stop_price=stop,
        target_price=target,
        quantity=qty,
    )
