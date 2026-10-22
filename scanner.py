#!/usr/bin/env python3
"""MEXC ICT Power Of Three signal scanner -> Telegram.  SIGNAL ONLY: never places orders."""
from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, List, Optional, Protocol, Sequence

from config import ScannerConfig
from models import Candle, ConfigError, Signal
from mexc_data import MexcApiError, MexcClient, drop_unclosed_candles, timeframe_ms
from po3_strategy import PO3Config, STRATEGY_NAME, run_po3, signal_id
from state_store import SentSignalStore
from telegram_bot import TelegramSender, format_signal_message, load_credentials

log = logging.getLogger("scanner")


class Sender(Protocol):
    def send_text(self, text: str) -> bool: ...


class PrintSender:
    """Used by --dry-run: prints instead of sending."""

    def send_text(self, text: str) -> bool:
        print("---- DRY RUN (not sent) ----")
        print(text)
        return True


@dataclass
class ScanSummary:
    symbols: int = 0
    series_scanned: int = 0
    fetch_errors: int = 0
    signals_found_in_window: int = 0
    fresh: int = 0
    skipped_already_sent: int = 0
    skipped_resolved: int = 0
    skipped_stale: int = 0
    sent: int = 0
    send_failures: int = 0
    notes: List[str] = field(default_factory=list)


def find_fresh_signals(
    closed: Sequence[Candle],
    timeframe: str,
    now_ms: int,
    strat_cfg: PO3Config,
    max_age_minutes: int,
    summary: Optional[ScanSummary] = None,
) -> List[Signal]:
    """Run the strategy on CLOSED candles and keep only signals that are new enough to alert.

    A signal is alertable when its candle closed within ``max_age_minutes`` AND the trade
    has not already hit TP/SL in later closed candles. This is what stops a fresh run
    (empty state, missed schedules, history window) from re-announcing old signals.
    """
    tf_ms = timeframe_ms(timeframe)
    signals = run_po3(list(closed), strat_cfg)
    out: List[Signal] = []
    for s in signals:
        if summary:
            summary.signals_found_in_window += 1
        age_ms = now_ms - (s.candle_open_ms + tf_ms)
        if age_ms > max_age_minutes * 60_000:
            if summary:
                summary.skipped_stale += 1
            continue
        if s.exit_index is not None:
            if summary:
                summary.skipped_resolved += 1
            continue
        out.append(s)
    return out


def run_scan(
    client,
    sender: Sender,
    store: SentSignalStore,
    cfg: ScannerConfig,
    strat_cfg: PO3Config,
    symbols: Sequence[str],
    now_ms: int,
    dry_run: bool = False,
    sleep: Callable[[float], None] = time.sleep,
) -> ScanSummary:
    summary = ScanSummary(symbols=len(symbols))
    buffer_ms = cfg.close_buffer_seconds * 1000
    last_alert_sent = False

    for symbol in symbols:
        for tf in cfg.timeframes:
            tf_ms = timeframe_ms(tf)
            try:
                candles = client.get_klines(symbol, tf, cfg.candle_limit)
            except (MexcApiError, ValueError) as exc:
                summary.fetch_errors += 1
                log.warning("%s %s: fetch failed (%s)", symbol, tf, exc)
                continue
            finally:
                if cfg.request_delay_seconds:
                    sleep(cfg.request_delay_seconds)

            closed = drop_unclosed_candles(candles, tf_ms, now_ms, buffer_ms)
            if len(closed) < strat_cfg.min_candles + 10:
                log.debug("%s %s: only %d closed candles - skipped", symbol, tf, len(closed))
                continue
            summary.series_scanned += 1

            fresh = find_fresh_signals(closed, tf, now_ms, strat_cfg, cfg.max_age_minutes[tf], summary)
            for sig in fresh:  # oldest first
                summary.fresh += 1
                sid = signal_id(symbol, tf, sig.direction, sig.candle_open_ms)
                if sid in store:
                    summary.skipped_already_sent += 1
                    continue
                text = format_signal_message(sig, symbol, tf, tf_ms)
                # Keep Telegram messages readable and avoid bursts when many pairs signal together.
                if last_alert_sent:
                    sleep(15)
                if sender.send_text(text):
                    summary.sent += 1
                    last_alert_sent = True
                    if not dry_run:
                        store.add(sid)
                        store.save()  # persist immediately: a crash later can't cause a re-send
                else:
                    summary.send_failures += 1  # NOT recorded -> retried next run while still fresh
    if not dry_run:
        store.prune()
        store.save()
    return summary


