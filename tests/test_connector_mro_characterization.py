"""Characterization: IBKRConnector mixin MRO before a module split.

Class-level only — does not instantiate a connector or touch TWS.
"""

from __future__ import annotations

from abcxauto.broker.bars import IBKRBarsMixin
from abcxauto.broker.connector import IBKRConnector, IBKRQueriesMixin
from abcxauto.broker.options import IBKROptionsMixin
from abcxauto.broker.orders import IBKROrdersMixin


def test_ibkr_connector_mro_orders_before_options_before_queries_before_bars():
    """MRO is Orders → Options → Queries → Bars so stock/close routing wins."""
    mro = IBKRConnector.__mro__
    assert mro.index(IBKROrdersMixin) < mro.index(IBKROptionsMixin)
    assert mro.index(IBKROptionsMixin) < mro.index(IBKRQueriesMixin)
    assert mro.index(IBKRQueriesMixin) < mro.index(IBKRBarsMixin)
    assert mro.index(IBKRConnector) == 0
    assert mro[1] is IBKROrdersMixin
    assert mro[2] is IBKROptionsMixin
    assert mro[3] is IBKRQueriesMixin
    assert mro[4] is IBKRBarsMixin


def test_close_option_position_is_orders_mixin_not_options():
    """Options mixin defines the same name; Orders shadows it on the class."""
    assert (
        IBKRConnector.close_option_position
        is IBKROrdersMixin.close_option_position
    )
    assert (
        IBKRConnector.close_option_position
        is not IBKROptionsMixin.close_option_position
    )
    assert hasattr(IBKROptionsMixin, "close_option_position")
