"""modify_target / modify_stop must reread IBKR; success is not a local write."""

from types import SimpleNamespace

import pytest

from abcxauto.broker.orders import (
    IBKROrdersMixin,
    _finite_px,
    _oid_matches,
    modify_did_stick,
)


def test_oid_matches_order_id_or_perm():
    assert _oid_matches(SimpleNamespace(orderId=1503, permId=9), 1503)
    assert _oid_matches(SimpleNamespace(orderId=0, permId=1503), 1503)
    assert not _oid_matches(SimpleNamespace(orderId=1, permId=2), 1503)


def test_finite_px_drops_ibkr_unset():
    assert _finite_px(0.49) == 0.49
    assert _finite_px(1.7976931348623157e308) is None
    assert _finite_px(0) is None


def test_modify_did_stick():
    assert modify_did_stick(requested=0.49, live=0.49)
    assert not modify_did_stick(requested=0.49, live=0.52)
    assert not modify_did_stick(requested=0.49, live=None)


@pytest.mark.asyncio
async def test_modify_target_rejects_when_ibkr_keeps_old_lmt(monkeypatch):
    order = SimpleNamespace(orderId=1503, permId=99, lmtPrice=0.52)
    trade = SimpleNamespace(contract=object(), order=order)

    def placeOrder(_contract, o):
        o.lmtPrice = 0.52

    async def _noop(*_a, **_k):
        return None

    ib = SimpleNamespace(
        openTrades=lambda: [trade],
        placeOrder=placeOrder,
        reqAllOpenOrdersAsync=_noop,
    )
    mixin = IBKROrdersMixin()
    mixin.ib = ib

    async def _ok():
        return True

    mixin._ensure_connected = _ok
    monkeypatch.setattr("abcxauto.broker.orders._safe_sleep", _noop)

    out = await mixin.modify_target_price(1503, 0.48)
    assert "error" in out
    assert out["live_lmt"] == 0.52
    assert out["requested"] == 0.48
