"""Pure-Python technical indicators and trend-regime logic for the flip tracker.

No third-party dependencies. Everything works on plain lists of floats so the
same code runs inside GitHub Actions, a laptop, or a unit test with synthetic
prices.

Trend regime definition (applied to a series of candle closes):

    bullish  -> close above the slow EMA (21) AND fast EMA (9) above slow EMA
    bearish  -> close below the slow EMA AND fast EMA below slow EMA
    anything in between keeps the previous regime (hysteresis, avoids whipsaw)

A "flip bullish" is the first candle where the regime moves bearish -> bullish.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Optional

FAST = 9
SLOW = 21
MACD_FAST = 12
MACD_SLOW = 26
MACD_SIGNAL = 9
NEAR_EMA_PCT = 5.0  # "about to flip" proximity threshold (percent below slow EMA)


def ema(values: list[float], period: int) -> list[Optional[float]]:
    """Exponential moving average seeded with a simple average of the first `period` values."""
    out: list[Optional[float]] = []
    k = 2.0 / (period + 1)
    prev: Optional[float] = None
    for i, v in enumerate(values):
        if i < period - 1:
            out.append(None)
            continue
        if prev is None:
            prev = sum(values[:period]) / period
        else:
            prev = (v - prev) * k + prev
        out.append(prev)
    return out


def macd(values: list[float]) -> tuple[list[Optional[float]], list[Optional[float]], list[Optional[float]]]:
    """Return (macd_line, signal_line, histogram) using 12/26/9 defaults."""
    fast = ema(values, MACD_FAST)
    slow = ema(values, MACD_SLOW)
    line: list[Optional[float]] = [
        (f - s) if (f is not None and s is not None) else None for f, s in zip(fast, slow)
    ]
    defined = [x for x in line if x is not None]
    sig_defined = ema(defined, MACD_SIGNAL) if defined else []
    signal: list[Optional[float]] = []
    j = 0
    for x in line:
        if x is None:
            signal.append(None)
        else:
            signal.append(sig_defined[j])
            j += 1
    hist = [(l - s) if (l is not None and s is not None) else None for l, s in zip(line, signal)]
    return line, signal, hist


def regimes(closes: list[float], fast: int = FAST, slow: int = SLOW) -> list[Optional[str]]:
    """Classify each candle as 'bullish' / 'bearish' with hysteresis; None while EMAs warm up."""
    ef = ema(closes, fast)
    es = ema(closes, slow)
    out: list[Optional[str]] = []
    prev: Optional[str] = None
    for c, f, s in zip(closes, ef, es):
        if f is None or s is None:
            out.append(None)
            continue
        if c > s and f > s:
            r = "bullish"
        elif c < s and f < s:
            r = "bearish"
        elif prev is not None:
            r = prev
        else:
            r = "bullish" if c > s else "bearish"
        out.append(r)
        prev = r
    return out


@dataclass
class Candle:
    day: date            # the calendar date the candle belongs to (for weekly: the Monday)
    close: float
    volume: float = 0.0
    complete: bool = True


@dataclass
class TrendRead:
    """Everything the reports need to know about one coin on one timeframe."""

    timeframe: str
    regime: Optional[str] = None            # regime on the last completed candle
    prev_regime: Optional[str] = None
    bars_in_regime: int = 0                 # how many completed candles the regime has held
    flipped_bullish: bool = False           # regime flipped bearish->bullish on the last completed candle
    flipped_bearish: bool = False
    flip_date: Optional[str] = None         # date of the most recent bearish->bullish flip, if any
    flip_close: Optional[float] = None      # close on that flip candle
    close: Optional[float] = None
    ema_fast: Optional[float] = None
    ema_slow: Optional[float] = None
    pct_vs_slow: Optional[float] = None     # % distance of close from the slow EMA (+ above / - below)
    macd_hist: Optional[float] = None
    approach_score: int = 0                 # 0-4: how close a bearish coin is to flipping
    approach_reasons: list[str] = field(default_factory=list)
    live_regime: Optional[str] = None       # regime including the in-progress candle
    live_close: Optional[float] = None
    volume_ratio: Optional[float] = None    # last candle volume / average of prior 4
    candles: int = 0


def analyse(candles: list[Candle], timeframe: str) -> TrendRead:
    read = TrendRead(timeframe=timeframe)
    done = [c for c in candles if c.complete]
    read.candles = len(done)
    if len(done) < SLOW + 2:
        return read

    closes = [c.close for c in done]
    reg = regimes(closes)
    ef = ema(closes, FAST)
    es = ema(closes, SLOW)
    _, _, hist = macd(closes)

    read.regime = reg[-1]
    read.prev_regime = reg[-2]
    read.close = closes[-1]
    read.ema_fast = ef[-1]
    read.ema_slow = es[-1]
    read.macd_hist = hist[-1]
    if read.ema_slow:
        read.pct_vs_slow = (closes[-1] / read.ema_slow - 1.0) * 100.0

    # bars the current regime has held, and the most recent bullish flip date
    n = 0
    for r in reversed(reg):
        if r == read.regime:
            n += 1
        else:
            break
    read.bars_in_regime = n
    read.flipped_bullish = read.regime == "bullish" and read.prev_regime == "bearish"
    read.flipped_bearish = read.regime == "bearish" and read.prev_regime == "bullish"
    for i in range(len(reg) - 1, 0, -1):
        if reg[i] == "bullish" and reg[i - 1] == "bearish":
            read.flip_date = done[i].day.isoformat()
            read.flip_close = done[i].close
            break

    # volume confirmation
    vols = [c.volume for c in done]
    if len(vols) >= 5 and vols[-1] and sum(vols[-5:-1]) > 0:
        read.volume_ratio = vols[-1] / (sum(vols[-5:-1]) / 4.0)

    # live read including the in-progress candle
    live = candles[-1]
    if not live.complete:
        live_closes = closes + [live.close]
        read.live_close = live.close
        read.live_regime = regimes(live_closes)[-1]
    else:
        read.live_close = closes[-1]
        read.live_regime = read.regime

    # "about to flip" scoring for coins still bearish
    if read.regime == "bearish":
        score = 0
        reasons: list[str] = []
        gap = [(f - s) if (f is not None and s is not None) else None for f, s in zip(ef, es)]
        if read.pct_vs_slow is not None and read.pct_vs_slow >= -NEAR_EMA_PCT:
            score += 1
            if read.pct_vs_slow >= 0:
                reasons.append("price already above the 21 EMA")
            else:
                reasons.append(f"price within {abs(read.pct_vs_slow):.1f}% of the 21 EMA")
        if (
            len(gap) >= 3
            and None not in gap[-3:]
            and abs(gap[-1]) < abs(gap[-2]) < abs(gap[-3])
        ):
            score += 1
            reasons.append("9/21 EMA gap narrowing for 2 candles")
        if len(hist) >= 3 and None not in hist[-3:] and hist[-1] > hist[-2] > hist[-3]:
            score += 1
            reasons.append("MACD histogram rising for 2 candles")
        if ef[-1] is not None and ef[-2] is not None and ef[-1] > ef[-2]:
            score += 1
            reasons.append("9 EMA turning up")
        if read.live_regime == "bullish":
            reasons.append("live candle is printing bullish (not yet closed)")
        read.approach_score = score
        read.approach_reasons = reasons
    return read


# ----------------------------------------------------------------------------
# Candle construction from CoinGecko market_chart data
# ----------------------------------------------------------------------------

def _utc(ts_ms: float) -> datetime:
    return datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)


def daily_candles_from_chart(prices: list[list[float]], volumes: list[list[float]], today: date) -> list[Candle]:
    """Turn CoinGecko `market_chart` points into completed daily candles plus one live candle.

    CoinGecko stamps a daily point at (or within minutes of) 00:00 UTC, which is
    effectively the close of the previous day. The final point carries the current
    time and price; it becomes the in-progress candle for `today`.
    """
    def is_midnight(dt: datetime) -> bool:
        return dt.hour == 0 and dt.minute < 30

    vol_by_day: dict[date, float] = {}
    for ts, v in volumes:
        dt = _utc(ts)
        d = dt.date() - timedelta(days=1) if is_midnight(dt) else dt.date()
        vol_by_day[d] = float(v)

    by_day: dict[date, float] = {}
    live: Optional[tuple[date, float]] = None
    for ts, p in prices:
        dt = _utc(ts)
        if is_midnight(dt):
            by_day[dt.date() - timedelta(days=1)] = float(p)
        else:
            live = (dt.date(), float(p))

    candles = [
        Candle(day=d, close=p, volume=vol_by_day.get(d, 0.0), complete=True)
        for d, p in sorted(by_day.items())
        if d < today
    ]
    if live is not None:
        candles.append(Candle(day=today, close=live[1], volume=vol_by_day.get(live[0], 0.0), complete=False))
    elif by_day and max(by_day) >= today:
        candles.append(Candle(day=today, close=by_day[max(by_day)], volume=0.0, complete=False))
    return candles


def weekly_candles(daily: list[Candle], today: date) -> list[Candle]:
    """Aggregate daily candles into Monday-start weekly candles.

    A week is complete once its Sunday is strictly before `today`.
    """
    groups: dict[date, list[Candle]] = {}
    for c in daily:
        monday = c.day - timedelta(days=c.day.weekday())
        groups.setdefault(monday, []).append(c)
    out: list[Candle] = []
    for monday in sorted(groups):
        cs = groups[monday]
        sunday = monday + timedelta(days=6)
        out.append(
            Candle(
                day=monday,
                close=cs[-1].close,
                volume=sum(c.volume for c in cs),
                complete=sunday < today,
            )
        )
    return out
