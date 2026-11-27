# Market Data Interface

The single Python API that all FinAlly code uses to get stock prices. It is backed by
the Massive API when `MASSIVE_API_KEY` is set, and by the built-in simulator otherwise.
The code is in `backend/app/market/`. The Massive details are in `MASSIVE_API.md` and the
simulator in `MARKET_SIMULATOR.md`.

## 1. Design in one picture

```
           MASSIVE_API_KEY set?
                  │
   create_market_data_source(cache)          factory.py
        │yes                    │no
        ▼                       ▼
 MassiveDataSource       SimulatorDataSource      both implement MarketDataSource (interface.py)
 (REST poller)           (GBM, every 500ms)
        │ cache.update()        │ cache.update()
        └──────────┬────────────┘
                   ▼
              PriceCache                       cache.py: latest PriceUpdate per ticker + version
                   │ read only
     ┌─────────────┼───────────────┬───────────────┐
     ▼             ▼               ▼               ▼
 SSE stream    portfolio      trade execution   chat context
 (stream.py)   valuation      (needs a price)   (LLM prompt)
```

Rules:
- **Producers write and consumers read.** Nothing downstream calls a data source to get a
  price. Everything reads `PriceCache`.
- **There is one writer at a time.** The factory picks exactly one source at startup.
- **The source only knows a ticker list.** Which tickers to track (watchlist plus open
  positions) is decided by the caller, the watchlist and portfolio services, not by the
  source.

## 2. Public API (`from app.market import ...`)

### `PriceUpdate` (models.py)

An immutable snapshot, created only by `PriceCache.update()`.

```python
@dataclass(frozen=True, slots=True)
class PriceUpdate:
    ticker: str
    price: float            # rounded to 2 dp
    previous_price: float   # price from the previous update (== price on first update)
    timestamp: float        # Unix seconds

    change: float           # property: price - previous_price (tick-to-tick)
    change_percent: float   # property: % tick-to-tick
    direction: str          # property: "up" | "down" | "flat"
    def to_dict(self) -> dict  # the exact SSE per-ticker shape from PLAN.md section 6
```

### `PriceCache` (cache.py)

A thread-safe in-memory store, and the single source of truth for prices.

```python
cache = PriceCache()
cache.update(ticker, price, timestamp=None) -> PriceUpdate   # producers only
cache.get(ticker) -> PriceUpdate | None
cache.get_price(ticker) -> float | None
cache.get_all() -> dict[str, PriceUpdate]                     # shallow copy
cache.remove(ticker) -> None
cache.version -> int        # bumped on each update; SSE sends only when it changes
"AAPL" in cache; len(cache)
```

### `MarketDataSource` (interface.py)

```python
class MarketDataSource(ABC):
    async def start(self, tickers: list[str]) -> None   # seed/poll once, then start background task
    async def stop(self) -> None                        # cancel task; safe to call twice
    async def add_ticker(self, ticker: str) -> None     # no-op if present
    async def remove_ticker(self, ticker: str) -> None  # no-op if absent; also cache.remove()
    def get_tickers(self) -> list[str]
```

### `create_market_data_source(cache)` (factory.py)

```python
api_key = os.environ.get("MASSIVE_API_KEY", "").strip()
return MassiveDataSource(api_key, cache) if api_key else SimulatorDataSource(cache)
```

It returns a source that hasn't been started yet. An empty or whitespace key means the
simulator.

### `create_stream_router(cache)` (stream.py)

`GET /api/stream/prices` is an SSE stream. It sends `retry: 1000` first. After that it
checks every 500ms and sends `data: {TICKER: PriceUpdate.to_dict(), ...}` only when
`cache.version` has changed.

## 3. Wiring into FastAPI (for the backend agent)

```python
from contextlib import asynccontextmanager
from fastapi import FastAPI
from app.market import PriceCache, create_market_data_source, create_stream_router

cache = PriceCache()                            # created at import, so routers can use it

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()                                   # tables + seed first (PLAN section 7)
    source = create_market_data_source(cache)
    await source.start(tracked_tickers())       # watchlist UNION open positions
    app.state.prices, app.state.market = cache, source
    yield
    await source.stop()

app = FastAPI(lifespan=lifespan)
app.include_router(create_stream_router(cache))
```

Typical downstream use:

```python
price = request.app.state.prices.get_price(ticker)
if price is None:
    raise HTTPException(400, f"No price available for {ticker}")
```

### Keeping the tracked set right

The tracked set is **watchlist UNION positions** (PLAN section 6). The caller keeps it in
sync:

| Event | Call |
|---|---|
| Watchlist add | `await market.add_ticker(t)` (idempotent) |
| Buy creates a new position | `await market.add_ticker(t)` (already tracked if it was traded, but harmless) |
| Watchlist remove | `remove_ticker(t)` **only if** no position is held |
| Sell closes a position | `remove_ticker(t)` **only if** the ticker isn't on the watchlist |

