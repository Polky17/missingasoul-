#!/usr/bin/env python3
"""Crypto bullish-flip tracker.

Pulls the top coins by market cap from CoinGecko (free API, no key required),
plus everything in watchlist.txt, builds daily and weekly candles, works out
which coins have just flipped from a bearish to a bullish trend, which ones look
about to flip, and writes:

  data/state.json        per-coin regime memory + running log of every flip
  data/tracker.json      full snapshot the dashboard reads
  reports/YYYY-MM-DD.md  the morning report (daily flips + weekly context)
  reports/latest.md      copy of the most recent morning report
  reports/weekly/*.md    the Monday weekly list (weekly flips + about-to-flip)
  reports/latest-weekly.md

Usage:
  python flip_tracker.py                       # auto: morning report, plus weekly on Mondays
  python flip_tracker.py --mode weekly         # force the weekly list
  python flip_tracker.py --top 50              # fewer coins (faster, fewer API calls)
  python flip_tracker.py --fixture path.json   # offline run against saved/synthetic data
  python flip_tracker.py --add-watch injective-protocol   # add a coin to watchlist.txt and exit

Set COINGECKO_API_KEY (a free "demo" key) to get a friendlier rate limit.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Optional

from indicators import TrendRead, analyse, daily_candles_from_chart, weekly_candles

HERE = Path(__file__).resolve().parent
DATA_DIR = HERE / "data"
REPORT_DIR = HERE / "reports"
WATCHLIST_FILE = HERE / "watchlist.txt"
API = "https://api.coingecko.com/api/v3"

# Things that are not "coins flipping bullish" in any useful sense.
EXCLUDED_IDS = {
    "tether", "usd-coin", "dai", "first-digital-usd", "ethena-usde", "usds", "true-usd",
    "paypal-usd", "frax", "usdd", "binance-peg-busd", "binance-usd", "pax-dollar", "gemini-dollar",
    "wrapped-bitcoin", "wrapped-steth", "staked-ether", "weth", "wrapped-eeth", "coinbase-wrapped-btc",
    "rocket-pool-eth", "ether-fi-staked-eth", "bridged-usdc-polygon-pos-bridge", "binance-bridged-usdt-bnb-smart-chain",
    "renzo-restaked-eth", "kelp-dao-restaked-eth", "solv-btc", "lombard-staked-btc", "wrapped-bnb",
    "usdt0", "susds", "blackrock-usd-institutional-digital-liquidity-fund", "usd1-wlfi", "tether-gold", "pax-gold",
}
EXCLUDED_WORDS = ("usd", "wrapped", "staked", "bridged", "restaked", "stable", "tokenized", "gold")


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ----------------------------------------------------------------------------
# Watchlist
# ----------------------------------------------------------------------------

def load_watchlist(path: Path = WATCHLIST_FILE) -> list[str]:
    if not path.exists():
        return []
    ids: list[str] = []
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip().lower()
        if line and line not in ids:
            ids.append(line)
    return ids


def add_to_watchlist(coin_id: str, path: Path = WATCHLIST_FILE) -> bool:
    coin_id = coin_id.strip().lower()
    if coin_id in load_watchlist(path):
        return False
    text = path.read_text() if path.exists() else ""
    if text and not text.endswith("\n"):
        text += "\n"
    path.write_text(text + coin_id + "\n")
    return True


# ----------------------------------------------------------------------------
# Data access
# ----------------------------------------------------------------------------

class CoinGecko:
    def __init__(self, api_key: Optional[str] = None, pause: float = 2.5):
        self.api_key = api_key
        self.pause = pause if not api_key else max(1.0, pause - 0.5)
        self._last = 0.0

    def get(self, path: str, params: dict[str, Any]) -> Any:
        url = f"{API}{path}?{urllib.parse.urlencode(params)}"
        headers = {"Accept": "application/json", "User-Agent": "crypto-flip-tracker/1.0"}
        if self.api_key:
            headers["x-cg-demo-api-key"] = self.api_key
        for attempt in range(6):
            wait = self.pause - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            req = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    self._last = time.monotonic()
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                self._last = time.monotonic()
                if e.code in (429, 500, 502, 503, 504):
                    backoff = 65 if e.code == 429 else 10 * (attempt + 1)
                    log(f"  HTTP {e.code} on {path}; waiting {backoff}s (attempt {attempt + 1}/6)")
                    time.sleep(backoff)
                    continue
                raise
            except (urllib.error.URLError, TimeoutError) as e:
                self._last = time.monotonic()
                log(f"  network error on {path}: {e}; retrying in 15s")
                time.sleep(15)
        raise RuntimeError(f"gave up on {path}")

    def top_markets(self, n: int) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        page = 1
        while len(rows) < n and page <= 4:
            batch = self.get(
                "/coins/markets",
                {
                    "vs_currency": "usd",
                    "order": "market_cap_desc",
                    "per_page": 250,
                    "page": page,
                    "price_change_percentage": "7d,30d",
                },
            )
            if not batch:
                break
            for row in batch:
                if is_excluded(row):
                    continue
                rows.append(row)
                if len(rows) >= n:
                    break
            page += 1
        return rows

    def markets_by_ids(self, ids: list[str]) -> list[dict[str, Any]]:
        if not ids:
            return []
        return self.get(
            "/coins/markets",
            {"vs_currency": "usd", "ids": ",".join(ids), "per_page": 250, "page": 1, "price_change_percentage": "7d,30d"},
        )

    def chart(self, coin_id: str, days: int = 365) -> dict[str, Any]:
        return self.get(f"/coins/{coin_id}/market_chart", {"vs_currency": "usd", "days": days})


class FixtureSource:
    """Offline stand-in for CoinGecko: a JSON file with {"markets": [...], "charts": {id: {...}}}."""

    def __init__(self, path: Path):
        self.data = json.loads(path.read_text())

    def top_markets(self, n: int) -> list[dict[str, Any]]:
        return [r for r in self.data["markets"] if not is_excluded(r)][:n]

    def markets_by_ids(self, ids: list[str]) -> list[dict[str, Any]]:
        return [r for r in self.data["markets"] if r["id"] in ids]

    def chart(self, coin_id: str, days: int = 365) -> dict[str, Any]:
        return self.data["charts"][coin_id]


def is_excluded(row: dict[str, Any]) -> bool:
    cid = (row.get("id") or "").lower()
    name = (row.get("name") or "").lower()
    if cid in EXCLUDED_IDS:
        return True
    return any(w in name for w in EXCLUDED_WORDS) or any(w in cid for w in EXCLUDED_WORDS)


# ----------------------------------------------------------------------------
# Analysis
# ----------------------------------------------------------------------------

def read_to_dict(r: TrendRead) -> dict[str, Any]:
    d = asdict(r)
    for k in ("close", "ema_fast", "ema_slow", "pct_vs_slow", "macd_hist", "live_close", "volume_ratio", "flip_close"):
        if d[k] is not None:
            d[k] = round(d[k], 6 if k in ("close", "ema_fast", "ema_slow", "live_close", "flip_close") else 3)
    return d


def analyse_coin(row: dict[str, Any], chart: dict[str, Any], today: date, watchlist: bool = False) -> dict[str, Any]:
    daily = daily_candles_from_chart(chart.get("prices", []), chart.get("total_volumes", []), today)
    weekly = weekly_candles(daily, today)
    d = analyse(daily, "daily")
    w = analyse(weekly, "weekly")
    return {
        "id": row["id"],
        "symbol": (row.get("symbol") or "").upper(),
        "name": row.get("name"),
        "rank": row.get("market_cap_rank"),
        "price": row.get("current_price"),
        "market_cap": row.get("market_cap"),
        "volume_24h": row.get("total_volume"),
        "change_7d": row.get("price_change_percentage_7d_in_currency"),
        "change_30d": row.get("price_change_percentage_30d_in_currency"),
        "image": row.get("image"),
        "watchlist": watchlist,
        "daily": read_to_dict(d),
        "weekly": read_to_dict(w),
    }


def load_state() -> dict[str, Any]:
    p = DATA_DIR / "state.json"
    if p.exists():
        return json.loads(p.read_text())
    return {"updated": None, "coins": {}, "flips": []}


def update_state(state: dict[str, Any], coins: list[dict[str, Any]], today: date) -> list[dict[str, Any]]:
    """Compare today's regimes with the remembered ones and log every change. Returns new flips."""
    new_flips: list[dict[str, Any]] = []
    seen = {(f["coin"], f["timeframe"], f["date"]) for f in state["flips"]}
    for c in coins:
        mem = state["coins"].get(c["id"], {})
        for tf in ("daily", "weekly"):
            r = c[tf]
            if r["regime"] is None:
                continue
            # Prefer the indicator's own flip detection; fall back to memory diff when the
            # tracker was not run on the exact flip candle.
            flipped_up = r["flipped_bullish"] or (mem.get(f"{tf}_regime") == "bearish" and r["regime"] == "bullish")
            flipped_down = r["flipped_bearish"] or (mem.get(f"{tf}_regime") == "bullish" and r["regime"] == "bearish")
            # Backfill: the indicator remembers the most recent bullish flip even when the
            # tracker was not running on that candle, so the log has history from day one.
            backfill = r["regime"] == "bullish" and r["flip_date"] is not None and not flipped_up
            if flipped_up or flipped_down or backfill:
                if flipped_up or backfill:
                    flip_day = r["flip_date"] or today.isoformat()
                    price = r["flip_close"] if r["flip_close"] is not None else c["price"]
                else:
                    flip_day = today.isoformat()
                    price = c["price"]
                key = (c["id"], tf, flip_day)
                if key not in seen:
                    entry = {
                        "date": flip_day,
                        "logged": today.isoformat(),
                        "coin": c["id"],
                        "symbol": c["symbol"],
                        "name": c["name"],
                        "rank": c["rank"],
                        "timeframe": tf,
                        "direction": "bearish" if flipped_down else "bullish",
                        "price": price,
                        "watchlist": c.get("watchlist", False),
                    }
                    state["flips"].append(entry)
                    seen.add(key)
                    if flipped_up:
                        new_flips.append(entry)
            mem[f"{tf}_regime"] = r["regime"]
            if flipped_up:
                mem[f"{tf}_flip_date"] = r["flip_date"] or today.isoformat()
        mem["symbol"] = c["symbol"]
        mem["name"] = c["name"]
        mem["last_seen"] = today.isoformat()
        state["coins"][c["id"]] = mem
    state["flips"].sort(key=lambda f: (f["date"], f["timeframe"], f["coin"]))
    state["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return new_flips


def build_lists(coins: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    def strength(c: dict[str, Any], tf: str) -> float:
        r = c[tf]
        s = 0.0
        s += min(max(r["pct_vs_slow"] or 0.0, 0.0), 15.0)          # up to 15 pts for distance above 21 EMA
        s += 5.0 if (r["macd_hist"] or 0) > 0 else 0.0
        s += 5.0 if (r["volume_ratio"] or 0) >= 1.2 else 0.0
        s += 5.0 if c["daily"]["regime"] == "bullish" and c["weekly"]["regime"] == "bullish" else 0.0
        s += max(0.0, 5.0 - (c["rank"] or 100) / 20.0)             # slight tilt to bigger names
        return round(s, 2)

    weekly_flipped = [c for c in coins if c["weekly"]["flipped_bullish"]]
    weekly_recent = [c for c in coins if c["weekly"]["regime"] == "bullish" and 1 < c["weekly"]["bars_in_regime"] <= 4]
    weekly_about = [c for c in coins if c["weekly"]["regime"] == "bearish" and c["weekly"]["approach_score"] >= 2]
    daily_flipped = [c for c in coins if c["daily"]["flipped_bullish"]]
    daily_recent = [c for c in coins if c["daily"]["regime"] == "bullish" and 1 < c["daily"]["bars_in_regime"] <= 5]
    daily_about = [c for c in coins if c["daily"]["regime"] == "bearish" and c["daily"]["approach_score"] >= 3]
    weekly_flipped_bearish = [c for c in coins if c["weekly"]["flipped_bearish"]]
    daily_flipped_bearish = [c for c in coins if c["daily"]["flipped_bearish"]]
    watchlist = [c for c in coins if c.get("watchlist")]

    for c in coins:
        c["strength_weekly"] = strength(c, "weekly")
        c["strength_daily"] = strength(c, "daily")

    def by_strength(tf: str):
        return lambda c: -c[f"strength_{tf}"]

    def by_approach(tf: str):
        return lambda c: (-c[tf]["approach_score"], -(c[tf]["pct_vs_slow"] or -99))

    return {
        "weekly_flipped": sorted(weekly_flipped, key=by_strength("weekly")),
        "weekly_recent": sorted(weekly_recent, key=by_strength("weekly")),
        "weekly_about_to_flip": sorted(weekly_about, key=by_approach("weekly")),
        "daily_flipped": sorted(daily_flipped, key=by_strength("daily")),
        "daily_recent": sorted(daily_recent, key=by_strength("daily")),
        "daily_about_to_flip": sorted(daily_about, key=by_approach("daily")),
        "weekly_flipped_bearish": sorted(weekly_flipped_bearish, key=lambda c: c["rank"] or 999),
        "daily_flipped_bearish": sorted(daily_flipped_bearish, key=lambda c: c["rank"] or 999),
        "watchlist": sorted(watchlist, key=lambda c: c["rank"] or 9999),
    }


# ----------------------------------------------------------------------------
# Reports
# ----------------------------------------------------------------------------

def fmt_price(p: Optional[float]) -> str:
    if p is None:
        return "-"
    if p >= 1000:
        return f"${p:,.0f}"
    if p >= 1:
        return f"${p:,.2f}"
    return f"${p:.4f}"


def fmt_pct(x: Optional[float]) -> str:
    return "-" if x is None else f"{x:+.1f}%"


def coin_label(c: dict[str, Any]) -> str:
    star = "⭐ " if c.get("watchlist") else ""
    return f"{star}**{c['symbol']}** {c['name']}"


def table(rows: list[dict[str, Any]], tf: str, kind: str) -> str:
    if not rows:
        return "_None right now._\n"
    if kind == "flipped":
        head = "| # | Coin | Price | vs 21 EMA | MACD hist | Vol x avg | Daily trend | 7d | 30d | Strength |\n|---|---|---|---|---|---|---|---|---|---|\n"
        body = ""
        for c in rows:
            r = c[tf]
            other = c["daily"]["regime"] if tf == "weekly" else c["weekly"]["regime"]
            body += (
                f"| {c['rank']} | {coin_label(c)} | {fmt_price(c['price'])} | {fmt_pct(r['pct_vs_slow'])} | "
                f"{'+' if (r['macd_hist'] or 0) > 0 else '-'} | {(r['volume_ratio'] or 0):.2f} | {other or '-'} | "
                f"{fmt_pct(c['change_7d'])} | {fmt_pct(c['change_30d'])} | {c[f'strength_{tf}']:.0f} |\n"
            )
        return head + body
    if kind == "recent":
        head = f"| # | Coin | Price | Flipped on | {'Weeks' if tf == 'weekly' else 'Days'} bullish | vs 21 EMA | 7d | 30d | Strength |\n|---|---|---|---|---|---|---|---|---|\n"
        body = ""
        for c in rows:
            r = c[tf]
            body += (
                f"| {c['rank']} | {coin_label(c)} | {fmt_price(c['price'])} | {r['flip_date'] or '-'} | {r['bars_in_regime']} | "
                f"{fmt_pct(r['pct_vs_slow'])} | {fmt_pct(c['change_7d'])} | {fmt_pct(c['change_30d'])} | {c[f'strength_{tf}']:.0f} |\n"
            )
        return head + body
    # about to flip
    head = "| # | Coin | Price | vs 21 EMA | Score /4 | Why |\n|---|---|---|---|---|---|\n"
    body = ""
    for c in rows:
        r = c[tf]
        body += (
            f"| {c['rank']} | {coin_label(c)} | {fmt_price(c['price'])} | {fmt_pct(r['pct_vs_slow'])} | "
            f"{r['approach_score']} | {'; '.join(r['approach_reasons']) or '-'} |\n"
        )
    return head + body


def status_line(r: dict[str, Any], unit: str) -> str:
    if r["regime"] is None:
        return "not enough history"
    if r["flipped_bullish"]:
        return f"🟢 flipped bullish this {unit}"
    if r["flipped_bearish"]:
        return f"🔴 flipped bearish this {unit}"
    if r["regime"] == "bullish":
        return f"bullish for {r['bars_in_regime']} {unit}s"
    if r["approach_score"] >= 3:
        return f"⏳ about to flip ({r['approach_score']}/4)"
    if r["approach_score"] == 2:
        return f"warming up ({r['approach_score']}/4)"
    return f"bearish for {r['bars_in_regime']} {unit}s"


def watchlist_section(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "_Watchlist is empty. Add CoinGecko ids to watchlist.txt._\n"
    head = "| # | Coin | Price | Weekly | vs W 21 EMA | Daily | vs D 21 EMA | 7d | 30d |\n|---|---|---|---|---|---|---|---|---|\n"
    body = ""
    for c in rows:
        body += (
            f"| {c['rank'] or '-'} | **{c['symbol']}** {c['name']} | {fmt_price(c['price'])} | {status_line(c['weekly'], 'week')} | "
            f"{fmt_pct(c['weekly']['pct_vs_slow'])} | {status_line(c['daily'], 'day')} | {fmt_pct(c['daily']['pct_vs_slow'])} | "
            f"{fmt_pct(c['change_7d'])} | {fmt_pct(c['change_30d'])} |\n"
        )
    notes = ""
    for c in rows:
        why = c["weekly"]["approach_reasons"] if c["weekly"]["regime"] == "bearish" else []
        if why:
            notes += f"- **{c['symbol']}** weekly: {'; '.join(why)}\n"
    return head + body + ("\n" + notes if notes else "")


def market_breadth(coins: list[dict[str, Any]]) -> str:
    n = len(coins)
    wb = sum(1 for c in coins if c["weekly"]["regime"] == "bullish")
    db = sum(1 for c in coins if c["daily"]["regime"] == "bullish")
    btc = next((c for c in coins if c["id"] == "bitcoin"), None)
    eth = next((c for c in coins if c["id"] == "ethereum"), None)
    lines = [
        f"- **Weekly breadth:** {wb}/{n} coins in a weekly bullish trend ({wb / n * 100:.0f}%)" if n else "",
        f"- **Daily breadth:** {db}/{n} coins in a daily bullish trend ({db / n * 100:.0f}%)" if n else "",
    ]
    for c in (btc, eth):
        if c:
            lines.append(
                f"- **{c['symbol']}:** weekly {c['weekly']['regime'] or '-'} ({c['weekly']['bars_in_regime']} wks), "
                f"daily {c['daily']['regime'] or '-'} ({c['daily']['bars_in_regime']} days), {fmt_pct(c['weekly']['pct_vs_slow'])} vs weekly 21 EMA"
            )
    return "\n".join(l for l in lines if l) + "\n"


def picks_section(lists: dict[str, list[dict[str, Any]]], tf: str) -> str:
    cands = lists[f"{tf}_flipped"] + lists[f"{tf}_recent"]
    cands = sorted(cands, key=lambda c: -c[f"strength_{tf}"])[:3]
    if not cands:
        return "_No fresh candidates to shortlist today._\n"
    out = ""
    for i, c in enumerate(cands, 1):
        r = c[tf]
        notes = []
        if r["flipped_bullish"]:
            notes.append(f"flipped this {'week' if tf == 'weekly' else 'day'}")
        else:
            notes.append(f"{r['bars_in_regime']} {'weeks' if tf == 'weekly' else 'days'} into the trend")
        if (r["macd_hist"] or 0) > 0:
            notes.append("MACD positive")
        if (r["volume_ratio"] or 0) >= 1.2:
            notes.append(f"volume {r['volume_ratio']:.1f}x average")
        if c["daily"]["regime"] == "bullish" and c["weekly"]["regime"] == "bullish":
            notes.append("daily and weekly both bullish")
        if c.get("watchlist"):
            notes.append("on your watchlist")
        out += f"{i}. **{c['symbol']}** ({c['name']}, #{c['rank']}) at {fmt_price(c['price'])}: {', '.join(notes)}.\n"
    out += "\nRun `python picker.py` for the Q&A picker that narrows this to one or two coins for your style.\n"
    return out


def recent_flip_log(state: dict[str, Any], tf: str, limit: int = 15) -> str:
    rows = [f for f in reversed(state["flips"]) if f["timeframe"] == tf][:limit]
    if not rows:
        return "_No flips logged yet._\n"
    head = "| Date | Coin | Direction | Price then |\n|---|---|---|---|\n"
    body = "".join(
        f"| {f['date']} | {'⭐ ' if f.get('watchlist') else ''}**{f['symbol']}** {f['name']} (#{f['rank']}) | {'🟢 bullish' if f['direction'] == 'bullish' else '🔴 bearish'} | {fmt_price(f['price'])} |\n"
        for f in rows
    )
    return head + body


def morning_report(today: date, coins: list[dict[str, Any]], lists: dict[str, list[dict[str, Any]]], state: dict[str, Any]) -> str:
    md = f"# Morning flip report - {today.strftime('%A %d %B %Y')}\n\n"
    md += f"Top {len(coins)} coins by market cap plus your watchlist (stablecoins and wrapped tokens excluded). Signals are read on the last **completed** candle.\n\n"
    md += "## Market pulse\n\n" + market_breadth(coins) + "\n"
    md += "## ⭐ Watchlist\n\n" + watchlist_section(lists["watchlist"]) + "\n"
    md += "## 🟢 Flipped bullish on the DAILY (last completed day)\n\n" + table(lists["daily_flipped"], "daily", "flipped") + "\n"
    md += "## Fresh daily uptrends (flipped in the last 5 days)\n\n" + table(lists["daily_recent"], "daily", "recent") + "\n"
    md += "## ⏳ About to flip bullish on the DAILY\n\n" + table(lists["daily_about_to_flip"], "daily", "about") + "\n"
    md += "## Weekly picture\n\n"
    md += "### Flipped bullish on the WEEKLY (last completed week)\n\n" + table(lists["weekly_flipped"], "weekly", "flipped") + "\n"
    md += "### About to flip bullish on the WEEKLY\n\n" + table(lists["weekly_about_to_flip"], "weekly", "about") + "\n"
    if lists["daily_flipped_bearish"]:
        md += "## 🔴 Lost the daily trend (flipped bearish)\n\n"
        md += ", ".join(f"**{c['symbol']}**" for c in lists["daily_flipped_bearish"]) + "\n\n"
    md += "## Shortlist for today\n\n" + picks_section(lists, "daily") + "\n"
    md += "## Recent daily flips (tracker log)\n\n" + recent_flip_log(state, "daily") + "\n"
    md += footer()
    return md


def weekly_report(today: date, coins: list[dict[str, Any]], lists: dict[str, list[dict[str, Any]]], state: dict[str, Any]) -> str:
    md = f"# Weekly bullish-flip list - week of {today.strftime('%d %B %Y')}\n\n"
    md += f"Top {len(coins)} coins by market cap plus your watchlist. Weekly candles close Sunday night UTC; this list reads the week that just closed.\n\n"
    md += "## Market pulse\n\n" + market_breadth(coins) + "\n"
    md += "## ⭐ Watchlist\n\n" + watchlist_section(lists["watchlist"]) + "\n"
    md += "## 🟢 Flipped bullish on the WEEKLY this week\n\n" + table(lists["weekly_flipped"], "weekly", "flipped") + "\n"
    md += "## Still early: flipped bullish in the last 4 weeks\n\n" + table(lists["weekly_recent"], "weekly", "recent") + "\n"
    md += "## ⏳ About to flip bullish on the WEEKLY\n\n"
    md += "Bearish coins scoring 2+ out of 4 on: price near/above the 21 EMA, 9/21 EMA gap narrowing, MACD histogram rising, 9 EMA turning up.\n\n"
    md += table(lists["weekly_about_to_flip"], "weekly", "about") + "\n"
    if lists["weekly_flipped_bearish"]:
        md += "## 🔴 Flipped bearish on the WEEKLY this week\n\n"
        md += ", ".join(f"**{c['symbol']}**" for c in lists["weekly_flipped_bearish"]) + "\n\n"
    md += "## Shortlist for the week\n\n" + picks_section(lists, "weekly") + "\n"
    md += "## Recent weekly flips (tracker log)\n\n" + recent_flip_log(state, "weekly") + "\n"
    md += footer()
    return md


def footer() -> str:
    return (
        "---\n"
        "**Method.** Trend = bullish when close is above the 21 EMA and the 9 EMA is above the 21 EMA; bearish when both are below; "
        "anything in between keeps the previous state so a single wick cannot flip it. A flip is the first candle the state changes. "
        "Data: CoinGecko daily closes, weekly candles aggregated Monday to Sunday. ⭐ marks coins on your watchlist. "
        "This is a screening tool, not financial advice; check the chart before you buy anything.\n"
    )


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def run(
    mode: str,
    top: int,
    source: Any,
    today: date,
    out_data: Path,
    out_reports: Path,
    limit_ids: Optional[set[str]] = None,
    watchlist: Optional[list[str]] = None,
) -> dict[str, Any]:
    out_data.mkdir(parents=True, exist_ok=True)
    (out_reports / "weekly").mkdir(parents=True, exist_ok=True)
    watch = list(watchlist if watchlist is not None else load_watchlist())

    log(f"Fetching top {top} coins...")
    markets = source.top_markets(top)
    if limit_ids:
        markets = [m for m in markets if m["id"] in limit_ids]
    have = {m["id"] for m in markets}
    missing_watch = [w for w in watch if w not in have]
    if missing_watch:
        log(f"Fetching watchlist coins outside the top {top}: {', '.join(missing_watch)}")
        extra = source.markets_by_ids(missing_watch)
        found = {m["id"] for m in extra}
        for w in missing_watch:
            if w not in found:
                log(f"  watchlist id '{w}' not found on CoinGecko; check the id in watchlist.txt")
        markets.extend(extra)
    watch_set = set(watch)

    coins: list[dict[str, Any]] = []
    for i, row in enumerate(markets, 1):
        try:
            chart = source.chart(row["id"])
        except Exception as e:  # keep going; one bad coin must not kill the report
            log(f"  [{i}/{len(markets)}] {row['id']}: failed ({e})")
            continue
        c = analyse_coin(row, chart, today, watchlist=row["id"] in watch_set)
        coins.append(c)
        log(f"  [{i}/{len(markets)}] {c['symbol']:<6} weekly={c['weekly']['regime']} daily={c['daily']['regime']}{'  (watchlist)' if c['watchlist'] else ''}")

    global DATA_DIR
    DATA_DIR = out_data
    state = load_state()
    new_flips = update_state(state, coins, today)
    lists = build_lists(coins)

    is_weekly = mode == "weekly" or (mode == "auto" and today.weekday() == 0)
    stamp = today.isoformat()

    morning = morning_report(today, coins, lists, state)
    (out_reports / f"{stamp}.md").write_text(morning)
    (out_reports / "latest.md").write_text(morning)
    written = [f"reports/{stamp}.md"]
    if is_weekly:
        weekly = weekly_report(today, coins, lists, state)
        (out_reports / "weekly" / f"{stamp}.md").write_text(weekly)
        (out_reports / "latest-weekly.md").write_text(weekly)
        written.append(f"reports/weekly/{stamp}.md")

    snapshot = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "as_of": stamp,
        "mode": "weekly+morning" if is_weekly else "morning",
        "universe": len(coins),
        "watchlist": watch,
        "coins": coins,
        "lists": {k: [c["id"] for c in v] for k, v in lists.items()},
        "flips": [f for f in state["flips"] if f["date"] >= (date.fromordinal(today.toordinal() - 180)).isoformat()],
        "new_flips_today": new_flips,
    }
    (out_data / "tracker.json").write_text(json.dumps(snapshot, indent=1))
    (out_data / "state.json").write_text(json.dumps(state, indent=1))

    # keep the report folder tidy: last 60 daily reports
    olds = sorted(p for p in out_reports.glob("20*.md"))
    for p in olds[:-60]:
        p.unlink()

    log(f"Wrote {', '.join(written)}; {len(new_flips)} new bullish flips logged.")
    return {"written": written, "weekly": is_weekly, "new_flips": new_flips, "lists": lists, "coins": coins}


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mode", choices=["auto", "morning", "weekly"], default="auto")
    ap.add_argument("--top", type=int, default=int(os.environ.get("TRACKER_TOP", "100")))
    ap.add_argument("--fixture", type=Path, help="offline JSON fixture instead of CoinGecko")
    ap.add_argument("--today", type=date.fromisoformat, default=None, help="override the run date (YYYY-MM-DD)")
    ap.add_argument("--out", type=Path, default=HERE, help="output root (contains data/ and reports/)")
    ap.add_argument("--only", help="comma-separated coin ids to restrict to (debugging)")
    ap.add_argument("--pause", type=float, default=2.5, help="seconds between API calls")
    ap.add_argument("--add-watch", metavar="COIN_ID", help="add a CoinGecko id to watchlist.txt and exit")
    args = ap.parse_args(argv)

    if args.add_watch:
        added = add_to_watchlist(args.add_watch)
        print(f"{'Added' if added else 'Already on the watchlist:'} {args.add_watch.strip().lower()}")
        print("Watchlist: " + ", ".join(load_watchlist()))
        return 0

    today = args.today or datetime.now(timezone.utc).date()
    source: Any = FixtureSource(args.fixture) if args.fixture else CoinGecko(os.environ.get("COINGECKO_API_KEY") or None, args.pause)
    limit = set(args.only.split(",")) if args.only else None
    result = run(args.mode, args.top, source, today, args.out / "data", args.out / "reports", limit)

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as fh:
            fh.write((args.out / "reports" / ("latest-weekly.md" if result["weekly"] else "latest.md")).read_text())
    return 0


if __name__ == "__main__":
    sys.exit(main())
