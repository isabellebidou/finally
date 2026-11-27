# Market Simulator

How FinAlly makes up realistic live stock prices when no `MASSIVE_API_KEY` is set. It is
the default data source: it runs in-process with no network access, and it implements
`MarketDataSource` (see `MARKET_INTERFACE.md`).

## 1. Goals

- Prices that look real: random-walk drift, a volatility that fits each stock, no
  negative prices.
- Visible but plausible motion every 500ms, so prices flash and sparklines fill up.
- Stocks in the same sector move together (tech rallies as a group).
- Occasional drama: sudden moves of 2-5%.
- Any ticker works, including made-up ones added through the watchlist or by the AI.
- Cheap: one step for 30 tickers takes about 70 µs.

## 2. The model: Geometric Brownian Motion

Each tick, every price is multiplied by a log-normal factor:

```
S(t+dt) = S(t) * exp( (mu - sigma^2 / 2) * dt  +  sigma * sqrt(dt) * Z )
```

| Symbol | Meaning | Value |
|---|---|---|
| `mu` | annual drift (expected return) | 0.03 to 0.08 |
| `sigma` | annual volatility | 0.17 (V) to 0.50 (TSLA) |
| `dt` | one tick as a fraction of a trading year | `0.5 / (252 * 6.5 * 3600)` = 8.48e-8 |
| `Z` | standard normal, **correlated across tickers** | from a Cholesky decomposition |

Why GBM? `exp()` keeps prices positive, moves are proportional to price (a $800 stock
moves more dollars than a $50 one), and it is the textbook model, which suits the course.

How big the moves are (1 standard deviation, measured with the real `dt`):

| sigma | per tick (500ms) | per hour |
|---|---|---|
| 0.17 (V) | 0.005% | 0.42% |
| 0.22 (AAPL) | 0.0064%, about 1.2 cents on $190 | 0.54% |
| 0.50 (TSLA) | 0.015% | 1.24% |

So the time scale is real-time trading-day volatility, compressed only in that the
"market" never closes.

## 3. Correlated moves

A vector of independent normals `z` is turned into correlated normals with `L @ z`,
where `L` is the Cholesky factor of a correlation matrix `C` (`C = L @ L.T`).

How the correlation between two tickers is picked (`_pairwise_correlation`):

| Pair | rho |
|---|---|
| either one is TSLA | 0.3 (it does its own thing) |
| both tech (AAPL GOOGL MSFT AMZN META NVDA NFLX) | 0.6 |
| both finance (JPM V) | 0.5 |
| anything else, including unknown tickers | 0.3 |

This block structure behaves like a market factor plus a sector factor, so `C` stays
positive-definite as tickers are added (checked with 30 tickers). `L` is rebuilt only
when a ticker is added or removed, which is O(n^2) to fill and fine for n < 50, never
on each tick.

```python
corr = np.eye(n)
for i in range(n):
    for j in range(i + 1, n):
        corr[i, j] = corr[j, i] = _pairwise_correlation(tickers[i], tickers[j])
L = np.linalg.cholesky(corr)          # None when n <= 1
```

## 4. Random events

After the GBM step, each ticker has a probability `event_probability` (default 0.001) of
a shock:

```python
if random.random() < event_prob:
    price *= 1 + random.uniform(0.02, 0.05) * random.choice([-1, 1])
```

With 10 tickers at 2 ticks/s, that is about one event every 50 seconds somewhere on the
board. Shocks are not correlated; they are idiosyncratic "news".

## 5. Seed data and unknown tickers

`seed_prices.py` holds all the tunables as plain dicts:

- `SEED_PRICES`: realistic starting prices for the 10 default tickers (AAPL 190,
  GOOGL 175, MSFT 420, AMZN 185, TSLA 250, NVDA 800, META 500, JPM 195, V 280,
  NFLX 600).
- `TICKER_PARAMS`: a `sigma` and `mu` for each ticker.
- `DEFAULT_PARAMS`: `sigma=0.25, mu=0.05` for any other ticker.
- `CORRELATION_GROUPS` and the `*_CORR` constants.

An unknown ticker (e.g. `PYPL`, or a made-up `ZZZZ`) starts at
`random.uniform(50, 300)` with default params and correlation 0.3. Per PLAN.md, unknown
symbols are accepted and the simulator invents a price.

