"""Offline tests: synthetic price paths through the whole pipeline.

Run:  python -m unittest test_tracker -v   (from the crypto-flip-tracker folder)
"""

from __future__ import annotations

import json
import math
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import flip_tracker
import picker
from indicators import Candle, analyse, daily_candles_from_chart, ema, regimes, weekly_candles

TODAY = date(2026, 9, 21)  # a Monday


def series(days: int, shape) -> list[float]:
    return [shape(i) for i in range(days)]


def chart_from_closes(closes: list[float], today: date = TODAY) -> dict:
    """Build a CoinGecko-style market_chart: midnight stamps for each prior day plus a live point."""
    start = today - timedelta(days=len(closes) - 1)
    prices, vols = [], []
    for i, p in enumerate(closes[:-1]):
        d = start + timedelta(days=i + 1)  # midnight stamp of the next day = close of day i
        ts = int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)
        prices.append([ts, p])
        vols.append([ts, 1_000_000 + i * 1000])
    live_ts = int(datetime(today.year, today.month, today.day, 9, tzinfo=timezone.utc).timestamp() * 1000)
    prices.append([live_ts, closes[-1]])
    vols.append([live_ts, 900_000])
    return {"prices": prices, "total_volumes": vols}


def market_row(cid: str, sym: str, rank: int, price: float) -> dict:
    return {
        "id": cid, "symbol": sym, "name": sym.upper() + " coin", "market_cap_rank": rank, "current_price": price,
        "market_cap": price * 1e6, "total_volume": 1e6, "price_change_percentage_7d_in_currency": 3.0,
        "price_change_percentage_30d_in_currency": 12.0, "image": None,
    }


class IndicatorTests(unittest.TestCase):
    def test_ema_matches_hand_calc(self):
        vals = [1, 2, 3, 4, 5, 6]
        out = ema(vals, 3)
        self.assertEqual(out[:2], [None, None])
        self.assertAlmostEqual(out[2], 2.0)
        self.assertAlmostEqual(out[3], 3.0)  # (4-2)*0.5+2

    def test_regime_flips_on_uptrend_after_downtrend(self):
        closes = series(120, lambda i: 100 - i * 0.5 if i < 60 else 70 + (i - 60) * 1.2)
        reg = regimes(closes)
        self.assertEqual(reg[59], "bearish")
        self.assertEqual(reg[-1], "bullish")
        flips = sum(1 for a, b in zip(reg, reg[1:]) if a == "bearish" and b == "bullish")
        self.assertEqual(flips, 1)

    def test_hysteresis_ignores_single_wick(self):
        # steady uptrend with one sharp one-candle dip under the 21 EMA: regime must hold
        closes = [100.0 + i for i in range(60)]
        closes[50] = closes[49] * 0.85
        reg = regimes(closes)
        self.assertEqual(reg[49], "bullish")
        self.assertEqual(reg[50], "bullish")
        self.assertEqual(reg[51], "bullish")

    def test_daily_candles_shift_midnight_stamp_to_previous_day(self):
        closes = [float(i) for i in range(1, 11)]
        chart = chart_from_closes(closes)
        candles = daily_candles_from_chart(chart["prices"], chart["total_volumes"], TODAY)
        self.assertEqual(len(candles), 10)
        self.assertTrue(all(c.complete for c in candles[:-1]))
        self.assertFalse(candles[-1].complete)
        self.assertEqual(candles[-1].day, TODAY)
        self.assertEqual(candles[-2].day, TODAY - timedelta(days=1))
        self.assertEqual(candles[-2].close, 9.0)

    def test_weekly_aggregation_marks_current_week_incomplete(self):
        closes = [float(i) for i in range(1, 30)]
        chart = chart_from_closes(closes)
        daily = daily_candles_from_chart(chart["prices"], chart["total_volumes"], TODAY)
        weekly = weekly_candles(daily, TODAY)
        self.assertTrue(all(w.day.weekday() == 0 for w in weekly))
        self.assertFalse(weekly[-1].complete)
        self.assertTrue(weekly[-2].complete)
        self.assertEqual(weekly[-2].day, TODAY - timedelta(days=7))

    def test_analyse_flags_fresh_flip(self):
        closes = series(60, lambda i: 100 - i if i < 40 else 60 + (i - 40) * 3.5)
        candles = [Candle(day=TODAY - timedelta(days=60 - i), close=c, volume=1.0) for i, c in enumerate(closes)]
        r = analyse(candles, "daily")
        self.assertEqual(r.regime, "bullish")
        self.assertIsNotNone(r.flip_date)
        self.assertGreaterEqual(r.bars_in_regime, 1)

    def test_about_to_flip_scores_recovering_coin(self):
        # long downtrend then a base curling up but still under the 21 EMA
        closes = series(80, lambda i: 100 - i * 0.8 if i < 70 else 44 + (i - 70) * 0.3)
        candles = [Candle(day=TODAY - timedelta(days=80 - i), close=c, volume=1.0) for i, c in enumerate(closes)]
        r = analyse(candles, "daily")
        self.assertEqual(r.regime, "bearish")
        self.assertGreaterEqual(r.approach_score, 2, r.approach_reasons)


