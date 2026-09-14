from polymarket_btc_arb.markets.clob import best_ask_from_book


def test_best_ask_is_last_level():
    # Polymarket docs: asks sorted descending → best (lowest) ask is last
    book = {
        "asks": [
            {"price": "0.90", "size": "10"},
            {"price": "0.55", "size": "5"},
            {"price": "0.51", "size": "100"},
        ]
    }
    assert best_ask_from_book(book) == 0.51


def test_empty_book():
    assert best_ask_from_book({"asks": []}) is None
    assert best_ask_from_book({}) is None