Normalise tickers before calling: `t.strip().upper()`, matching `^[A-Z]{1,5}$`.

## 4. The two implementations

| | SimulatorDataSource | MassiveDataSource |
|---|---|---|
| Update rate | 500ms | 15s by default (configurable) |
| First price | Right away in `start()` / `add_ticker()` | After the first poll in `start()`. A ticker added later waits for the next poll |
| Unknown ticker | Gets a random seed price | No row comes back, so the price stays `None` and the UI shows "--" |
| Blocking I/O | none | The SDK is synchronous, so it runs through `asyncio.to_thread` |
| Failure handling | Logs the exception and keeps looping | Logs the error and retries on the next interval. It never raises out of the loop |

### MassiveDataSource: polling design

One poll is **one API call for all tracked tickers**, whatever the plan:

1. **Snapshot mode (paid plans).** Use `get_snapshot_all(STOCKS, tickers=[...])`. The
   price is `last_trade.price`, falling back to `day.close` and then `prev_day.close`.
   The timestamp is `last_trade.sip_timestamp / 1e9`, falling back to `updated / 1e9`.
2. **EOD mode (free Basic plan).** The snapshot endpoint returns 403 on that plan, so on
   the first `BadResponse` from a snapshot in `start()`, switch for good to
   `get_grouped_daily_aggs(date)`, walking back from yesterday to the most recent
   trading day. The price is `close` and the timestamp `timestamp / 1e3`. Poll every
   15 minutes or more, because the value only changes once a day.

```python
async def _poll_once(self) -> None:
    if not self._tickers:
        return
    try:
        prices = await asyncio.to_thread(self._fetch)     # {ticker: (price, ts_seconds)}
    except Exception as e:                                # BadResponse, network, ...
        logger.error("Massive poll failed: %s", e)
        return
    for ticker, (price, ts) in prices.items():
        self._cache.update(ticker, price, ts)

def _fetch_snapshot(self) -> dict[str, tuple[float, float]]:
    out = {}
    for s in self._client.get_snapshot_all(SnapshotMarketType.STOCKS, tickers=self._tickers):
        lt = s.last_trade
        if lt and lt.price:
            out[s.ticker] = (lt.price, lt.sip_timestamp / 1e9)
        elif s.day and s.day.close:
            out[s.ticker] = (s.day.close, s.updated / 1e9 if s.updated else time.time())
        elif s.prev_day and s.prev_day.close:
            out[s.ticker] = (s.prev_day.close, time.time())
    return out
```

Build the client with `RESTClient(api_key=key, retries=0)`, so the SDK's own 429 retries
don't use up the free plan's 5 calls/min.

Note that `PriceUpdate.change` is tick-to-tick. With Massive, a "tick" is one poll, so
the flash animations fire only when a poll brings a new price.

## 5. Changes needed in the current code

The current `massive_client.py` has a bug that the tests miss because they use
`MagicMock`. I reproduced it with the real SDK model:

```
>>> TickerSnapshot.from_dict({...sample from docs...}).last_trade.timestamp
AttributeError: 'LastTrade' object has no attribute 'timestamp'
```

- **Root cause:** `_poll_once` reads `snap.last_trade.timestamp`. The SDK field is
  `sip_timestamp`, and it is in **nanoseconds**, not milliseconds. Each snapshot raises
  `AttributeError`, which gets caught and logged as "Skipping snapshot", so **a real key
  never puts a price in the cache**.
- **Fix:** use `sip_timestamp / 1e9` with the fallbacks shown above.
- **Missing free-plan support:** snapshots are not on Stocks Basic. Add the EOD mode
  described above.
- **Retries:** pass `retries=0` to `RESTClient`.
- **Tests:** build fixtures with `TickerSnapshot.from_dict(<json from MASSIVE_API.md>)`
  and `GroupedDailyAgg.from_dict(...)`, not `MagicMock`, so that attribute names are
  checked against the real SDK.

Leave the interface, cache, factory, stream and simulator as they are: they match
PLAN.md.

## 6. Testing the interface

- **Contract test:** run the same test against both sources. Each one needs `start()`
  to fill the cache, `add_ticker` and `remove_ticker` to update `get_tickers()` and the
  cache, and a second `stop()` call to be safe.
- **Factory:** an unset, empty or whitespace key gives the simulator; a real key gives
  Massive.
- **Massive:** use SDK model fixtures. Cover snapshot mode, the switch to EOD on a 403,
  the walk back over weekends, a failed poll that leaves the cache unchanged, and an
  unknown ticker that gets no cache entry.
- **SSE:** an event is sent only when the version changes, and the payload has the
  shape from PLAN.md section 6.
