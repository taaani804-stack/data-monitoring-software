"""Plain data containers shared by the strategy, data and alert modules."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Candle:
    """One OHLCV candle. ``open_time_ms`` is the candle OPEN time (UTC, ms)."""

    open_time_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass(frozen=True)
class Signal:
    """An entry produced by the PO3 state machine (Pine: PO3 object with entryType set)."""

    direction: str  # "BUY" (Pine entryType "Long") or "SELL" (Pine "Short")
    entry_index: int  # index of the signal candle in the candle list
    candle_open_ms: int  # open time of the signal candle (Pine: entryTime)
    entry_price: float  # close of the signal candle (Pine: entryPrice := close)
    stop_loss: float
    take_profit: float
    atr: float  # ATR(50) on the signal candle
    # Filled in only if a later candle in the data already hit TP/SL.
    exit_index: Optional[int] = None
    exit_reason: Optional[str] = None  # "TP" or "SL"
    exit_price: Optional[float] = None

    @property
    def reward_risk(self) -> float:
        """Actual reward / risk implied by the TP and SL levels."""
        risk = abs(self.entry_price - self.stop_loss)
        reward = abs(self.take_profit - self.entry_price)
        return reward / risk if risk else float("nan")


class ConfigError(Exception):
    """Safe-to-print configuration problem (never contains secret values)."""
