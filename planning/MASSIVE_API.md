# Massive API Reference (formerly Polygon.io)

Reference for the parts of the Massive REST API that FinAlly uses to get current and
end-of-day (EOD) prices for several tickers at once. Last checked 2026-11-27 against the
official docs and the installed SDK (`massive` 2.2.0, `backend/.venv`).

## 1. Basics

| Item | Value |
|---|---|
| Base URL | `https://api.massive.com` (legacy `https://api.polygon.io` still works) |
| Auth | `Authorization: Bearer <KEY>` header (the SDK uses this), or `?apiKey=<KEY>` query param |
| Python SDK | `massive` on PyPI, needs Python 3.10+. Install with `uv add massive` |
| Env var | The SDK reads `MASSIVE_API_KEY` itself when you don't pass `api_key` |
| SDK errors | `massive.exceptions.AuthError` (no key), `massive.exceptions.BadResponse` (any non-200) |

```python
from massive import RESTClient

client = RESTClient()                   # reads MASSIVE_API_KEY from the environment
client = RESTClient(api_key="...")      # or pass it explicitly
client = RESTClient(retries=0)          # recommended for pollers, see section 6
```

The SDK client is **synchronous** (it uses urllib3). In FastAPI, call it with
`await asyncio.to_thread(...)`.

## 2. Plans: what you get

| Plan | Price | Rate limit | Recency | Snapshots | History |
|---|---|---|---|---|---|
| Stocks Basic | free | **5 calls/min** | End of day | **No** | 2 years |
| Stocks Starter | paid | unlimited* | 15-min delayed | Yes | 5 years |
| Stocks Developer | paid | unlimited* | 15-min delayed | Yes | 10 years |
| Stocks Advanced | $199/mo | unlimited* | Real-time | Yes | All |
| Stocks Business | paid | unlimited* | Real-time | Yes (+ `fmv`) | All |

\* Massive asks paid users to stay under about 100 requests/second. A 429 means you hit a limit.

**What this means for FinAlly:** a free key **cannot** call the snapshot endpoints. It
only gets EOD bars, and only after the trading day has closed. A day's bars become
available after midnight ET; asking earlier returns 403. The Aggregates endpoints
(sections 3.3 to 3.6) work on every plan.

## 3. Endpoints

### 3.1 Full Market Snapshot: many tickers in one call (primary, paid plans)

`GET /v2/snapshot/locale/us/markets/stocks/tickers?tickers=AAPL,MSFT,TSLA`

| Query param | Meaning |
|---|---|
| `tickers` | Comma-separated, case-sensitive. Leave it empty to get the whole market (10k+ rows) |
| `include_otc` | Include OTC securities (default `false`) |

```bash
curl -H "Authorization: Bearer $MASSIVE_API_KEY" \
  "https://api.massive.com/v2/snapshot/locale/us/markets/stocks/tickers?tickers=AAPL,MSFT"
```

Response (trimmed):

```json
{
  "status": "OK",
  "count": 1,
  "tickers": [{
    "ticker": "AAPL",
    "todaysChange": -0.124,
    "todaysChangePerc": -0.601,
    "updated": 1605192894630916600,
    "day":     {"o": 20.64, "h": 20.64, "l": 20.506, "c": 20.506, "v": 37216, "vw": 20.616},
    "prevDay": {"o": 20.79, "h": 21, "l": 20.5, "c": 20.63, "v": 292738, "vw": 20.6939},
    "min":     {"o": 20.5, "c": 20.506, "t": 1684428600000, "v": 5000, "av": 37216},
    "lastTrade": {"p": 20.506, "s": 2416, "t": 1605192894630916600, "x": 4, "c": [14, 41]},
    "lastQuote": {"p": 20.5, "s": 13, "P": 20.6, "S": 22, "t": 1605192959994246100}
  }]
}
```

How the SDK names these fields (`TickerSnapshot`):

