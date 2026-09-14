"""Gap / opportunity detection tests."""

from polymarket_btc_arb.arbitrage import find_opportunity


def test_edge_formula_basic():
    opp = find_opportunity(0.48, 0.48, min_edge=0.02)
    assert opp is not None
    assert abs(opp.pair_cost - 0.96) < 1e-9
    assert abs(opp.edge - 0.04) < 1e-9
    assert opp.is_actionable


def test_no_opportunity_when_edge_below_min():
    # edge = 0.01 < 0.02
    assert find_opportunity(0.495, 0.495, min_edge=0.02) is None


def test_exact_min_edge_is_actionable():
    opp = find_opportunity(0.49, 0.49, min_edge=0.02)
    assert opp is not None
    assert abs(opp.edge - 0.02) < 1e-9


def test_asks_only_rejects_none_and_invalid():
    assert find_opportunity(None, 0.4) is None
    assert find_opportunity(0.4, None) is None
    assert find_opportunity(0, 0.5) is None
    assert find_opportunity(-0.1, 0.5) is None
    assert find_opportunity(1.1, 0.1) is None


def test_sum_at_or_above_one_no_arb():
    assert find_opportunity(0.5, 0.5, min_edge=0.0) is None
    assert find_opportunity(0.6, 0.5, min_edge=0.0) is None


def test_default_min_edge_is_002():
    # edge 0.015 < default 0.02
    assert find_opportunity(0.49, 0.495) is None
    assert find_opportunity(0.48, 0.49) is not None
