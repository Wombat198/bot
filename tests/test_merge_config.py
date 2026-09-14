from polymarket_btc_arb.merge import DryRunMerger, merge_positions
from polymarket_btc_arb.config import load_config, DEFAULTS


def test_dry_run_merge():
    r = merge_positions("0xabc", 10.0, dry_run=True)
    assert r.success and r.dry_run
    assert "dry_run" in r.detail


def test_merger_class():
    r = DryRunMerger().merge_positions("0xcond", 5)
    assert r.condition_id == "0xcond"
    assert r.size == 5


def test_config_defaults_dry_run():
    cfg = load_config(None)
    assert cfg["execution"]["dry_run"] is True
    assert cfg["strategy"]["min_edge"] == DEFAULTS["strategy"]["min_edge"]
