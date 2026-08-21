"""Order proposals — pydantic schemas per strategy, validation.

Every strategy's parameter model mirrors the matching IBKRConnector method
signature exactly, so the executor can dispatch with ``**params.model_dump()``.
"""

from __future__ import annotations

import itertools
import re
from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, Field, ValidationError, model_validator

from abcxauto.config import get_config

_EXPIRATION_RE = re.compile(r"^\d{8}$")


def _check_expiration(value: str, label: str = "expiration") -> str:
    if not _EXPIRATION_RE.match(value):
        raise ValueError(f"{label} must be YYYYMMDD, got {value!r}")
    return value


_PROTECTION_REQUIRED_MSG = (
    "every new position requires a stop loss and take profit. Use 'bracket' "
    "(limit entry) or 'market_bracket' (market entry) instead. Set "
    "closing_position=true only if this order reduces or closes an existing position."
)


def _coerce_direction_aliases(data: Any) -> Any:
    """Map Grok's side/action BUY|SELL onto direction LONG|SHORT before validation."""
    if not isinstance(data, dict):
        return data
    out = dict(data)
    direction = out.get("direction")
    if direction in (None, ""):
        side = str(out.get("side") or out.get("action") or "").strip().upper()
        if side == "BUY":
            out["direction"] = "LONG"
        elif side == "SELL":
            out["direction"] = "SHORT"
    # Drop order-ticket aliases that are not schema fields (Grok often echoes them).
    for k in ("side", "secType", "sec_type", "exchange", "currency", "tif", "TIF"):
        out.pop(k, None)
    return out


# ---------------------------------------------------------------------------
# Exit-only bare stock orders (closing_position required)
# ---------------------------------------------------------------------------

class LimitOrderParams(BaseModel):
    symbol: str
    action: Literal["BUY", "SELL"]
    quantity: int = Field(gt=0)
    limit_price: float = Field(gt=0)
    tif: Literal["DAY", "GTC"] = "DAY"
    closing_position: bool = Field(default=False, exclude=True)

    @model_validator(mode="after")
    def _exits_only(self) -> "LimitOrderParams":
        if not self.closing_position:
            raise ValueError(f"Bare limit_order rejected — {_PROTECTION_REQUIRED_MSG}")
        return self


class MarketOrderParams(BaseModel):
    symbol: str
    action: Literal["BUY", "SELL"]
    quantity: int = Field(gt=0)
    conId: int | str | None = Field(default=None, exclude=True)
    closing_position: bool = Field(default=False, exclude=True)

    @model_validator(mode="after")
    def _exits_only(self) -> "MarketOrderParams":
        if not self.closing_position:
            raise ValueError(f"Bare market_order rejected — {_PROTECTION_REQUIRED_MSG}")
        return self


class StopOrderParams(BaseModel):
    symbol: str
    action: Literal["BUY", "SELL"]
    quantity: int = Field(gt=0)
    stop_price: float = Field(gt=0)
    closing_position: bool = Field(default=False, exclude=True)

    @model_validator(mode="after")
    def _exits_only(self) -> "StopOrderParams":
        if not self.closing_position:
            raise ValueError(f"Bare stop_order rejected — {_PROTECTION_REQUIRED_MSG}")
        return self


class StopLimitParams(BaseModel):
    symbol: str
    action: Literal["BUY", "SELL"]
    quantity: int = Field(gt=0)
    stop_price: float = Field(gt=0)
    limit_price: float = Field(gt=0)
    tif: Literal["DAY", "GTC"] = "GTC"
    closing_position: bool = Field(default=False, exclude=True)

    @model_validator(mode="after")
    def _exits_only(self) -> "StopLimitParams":
        if not self.closing_position:
            raise ValueError(f"Bare stop_limit rejected — {_PROTECTION_REQUIRED_MSG}")
        return self


# ---------------------------------------------------------------------------
# Entries + management (cycle allowlist)
# ---------------------------------------------------------------------------

