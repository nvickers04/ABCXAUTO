"""Order proposals — pydantic schemas per strategy, validation, ticket rendering.

Every strategy's parameter model mirrors the matching IBKRConnector method
signature exactly, so the executor can dispatch with ``**params.model_dump()``.
"""

from __future__ import annotations

import itertools
import re
from typing import Any, Dict, Literal, Optional, Union

from pydantic import BaseModel, Field, ValidationError, model_validator
from rich.table import Table

_EXPIRATION_RE = re.compile(r"^\d{8}$")


def _check_expiration(value: str, label: str = "expiration") -> str:
    if not _EXPIRATION_RE.match(value):
        raise ValueError(f"{label} must be YYYYMMDD, got {value!r}")
    return value


# ---------------------------------------------------------------------------
# Stock strategies
# ---------------------------------------------------------------------------

_PROTECTION_REQUIRED_MSG = (
    "every new position requires a stop loss and take profit. Use 'bracket' "
    "(limit entry) or 'market_bracket' (market entry) instead. Set "
    "closing_position=true only if this order reduces or closes an existing position."
)


class LimitOrderParams(BaseModel):
    symbol: str
    action: Literal["BUY", "SELL"]
    quantity: int = Field(gt=0)
    limit_price: float = Field(gt=0)
    tif: Literal["DAY", "GTC"] = "DAY"
    # Exit-only assertion; excluded from the gateway call. Executor re-verifies
    # against live positions before dispatch.
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
    # Protocol: conId is the single source of truth for closes (excluded from gateway kwargs).
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


class BracketParams(BaseModel):
    symbol: str
    quantity: int = Field(gt=0)
    direction: Literal["LONG", "SHORT"]
    entry_price: float = Field(gt=0)
    stop_price: float = Field(gt=0)
    target_price: float = Field(gt=0)
    time_bucket: Literal["intraday", "short_swing", "swing"] = "short_swing"

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
    """Market entry, then OCA stop + target sized to the actual fill."""

    symbol: str
    quantity: int = Field(gt=0)
    direction: Literal["LONG", "SHORT"]
    stop_price: float = Field(gt=0)
    target_price: float = Field(gt=0)

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


class ModifyOrderParams(BaseModel):
    """Edit a working order in place: price(s) and/or quantity.

    Cannot change symbol, action, or order type — cancel and re-propose for that.
    """

    order_id: int = Field(gt=0)
    limit_price: Optional[float] = Field(default=None, gt=0)
    stop_price: Optional[float] = Field(default=None, gt=0)
    quantity: Optional[int] = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _at_least_one_change(self) -> "ModifyOrderParams":
        if self.limit_price is None and self.stop_price is None and self.quantity is None:
            raise ValueError("Provide at least one of limit_price, stop_price, quantity")
        return self


class CloseOptionParams(BaseModel):
    """Close an existing option position (sell-to-close if long, buy-to-close if short)."""

    symbol: str
    expiration: str
    strike: float = Field(gt=0)
    right: Literal["C", "P"]
    quantity: Optional[int] = Field(default=None, gt=0)  # None = close full position
    limit_price: Optional[float] = Field(default=None, gt=0)  # None = mid-based limit

    @model_validator(mode="after")
    def _validate(self) -> "CloseOptionParams":
        _check_expiration(self.expiration)
        return self


class TrailingStopParams(BaseModel):
    symbol: str
    quantity: int = Field(gt=0)
    direction: Literal["LONG", "SHORT"]
    trail_amount: Optional[float] = Field(default=None, gt=0)
    trail_percent: Optional[float] = Field(default=None, gt=0, le=50)

    @model_validator(mode="after")
    def _exactly_one_trail(self) -> "TrailingStopParams":
        if (self.trail_amount is None) == (self.trail_percent is None):
            raise ValueError("Provide exactly one of trail_amount or trail_percent")
        return self


class TrailingStopLimitParams(TrailingStopParams):
    limit_offset: float = Field(default=0.1, ge=0)


# ---------------------------------------------------------------------------
# Option strategies
# ---------------------------------------------------------------------------

class VerticalSpreadParams(BaseModel):
    symbol: str
    expiration: str
    long_strike: float = Field(gt=0)
    short_strike: float = Field(gt=0)
    right: Literal["C", "P"]
    quantity: int = Field(default=1, gt=0)
    limit_price: Optional[float] = None

    @model_validator(mode="after")
    def _validate(self) -> "VerticalSpreadParams":
        _check_expiration(self.expiration)
        if self.long_strike == self.short_strike:
            raise ValueError("long_strike and short_strike must differ")
        return self


