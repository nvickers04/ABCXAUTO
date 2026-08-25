"""Extended strategy param models — ABC order + option surface.

Imported by ``proposals`` into ``STRATEGIES``. Field names match gateway kwargs
so ``params.model_dump(exclude_none=True)`` dispatches cleanly.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, Field, ValidationError, model_validator

_EXPIRATION_RE = re.compile(r"^\d{8}$")
_PROTECTION_REQUIRED_MSG = (
    "every new position requires a stop loss and take profit. Use 'bracket' "
    "(limit entry) or 'market_bracket' (market entry) instead. Set "
    "closing_position=true only if this order reduces or closes an existing position."
)


def _check_expiration(value: str, label: str = "expiration") -> str:
    if not _EXPIRATION_RE.match(value):
        raise ValueError(f"{label} must be YYYYMMDD, got {value!r}")
    return value


def _coerce_direction_aliases(data: Any) -> Any:
    if not isinstance(data, dict):
        return data
    out = dict(data)
    if out.get("direction") in (None, ""):
        side = str(out.get("side") or out.get("action") or "").strip().upper()
        if side == "BUY":
            out["direction"] = "LONG"
        elif side == "SELL":
            out["direction"] = "SHORT"
    for k in ("side", "secType", "sec_type", "exchange", "currency", "tif", "TIF"):
        out.pop(k, None)
    return out


def _exit_only(closing: bool) -> None:
    if closing is not True:
        raise ValueError(f"Bare stock order rejected — {_PROTECTION_REQUIRED_MSG}")


# ---------------------------------------------------------------------------
# Advanced / auction / algo stock (exit-only when opening risk)
# ---------------------------------------------------------------------------

class TrailingStopParams(BaseModel):
    symbol: str
    quantity: int = Field(gt=0)
    direction: Literal["LONG", "SHORT"]
    trail_amount: Optional[float] = Field(default=None, gt=0)
    trail_percent: Optional[float] = Field(default=None, gt=0)

    @model_validator(mode="before")
    @classmethod
    def _aliases(cls, data: Any) -> Any:
        return _coerce_direction_aliases(data)

    @model_validator(mode="after")
    def _trail(self) -> "TrailingStopParams":
        if self.trail_amount is None and self.trail_percent is None:
            raise ValueError("trailing_stop requires trail_amount or trail_percent")
        return self


class TrailingStopLimitParams(TrailingStopParams):
    limit_offset: float = Field(default=0.10, gt=0)


class MarketOnCloseParams(BaseModel):
    symbol: str
    action: Literal["BUY", "SELL"]
    quantity: int = Field(gt=0)
    closing_position: bool = Field(default=False, exclude=True)

    @model_validator(mode="after")
    def _exits(self) -> "MarketOnCloseParams":
        _exit_only(self.closing_position)
        return self


class LimitOnCloseParams(BaseModel):
    symbol: str
    action: Literal["BUY", "SELL"]
    quantity: int = Field(gt=0)
    limit_price: float = Field(gt=0)
    closing_position: bool = Field(default=False, exclude=True)

    @model_validator(mode="after")
    def _exits(self) -> "LimitOnCloseParams":
        _exit_only(self.closing_position)
        return self


class MarketOnOpenParams(MarketOnCloseParams):
    pass


class LimitOnOpenParams(LimitOnCloseParams):
    pass


class AdaptiveParams(BaseModel):
    symbol: str
    action: Literal["BUY", "SELL"]
    quantity: int = Field(gt=0)
    order_type: Literal["MKT", "LMT"] = "MKT"
    limit_price: Optional[float] = Field(default=None, gt=0)
    priority: Literal["Patient", "Normal", "Urgent"] = "Normal"
    closing_position: bool = Field(default=False, exclude=True)

    @model_validator(mode="after")
    def _exits(self) -> "AdaptiveParams":
        _exit_only(self.closing_position)
        if self.order_type == "LMT" and self.limit_price is None:
            raise ValueError("adaptive LMT requires limit_price")
        return self


class MidpriceParams(BaseModel):
    symbol: str
    action: Literal["BUY", "SELL"]
    quantity: int = Field(gt=0)
    price_cap: Optional[float] = Field(default=None, gt=0)
    closing_position: bool = Field(default=False, exclude=True)

    @model_validator(mode="after")
    def _exits(self) -> "MidpriceParams":
        _exit_only(self.closing_position)
        return self


class RelativeParams(BaseModel):
    symbol: str
    action: Literal["BUY", "SELL"]
    quantity: int = Field(gt=0)
    offset: float = Field(default=0.01, gt=0)
    limit_price: Optional[float] = Field(default=None, gt=0)
    closing_position: bool = Field(default=False, exclude=True)

    @model_validator(mode="after")
    def _exits(self) -> "RelativeParams":
        _exit_only(self.closing_position)
        return self


class LimitOrderGtdParams(BaseModel):
    symbol: str
    action: Literal["BUY", "SELL"]
    quantity: int = Field(gt=0)
    limit_price: float = Field(gt=0)
    good_till_date: str
    closing_position: bool = Field(default=False, exclude=True)

    @model_validator(mode="after")
    def _exits(self) -> "LimitOrderGtdParams":
        _exit_only(self.closing_position)
        return self


class FillOrKillParams(BaseModel):
    symbol: str
    action: Literal["BUY", "SELL"]
    quantity: int = Field(gt=0)
    limit_price: float = Field(gt=0)
    closing_position: bool = Field(default=False, exclude=True)

    @model_validator(mode="after")
    def _exits(self) -> "FillOrKillParams":
        _exit_only(self.closing_position)
        return self


class ImmediateOrCancelParams(FillOrKillParams):
    pass


class VwapParams(BaseModel):
    symbol: str
    action: Literal["BUY", "SELL"]
    quantity: int = Field(gt=0)
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    max_pct_volume: float = Field(default=25.0, gt=0, le=100)
    closing_position: bool = Field(default=False, exclude=True)

    @model_validator(mode="after")
    def _exits(self) -> "VwapParams":
        _exit_only(self.closing_position)
        return self


class TwapParams(BaseModel):
    symbol: str
    action: Literal["BUY", "SELL"]
    quantity: int = Field(gt=0)
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    closing_position: bool = Field(default=False, exclude=True)

    @model_validator(mode="after")
    def _exits(self) -> "TwapParams":
        _exit_only(self.closing_position)
        return self


class IcebergParams(BaseModel):
    symbol: str
    action: Literal["BUY", "SELL"]
    total_quantity: int = Field(gt=0)
    display_size: int = Field(gt=0)
    limit_price: float = Field(gt=0)
    closing_position: bool = Field(default=False, exclude=True)

    @model_validator(mode="after")
    def _exits(self) -> "IcebergParams":
        _exit_only(self.closing_position)
        if self.display_size > self.total_quantity:
            raise ValueError("display_size cannot exceed total_quantity")
        return self


class SnapToMidpointParams(BaseModel):
    symbol: str
    action: Literal["BUY", "SELL"]
    quantity: int = Field(gt=0)
    closing_position: bool = Field(default=False, exclude=True)

    @model_validator(mode="after")
    def _exits(self) -> "SnapToMidpointParams":
        _exit_only(self.closing_position)
        return self


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
    order_type: Literal["LMT", "MKT"] = "LMT"
    limit_price: Optional[float] = Field(default=None)
    closing_position: bool = False

    @model_validator(mode="after")
    def _exp(self) -> "VerticalSpreadParams":
        _check_expiration(self.expiration)
        return self


class IronCondorParams(BaseModel):
    symbol: str
    expiration: str
    put_long_strike: float = Field(gt=0)
    put_short_strike: float = Field(gt=0)
    call_short_strike: float = Field(gt=0)
    call_long_strike: float = Field(gt=0)
    quantity: int = Field(default=1, gt=0)
    limit_price: Optional[float] = Field(default=None)
    closing_position: bool = False

    @model_validator(mode="after")
    def _exp(self) -> "IronCondorParams":
        _check_expiration(self.expiration)
        return self


class IronButterflyParams(BaseModel):
    symbol: str
    expiration: str
    center_strike: float = Field(gt=0)
    wing_width: float = Field(gt=0)
    quantity: int = Field(default=1, gt=0)
    limit_price: Optional[float] = Field(default=None)
    closing_position: bool = False

    @model_validator(mode="after")
    def _exp(self) -> "IronButterflyParams":
        _check_expiration(self.expiration)
        return self


class StraddleParams(BaseModel):
    symbol: str
    expiration: str
    strike: float = Field(gt=0)
    quantity: int = Field(default=1, gt=0)
    action: Literal["BUY", "SELL"] = "BUY"
    limit_price: Optional[float] = Field(default=None)
    closing_position: bool = False

    @model_validator(mode="after")
    def _exp(self) -> "StraddleParams":
        _check_expiration(self.expiration)
        return self


class StrangleParams(BaseModel):
    symbol: str
    expiration: str
    put_strike: float = Field(gt=0)
    call_strike: float = Field(gt=0)
    quantity: int = Field(default=1, gt=0)
    action: Literal["BUY", "SELL"] = "BUY"
    limit_price: Optional[float] = Field(default=None)
    closing_position: bool = False

    @model_validator(mode="after")
    def _exp(self) -> "StrangleParams":
        _check_expiration(self.expiration)
        return self


class ButterflyParams(BaseModel):
    symbol: str
    expiration: str
    lower_strike: float = Field(gt=0)
    middle_strike: float = Field(gt=0)
    upper_strike: float = Field(gt=0)
    right: Literal["C", "P"]
    quantity: int = Field(default=1, gt=0)
    limit_price: Optional[float] = Field(default=None)
    closing_position: bool = False

    @model_validator(mode="after")
    def _exp(self) -> "ButterflyParams":
        _check_expiration(self.expiration)
        return self


class CalendarSpreadParams(BaseModel):
    symbol: str
    strike: float = Field(gt=0)
    near_expiration: str
    far_expiration: str
    right: Literal["C", "P"]
    quantity: int = Field(default=1, gt=0)
    limit_price: Optional[float] = Field(default=None)
    closing_position: bool = False

    @model_validator(mode="after")
    def _exp(self) -> "CalendarSpreadParams":
        _check_expiration(self.near_expiration, "near_expiration")
        _check_expiration(self.far_expiration, "far_expiration")
        return self


class DiagonalSpreadParams(BaseModel):
    symbol: str
    near_strike: float = Field(gt=0)
    far_strike: float = Field(gt=0)
    near_expiration: str
    far_expiration: str
    right: Literal["C", "P"]
    quantity: int = Field(default=1, gt=0)
    limit_price: Optional[float] = Field(default=None)
    closing_position: bool = False

    @model_validator(mode="after")
    def _exp(self) -> "DiagonalSpreadParams":
        _check_expiration(self.near_expiration, "near_expiration")
        _check_expiration(self.far_expiration, "far_expiration")
        return self


class BuyOptionParams(BaseModel):
    symbol: str
    expiration: str
    strike: float = Field(gt=0)
    right: Literal["C", "P"]
    quantity: int = Field(default=1, gt=0)
    limit_price: Optional[float] = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _exp(self) -> "BuyOptionParams":
        _check_expiration(self.expiration)
        return self


class CoveredCallParams(BaseModel):
    symbol: str
    expiration: str
    strike: float = Field(gt=0)
    shares: int = Field(default=100, gt=0)

    @model_validator(mode="after")
    def _exp(self) -> "CoveredCallParams":
        _check_expiration(self.expiration)
        if self.shares % 100 != 0:
            raise ValueError("shares must be a multiple of 100")
        return self


class CashSecuredPutParams(BaseModel):
    symbol: str
    expiration: str
    strike: float = Field(gt=0)
    contracts: int = Field(default=1, gt=0)
    limit_price: Optional[float] = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _exp(self) -> "CashSecuredPutParams":
        _check_expiration(self.expiration)
        return self


class ProtectivePutParams(CoveredCallParams):
    pass


class CollarParams(BaseModel):
    symbol: str
    expiration: str
    put_strike: float = Field(gt=0)
    call_strike: float = Field(gt=0)
    shares: int = Field(default=100, gt=0)

    @model_validator(mode="after")
    def _exp(self) -> "CollarParams":
        _check_expiration(self.expiration)
        if self.shares % 100 != 0:
            raise ValueError("shares must be a multiple of 100")
        return self


class RatioSpreadParams(BaseModel):
    symbol: str
    expiration: str
    long_strike: float = Field(gt=0)
    short_strike: float = Field(gt=0)
    right: Literal["C", "P"]
    ratio: int = Field(default=2, gt=1)
    quantity: int = Field(default=1, gt=0)
    limit_price: Optional[float] = Field(default=None)

    @model_validator(mode="after")
    def _exp(self) -> "RatioSpreadParams":
        _check_expiration(self.expiration)
        return self


class JadeLizardParams(BaseModel):
    symbol: str
    expiration: str
    put_strike: float = Field(gt=0)
    call_short_strike: float = Field(gt=0)
    call_long_strike: float = Field(gt=0)
    quantity: int = Field(default=1, gt=0)
    limit_price: Optional[float] = Field(default=None)

    @model_validator(mode="after")
    def _exp(self) -> "JadeLizardParams":
        _check_expiration(self.expiration)
        return self


class RollOptionParams(BaseModel):
    """Roll an existing option via conId or symbol+expiry+strike+right."""

    symbol: str
    quantity: int = Field(gt=0)
    conId: Optional[int] = Field(default=None, gt=0)
    expiration: Optional[str] = None
    strike: Optional[float] = Field(default=None, gt=0)
    right: Optional[Literal["C", "P"]] = None
    new_strike: Optional[float] = Field(default=None, gt=0)
    new_dte: Optional[int] = Field(default=None, gt=0)
    new_expiration: Optional[str] = None
    roll_type: Literal[
        "ROLL_OUT", "ROLL_UP", "ROLL_DOWN", "ROLL_OUT_UP", "ROLL_OUT_DOWN"
    ] = "ROLL_OUT"
    limit_price: Optional[float] = None

    @model_validator(mode="after")
    def _identity(self) -> "RollOptionParams":
        if self.conId is None and not (
            self.expiration and self.strike is not None and self.right
        ):
            raise ValueError(
                "roll_option requires conId or expiration+strike+right to identify the leg"
            )
        if self.expiration:
            _check_expiration(self.expiration)
        if self.new_expiration:
            _check_expiration(self.new_expiration, "new_expiration")
        return self


EXTRA_STRATEGIES: Dict[str, tuple[type[BaseModel], str]] = {
    "trailing_stop": (TrailingStopParams, "place_trailing_stop"),
    "trailing_stop_limit": (TrailingStopLimitParams, "place_trailing_stop_limit"),
    "market_on_close": (MarketOnCloseParams, "place_market_on_close"),
    "limit_on_close": (LimitOnCloseParams, "place_limit_on_close"),
    "market_on_open": (MarketOnOpenParams, "place_market_on_open"),
    "limit_on_open": (LimitOnOpenParams, "place_limit_on_open"),
    "adaptive": (AdaptiveParams, "place_adaptive"),
    "midprice": (MidpriceParams, "place_midprice"),
    "relative": (RelativeParams, "place_relative"),
    "limit_order_gtd": (LimitOrderGtdParams, "place_limit_order_gtd"),
    "fill_or_kill": (FillOrKillParams, "place_fill_or_kill"),
    "immediate_or_cancel": (ImmediateOrCancelParams, "place_immediate_or_cancel"),
    "vwap": (VwapParams, "place_vwap"),
    "twap": (TwapParams, "place_twap"),
    "iceberg": (IcebergParams, "place_iceberg_order"),
    "snap_to_midpoint": (SnapToMidpointParams, "place_snap_to_midpoint"),
    "vertical_spread": (VerticalSpreadParams, "place_vertical_spread"),
    "iron_condor": (IronCondorParams, "place_iron_condor"),
    "iron_butterfly": (IronButterflyParams, "place_iron_butterfly"),
    "straddle": (StraddleParams, "place_straddle"),
    "strangle": (StrangleParams, "place_strangle"),
    "butterfly": (ButterflyParams, "place_butterfly"),
    "calendar_spread": (CalendarSpreadParams, "place_calendar_spread"),
    "diagonal_spread": (DiagonalSpreadParams, "place_diagonal_spread"),
    "buy_option": (BuyOptionParams, "buy_option"),
    "covered_call": (CoveredCallParams, "place_covered_call"),
    "cash_secured_put": (CashSecuredPutParams, "sell_cash_secured_put"),
    "protective_put": (ProtectivePutParams, "place_protective_put"),
    "collar": (CollarParams, "place_collar"),
    "ratio_spread": (RatioSpreadParams, "place_ratio_spread"),
    "jade_lizard": (JadeLizardParams, "place_jade_lizard"),
    "roll_option": (RollOptionParams, "roll_option"),
}

EXTRA_MANAGEMENT = frozenset({"trailing_stop", "trailing_stop_limit", "roll_option"})

OPTION_STRATEGIES = frozenset({
    "vertical_spread", "iron_condor", "iron_butterfly", "straddle", "strangle",
    "butterfly", "calendar_spread", "diagonal_spread", "buy_option",
    "covered_call", "cash_secured_put", "protective_put", "collar",
    "ratio_spread", "jade_lizard", "close_option", "roll_option",
})

EXIT_ONLY_EXTRA = frozenset({
    "market_on_close", "limit_on_close", "market_on_open", "limit_on_open",
    "adaptive", "midprice", "relative", "limit_order_gtd", "fill_or_kill",
    "immediate_or_cancel", "vwap", "twap", "iceberg", "snap_to_midpoint",
})

# Dummies for required fields when probing a BUY without closing_position.
# Unknown required names fail-closed — do not invent a silent skip.
_NAKED_BUY_PROBE: Dict[str, Any] = {
    "symbol": "SPY",
    "action": "BUY",
    "quantity": 1,
    "limit_price": 100.0,
    "good_till_date": "20261231 16:00:00",
    "total_quantity": 10,
    "display_size": 1,
    "offset": 0.01,
    "order_type": "MKT",
    "priority": "Normal",
}


def extra_bare_stock_strategies() -> frozenset[str]:
    """Extra STK tickets that are not option shapes and not trail/roll management."""
    return frozenset(EXTRA_STRATEGIES) - OPTION_STRATEGIES - EXTRA_MANAGEMENT


def _naked_buy_probe(model_cls: type[BaseModel]) -> Dict[str, Any]:
    payload: Dict[str, Any] = {}
    missing: list[str] = []
    for name, field in model_cls.model_fields.items():
        if name == "closing_position":
            continue
        if not field.is_required():
            continue
        if name not in _NAKED_BUY_PROBE:
            missing.append(name)
            continue
        payload[name] = _NAKED_BUY_PROBE[name]
    if missing:
        raise RuntimeError(
            f"fail-closed: cannot probe {model_cls.__name__} naked BUY — "
            f"no dummy for required {missing}"
        )
    if "action" in model_cls.model_fields:
        payload["action"] = "BUY"
    payload.pop("closing_position", None)
    return payload


def extra_stock_naked_buy_leaks() -> list[str]:
    """Names / reasons for extra STK schemas that accept a BUY without closing_position.

    Empty list means fail-closed held. A new extra stock ticket must land in
    EXIT_ONLY_EXTRA and reject a naked BUY — otherwise this reports a leak.
    """
    leaks: list[str] = []
    expected = extra_bare_stock_strategies()
    extra = EXIT_ONLY_EXTRA - expected
    missing = expected - EXIT_ONLY_EXTRA
    if extra:
        leaks.append(f"EXIT_ONLY_EXTRA has non-bare extras: {sorted(extra)}")
    if missing:
        leaks.append(f"EXIT_ONLY_EXTRA missing bare STK: {sorted(missing)}")

    for name in sorted(expected | EXIT_ONLY_EXTRA):
        entry = EXTRA_STRATEGIES.get(name)
        if entry is None:
            leaks.append(f"{name}: in EXIT_ONLY_EXTRA but not EXTRA_STRATEGIES")
            continue
        model, _ = entry
        try:
            probe = _naked_buy_probe(model)
        except RuntimeError as exc:
            leaks.append(str(exc))
            continue
        for attempt in (probe, {**probe, "closing_position": False}):
            try:
                model.model_validate(attempt)
            except ValidationError:
                continue
            leaks.append(f"{name}: accepted BUY without closing_position")
            break
    return leaks


def assert_extra_stock_exit_only() -> None:
    leaks = extra_stock_naked_buy_leaks()
    if leaks:
        raise RuntimeError(
            "fail-closed: extra STK schema allows a naked buy without "
            f"closing_position: {'; '.join(leaks)}"
        )


assert_extra_stock_exit_only()
