"""Live gating: refuse without key / without confirm flag."""

from types import SimpleNamespace

from polymarket_btc_arb.live_gate import resolve_dry_run


def _cfg(pk=None):
    return {"_env": {"private_key": pk}, "execution": {"dry_run": True}}


def test_no_live_flag_stays_dry():
    args = SimpleNamespace(live=False, i_understand_risk=False)
    dry, reason = resolve_dry_run(args, _cfg("0xabc"), py_clob_ok=True)
    assert dry is True
    assert "no --live" in reason


def test_live_without_confirm_forced_dry():
    args = SimpleNamespace(live=True, i_understand_risk=False)
    dry, reason = resolve_dry_run(args, _cfg("0xabc"), py_clob_ok=True)
    assert dry is True
    assert "--i-understand-risk" in reason


def test_live_without_key_forced_dry():
    args = SimpleNamespace(live=True, i_understand_risk=True)
    dry, reason = resolve_dry_run(args, _cfg(None), py_clob_ok=True)
    assert dry is True
    assert "POLYMARKET_PRIVATE_KEY" in reason


def test_live_without_py_clob_forced_dry():
    args = SimpleNamespace(live=True, i_understand_risk=True)
    dry, reason = resolve_dry_run(args, _cfg("0xabc"), py_clob_ok=False)
    assert dry is True
    assert "py-clob-client" in reason


def test_live_all_gates_pass():
    args = SimpleNamespace(live=True, i_understand_risk=True)
    dry, reason = resolve_dry_run(args, _cfg("0xabc"), py_clob_ok=True)
    assert dry is False
    assert "LIVE" in reason