| JSON | SDK attribute | Notes |
|---|---|---|
| `ticker` | `snap.ticker` | |
| `lastTrade.p` | `snap.last_trade.price` | Best "current price" |
| `lastTrade.t` | `snap.last_trade.sip_timestamp` | **Nanoseconds**. There is no `.timestamp` attribute |
| `lastQuote.p` / `.P` | `snap.last_quote.bid_price` / `.ask_price` | |
| `day.c` | `snap.day.close` | Today's running close |
| `prevDay.c` | `snap.prev_day.close` | Previous session close |
| `min.t` | `snap.min.timestamp` | **Milliseconds** |
| `todaysChangePerc` | `snap.todays_change_percent` | vs previous close |
| `updated` | `snap.updated` | Nanoseconds |

`last_trade` and `last_quote` are `None` when the plan doesn't include trades or quotes,
and early in the day before any trade. Snapshot data is cleared at 3:30 AM ET and fills
up again from about 4:00 AM ET.

```python
from massive import RESTClient
from massive.rest.models import SnapshotMarketType

client = RESTClient()
snaps = client.get_snapshot_all(
    market_type=SnapshotMarketType.STOCKS,
    tickers=["AAPL", "MSFT", "TSLA"],
)
for s in snaps:
    if s.last_trade and s.last_trade.price:
        price, ts = s.last_trade.price, s.last_trade.sip_timestamp / 1e9
    elif s.day and s.day.close:          # fallback: running day close
        price, ts = s.day.close, s.updated / 1e9
    else:
        continue
    print(s.ticker, price, ts)
```

Unknown symbols are simply left out of the `tickers` array; they do not cause an error.

### 3.2 Unified Snapshot (v3): alternative, paid plans

`GET /v3/snapshot?ticker.any_of=AAPL,MSFT,SPY`. Up to **250** tickers per call (251
returns 400); `limit` defaults to 10, max 250. It uses readable field names:
`results[].session.{open,high,low,close,previous_close,change_percent}` and
`results[].last_trade.{price,sip_timestamp}` (ns). An unknown ticker comes back as a row
with `"error": "NOT_FOUND"`. SDK: `client.list_universal_snapshots(ticker_any_of=[...])`.
FinAlly does not need it; 3.1 is enough.

### 3.3 Daily Market Summary (grouped daily): EOD for all tickers (primary, free plan)

`GET /v2/aggs/grouped/locale/us/market/stocks/{YYYY-MM-DD}?adjusted=true`

Returns one daily bar for **every** US stock in a single call, so it costs 1 call however
many tickers we track. Filter the rows locally.

```json
{"status": "OK", "adjusted": true, "resultsCount": 3,
 "results": [{"T": "AAPL", "o": 324.66, "h": 326.2, "l": 322.3, "c": 325.12,
              "v": 26122646, "vw": 324.1, "n": 412345, "t": 1673298000000}]}
```

| JSON | SDK (`GroupedDailyAgg`) |
|---|---|
| `T` | `ticker` |
| `o h l c` | `open high low close` |
| `v vw n` | `volume vwap transactions` |
| `t` | `timestamp` (milliseconds) |

A weekend or holiday returns no `results`, and the SDK then returns `[]`. A date that
hasn't closed yet returns 403 on the free plan. So walk back from yesterday:

```python
from datetime import date, timedelta
from massive import RESTClient

def latest_eod_closes(client: RESTClient, tickers: set[str]) -> dict[str, float]:
    """Closing prices from the most recent completed trading day (1-5 API calls)."""
    day = date.today() - timedelta(days=1)
    for _ in range(5):
        rows = client.get_grouped_daily_aggs(day.isoformat(), adjusted=True)
        if rows:
            return {r.ticker: r.close for r in rows if r.ticker in tickers}
        day -= timedelta(days=1)
    return {}
```

### 3.4 Previous Day Bar: EOD for one ticker

`GET /v2/aggs/ticker/{TICKER}/prev`. Returns the previous session's OHLCV as
`results[0].{T,o,h,l,c,v,vw,t}`, where `t` is ms. It is 1 call per ticker, so 10 tickers
take 2 minutes on the free plan. Prefer 3.3.

```python
prev = client.get_previous_close_agg("AAPL")   # returns a list of PreviousCloseAgg
print(prev[0].close, prev[0].timestamp)
```

### 3.5 Daily Ticker Summary: open/close for one ticker on a date

`GET /v1/open-close/{TICKER}/{YYYY-MM-DD}`. Returns a flat object:
`{symbol, from, open, high, low, close, volume, preMarket, afterHours}`.