class BracketParams(BaseModel):
    symbol: str
    quantity: int = Field(gt=0)
    direction: Literal["LONG", "SHORT"]
    entry_price: float = Field(gt=0)
    stop_price: float = Field(gt=0)
    target_price: float = Field(gt=0)
    time_bucket: Literal["intraday", "short_swing", "swing"] = "short_swing"

    @model_validator(mode="before")
    @classmethod
    def _aliases(cls, data: Any) -> Any:
        return _coerce_direction_aliases(data)

    @model_validator(mode="after")
    def _price_ordering(self) -> "BracketParams":
        if self.direction == "LONG":
            if not (self.stop_price < self.entry_price < self.target_price):
                raise ValueError(
                    f"LONG bracket requires stop < entry < target, got "
                    f"stop={self.stop_price} entry={self.entry_price} target={self.target_price}"
                )
        else:
            if not (self.target_price < self.entry_price < self.stop_price):
                raise ValueError(
                    f"SHORT bracket requires target < entry < stop, got "
                    f"target={self.target_price} entry={self.entry_price} stop={self.stop_price}"
                )
        return self


class MarketBracketParams(BaseModel):
    """Market entry, then OCA stop + target sized to the actual fill.

    ``price_hint`` is optional sizing/R:R context (excluded from the gateway
    call). When absent, risk gates use a conservative stop/target bound and
    min_reward_risk is skipped (cannot be computed honestly without an entry).
    """

    symbol: str
    quantity: int = Field(gt=0)
    direction: Literal["LONG", "SHORT"]
    stop_price: float = Field(gt=0)
    target_price: float = Field(gt=0)
    price_hint: Optional[float] = Field(default=None, gt=0, exclude=True)

    @model_validator(mode="before")
    @classmethod
    def _aliases(cls, data: Any) -> Any:
        return _coerce_direction_aliases(data)

    @model_validator(mode="after")
    def _price_ordering(self) -> "MarketBracketParams":
        if self.direction == "LONG" and self.stop_price >= self.target_price:
            raise ValueError("LONG market_bracket requires stop_price < target_price")
        if self.direction == "SHORT" and self.stop_price <= self.target_price:
            raise ValueError("SHORT market_bracket requires stop_price > target_price")
        return self


class OcaParams(BaseModel):
    """Protective OCA pair (stop + target) for an existing position."""

    symbol: str
    quantity: int = Field(gt=0)
    direction: Literal["LONG", "SHORT"]
    stop_price: float = Field(gt=0)
    target_price: float = Field(gt=0)
    price_hint: Optional[float] = Field(default=None, gt=0, exclude=True)

    @model_validator(mode="before")
    @classmethod
    def _aliases(cls, data: Any) -> Any:
        return _coerce_direction_aliases(data)

    @model_validator(mode="after")
    def _price_ordering(self) -> "OcaParams":
        if self.direction == "LONG" and self.stop_price >= self.target_price:
            raise ValueError("LONG OCA requires stop_price < target_price")
        if self.direction == "SHORT" and self.stop_price <= self.target_price:
            raise ValueError("SHORT OCA requires stop_price > target_price")
        return self


class ModifyStopParams(BaseModel):
    """Move the stop price of an existing stop order (order_id from open_orders)."""

    order_id: int = Field(gt=0)
    new_stop_price: float = Field(gt=0)


class ModifyTargetParams(BaseModel):
    """Move the limit price of an existing take-profit order (order_id from open_orders)."""

    order_id: int = Field(gt=0)
    new_limit_price: float = Field(gt=0)


class CancelOrderParams(BaseModel):
    """Cancel a working order by id. Never leave a position unprotected."""

    order_id: int = Field(gt=0)


class CloseOptionParams(BaseModel):
    """Close an existing option position (sell-to-close if long, buy-to-close if short).

    Prefer ``conId`` when known; else symbol+expiration+strike+right.
    ``quantity`` may be partial (omit = close full matching leg).
    """

    symbol: str
    expiration: Optional[str] = None
    strike: Optional[float] = Field(default=None, gt=0)
    right: Optional[Literal["C", "P"]] = None
    quantity: Optional[int] = Field(default=None, gt=0)
    limit_price: Optional[float] = Field(default=None, gt=0)
    # Ledger identity for Act/gates; excluded from broker kwargs (resolved in executor).
    conId: Optional[int] = Field(default=None, gt=0, exclude=True)

    @model_validator(mode="after")
    def _validate(self) -> "CloseOptionParams":
        if self.conId is None and not (
            self.expiration and self.strike is not None and self.right
        ):
            raise ValueError(
                "close_option requires conId or expiration+strike+right"
            )
        if self.expiration:
            _check_expiration(self.expiration)
        return self


