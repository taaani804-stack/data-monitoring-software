"""MEXC USDT-M perpetual Futures public market data (read-only; no orders)."""
from __future__ import annotations

import logging
import math
import re
import time
from typing import Any, Callable, Dict, Iterable, List, Optional

import requests

from models import Candle

log = logging.getLogger("mexc")

# MEXC announced that Futures REST traffic moved to api.mexc.com in Jan 2026.
BASE_URL = "https://api.mexc.com"

TIMEFRAME_MS: Dict[str, int] = {"15m": 15 * 60_000, "1h": 60 * 60_000}
INTERVALS: Dict[str, str] = {"15m": "Min15", "1h": "Min60"}

STABLE_OR_PEGGED_BASES = {
    "USDT", "USDC", "FDUSD", "TUSD", "DAI", "BUSD", "USDD", "USDE", "USDP",
    "PYUSD", "USD1", "USDJ", "GUSD", "LUSD", "RLUSD", "XUSD", "EURC", "EURT",
    "AEUR", "EUR", "USDS",
}
_LEVERAGED_RE = re.compile(r"^[A-Z0-9]{2,}[2-5][LS]$")


class MexcApiError(Exception):
    """Raised for non-retryable API problems or exhausted retries."""


def timeframe_ms(timeframe: str) -> int:
    try:
        return TIMEFRAME_MS[timeframe]
    except KeyError:
        raise ValueError(
            f"Unsupported timeframe {timeframe!r}; supported: {sorted(TIMEFRAME_MS)}"
        )


def drop_unclosed_candles(
    candles: List[Candle], tf_ms: int, now_ms: int, buffer_ms: int = 0
) -> List[Candle]:
    """Keep only candles whose period has ended, including the safety buffer."""
    return [
        c for c in candles
        if c.open_time_ms + tf_ms + buffer_ms <= now_ms
    ]


def _to_float(value: Any) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def parse_klines(rows: Any) -> List[Candle]:
    """Parse row-oriented candles; retained for compatibility with existing tests/tools."""
    if not isinstance(rows, list):
        raise MexcApiError("Unexpected klines payload (not a list)")
    by_time: Dict[int, Candle] = {}
    bad = 0
    for row in rows:
        try:
            t = int(row[0])
            o, h, l, c = (_to_float(row[i]) for i in (1, 2, 3, 4))
            v = _to_float(row[5]) if len(row) > 5 else 0.0
        except (TypeError, ValueError, IndexError):
            bad += 1
            continue
        if None in (o, h, l, c) or h < l or min(o, h, l, c) <= 0:
            bad += 1
            continue
        by_time[t] = Candle(t, o, h, l, c, v or 0.0)
    if bad:
        log.warning("Skipped %d malformed kline row(s)", bad)
    return [by_time[t] for t in sorted(by_time)]


def parse_futures_klines(data: Any) -> List[Candle]:
    """Parse MEXC Futures' column-oriented data arrays."""
    if not isinstance(data, dict):
        raise MexcApiError("Unexpected Futures kline payload (data is not an object)")
    keys = ("time", "open", "high", "low", "close")
    if any(not isinstance(data.get(k), list) for k in keys):
        raise MexcApiError("Unexpected Futures kline payload (missing candle arrays)")
    times = data["time"]
    columns = [data["open"], data["high"], data["low"], data["close"]]
    volumes = data.get("vol", [])
    by_time: Dict[int, Candle] = {}
    bad = 0
    for i, raw_t in enumerate(times):
        try:
            t = int(raw_t)
            o, h, l, c = (_to_float(col[i]) for col in columns)
            v = _to_float(volumes[i]) if isinstance(volumes, list) and i < len(volumes) else 0.0
        except (TypeError, ValueError, IndexError):
            bad += 1
            continue
        if t < 100_000_000_000:
            t *= 1000
        if None in (o, h, l, c) or h < l or min(o, h, l, c) <= 0:
            bad += 1
            continue
        by_time[t] = Candle(t, o, h, l, c, v or 0.0)
    if bad:
        log.warning("Skipped %d malformed Futures kline row(s)", bad)
    return [by_time[t] for t in sorted(by_time)]


def is_unsuitable_base(base: str) -> bool:
    base = base.upper()
    return base in STABLE_OR_PEGGED_BASES or bool(_LEVERAGED_RE.match(base))


def _unwrap_data(payload: Any, endpoint: str) -> Any:
    if not isinstance(payload, dict):
        raise MexcApiError(f"{endpoint}: unexpected response payload")
    if payload.get("success") is False:
        raise MexcApiError(
            f"{endpoint}: MEXC error {payload.get('code', '')} "
            f"{payload.get('message') or payload.get('msg') or ''}".strip()
        )
    if "data" not in payload:
        raise MexcApiError(f"{endpoint}: response has no data field")
    return payload["data"]


