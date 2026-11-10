# Crypto bullish-flip tracker

A screener that watches the top coins by market cap, plus your own watchlist,
and tells you every morning and every Monday which ones have just flipped from
a bearish trend to a bullish one, which ones look about to, and which one or
two are worth a closer look.

## What you get

| When | What | Where |
|---|---|---|
| Every morning (06:20 UTC) | Watchlist status, coins that flipped bullish on the **daily** chart yesterday, fresh daily uptrends, daily "about to flip" list, weekly context, shortlist | `reports/latest.md` (dated copy in `reports/`) |
| Every Monday | Watchlist status, coins that flipped bullish on the **weekly** chart in the week that just closed, still-early weekly uptrends (last 4 weeks), weekly "about to flip" list, shortlist | `reports/latest-weekly.md` (dated copy in `reports/weekly/`) |
| Always | Running log of every bearish-to-bullish and bullish-to-bearish flip the tracker has seen | `data/state.json` |
| Always | Live dashboard with filters, a watchlist tab and the Q&A picker | `dashboard/index.html` (served by GitHub Pages) |

The GitHub Actions workflow in `.github/workflows/crypto-flip-tracker.yml` runs
the whole thing and commits the results back to the repo, so the reports and the
dashboard update themselves. You can also press **Run workflow** on the Actions
tab at any time.

## Watchlist

`watchlist.txt` holds CoinGecko ids, one per line. Every coin on it is screened
and reported even when it sits outside the top 100 by market cap, gets its own
section at the top of each report, a ⭐ wherever it appears in the other
tables, a tab on the dashboard, and a pass on the size filter in the picker.

Add a coin from the command line (the id is the last part of the coin's
coingecko.com URL):

```
cd crypto-flip-tracker
python flip_tracker.py --add-watch injective-protocol
```

Currently on the list: Injective (`injective-protocol`).

## How a "flip" is defined

On each timeframe (daily candles, and Monday-to-Sunday weekly candles):

- **Bullish trend** = close above the 21 EMA **and** the 9 EMA above the 21 EMA.
- **Bearish trend** = close below the 21 EMA **and** the 9 EMA below the 21 EMA.
- Anything in between keeps the previous state, so one wick cannot flip the trend.
- **Flipped bullish** = the first completed candle where the state goes bearish to bullish.
- **About to flip** = a bearish coin scoring 2 or more out of 4 on:
  price within 5% of (or above) the 21 EMA, 9/21 EMA gap narrowing for two
  candles, MACD histogram rising for two candles, 9 EMA turning up.

Signals are always read on the last **completed** candle. The live candle is
shown as a hint ("live candle is printing bullish") but never counts as a flip.

Stablecoins, wrapped and staked tokens are excluded from the market-cap universe.

## The Q&A picker

```
cd crypto-flip-tracker
python picker.py
```

It asks five questions (holding horizon, coin size, fresh vs confirmed flips,
whether daily and weekly must agree, one or two picks) and scores the tracked
coins against your answers. The same picker lives on the dashboard page.

Non-interactive: `python picker.py --horizon weeks --risk mid --style fresh --both yes --picks 2`

## Running it yourself

No dependencies beyond Python 3.10+.

```
cd crypto-flip-tracker
python flip_tracker.py                # morning report, plus the weekly list on Mondays
python flip_tracker.py --mode weekly  # force the weekly list
python flip_tracker.py --top 50       # smaller universe, faster
python -m unittest test_tracker -v    # offline tests on synthetic prices
```

CoinGecko's free API is rate limited. The script paces itself (about one call
every 2.5 seconds, so 100 coins takes 4 to 5 minutes) and backs off on 429s.
A free CoinGecko "demo" key in the `COINGECKO_API_KEY` secret makes it faster.

## Dashboard

`dashboard/index.html` reads `../data/tracker.json`. Once this folder is on the
branch GitHub Pages serves, it is available at:

`https://polky17.github.io/missingasoul-/crypto-flip-tracker/dashboard/`

Locally: `cd crypto-flip-tracker && python -m http.server 8000` then open
`http://localhost:8000/dashboard/`.

## Not financial advice

This is a screening tool. It finds candidates; it does not know your position
size, your stops, or what the chart looks like in your timezone. Look before you buy.
