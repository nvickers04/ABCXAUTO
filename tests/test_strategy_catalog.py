from abcxauto.strategy_catalog import catalog_payload, resolve_basis


def test_catalog_maps_established_names_to_send():
    payload = catalog_payload()
    assert payload["n"] >= 10
    keys = {row["key"] for row in payload["rows"]}
    sends = {row["send"] for row in payload["rows"]}
    assert "debit_vertical" in keys
    assert "vertical_spread" in sends
    assert "iron_condor" in keys
    assert resolve_basis("bull put") == "credit_vertical"
    assert resolve_basis("csp") == "cash_secured_put"
    assert resolve_basis("nope") is None


def test_catalog_filter():
    payload = catalog_payload("condor")
    assert payload["n"] >= 1
    assert any(row["send"] == "iron_condor" for row in payload["rows"])