```python
d = client.get_daily_open_close_agg("AAPL", "2026-11-25")
print(d.open, d.close, d.pre_market, d.after_hours)
```

### 3.6 Custom Bars: history for one ticker (charts, not prices)

`GET /v2/aggs/ticker/{TICKER}/range/{multiplier}/{timespan}/{from}/{to}`.
`timespan` is `minute|hour|day|week|month|...`. `from` and `to` are `YYYY-MM-DD` or ms,
and both are inclusive. Optional params: `adjusted`, `sort=asc|desc`, `limit` (max
50000). Results are `results[].{o,h,l,c,v,vw,n,t}` with `t` in ms, and large ranges
include a `next_url`.

```python
bars = list(client.list_aggs("AAPL", 1, "day", "2026-10-01", "2026-11-26", limit=50000))
closes = [(b.timestamp / 1000, b.close) for b in bars]   # list_aggs follows next_url for you
```

Out of scope for FinAlly: the plan builds charts from SSE data since page load. Listed
here for completeness.

### 3.7 Last Trade / Last Quote: one ticker

- `GET /v2/last/trade/{TICKER}`: `client.get_last_trade("AAPL")` returns `.price` and `.sip_timestamp` (ns)
- `GET /v2/last/nbbo/{TICKER}`: `client.get_last_quote("AAPL")` returns `.bid_price` and `.ask_price`

Each costs 1 call per ticker, and they need a plan that includes trades or quotes. Use
3.1 instead when you need several tickers.

## 4. Timestamp units (an easy mistake)

| Field | Unit | To Unix seconds |
|---|---|---|
| `lastTrade.t`, `lastQuote.t`, `updated`, v3 `sip_timestamp` | nanoseconds | `/ 1e9` |
| Aggregate `t` (`min.t`, grouped, prev, custom bars) | milliseconds | `/ 1e3` |

The docs disagree on whether a grouped-daily `t` is the start or the end of the day.
Treat it as "the trading day" and don't depend on the exact hour.

## 5. Choosing an endpoint

| Need | Paid plan | Free plan |
|---|---|---|
| Current price, many tickers | 3.1 Full Market Snapshot (1 call) | not available |
| Latest close, many tickers | 3.1 `prev_day.close` / `day.close` | 3.3 Grouped daily (1 call) |
| Close on a given date | 3.3 | 3.3 |
| Intraday or history chart | 3.6 | 3.6 (EOD bars only) |

## 6. Errors, retries and polling

- Any non-200 raises `BadResponse(body)`. A 403 means the plan doesn't include the data
  (e.g. a snapshot on the free plan, or today's bar before midnight ET). 401 means a bad
  key, and 429 means rate limited.
- The SDK **retries on 413/429/499/5xx** (3 times by default, backoff from 0.1s). On the
  free plan each retry uses up the 5/min budget. A poller already retries on its next
  tick, so build it with `RESTClient(retries=0)`.
- Default timeouts are 10s connect and 10s read.
- Polling intervals: paid real-time, 2-5s. Paid delayed, 15s or more is plenty (the data
  is already 15 minutes old). Free EOD, every 15-60 minutes is enough, since the value
  changes once a day.

## Sources

- https://massive.com/docs/rest/stocks/snapshots/full-market-snapshot
- https://massive.com/docs/rest/stocks/snapshots/unified-snapshot
- https://massive.com/docs/rest/stocks/aggregates/daily-market-summary
- https://massive.com/docs/rest/stocks/aggregates/previous-day-bar
- https://massive.com/docs/rest/stocks/aggregates/daily-ticker-summary
- https://massive.com/docs/rest/stocks/aggregates/custom-bars
- https://massive.com/docs/rest/stocks/overview
- https://massive.com/pricing
- https://massive.com/knowledge-base/article/what-is-the-max-number-of-tickers-i-can-pass-through-polygons-snapshot
- https://massive.com/knowledge-base/article/on-the-free-basic-plan-when-can-i-pull-the-previous-trading-days-data
- https://github.com/massive-com/client-python
- Installed SDK source: `backend/.venv/lib/python3.13/site-packages/massive/` (v2.2.0)