Prices are **not** persisted. They reset to the seed values on restart while positions
persist, so P&L can jump. PLAN.md accepts this.

## 6. Code structure

```
backend/app/market/
├── seed_prices.py   # constants only (prices, params, correlation groups)
├── simulator.py     # GBMSimulator (pure math) + SimulatorDataSource (async adapter)
├── cache.py         # PriceCache (shared with Massive)
└── interface.py     # MarketDataSource ABC
```

There are two classes on purpose:

**`GBMSimulator`** is pure, synchronous and easy to test. It knows nothing about asyncio
or the cache.

```python
sim = GBMSimulator(tickers=["AAPL", "MSFT"], dt=DEFAULT_DT, event_probability=0.001)
sim.step()               # -> {"AAPL": 190.01, "MSFT": 419.98}  (rounded to 2 dp)
sim.add_ticker("PYPL")   # seeds price + params, rebuilds Cholesky; no-op if present
sim.remove_ticker("MSFT")
sim.get_price("AAPL")    # unrounded internal price, or None
sim.get_tickers()
```

The hot path in `step()` makes one `np.random.standard_normal(n)`, one matrix-vector
product, then a Python loop to apply drift, diffusion and events. Internal prices keep
full precision; only the returned values are rounded, so rounding never builds up drift.

**`SimulatorDataSource`** is the `MarketDataSource` adapter.

```python
class SimulatorDataSource(MarketDataSource):
    def __init__(self, price_cache, update_interval=0.5, event_probability=0.001): ...

    async def start(self, tickers):
        self._sim = GBMSimulator(tickers, event_probability=self._event_prob)
        for t in tickers:                                  # seed so SSE has data at once
            self._cache.update(t, self._sim.get_price(t))
        self._task = asyncio.create_task(self._run_loop(), name="simulator-loop")

    async def _run_loop(self):
        while True:
            try:
                for t, p in self._sim.step().items():
                    self._cache.update(t, p)
            except Exception:
                logger.exception("Simulator step failed")   # never kill the loop
            await asyncio.sleep(self._interval)

    async def add_ticker(self, t):     # adds to sim AND seeds the cache right away
    async def remove_ticker(self, t):  # removes from sim AND cache
    async def stop(self):              # cancel task, await it, swallow CancelledError
```

Because `add_ticker` seeds the cache straight away, a ticker added by the AI in a chat
reply has a price at once, and a trade on it in the same reply works (unlike with
Massive).

## 7. Tuning knobs

| Want | Change |
|---|---|
| Calmer or wilder market | `sigma` in `TICKER_PARAMS` / `DEFAULT_PARAMS` |
| More or less drama | `event_probability` (constructor) |
| Faster or slower ticks | `update_interval`. Keep `dt` matched (`interval / TRADING_SECONDS_PER_YEAR`) |
| Stronger sector moves | `INTRA_TECH_CORR` and friends (stay below 1 so `C` stays positive-definite) |
| Repeatable runs in tests | `np.random.seed(n); random.seed(n)` |

Known quirk: prices are rounded to cents in `PriceCache`. A cheap, low-sigma ticker can
show several "flat" ticks in a row, because its per-tick move is under half a cent. This
is accepted.

## 8. Testing

Tests are in `backend/tests/market/test_simulator.py` and `test_simulator_source.py`.

What they already check:
- `step()` returns every ticker, with positive prices rounded to 2 dp; the empty case
- the seed prices are used; an unknown ticker gets a random seed
- add and remove are idempotent; Cholesky is rebuilt on add and is `None` with 1 ticker
- the pairwise correlation rules (tech, finance, TSLA, cross-sector); `dt` is sensible
- the source fills the cache on start, prices change over time, stop is clean,
  add and remove work, and the loop survives an exception

What is still missing and worth adding:
- a statistical GBM check with a fixed seed: the log-return mean is about
  `(mu - sigma^2/2)dt` and the std about `sigma*sqrt(dt)`
- the sample correlation of a tech pair is higher than that of a cross-sector pair
- `event_probability=1.0` gives a 2-5% move each tick, and `0.0` gives none

Run them with `cd backend && uv run pytest tests/market -q`.
