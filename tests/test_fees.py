"""Fee curve + net edge tests (Crypto rate 0.07)."""

from polymarket_btc_arb.fees import (
    FeeConfig,
    edge_after_fees,
    is_tradeable_after_fees,
    taker_fee_usdc,
)
from polymarket_btc_arb.arbitrage import find_opportunity


def test_crypto_fee_at_mid_matches_docs_table():
    # Docs: 100 shares @ $0.50 → $1.75 taker fee (Crypto 0.07)
    fee = taker_fee_usdc(100, 0.50, 0.07)
    assert abs(fee - 1.75) < 1e-9


def test_crypto_fee_symmetric():
    assert abs(taker_fee_usdc(100, 0.30, 0.07) - 1.47) < 1e-9
    assert abs(taker_fee_usdc(100, 0.70, 0.07) - 1.47) < 1e-9


def test_net_edge_subtracts_both_legs():
    cfg = FeeConfig(taker_rate=0.07, assume_taker=True)
    # Gross edge at 0.48+0.48 = 0.04
    br = edge_after_fees(0.48, 0.48, shares=100, fee_cfg=cfg)
    assert abs(br.gross_edge - 0.04) < 1e-9
    # fee_ps = 2 * (0.07 * 0.48 * 0.52) = 2 * 0.017472 = 0.034944
    assert br.net_edge < br.gross_edge
    assert abs(br.fee_per_share - 2 * 0.07 * 0.48 * 0.52) < 1e-9


def test_tradeable_requires_net_edge_floor():
    cfg = FeeConfig(taker_rate=0.07, assume_taker=True)
    # Thin gross edge wiped by fees near mid
    ok, br = is_tradeable_after_fees(
        0.49, 0.49, min_edge=0.02, min_net_edge=0.02, shares=1, fee_cfg=cfg
    )
    assert br.gross_edge >= 0.02 - 1e-9
    assert not ok  # net should be below 0.02 after fees


def test_maker_path_zero_fee_when_not_assuming_taker():
    cfg = FeeConfig(taker_rate=0.07, assume_taker=False)
    br = edge_after_fees(0.48, 0.48, shares=10, fee_cfg=cfg)
    assert br.fee_usdc == 0.0
    assert abs(br.net_edge - 0.04) < 1e-9


def test_find_opportunity_with_fees():
    cfg = FeeConfig(taker_rate=0.07, assume_taker=True)
    # Wide edge should still clear net floor
    opp = find_opportunity(
        0.40,
        0.40,
        min_edge=0.02,
        min_net_edge=0.05,
        shares=1,
        fee_cfg=cfg,
    )
    assert opp is not None
    assert opp.net_edge >= 0.05
    assert opp.edge == opp.gross_edge