class WatchlistTests(unittest.TestCase):
    def test_load_and_add(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "watchlist.txt"
            p.write_text("# comment\ninjective-protocol\n\nBitcoin  # inline\n")
            self.assertEqual(flip_tracker.load_watchlist(p), ["injective-protocol", "bitcoin"])
            self.assertTrue(flip_tracker.add_to_watchlist("Solana", p))
            self.assertFalse(flip_tracker.add_to_watchlist("solana", p))
            self.assertEqual(flip_tracker.load_watchlist(p), ["injective-protocol", "bitcoin", "solana"])

    def test_repo_watchlist_contains_injective(self):
        self.assertIn("injective-protocol", flip_tracker.load_watchlist())


class PipelineTests(unittest.TestCase):
    def build_fixture(self) -> dict:
        n = 400
        flipper = series(n, lambda i: 50 - i * 0.08 if i < 370 else 20.4 + (i - 370) * 0.9)         # weekly flip just happened
        early = series(n, lambda i: 50 - i * 0.08 if i < 340 else 22.8 + (i - 340) * 0.6)           # flipped a few weeks ago
        stuck = series(n, lambda i: 200 - i * 0.3)                                                    # still bearish
        curling = series(n, lambda i: 200 - i * 0.3 if i < 380 else 86 + (i - 380) * 0.45)         # about to flip
        leader = series(n, lambda i: 1000 + i * 2 + 30 * math.sin(i / 9))                            # long bull
        watched = series(n, lambda i: 30 - i * 0.05 if i < 385 else 10.75 + (i - 385) * 0.2)        # watchlist coin, rank 140
        return {
            "markets": [
                market_row("bitcoin", "btc", 1, leader[-1]),
                market_row("tether", "usdt", 2, 1.0),
                market_row("flipper", "flp", 8, flipper[-1]),
                market_row("early", "erl", 15, early[-1]),
                market_row("stuck", "stk", 30, stuck[-1]),
                market_row("curling", "crl", 42, curling[-1]),
                market_row("injective-protocol", "inj", 140, watched[-1]),
            ],
            "charts": {
                "bitcoin": chart_from_closes(leader),
                "tether": chart_from_closes([1.0] * n),
                "flipper": chart_from_closes(flipper),
                "early": chart_from_closes(early),
                "stuck": chart_from_closes(stuck),
                "curling": chart_from_closes(curling),
                "injective-protocol": chart_from_closes(watched),
            },
        }

    def test_end_to_end_weekly_and_morning(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            fx = root / "fixture.json"
            fx.write_text(json.dumps(self.build_fixture()))
            src = flip_tracker.FixtureSource(fx)
            # top 5 excludes the rank-140 watchlist coin; the watchlist must pull it in anyway
            res = flip_tracker.run("auto", 5, src, TODAY, root / "data", root / "reports", watchlist=["injective-protocol"])

            ids = [c["id"] for c in res["coins"]]
            self.assertNotIn("tether", ids, "stablecoins must be excluded")
            self.assertIn("injective-protocol", ids, "watchlist coins must be screened even outside the top N")
            self.assertTrue(res["weekly"], "Monday runs must produce the weekly list")
            self.assertTrue((root / "reports" / "latest-weekly.md").exists())
            self.assertTrue((root / "reports" / "latest.md").exists())
            self.assertTrue((root / "data" / "tracker.json").exists())

            by_id = {c["id"]: c for c in res["coins"]}
            self.assertTrue(by_id["injective-protocol"]["watchlist"])
            self.assertFalse(by_id["bitcoin"]["watchlist"])
            self.assertEqual([c["id"] for c in res["lists"]["watchlist"]], ["injective-protocol"])
            self.assertEqual(by_id["bitcoin"]["weekly"]["regime"], "bullish")
            self.assertEqual(by_id["stuck"]["weekly"]["regime"], "bearish")
            self.assertEqual(by_id["flipper"]["weekly"]["regime"], "bullish")
            self.assertIn("flipper", [c["id"] for c in res["lists"]["weekly_flipped"] + res["lists"]["weekly_recent"]])
            self.assertEqual(by_id["curling"]["weekly"]["regime"], "bearish")
            self.assertGreaterEqual(by_id["curling"]["weekly"]["approach_score"], 2, by_id["curling"]["weekly"]["approach_reasons"])
            self.assertIn("curling", [c["id"] for c in res["lists"]["weekly_about_to_flip"]])

            state = json.loads((root / "data" / "state.json").read_text())
            self.assertTrue(any(f["coin"] == "flipper" and f["direction"] == "bullish" for f in state["flips"]))

            weekly_md = (root / "reports" / "latest-weekly.md").read_text()
            self.assertIn("FLP", weekly_md)
            self.assertIn("CRL", weekly_md)
            self.assertIn("Watchlist", weekly_md)
            self.assertIn("INJ", weekly_md)

            # second run the next day: no duplicate flip entries, morning report only
            res2 = flip_tracker.run("auto", 5, src, TODAY + timedelta(days=1), root / "data", root / "reports", watchlist=["injective-protocol"])
            self.assertFalse(res2["weekly"])
            state2 = json.loads((root / "data" / "state.json").read_text())
            keys = [(f["coin"], f["timeframe"], f["date"]) for f in state2["flips"]]
            self.assertEqual(len(keys), len(set(keys)))

            # picker returns something sensible, and watchlist coins bypass the size filter
            snap = json.loads((root / "data" / "tracker.json").read_text())
            picks = picker.pick(snap, {"horizon": "weeks", "risk": "any", "style": "either", "both": "no", "picks": "2"})
            self.assertTrue(picks)
            self.assertIn(picks[0]["symbol"], {"FLP", "ERL", "BTC", "INJ"})
            large = picker.pick(snap, {"horizon": "days", "risk": "large", "style": "either", "both": "no", "picks": "2"})
            large_syms = {p["symbol"] for p in large}
            self.assertFalse({"CRL", "STK"} & large_syms)


if __name__ == "__main__":
    unittest.main()
