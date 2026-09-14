"""Kill switch and risk gate tests."""

from polymarket_btc_arb.risk import RiskConfig, RiskManager


def test_kill_switch_on_max_daily_loss():
    rm = RiskManager(
        RiskConfig(max_daily_loss=50, kill_switch=True, dry_run=True)
    )
    ok, _ = rm.allow_trade(10)
    assert ok
    rm.record_pnl(-30)
    assert not rm.is_killed
    rm.record_pnl(-25)  # cumulative -55
    assert rm.is_killed
    ok2, reason = rm.allow_trade(5)
    assert not ok2
    assert "kill_switch" in reason


def test_kill_switch_disabled_does_not_block():
    rm = RiskManager(
        RiskConfig(max_daily_loss=10, kill_switch=False, dry_run=True)
    )
    rm.record_pnl(-100)
    assert not rm.is_killed
    ok, _ = rm.allow_trade(5)
    assert ok


def test_max_position_size_blocks():
    rm = RiskManager(RiskConfig(max_position_size=20, dry_run=True))
    ok, reason = rm.allow_trade(25)
    assert not ok
    assert "max_position_size" in reason


def test_clamp_size():
    rm = RiskManager(RiskConfig(max_position_size=20))
    assert rm.clamp_size(100) == 20
    assert rm.clamp_size(5) == 5


def test_dry_run_default_true():
    assert RiskManager().dry_run is True
