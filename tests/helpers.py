from models import Candle

TF15 = 15 * 60_000
T0 = 1_760_000_000_000 - (1_760_000_000_000 % TF15)  # aligned to a 15m boundary


def candle(i, o, h, l, c, t0=T0, tf=TF15):
    return Candle(t0 + i * tf, float(o), float(h), float(l), float(c), 1.0)


def flat(start, count, mid=100.0, half=0.5):
    """Tight range bars: TR == 2*half, close == mid."""
    return [candle(start + k, mid, mid + half, mid - half, mid) for k in range(count)]


def base_series():
    """60 flat bars (indices 0..59): ATR(50)=1 from bar 49, accumulation range 1 <= 5*ATR."""
    return flat(0, 60)


def buy_series(extra=()):
    """Bar 60 sweeps the lows (low 96) and closes 99.5 -> Bearish manipulation -> BUY."""
    return base_series() + [candle(60, 100, 100, 96, 99.5)] + list(extra)


def sell_series(extra=()):
    """Bar 60 sweeps the highs (high 104) and closes 100.5 -> Bullish manipulation -> SELL."""
    return base_series() + [candle(60, 100, 104, 100, 100.5)] + list(extra)