class IronCondorParams(BaseModel):
    symbol: str
    expiration: str
    put_long_strike: float = Field(gt=0)
    put_short_strike: float = Field(gt=0)
    call_short_strike: float = Field(gt=0)
    call_long_strike: float = Field(gt=0)
    quantity: int = Field(default=1, gt=0)
    limit_price: Optional[float] = None

    @model_validator(mode="after")
    def _validate(self) -> "IronCondorParams":
        _check_expiration(self.expiration)
        strikes = (
            self.put_long_strike,
            self.put_short_strike,
            self.call_short_strike,
            self.call_long_strike,
        )
        if not all(a < b for a, b in itertools.pairwise(strikes)):
            raise ValueError(
                "Iron condor requires put_long < put_short < call_short < call_long, "
                f"got {strikes}"
            )
        return self


class IronButterflyParams(BaseModel):
    symbol: str
    expiration: str
    center_strike: float = Field(gt=0)
    wing_width: float = Field(gt=0)
    quantity: int = Field(default=1, gt=0)
    limit_price: Optional[float] = None

    @model_validator(mode="after")
    def _validate(self) -> "IronButterflyParams":
        _check_expiration(self.expiration)
        return self


class StraddleParams(BaseModel):
    symbol: str
    expiration: str
    strike: float = Field(gt=0)
    quantity: int = Field(default=1, gt=0)
    action: Literal["BUY", "SELL"] = "BUY"
    limit_price: Optional[float] = None

    @model_validator(mode="after")
    def _validate(self) -> "StraddleParams":
        _check_expiration(self.expiration)
        return self


class StrangleParams(BaseModel):
    symbol: str
    expiration: str
    put_strike: float = Field(gt=0)
    call_strike: float = Field(gt=0)
    quantity: int = Field(default=1, gt=0)
    action: Literal["BUY", "SELL"] = "BUY"
    limit_price: Optional[float] = None

    @model_validator(mode="after")
    def _validate(self) -> "StrangleParams":
        _check_expiration(self.expiration)
        if self.put_strike >= self.call_strike:
            raise ValueError("Strangle requires put_strike < call_strike")
        return self


class ButterflyParams(BaseModel):
    symbol: str
    expiration: str
    lower_strike: float = Field(gt=0)
    middle_strike: float = Field(gt=0)
    upper_strike: float = Field(gt=0)
    right: Literal["C", "P"] = "C"
    quantity: int = Field(default=1, gt=0)
    limit_price: Optional[float] = None

    @model_validator(mode="after")
    def _validate(self) -> "ButterflyParams":
        _check_expiration(self.expiration)
        if not (self.lower_strike < self.middle_strike < self.upper_strike):
            raise ValueError("Butterfly requires lower < middle < upper strikes")
        return self


class CalendarSpreadParams(BaseModel):
    symbol: str
    strike: float = Field(gt=0)
    near_expiration: str
    far_expiration: str
    right: Literal["C", "P"] = "C"
    quantity: int = Field(default=1, gt=0)
    limit_price: Optional[float] = None

    @model_validator(mode="after")
    def _validate(self) -> "CalendarSpreadParams":
        _check_expiration(self.near_expiration, "near_expiration")
        _check_expiration(self.far_expiration, "far_expiration")
        if self.near_expiration >= self.far_expiration:
            raise ValueError("Calendar requires near_expiration before far_expiration")
        return self


class DiagonalSpreadParams(BaseModel):
    symbol: str
    near_strike: float = Field(gt=0)
    far_strike: float = Field(gt=0)
    near_expiration: str
    far_expiration: str
    right: Literal["C", "P"] = "C"
    quantity: int = Field(default=1, gt=0)
    limit_price: Optional[float] = None

    @model_validator(mode="after")
    def _validate(self) -> "DiagonalSpreadParams":
        _check_expiration(self.near_expiration, "near_expiration")
        _check_expiration(self.far_expiration, "far_expiration")
        if self.near_expiration >= self.far_expiration:
            raise ValueError("Diagonal requires near_expiration before far_expiration")
        return self


class CoveredCallParams(BaseModel):
    symbol: str
    expiration: str
    strike: float = Field(gt=0)
    shares: int = Field(default=100, gt=0, multiple_of=100)

    @model_validator(mode="after")
    def _validate(self) -> "CoveredCallParams":
        _check_expiration(self.expiration)
        return self


class ProtectivePutParams(CoveredCallParams):
    pass


