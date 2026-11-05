"""Data API v2 приводится к формату живого потока."""
from polymarket import Api


def test_trades_v2_fields_renamed_to_stream_format(monkeypatch):
    row = {"proxy_wallet": "0xabc", "token_id": "123", "condition_id": "0xc",
           "event_slug": "e", "outcome_index": 1, "transaction_hash": "0xt",
           "side": "BUY", "size": 10.0, "price": 0.5, "title": "T", "slug": "s"}
    calls = []

    def fake_get(url, params=None):
        calls.append((url, params))
        return {"data": [row], "pagination": {}}

    api = Api()
    monkeypatch.setattr(api, "get", fake_get)
    trade = api.trades("0xabc", limit=5)[0]

    assert calls[0][0].endswith("/v2/trades")
    assert calls[0][1]["taker_only"] == "false"
    assert trade["proxyWallet"] == "0xabc" and trade["asset"] == "123"
    assert trade["conditionId"] == "0xc" and trade["eventSlug"] == "e"
    assert trade["transactionHash"] == "0xt" and trade["side"] == "BUY"
