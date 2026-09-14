"""Merge: dry success vs live failure without client/RPC."""

from polymarket_btc_arb.merge import DryRunMerger, LiveMerger, merge_positions


def test_dry_run_merge_success():
    r = merge_positions("0xabc", 10.0, dry_run=True)
    assert r.success and r.dry_run


def test_live_merger_fails_without_client_or_rpc(monkeypatch):
    monkeypatch.delenv("POLYMARKET_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("POLYMARKET_RPC_URL", raising=False)
    monkeypatch.delenv("RPC_URL", raising=False)
    m = LiveMerger(client=None, private_key=None, rpc_url=None)
    r = m.merge_positions("0xcond", 5.0)
    assert r.success is False
    assert r.dry_run is False
    assert "unavailable" in r.detail.lower() or "need" in r.detail.lower()


def test_live_via_helper_without_backend_fails():
    r = merge_positions("0xcond", 1.0, dry_run=False, backend=None)
    assert r.success is False
    assert r.dry_run is False


def test_dry_run_merger_class():
    r = DryRunMerger().merge_positions("0xcond", 5)
    assert r.success and r.dry_run
