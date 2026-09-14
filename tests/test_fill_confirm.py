"""Fill confirmation parsing / polling (mocked client)."""

from polymarket_btc_arb.executor import (
    LiveClobBackend,
    confirm_order_fill,
    parse_post_order_fill,
)


def test_parse_matched_response_uses_taking_amount():
    resp = {
        "orderID": "0xabc",
        "takingAmount": "5.0",
        "makingAmount": "2.5",
        "status": "matched",
        "success": True,
    }
    filled, size, oid, status = parse_post_order_fill(resp, 5.0)
    assert filled is True
    assert size == 5.0
    assert oid == "0xabc"
    assert status == "matched"


def test_parse_live_not_optimistic_full_fill():
    resp = {"orderID": "0x1", "status": "live", "success": True}
    filled, size, oid, status = parse_post_order_fill(resp, 10.0)
    assert filled is False
    assert size == 0.0
    assert oid == "0x1"


def test_confirm_polls_get_order_until_matched():
    class FakeClient:
        def __init__(self):
            self.n = 0

        def get_order(self, oid):
            self.n += 1
            if self.n < 2:
                return {
                    "id": oid,
                    "status": "ORDER_STATUS_LIVE",
                    "size_matched": "0",
                    "original_size": "10",
                }
            return {
                "id": oid,
                "status": "ORDER_STATUS_MATCHED",
                "size_matched": "10",
                "original_size": "10",
            }

    filled, size, status = confirm_order_fill(
        FakeClient(),
        "oid1",
        10.0,
        timeout_sec=2.0,
        poll_interval=0.01,
        initial_resp={"orderID": "oid1", "status": "live"},
    )
    assert filled is True
    assert size == 10.0
    assert "matched" in (status or "")


def test_live_backend_not_optimistic_without_match(monkeypatch):
    class FakeClient:
        def create_order(self, args):
            return object()

        def post_order(self, signed, order_type):
            return {"orderID": "x", "status": "live", "success": True}

        def get_order(self, oid):
            return {
                "id": oid,
                "status": "ORDER_STATUS_LIVE",
                "size_matched": "0",
                "original_size": "5",
            }

        def cancel(self, oid):
            return True

    # Avoid importing real OrderArgs path failures — patch place internals
    be = LiveClobBackend(FakeClient(), fill_timeout_sec=0.05, poll_interval_sec=0.01)

    import polymarket_btc_arb.executor as ex_mod

    class FakeOrderArgs:
        def __init__(self, **kw):
            self.kw = kw

    class FakeOrderType:
        GTC = "GTC"
        FOK = "FOK"

    monkeypatch.setitem(
        __import__("sys").modules,
        "py_clob_client.clob_types",
        type("m", (), {"OrderArgs": FakeOrderArgs, "OrderType": FakeOrderType})(),
    )
    monkeypatch.setitem(
        __import__("sys").modules,
        "py_clob_client.order_builder.constants",
        type("m", (), {"BUY": "BUY", "SELL": "SELL"})(),
    )

    # Re-bind after module stubs — LiveClobBackend imports inside _place
    result = be.buy("token", 0.5, 5.0, maker=True)
    assert result.filled is False
    assert result.fill_size == 0.0
    assert result.dry_run is False