def inspect(client, symbol: str, timeframe: str, cfg: ScannerConfig, strat_cfg: PO3Config, now_ms: int) -> int:
    """Print every signal PO3 finds in the fetched window, for comparing against TradingView."""
    tf_ms = timeframe_ms(timeframe)
    candles = drop_unclosed_candles(client.get_klines(symbol, timeframe, cfg.candle_limit), tf_ms, now_ms, cfg.close_buffer_seconds * 1000)
    signals = run_po3(candles, strat_cfg)
    print(f"{symbol} {timeframe}: {len(candles)} closed candles, first={_fmt(candles[0].open_time_ms) if candles else 'n/a'}, {len(signals)} signal(s)")
    for s in signals:
        outcome = f"{s.exit_reason}@{s.exit_price:.8g} (candle {_fmt(candles[s.exit_index].open_time_ms)})" if s.exit_index is not None else "open"
        print(f"  {s.direction:4} candle={_fmt(s.candle_open_ms)} entry={s.entry_price:.8g} SL={s.stop_loss:.8g} TP={s.take_profit:.8g} -> {outcome}")
    return 0


def _fmt(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _now_ms(client) -> int:
    try:
        return client.get_server_time_ms()
    except Exception as exc:  # fall back to the runner clock
        log.warning("Server time unavailable (%s) - using local clock", type(exc).__name__)
        return int(time.time() * 1000)


def normalize_symbol(symbol: str) -> str:
    """Normalize common BTCUSDT input to the MEXC Futures symbol BTC_USDT."""
    symbol = symbol.strip().upper()
    if symbol.endswith("_USDT"):
        return symbol
    if symbol.endswith("USDT"):
        return symbol[:-4] + "_USDT"
    return symbol


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="MEXC ICT Power Of Three signal scanner (signal-only).")
    p.add_argument("--dry-run", action="store_true", help="print signals instead of sending; state is not modified; Telegram secrets not required")
    p.add_argument("--telegram-test", action="store_true", help="send one test message to verify TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID, then exit")
    p.add_argument("--symbols", help="comma-separated Futures symbols (e.g. BTC_USDT,ETH_USDT) instead of the automatic top-N")
    p.add_argument("--timeframes", help="comma-separated timeframe override (default: 1h only)")
    p.add_argument("--inspect", metavar="SYMBOL", help="list all PO3 signals in the fetched window for SYMBOL (no alerts)")
    p.add_argument("-v", "--verbose", action="store_true")
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    # A Telegram test must never silently use PrintSender because --dry-run or
    # --inspect selected it. Reject conflicting modes instead of reporting a
    # successful test when no Telegram request was made.
    if args.telegram_test and (args.dry_run or args.inspect):
        print("--telegram-test cannot be combined with --dry-run or --inspect.", file=sys.stderr)
        return 2

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    try:
        cfg = ScannerConfig.from_env()
        if args.timeframes:
            tfs = tuple(t.strip() for t in args.timeframes.split(",") if t.strip())
            for t in tfs:
                timeframe_ms(t)
            cfg = ScannerConfig(**{**cfg.__dict__, "timeframes": tfs})
        needs_telegram = not (args.dry_run or args.inspect)
        sender: Sender
        if needs_telegram:
            sender = TelegramSender(load_credentials())
        else:
            sender = PrintSender()
    except (ConfigError, ValueError) as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2

    if args.telegram_test:
        ok = sender.send_text(f"✅ {STRATEGY_NAME}: Telegram configuration works. Signal-only scanner, no orders are placed.")
        print("Test message sent." if ok else "Test message FAILED (check token / chat id; see log above).")
        return 0 if ok else 1

    strat_cfg = PO3Config()
    client = MexcClient()
    now_ms = _now_ms(client)

    if args.inspect:
        tf = cfg.timeframes[0]
        return inspect(client, normalize_symbol(args.inspect), tf, cfg, strat_cfg, now_ms)

    try:
        symbols = [normalize_symbol(s) for s in args.symbols.split(",") if s.strip()] if args.symbols else client.get_top_symbols(cfg.top_n)
    except MexcApiError as exc:
        print(f"Could not load MEXC symbol list: {exc}", file=sys.stderr)
        return 1
    log.info("Scanning %d symbols x %s (dry_run=%s)", len(symbols), ",".join(cfg.timeframes), args.dry_run)

    store = SentSignalStore(cfg.state_path, cfg.retention_days)
    summary = run_scan(client, sender, store, cfg, strat_cfg, symbols, now_ms, dry_run=args.dry_run)
    log.info(
        "Done: series=%d fetch_errors=%d window_signals=%d fresh=%d already_sent=%d resolved=%d stale=%d sent=%d send_failures=%d",
        summary.series_scanned, summary.fetch_errors, summary.signals_found_in_window, summary.fresh,
        summary.skipped_already_sent, summary.skipped_resolved, summary.skipped_stale, summary.sent, summary.send_failures,
    )
    if summary.series_scanned == 0:
        print("No series could be scanned (all fetches failed?).", file=sys.stderr)
        return 1
    return 1 if summary.send_failures else 0


if __name__ == "__main__":
    sys.exit(main())