def select_top_symbols(
    contract_info: Any, tickers: Iterable[Dict[str, Any]], top_n: int = 50
) -> List[str]:
    """Select enabled USDT-settled perpetual contracts by 24h turnover."""
    if isinstance(contract_info, dict):
        contracts = contract_info.get("data", contract_info.get("contracts", []))
    elif isinstance(contract_info, list):
        contracts = contract_info
    else:
        contracts = []

    eligible: Dict[str, Dict[str, Any]] = {}
    for item in contracts:
        if not isinstance(item, dict):
            continue
        sym = str(item.get("symbol", "")).upper()
        base = str(item.get("baseCoin", "")).upper()
        quote = str(item.get("quoteCoin", "")).upper()
        settle = str(item.get("settleCoin", "")).upper()
        future_type = item.get("futureType", 1)
        state = item.get("state", 0)
        if not sym or not base or quote != "USDT" or settle != "USDT":
            continue
        if sym != f"{base}_USDT":
            continue
        try:
            if int(future_type) != 1:  # perpetual only; exclude delivery contracts
                continue
            if int(state) != 0:  # MEXC state 0 is the normal trading state
                continue
        except (TypeError, ValueError):
            continue
        if is_unsuitable_base(base):
            continue
        eligible[sym] = item

    ranked = []
    for ticker in tickers:
        if not isinstance(ticker, dict):
            continue
        sym = str(ticker.get("symbol", "")).upper()
        if sym not in eligible:
            continue
        last = _to_float(ticker.get("lastPrice"))
        turnover = _to_float(ticker.get("amount24"))
        if not last or last <= 0 or not turnover or turnover <= 0:
            continue
        ranked.append((turnover, sym))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    return [sym for _, sym in ranked[:max(0, top_n)]]


class MexcClient:
    def __init__(
        self,
        base_url: Optional[str] = None,
        timeout: float = 15.0,
        session: Optional[requests.Session] = None,
        max_retries: int = 4,
        backoff_seconds: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.base_url = (base_url or BASE_URL).rstrip("/")
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update({
            "User-Agent": "po3-signal-scanner/1.0",
            "Accept": "application/json",
        })
        self.max_retries = max_retries
        self.backoff = backoff_seconds
        self._sleep = sleep

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        url = self.base_url + path
        last_err = "unknown error"
        for attempt in range(self.max_retries + 1):
            delay = self.backoff * (2 ** attempt)
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                last_err = type(exc).__name__
                if attempt < self.max_retries:
                    self._sleep(delay)
                continue
            if resp.status_code == 200:
                try:
                    return resp.json()
                except ValueError:
                    raise MexcApiError(f"{path}: response was not valid JSON")
            if resp.status_code in (418, 429) or resp.status_code >= 500:
                last_err = f"HTTP {resp.status_code}"
                retry_after = resp.headers.get("Retry-After")
                try:
                    delay = max(delay, float(retry_after)) if retry_after else delay
                except ValueError:
                    pass
                if attempt < self.max_retries:
                    self._sleep(min(delay, 30.0))
                continue
            raise MexcApiError(f"{path}: HTTP {resp.status_code} {resp.text[:200]}")
        raise MexcApiError(
            f"{path}: gave up after {self.max_retries + 1} attempts ({last_err})"
        )

    def get_server_time_ms(self) -> int:
        payload = self._get("/api/v1/contract/ping")
        return int(_unwrap_data(payload, "/api/v1/contract/ping"))

    def get_exchange_info(self) -> List[Dict[str, Any]]:
        payload = self._get("/api/v1/contract/detail")
        data = _unwrap_data(payload, "/api/v1/contract/detail")
        if not isinstance(data, list):
            raise MexcApiError("/api/v1/contract/detail: data is not a list")
        return data

    def get_tickers_24h(self) -> List[Dict[str, Any]]:
        payload = self._get("/api/v1/contract/ticker")
        data = _unwrap_data(payload, "/api/v1/contract/ticker")
        if isinstance(data, dict):
            return [data]
        if not isinstance(data, list):
            raise MexcApiError("/api/v1/contract/ticker: data is not a list/object")
        return data

    def get_top_symbols(self, top_n: int = 50) -> List[str]:
        symbols = select_top_symbols(
            self.get_exchange_info(), self.get_tickers_24h(), top_n
        )
        if not symbols:
            raise MexcApiError("No eligible USDT-M perpetual Futures symbols found")
        return symbols

    def get_klines(
        self, symbol: str, timeframe: str, limit: int = 500
    ) -> List[Candle]:
        """Fetch the most recent Futures candles; caller must drop the forming candle."""
        interval = INTERVALS.get(timeframe)
        if not interval:
            raise ValueError(f"Unsupported timeframe {timeframe!r}")
        tf_seconds = TIMEFRAME_MS[timeframe] // 1000
        end = int(time.time())
        start = end - max(1, min(int(limit), 2000)) * tf_seconds
        path = f"/api/v1/contract/kline/{symbol}"
        payload = self._get(path, {"interval": interval, "start": start, "end": end})
        data = _unwrap_data(payload, path)
        return parse_futures_klines(data)