# ---------------------------------------------------------------------------
# Registry: strategy name -> (params model, IBKRConnector method name)
# ---------------------------------------------------------------------------

STRATEGIES: Dict[str, tuple[type[BaseModel], str]] = {
    "limit_order": (LimitOrderParams, "place_limit_order"),
    "market_order": (MarketOrderParams, "place_market_order"),
    "stop_order": (StopOrderParams, "place_stop_order"),
    "stop_limit": (StopLimitParams, "place_stop_limit"),
    "bracket": (BracketParams, "place_bracket_order"),
    "market_bracket": (MarketBracketParams, "place_market_bracket"),
    "oca": (OcaParams, "place_oca"),
    "modify_stop": (ModifyStopParams, "modify_stop_price"),
    "modify_target": (ModifyTargetParams, "modify_target_price"),
    "cancel_order": (CancelOrderParams, "cancel_order"),
    "close_option": (CloseOptionParams, "close_option_position"),
}

from abcxauto.strategy_params import (  # noqa: E402
    EXTRA_MANAGEMENT,
    EXTRA_STRATEGIES,
)

STRATEGIES.update(EXTRA_STRATEGIES)

# Order-management strategies: edit/cancel working orders or add protection —
# they never open a new position. All strategies auto-execute.
MANAGEMENT_STRATEGIES = frozenset({
    "oca",
    "modify_stop",
    "modify_target",
    "cancel_order",
}) | EXTRA_MANAGEMENT


class OrderProposal(BaseModel):
    """A validated order proposal ready for execution."""

    id: int
    strategy: str
    params: BaseModel
    rationale: str
    max_loss: Optional[str] = None
    max_gain: Optional[str] = None

    @property
    def gateway_method(self) -> str:
        return STRATEGIES[self.strategy][1]

    @property
    def is_management(self) -> bool:
        return self.strategy in MANAGEMENT_STRATEGIES


_id_counter = itertools.count(1)


class ProposalValidationError(Exception):
    """Raised when a propose_order payload fails validation (fed back to Grok)."""


def validate_proposal(
    strategy: str,
    params: Dict[str, Any],
    rationale: str,
    max_loss: Optional[str] = None,
    max_gain: Optional[str] = None,
    *,
    quote_last: Optional[float] = None,
    posture: Optional[str] = None,
) -> OrderProposal:
    """Validate a raw propose_order payload into an OrderProposal.

    Raises ProposalValidationError with a Grok-readable message on failure.
    Live-quote geometry is checked for bracket / market_bracket / oca — Grok's
    prices are never rewritten, only accepted or rejected with a reason code.
    """
    entry = STRATEGIES.get(strategy)
    if entry is None:
        raise ProposalValidationError(
            f"Unknown strategy {strategy!r}. Valid strategies: {', '.join(sorted(STRATEGIES))}"
        )
    model_cls, _ = entry
    if not rationale or not rationale.strip():
        raise ProposalValidationError("rationale is required — explain the trade in plain English")
    try:
        parsed = model_cls(**params)
    except ValidationError as e:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" if err["loc"] else err["msg"]
            for err in e.errors()
        )
        raise ProposalValidationError(f"Invalid {strategy} params — {problems}") from e
    if hasattr(parsed, "symbol"):
        parsed.symbol = parsed.symbol.upper()

    if strategy in ("market_bracket", "oca", "bracket"):
        from abcxauto.config import resolve_effective_posture
        from abcxauto.structure_grade import check_live_geometry

        cfg = get_config()
        eff = posture or resolve_effective_posture(
            getattr(cfg, "risk_posture", "") or "",
            getattr(cfg, "trading_mode", "paper") or "paper",
        )
        raw_params = (
            parsed.model_dump()
            if hasattr(parsed, "model_dump")
            else dict(params)
        )
        # Prefer explicit price_hint on the model when present
        hint = getattr(parsed, "price_hint", None)
        q = quote_last if quote_last is not None else hint
        ok_g, code, msg = check_live_geometry(
            strategy, raw_params, quote_last=q, posture=eff or "balanced",
        )
        if not ok_g:
            raise ProposalValidationError(f"{code}: {msg}")

    return OrderProposal(
        id=next(_id_counter),
        strategy=strategy,
        params=parsed,
        rationale=rationale.strip(),
        max_loss=max_loss,
        max_gain=max_gain,
    )
