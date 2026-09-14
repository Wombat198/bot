"""Pure WS book ask extraction."""

from polymarket_btc_arb.markets.ws_book import (
    best_ask_from_price_change,
    best_ask_from_ws_book,
)
from polymarket_btc_arb.markets.clob import best_ask_from_book


def test_ws_book_descending_asks_last_is_best():
    msg = {
        "event_type": "book",
        "asset_id": "1",
        "asks": [
            {"price": "0.90", "size": "10"},
            {"price": "0.55", "size": "5"},
            {"price": "0.51", "size": "100"},
        ],
    }
    assert best_ask_from_ws_book(msg) == 0.51
    assert best_ask_from_book(msg) == 0.51


def test_price_change_best_ask():
    pc = {"asset_id": "1", "best_ask": "0.42", "best_bid": "0.40", "size": "1"}
    assert best_ask_from_price_change(pc) == 0.42


def test_empty_and_invalid():
    assert best_ask_from_ws_book({"asks": []}) is None
    assert best_ask_from_price_change({"best_ask": None}) is None
    assert best_ask_from_price_change({"best_ask": "nope"}) is None
