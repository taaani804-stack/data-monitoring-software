"""Runs scanner.main() through real HTTP against a local fake MEXC + Telegram server."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

import mexc_data
import scanner
import telegram_bot
from tests.helpers import T0, TF15, buy_series

TF1H = 3_600_000
SECRET = "999:E2E-SECRET-TOKEN"
SIGNAL_CLOSE = T0 + 61 * TF15


class State:
    sent = []
    kline_requests = []
    now_ms = SIGNAL_CLOSE + 20_000


def _rows(candles, forming):
    all_candles = list(candles)
    if forming:
        t = candles[-1].open_time_ms + TF15
        from models import Candle
        all_candles.append(Candle(t, 100, 200, 1, 150, 1))
    return {
        "time": [c.open_time_ms // 1000 for c in all_candles],
        "open": [str(c.open) for c in all_candles],
        "high": [str(c.high) for c in all_candles],
        "low": [str(c.low) for c in all_candles],
        "close": [str(c.close) for c in all_candles],
        "vol": ["1" for _ in all_candles],
        "amount": ["1" for _ in all_candles],
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == "/api/v1/contract/ping":
            return self._json({"success": True, "data": State.now_ms})
        if u.path == "/api/v1/contract/detail":
            return self._json({"success": True, "data": [
                {"symbol": "AAA_USDT", "baseCoin": "AAA", "quoteCoin": "USDT",
                 "settleCoin": "USDT", "futureType": 1, "state": 0},
                {"symbol": "USDC_USDT", "baseCoin": "USDC", "quoteCoin": "USDT",
                 "settleCoin": "USDT", "futureType": 1, "state": 0},
            ]})
        if u.path == "/api/v1/contract/ticker":
            return self._json({"success": True, "data": [
                {"symbol": "AAA_USDT", "lastPrice": "100", "amount24": "5000000"},
                {"symbol": "USDC_USDT", "lastPrice": "1", "amount24": "9000000"},
            ]})
        if u.path.startswith("/api/v1/contract/kline/"):
            symbol = u.path.rsplit("/", 1)[-1]
            interval = q["interval"][0]
            State.kline_requests.append((symbol, interval))
            if symbol == "AAA_USDT" and interval == "Min15":
                return self._json({"success": True, "data": _rows(buy_series(), forming=True)})
            if interval == "Min60":
                return self._json({"success": True, "data": {
                    "time": [], "open": [], "high": [], "low": [], "close": [], "vol": []
                }})
            return self._json({"success": False, "code": 500, "msg": "Invalid interval"}, 400)
        self._json({"msg": "nope"}, 404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        payload = json.loads(self.rfile.read(n))
        State.sent.append((self.path, payload))
        self._json({"ok": True})


@pytest.fixture
def server(monkeypatch, tmp_path):
    State.sent, State.kline_requests, State.now_ms = [], [], SIGNAL_CLOSE + 20_000
    srv = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}"
    monkeypatch.setattr(mexc_data, "BASE_URL", url)
    monkeypatch.setattr(telegram_bot, "API_URL", url)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", SECRET)
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "4242")
    monkeypatch.setenv("STATE_PATH", str(tmp_path / "sent.json"))
    monkeypatch.setattr(scanner.time, "sleep", lambda _s: None)
    yield url
    srv.shutdown()


def test_full_run_sends_one_alert_and_second_run_is_silent(server, capsys):
    assert scanner.main(["--timeframes", "15m"]) == 0
    assert len(State.sent) == 1
    path, payload = State.sent[0]
    assert path == f"/bot{SECRET}/sendMessage" and payload["chat_id"] == "4242"
    assert "BUY SIGNAL" in payload["text"] and "AAA_USDT" in payload["text"] and "(15m)" in payload["text"]
    assert "94.2" in payload["text"] and "104.058" in payload["text"]
    assert ("AAA_USDT", "Min15") in State.kline_requests

    State.now_ms += 15 * 60_000  # next scheduled run (candle 61 has now closed too)
    assert scanner.main(["--timeframes", "15m"]) == 0
    assert len(State.sent) == 1  # no duplicate
    out = capsys.readouterr()
    assert SECRET not in out.out and SECRET not in out.err  # secrets never printed


def test_stablecoin_pair_is_not_scanned(server):
    scanner.main(["--timeframes", "15m"])
    # only the eligible AAAUSDT 15m series was fetched; USDCUSDT was filtered out
    assert len(State.kline_requests) == 1


def test_forming_candle_never_triggers_alert(server):
    State.now_ms = T0 + 61 * TF15 + 60_000  # forming candle (index 61) is 1 min old, it has a huge fake range
    scanner.main(["--timeframes", "15m"])
    texts = [p["text"] for _, p in State.sent]
    assert all("SELL" not in t for t in texts)  # the forming candle's spike must not create a signal


def test_dry_run_needs_no_secrets_and_sends_nothing(server, monkeypatch, capsys):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
    monkeypatch.delenv("TELEGRAM_CHAT_ID")
    assert scanner.main(["--dry-run", "--timeframes", "15m"]) == 0
    assert State.sent == []
    assert "DRY RUN" in capsys.readouterr().out


def test_telegram_test_mode_sends_one_message_and_no_scan(server):
    assert scanner.main(["--telegram-test"]) == 0
    assert len(State.sent) == 1 and "configuration works" in State.sent[0][1]["text"]
    assert State.kline_requests == []


def test_inspect_lists_signal_without_sending(server, monkeypatch, capsys):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
    monkeypatch.delenv("TELEGRAM_CHAT_ID")
    assert scanner.main(["--inspect", "AAAUSDT", "--timeframes", "15m"]) == 0
    out = capsys.readouterr().out
    assert "1 signal(s)" in out and "BUY" in out and "open" in out
    assert State.sent == []