class CollarParams(BaseModel):
    symbol: str
    expiration: str
    put_strike: float = Field(gt=0)
    call_strike: float = Field(gt=0)
    shares: int = Field(default=100, gt=0, multiple_of=100)

    @model_validator(mode="after")
    def _validate(self) -> "CollarParams":
        _check_expiration(self.expiration)
        if self.put_strike >= self.call_strike:
            raise ValueError("Collar requires put_strike < call_strike")
        return self


class RatioSpreadParams(BaseModel):
    symbol: str
    expiration: str
    long_strike: float = Field(gt=0)
    short_strike: float = Field(gt=0)
    right: Literal["C", "P"] = "C"
    ratio: tuple[int, int] = (1, 2)
    quantity: int = Field(default=1, gt=0)
    limit_price: Optional[float] = None

    @model_validator(mode="after")
    def _validate(self) -> "RatioSpreadParams":
        _check_expiration(self.expiration)
        if self.long_strike == self.short_strike:
            raise ValueError("long_strike and short_strike must differ")
        if self.ratio[0] <= 0 or self.ratio[1] <= self.ratio[0]:
            raise ValueError("ratio must be (long, short) with short > long > 0")
        return self


class JadeLizardParams(BaseModel):
    symbol: str
    expiration: str
    put_strike: float = Field(gt=0)
    call_short_strike: float = Field(gt=0)
    call_long_strike: float = Field(gt=0)
    quantity: int = Field(default=1, gt=0)
    limit_price: Optional[float] = None

    @model_validator(mode="after")
    def _validate(self) -> "JadeLizardParams":
        _check_expiration(self.expiration)
        if self.put_strike >= self.call_short_strike:
            raise ValueError("Jade lizard requires put_strike < call_short_strike")
        if self.call_short_strike >= self.call_long_strike:
            raise ValueError("Jade lizard requires call_short_strike < call_long_strike")
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
    "modify_order": (ModifyOrderParams, "modify_order"),
    "cancel_order": (CancelOrderParams, "cancel_order"),
    "close_option": (CloseOptionParams, "close_option_position"),
    "trailing_stop": (TrailingStopParams, "place_trailing_stop"),
    "trailing_stop_limit": (TrailingStopLimitParams, "place_trailing_stop_limit"),
    "vertical_spread": (VerticalSpreadParams, "place_vertical_spread"),
    "iron_condor": (IronCondorParams, "place_iron_condor"),
    "iron_butterfly": (IronButterflyParams, "place_iron_butterfly"),
    "straddle": (StraddleParams, "place_straddle"),
    "strangle": (StrangleParams, "place_strangle"),
    "butterfly": (ButterflyParams, "place_butterfly"),
    "calendar_spread": (CalendarSpreadParams, "place_calendar_spread"),
    "diagonal_spread": (DiagonalSpreadParams, "place_diagonal_spread"),
    "covered_call": (CoveredCallParams, "place_covered_call"),
    "protective_put": (ProtectivePutParams, "place_protective_put"),
    "collar": (CollarParams, "place_collar"),
    "ratio_spread": (RatioSpreadParams, "place_ratio_spread"),
    "jade_lizard": (JadeLizardParams, "place_jade_lizard"),
}

# Order-management strategies: they edit/cancel already-sent orders or add
# protection to existing exposure — they never open a new position or exit
# one. In ABCXAUTO all strategies (including management) auto-execute.
MANAGEMENT_STRATEGIES = frozenset({
    "oca",
    "modify_stop",
    "modify_target",
    "modify_order",
    "cancel_order",
    "trailing_stop",
    "trailing_stop_limit",
})


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
) -> OrderProposal:
    """Validate a raw propose_order payload into an OrderProposal.

    Raises ProposalValidationError with a Grok-readable message on failure.
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
    return OrderProposal(
        id=next(_id_counter),
        strategy=strategy,
        params=parsed,
        rationale=rationale.strip(),
        max_loss=max_loss,
        max_gain=max_gain,
    )


def render_ticket(proposal: OrderProposal) -> Table:
    """Render a proposal as a rich table for the terminal."""
    table = Table(
        title=f"ORDER TICKET #{proposal.id} — {proposal.strategy.replace('_', ' ').upper()}",
        title_style="bold yellow",
        show_header=True,
        header_style="bold",
    )
    table.add_column("Field", style="cyan", no_wrap=True)
    table.add_column("Value")

    for key, value in proposal.params.model_dump().items():
        if value is None:
            continue
        table.add_row(key, str(value))
    if proposal.max_loss:
        table.add_row("max loss", f"[red]{proposal.max_loss}[/red]")
    if proposal.max_gain:
        table.add_row("max gain", f"[green]{proposal.max_gain}[/green]")
    table.add_row("rationale", proposal.rationale)
    table.add_row("executes via", proposal.gateway_method)
    return table
