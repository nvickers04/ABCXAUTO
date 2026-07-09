"""Two-round simplification passes."""

from abcxauto.rocket import TWEAKS
from abcxauto.simplify import pass_one_runtime_prune, pass_two_structure_scan, run_two_simplification_passes


def test_two_passes_return_audit():
    before = dict(TWEAKS)
    try:
        TWEAKS.clear()
        TWEAKS["dead_noise_key"] = 1
        TWEAKS["cycle_sleep_s"] = 8
        rep = run_two_simplification_passes(lab={"pass_rate": 1.0})
        assert "round1" in rep and "round2" in rep
        assert "simplify" in rep["summary"]
        assert "dead_noise_key" not in TWEAKS
        assert "cycle_sleep_s" in TWEAKS
    finally:
        TWEAKS.clear()
        TWEAKS.update(before)


def test_pass_one_and_two_lists():
    r1 = pass_one_runtime_prune()
    r2 = pass_two_structure_scan({"pass_rate": 1.0})
    assert isinstance(r1, list) and isinstance(r2, list)
    assert r1 and r2
