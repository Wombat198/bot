"""Both-legs-or-neither: unwind naked leg on one-sided fill."""

from polymarket_btc_arb.arbitrage import find_opportunity
from polymarket_btc_arb.executor import DryRunBackend, Executor, LegResult, LegSide
from polymarket_btc_arb.risk import RiskConfig, RiskManager


class OneSidedBackend:
    """Fills YES only — forces unwind path."""

    def __init__(self) -> None:
        self.sells: list[tuple] = []

    def buy(self, token_id, price, size, *, maker):
        # YES token id convention in test: starts with "YES"
        filled = token_id.startswith("YES")
        return LegResult(
            side=LegSide.YES,
            token_id=token_id,
            price=price,
            size=size,
            filled=filled,
            order_id="t",
            fill_size=size if filled else 0.0,
            dry_run=True,
        )

    def sell(self, token_id, price, size, *, maker):
        self.sells.append((token_id, price, size, maker))
        return LegResult(
            side=LegSide.YES,
            token_id=token_id,
            price=price,
            size=size,
            filled=True,
            order_id="u",
            fill_size=size,
            dry_run=True,
        )


def test_both_legs_fill_dry_run():
    opp = find_opportunity(0.45, 0.45, min_edge=0.02)
    assert opp is not None
    ex = Executor(
        risk=RiskManager(RiskConfig(max_position_size=10, dry_run=True)),
        backend=DryRunBackend(fill=True),
        require_both_legs=True,
        prefer_maker=True,
    )
    report = ex.execute_pair(opp, "YES_TOKEN", "NO_TOKEN", 10)
    assert report.both_filled
    assert not report.unwound
    assert report.dry_run
    assert any("BUY" in a for a in report.planned_actions)


def test_one_sided_yes_triggers_unwind():
    opp = find_opportunity(0.45, 0.45, min_edge=0.02)
    backend = OneSidedBackend()
    ex = Executor(
        risk=RiskManager(RiskConfig(max_position_size=10, dry_run=True)),
        backend=backend,
        require_both_legs=True,
    )
    report = ex.execute_pair(opp, "YES_abc", "NO_abc", 10)
    assert report.yes.filled
    assert not report.no.filled
    assert not report.both_filled
    assert report.unwound
    assert len(backend.sells) == 1
    assert backend.sells[0][0] == "YES_abc"
    assert any("UNWIND" in a for a in report.planned_actions)


def test_kill_switch_aborts_before_orders():
    opp = find_opportunity(0.4, 0.4, min_edge=0.02)
    rm = RiskManager(RiskConfig(max_daily_loss=10, kill_switch=True, dry_run=True))
    rm.engage_kill_switch("test")
    backend = DryRunBackend()
    ex = Executor(risk=rm, backend=backend)
    report = ex.execute_pair(opp, "Y", "N", 5)
    assert report.aborted
    assert not report.both_filled
    assert backend.actions == []


def test_neither_fill_no_unwind():
    class NoneFill(DryRunBackend):
        def buy(self, token_id, price, size, *, maker):
            r = super().buy(token_id, price, size, maker=maker)
            r.filled = False
            r.fill_size = 0.0
            return r

    opp = find_opportunity(0.45, 0.45)
    backend = NoneFill()
    ex = Executor(
        risk=RiskManager(RiskConfig(dry_run=True)),
        backend=backend,
        require_both_legs=True,
    )
    report = ex.execute_pair(opp, "Y", "N", 5)
    assert not report.both_filled
    assert not report.unwound
