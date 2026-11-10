#!/usr/bin/env python3
"""Q&A coin picker.

Asks a handful of questions about how you want to trade this leg of the bull
market, then scores the coins in data/tracker.json and hands back one or two
picks with plain-English reasons. Coins on your watchlist skip the size filter.

Interactive:   python picker.py
Non-interactive:  python picker.py --horizon weeks --risk large --picks 2 --style fresh --both yes
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).resolve().parent

QUESTIONS = [
    {
        "key": "horizon",
        "text": "How long do you plan to hold?",
        "options": {"days": "A few days (swing on the daily chart)", "weeks": "A few weeks (ride the weekly trend)", "months": "Months (position for the cycle)"},
        "default": "weeks",
    },
    {
        "key": "risk",
        "text": "How big should the coins be?",
        "options": {"large": "Top 20 only (BTC, ETH, SOL tier)", "mid": "Top 50", "any": "Anything in the tracked universe"},
        "default": "mid",
    },
    {
        "key": "style",
        "text": "Do you want the freshest flips or ones with a little confirmation?",
        "options": {"fresh": "Fresh: flipped on the last candle, earliest entry", "confirmed": "Confirmed: flipped 2 to 4 candles ago and still holding", "either": "Either, just give me the strongest"},
        "default": "either",
    },
    {
        "key": "both",
        "text": "Require daily AND weekly trends to both be bullish?",
        "options": {"yes": "Yes, I want both timeframes agreeing", "no": "No, the chosen timeframe is enough"},
        "default": "yes",
    },
    {
        "key": "picks",
        "text": "How many coins do you want?",
        "options": {"1": "Just one", "2": "Two"},
        "default": "2",
    },
]


def ask(q: dict[str, Any]) -> str:
    keys = list(q["options"])
    print(f"\n{q['text']}")
    for i, k in enumerate(keys, 1):
        print(f"  {i}. {q['options'][k]}" + ("  (default)" if k == q["default"] else ""))
    while True:
        raw = input("> ").strip().lower()
        if raw == "":
            return q["default"]
        if raw.isdigit() and 1 <= int(raw) <= len(keys):
            return keys[int(raw) - 1]
        if raw in keys:
            return raw
        print("  pick a number from the list")


def score(c: dict[str, Any], a: dict[str, str]) -> tuple[float, list[str], bool]:
    tf = "daily" if a["horizon"] == "days" else "weekly"
    r = c[tf]
    reasons: list[str] = []
    if r["regime"] != "bullish":
        return -1, [], False
    rank = c["rank"] or 999
    watch = bool(c.get("watchlist"))
    if a["risk"] == "large" and rank > 20 and not watch:
        return -1, [], False
    if a["risk"] == "mid" and rank > 50 and not watch:
        return -1, [], False
    bars = r["bars_in_regime"]
    if a["style"] == "fresh" and bars != 1:
        return -1, [], False
    if a["style"] == "confirmed" and not (2 <= bars <= 4):
        return -1, [], False
    if a["style"] == "either" and bars > (6 if tf == "weekly" else 10):
        return -1, [], False
    both = c["daily"]["regime"] == "bullish" and c["weekly"]["regime"] == "bullish"
    if a["both"] == "yes" and not both:
        return -1, [], False

    s = 0.0
    unit = "week" if tf == "weekly" else "day"
    if bars == 1:
        s += 10
        reasons.append(f"flipped bullish on the last {unit}ly candle")
    elif bars <= 4:
        s += 8
        reasons.append(f"{bars} {unit}s into a fresh uptrend")
    else:
        s += 4
        reasons.append(f"{bars} {unit}s into the uptrend")
    pct = r["pct_vs_slow"] or 0.0
    if 0 < pct <= 12:
        s += 6
        reasons.append(f"{pct:.1f}% above the 21 EMA, not over-extended")
    elif pct > 12:
        s += 2
        reasons.append(f"{pct:.1f}% above the 21 EMA (stretched, consider waiting for a dip)")
    if (r["macd_hist"] or 0) > 0:
        s += 4
        reasons.append("MACD histogram positive")
    vr = r["volume_ratio"] or 0
    if vr >= 1.2:
        s += 4
        reasons.append(f"volume {vr:.1f}x its recent average")
    if both:
        s += 5
        reasons.append("daily and weekly trends agree")
    if a["horizon"] == "months":
        s += max(0.0, 6 - rank / 10)  # tilt to the big names for long holds
    if watch:
        s += 2
        reasons.append("on your watchlist")
    ch30 = c.get("change_30d")
    if ch30 is not None and ch30 > 60:
        s -= 3
        reasons.append(f"already up {ch30:.0f}% in 30 days")
    return s, reasons, True


def pick(snapshot: dict[str, Any], answers: dict[str, str]) -> list[dict[str, Any]]:
    scored = []
    for c in snapshot["coins"]:
        s, reasons, ok = score(c, answers)
        if ok:
            scored.append((s, c, reasons))
    scored.sort(key=lambda t: (-t[0], t[1]["rank"] or 999))
    n = int(answers["picks"])
    return [{"symbol": c["symbol"], "name": c["name"], "rank": c["rank"], "price": c["price"], "score": s, "reasons": r} for s, c, r in scored[:n]]


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--tracker", type=Path, default=HERE / "data" / "tracker.json")
    for q in QUESTIONS:
        ap.add_argument(f"--{q['key']}", choices=list(q["options"]))
    args = ap.parse_args(argv)

    if not args.tracker.exists():
        print(f"No tracker data at {args.tracker}. Run flip_tracker.py first (or wait for the scheduled run).")
        return 1
    snapshot = json.loads(args.tracker.read_text())

    answers: dict[str, str] = {}
    interactive = any(getattr(args, q["key"]) is None for q in QUESTIONS)
    if interactive and not sys.stdin.isatty():
        interactive = False
    for q in QUESTIONS:
        given = getattr(args, q["key"])
        answers[q["key"]] = given if given else (ask(q) if interactive else q["default"])

    picks = pick(snapshot, answers)
    print(f"\nData as of {snapshot['as_of']} ({snapshot['universe']} coins screened).")
    if not picks:
        print("Nothing fits those answers right now. Loosen the size or timeframe filters, or wait for the next flips.")
        return 0
    print(f"\nYour pick{'s' if len(picks) > 1 else ''}:\n")
    for i, p in enumerate(picks, 1):
        print(f"{i}. {p['symbol']} ({p['name']}, market cap #{p['rank']}) at ${p['price']:,.4g}")
        for r in p["reasons"]:
            print(f"   - {r}")
        print()
    print("Screening tool only. Look at the chart and size the position before you buy.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
