import json
from datetime import datetime, timedelta, timezone

import pytest

import scanner
from config import ScannerConfig
from po3_strategy import PO3Config, signal_id
from state_store import SentSignalStore
from tests.helpers import T0, TF15, base_series, buy_series, candle, flat

SIGNAL_CANDLE_OPEN = T0 + 60 * TF15
SIGNAL_CANDLE_CLOSE = SIGNAL_CANDLE_OPEN + TF15
CFG = ScannerConfig(timeframes=("15m",), request_delay_seconds=0, close_buffer_seconds=0)


class FakeClient:
    def __init__(self, candles):
        self.candles = candles

    def get_klines(self, symbol, timeframe, limit):
        return list(self.candles)


class FakeSender:
    def __init__(self, results=None):
        self.results, self.messages = list(results or []), []

    def send_text(self, text):
        self.messages.append(text)
        return self.results.pop(0) if self.results else True


def scan(tmp_path, candles, now_ms, sender, dry_run=False, symbols=("BTCUSDT",)):
    store = SentSignalStore(str(tmp_path / "sent.json"))
    summary = scanner.run_scan(FakeClient(candles), sender, store, CFG, PO3Config(), list(symbols), now_ms,
                               dry_run=dry_run, sleep=lambda _s: None)
    return summary


# ---- closed-candle handling at scanner level --------------------------------
def test_signal_on_still_forming_candle_is_not_alerted(tmp_path):
    candles = buy_series()  # sweep candle is the LAST row, as MEXC returns the forming candle
    mid_candle = SIGNAL_CANDLE_OPEN + 5 * 60_000
    s = FakeSender()
    summary = scan(tmp_path, candles, mid_candle, s)
    assert s.messages == [] and summary.sent == 0 and summary.signals_found_in_window == 0


def test_same_candle_alerts_once_it_has_closed(tmp_path):
    s = FakeSender()
    summary = scan(tmp_path, buy_series(), SIGNAL_CANDLE_CLOSE + 10_000, s)
    assert summary.sent == 1 and "BUY SIGNAL" in s.messages[0]


# ---- duplicate prevention -----------------------------------------------------
def test_signal_sent_once_across_runs_with_persisted_state(tmp_path):
    now = SIGNAL_CANDLE_CLOSE + 10_000
    s1, s2 = FakeSender(), FakeSender()
    assert scan(tmp_path, buy_series(), now, s1).sent == 1
    assert (tmp_path / "sent.json").exists()
    summary2 = scan(tmp_path, buy_series(), now + 5 * 60_000, s2)  # next run, new process / new store object
    assert s2.messages == [] and summary2.skipped_already_sent == 1


def test_failed_send_is_not_recorded_and_is_retried(tmp_path):
    now = SIGNAL_CANDLE_CLOSE + 10_000
    bad = FakeSender([False])
    r1 = scan(tmp_path, buy_series(), now, bad)
    assert r1.send_failures == 1 and r1.sent == 0
    good = FakeSender([True])
    r2 = scan(tmp_path, buy_series(), now + 15 * 60_000, good)  # still inside the 30 min window
    assert r2.sent == 1


def test_old_historical_signals_are_never_sent_as_new(tmp_path):
    """Empty state (first run / lost state) + old signal in the history window -> silent."""
    now = SIGNAL_CANDLE_CLOSE + 3 * 3_600_000
    candles = buy_series(flat(61, 20, mid=100, half=1.0))
    now = candles[-1].open_time_ms + TF15 + 10_000
    s = FakeSender()
    summary = scan(tmp_path, candles, now, s)
    assert s.messages == [] and summary.skipped_stale == 1 and summary.sent == 0


def test_missed_run_still_alerts_within_age_window(tmp_path):
    candles = buy_series([candle(61, 100, 101, 99, 100)])  # one quiet candle after the signal
    now = candles[-1].open_time_ms + TF15 + 10_000  # signal candle closed ~15 min ago
    s = FakeSender()
    assert scan(tmp_path, candles, now, s).sent == 1


def test_signal_already_resolved_is_skipped(tmp_path):
    candles = buy_series([candle(61, 100, 105, 99, 104)])  # TP already hit
    now = candles[-1].open_time_ms + TF15 + 10_000
    s = FakeSender()
    summary = scan(tmp_path, candles, now, s)
    assert s.messages == [] and summary.skipped_resolved == 1


def test_dry_run_prints_but_does_not_persist(tmp_path):
    s = FakeSender()
    summary = scan(tmp_path, buy_series(), SIGNAL_CANDLE_CLOSE + 10_000, s, dry_run=True)
    assert summary.sent == 1
    assert not (tmp_path / "sent.json").exists()


def test_signal_ids_differ_by_symbol_timeframe_direction_candle():
    base = signal_id("BTCUSDT", "15m", "BUY", 1000)
    assert base == signal_id("BTCUSDT", "15m", "BUY", 1000)
    assert len({base, signal_id("ETHUSDT", "15m", "BUY", 1000), signal_id("BTCUSDT", "1h", "BUY", 1000),
                signal_id("BTCUSDT", "15m", "SELL", 1000), signal_id("BTCUSDT", "15m", "BUY", 2000)}) == 5


def test_state_file_stores_signal_id(tmp_path):
    scan(tmp_path, buy_series(), SIGNAL_CANDLE_CLOSE + 10_000, FakeSender())
    data = json.loads((tmp_path / "sent.json").read_text())
    assert signal_id("BTCUSDT", "15m", "BUY", SIGNAL_CANDLE_OPEN) in data["sent"]


# ---- state store ----------------------------------------------------------------
def test_corrupt_state_file_does_not_crash(tmp_path):
    p = tmp_path / "sent.json"
    p.write_text("{not json")
    assert len(SentSignalStore(str(p))) == 0


def test_prune_removes_only_old_entries(tmp_path):
    st = SentSignalStore(str(tmp_path / "s.json"), retention_days=14)
    now = datetime(2026, 10, 18, tzinfo=timezone.utc)
    st.add("old", now - timedelta(days=15))
    st.add("new", now - timedelta(days=1))
    assert st.prune(now) == 1
    assert "old" not in st and "new" in st


def test_save_is_atomic_and_roundtrips(tmp_path):
    p = str(tmp_path / "deep" / "s.json")
    st = SentSignalStore(p)
    st.add("x")
    st.save()
    assert "x" in SentSignalStore(p)
    assert [f.name for f in (tmp_path / "deep").iterdir()] == ["s.json"]  # no temp files left


# ---- configuration safety ---------------------------------------------------------
def test_main_without_telegram_env_exits_2_with_safe_message(monkeypatch, capsys):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    assert scanner.main(["--symbols", "BTCUSDT"]) == 2
    err = capsys.readouterr().err
    assert "TELEGRAM_BOT_TOKEN" in err and "TELEGRAM_CHAT_ID" in err


def test_scanner_config_env_validation(monkeypatch):
    from models import ConfigError
    with pytest.raises(ConfigError):
        ScannerConfig.from_env({"TOP_N_SYMBOLS": "abc"})
    assert ScannerConfig.from_env({}).top_n == 150
    assert ScannerConfig.from_env({}).timeframes == ("1h",)
    assert ScannerConfig.from_env({"MAX_SIGNAL_AGE_MINUTES_1H": "120"}).max_age_minutes["1h"] == 120



def test_futures_symbol_normalization():
    assert scanner.normalize_symbol("BTCUSDT") == "BTC_USDT"
    assert scanner.normalize_symbol("btc_usdt") == "BTC_USDT"
    assert scanner.normalize_symbol(" ETHUSDT ") == "ETH_USDT"
