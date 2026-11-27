# Market Data Backend — Detailed Design

Implementation-ready design for everything under `backend/app/market/`: the unified
interface, the price cache, the GBM simulator, the Massive (Polygon.io) poller, the SSE
stream, and how the rest of the backend wires them in.

**Status.** This is the target design. It replaces `archive/MARKET_DATA_DESIGN.md`. Most
of it matches the code already in `backend/app/market/`. Section 0 lists every change
from that code, and section 17 is the checklist for making those changes.

**Verified.** Every module listing in this document was run against `massive` 2.2.0,
Python 3.13 and numpy 2. The test listings in section 14 (23 tests) pass, and `ruff
check` is clean.

Related docs: `PLAN.md` (sections 6 and 8 are the contract), `MARKET_INTERFACE.md` (a
short API summary), `MARKET_SIMULATOR.md` (the simulator maths in prose), and
`MASSIVE_API.md` (an endpoint reference).

---

## Contents

0. [Changes from the current code](#0-changes-from-the-current-code)
1. [Requirements](#1-requirements)
2. [Architecture](#2-architecture)
3. [`models.py`: PriceUpdate](#3-modelspy-priceupdate)
4. [`tickers.py`: normalisation](#4-tickerspy-normalisation)
5. [`cache.py`: PriceCache](#5-cachepy-pricecache)
6. [`interface.py`: MarketDataSource](#6-interfacepy-marketdatasource)
7. [`seed_prices.py`: tunables](#7-seed_pricespy-tunables)
8. [`simulator.py`: GBM simulator](#8-simulatorpy-gbm-simulator)
9. [`massive_client.py`: Massive poller](#9-massive_clientpy-massive-poller)
10. [`factory.py`: picking a source](#10-factorypy-picking-a-source)
11. [`stream.py`: SSE endpoint](#11-streampy-sse-endpoint)
12. [`tracking.py`: watchlist UNION positions](#12-trackingpy-watchlist-union-positions)
13. [FastAPI integration and usage examples](#13-fastapi-integration-and-usage-examples)
14. [Tests](#14-tests)
15. [Edge cases and failure modes](#15-edge-cases-and-failure-modes)
16. [Configuration](#16-configuration)
17. [Implementation checklist](#17-implementation-checklist)

---

## 0. Changes from the current code

| # | Module | Change | Why |
|---|---|---|---|
| 1 | `massive_client.py` | Read `last_trade.sip_timestamp` (**ns**, `/1e9`) instead of `last_trade.timestamp` | **Bug.** The SDK's `LastTrade` has no `timestamp` attribute, so every real snapshot raises `AttributeError` and gets skipped. A real key never prices anything. The tests miss it because they use `MagicMock` |
| 2 | `massive_client.py` | Add end-of-day (EOD) mode: grouped daily bars, switched to automatically when the snapshot endpoint fails | The free Basic plan returns 403 for snapshots |
| 3 | `massive_client.py` | Use `RESTClient(retries=0)`, inject the client, copy the ticker list before running in a thread, poll right away on `add_ticker`, skip writes when the quote hasn't changed | Saves the rate budget, makes it testable, avoids a race, gives faster feedback, avoids sending unchanged quotes |
| 4 | `cache.py` | `remove()` bumps `version`, `version` is read under the lock, and the timestamp check is `is None` instead of `or` | Removed tickers leave the SSE stream promptly. A timestamp of `0.0` is no longer replaced |
| 5 | `stream.py` | Build the router inside the factory (no module-level `router`), add a heartbeat comment, send `{}` when the cache empties | Calling the factory twice no longer registers the route twice. Proxies keep the connection open during quiet EOD hours |
| 6 | `simulator.py` | `dt` comes from `update_interval`. `GBMSimulator` exists from `__init__`, so `add_ticker` works before `start`. Unknown tickers get a seed that is **stable per symbol**. Tickers are trimmed and uppercased | Volatility stays right at any tick rate. No silent no-op. A made-up ticker restarts at the same price |
| 7 | new `tickers.py` | `normalize_ticker()` and `TICKER_RE` (`^[A-Z]{1,5}$`) | One rule shared by the watchlist, trade and chat paths (PLAN §6) |
| 8 | new `tracking.py` | `sync_tracking(source, t, watched=, held=)` | One place that enforces "tracked = watchlist UNION positions" |

These modules don't change: `models.py`, `interface.py` (its docstrings get a little
tighter), `seed_prices.py` and `factory.py`.

---

## 1. Requirements

From PLAN.md, with where each one is met:

| Requirement | Where |
|---|---|
| Simulator by default; Massive when `MASSIVE_API_KEY` is non-empty | `factory.py` |
| Both behind one interface; downstream code doesn't know which source is running | `interface.py`, `PriceCache` |
| GBM, ~500 ms ticks, correlated sectors, occasional 2-5% events, realistic seeds | `simulator.py`, `seed_prices.py` |
| Massive: REST polling, all tickers in one call, 15 s on the free tier | `massive_client.py` |
| Cache holds the latest price, previous price and timestamp per ticker | `cache.py`, `models.py` |
| Tracked set = watchlist UNION open positions, loaded at startup | `tracking.py`, lifespan (§13) |
| Tickers are uppercased, 1-5 letters; unknown symbols accepted (the simulator invents a price, Massive shows "--") | `tickers.py`, §8, §9 |
| `GET /api/stream/prices`: checks every ~500 ms, sends only on change, sends a JSON map of all tracked tickers | `stream.py` |
| Per-ticker SSE shape `{ticker, price, previous_price, timestamp, change, change_percent, direction}`, timestamp in Unix seconds, tick-to-tick change | `PriceUpdate.to_dict()` |
| Trades need a cached price; a held ticker with no price is valued at `avg_cost` | §13 examples |
| Single uvicorn worker (the cache lives in one process) | §16 |

---

## 2. Architecture

```
                         MASSIVE_API_KEY set?
                                │
              create_market_data_source(cache)            factory.py
                 │ yes                      │ no
                 ▼                          ▼
        MassiveDataSource           SimulatorDataSource    both implement MarketDataSource
        snapshot | EOD poller       GBM step every 0.5 s
                 │ cache.update()           │ cache.update()
                 └────────────┬─────────────┘
                              ▼
                         PriceCache                         latest PriceUpdate per ticker + version
                              │ read-only
      ┌───────────────┬───────┴───────┬────────────────┬─────────────────┐
      ▼               ▼               ▼                ▼                 ▼
 SSE stream     GET /api/portfolio  POST trade     GET /api/watchlist   chat context
 (stream.py)    (valuation)         (needs price)  (price per row)      (LLM prompt)

 Watchlist / trade / chat handlers ──sync_tracking()──► source.add_ticker / remove_ticker
```

Three rules:

1. **Producers write and consumers read.** Nothing downstream asks a source for a
   price. Everything reads `PriceCache`.
2. **One writer.** The factory picks exactly one source at startup, and it is the only
   thing that calls `cache.update()`.
3. **The source only knows a list of tickers.** *Which* tickers to track is business
   logic, and it lives in the handlers (through `sync_tracking`), not in the source.

### File layout

```
backend/app/market/
├── __init__.py          public API (import from app.market only)
├── models.py            PriceUpdate (frozen dataclass)
├── tickers.py           normalize_ticker, clean_ticker, TICKER_RE         [new]
├── cache.py             PriceCache (thread-safe, versioned)
├── interface.py         MarketDataSource (ABC)
├── seed_prices.py       seed prices, GBM params, correlation groups
├── simulator.py         GBMSimulator (maths) + SimulatorDataSource (async adapter)
├── massive_client.py    MassiveDataSource + parse_snapshots / parse_grouped
├── factory.py           create_market_data_source
├── stream.py            create_stream_router, generate_events, format_event
└── tracking.py          sync_tracking                                      [new]
```

### Threading model

Everything runs on the single asyncio event loop except one thing: Massive SDK calls,
which are synchronous (urllib3), run in `asyncio.to_thread`. `PriceCache` takes a
`threading.Lock` on every method, so it is safe whichever thread writes to it. The
simulator never leaves the event loop: one `step()` for 30 tickers takes about 70 µs.

---

## 3. `models.py`: PriceUpdate

This is the only type that leaves the market package. It is immutable, and `change`,
`change_percent` and `direction` are derived from `price` and `previous_price`, so they
can never disagree with them. Unchanged from the current code.

```python
# backend/app/market/models.py
"""Data models for market data."""

from __future__ import annotations

import time
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class PriceUpdate:
    """Immutable snapshot of a single ticker's price at a point in time."""

    ticker: str
    price: float
    previous_price: float
    timestamp: float = field(default_factory=time.time)  # Unix seconds

    @property
    def change(self) -> float:
        """Absolute price change from previous update."""
        return round(self.price - self.previous_price, 4)

    @property
    def change_percent(self) -> float:
        """Percentage change from previous update."""
        if self.previous_price == 0:
            return 0.0
        return round((self.price - self.previous_price) / self.previous_price * 100, 4)

    @property
    def direction(self) -> str:
        """'up', 'down', or 'flat'."""
        if self.price > self.previous_price:
            return "up"
        elif self.price < self.previous_price:
            return "down"
        return "flat"

    def to_dict(self) -> dict:
        """Serialize for JSON / SSE transmission."""
        return {
            "ticker": self.ticker,
            "price": self.price,
            "previous_price": self.previous_price,
            "timestamp": self.timestamp,
            "change": self.change,
            "change_percent": self.change_percent,
            "direction": self.direction,
        }
```

```python
>>> PriceUpdate("AAPL", 190.12, 190.00, 1764230400.0).to_dict()
{'ticker': 'AAPL', 'price': 190.12, 'previous_price': 190.0, 'timestamp': 1764230400.0,
 'change': 0.12, 'change_percent': 0.0632, 'direction': 'up'}
```

---

## 4. `tickers.py`: normalisation

There are two functions on purpose:

- `normalize_ticker()` validates, and raises `ValueError` for anything that isn't 1-5
  letters. Call it at **every API boundary**: the watchlist POST and DELETE, the trade
  POST, and each action from the chat. Turn the `ValueError` into a 422.
- `clean_ticker()` only trims and uppercases, and never raises. The data sources use it
  as a safety net, so a lower-case ticker that slips through still matches.

```python
# backend/app/market/tickers.py
"""Ticker symbol normalisation and validation (PLAN.md section 6)."""

from __future__ import annotations

import re

TICKER_RE = re.compile(r"^[A-Z]{1,5}$")


def clean_ticker(raw: str) -> str:
    """Trim and uppercase. Never raises; used defensively inside data sources."""
    return raw.strip().upper()


def normalize_ticker(raw: str) -> str:
    """Trim, uppercase and validate. Raises ValueError for anything but 1-5 letters.

    Call this at every API boundary (watchlist, trade, chat actions) before the
    ticker reaches the database or a data source.
    """
    ticker = clean_ticker(raw)
    if not TICKER_RE.fullmatch(ticker):
        raise ValueError(f"Invalid ticker {raw!r}: must be 1-5 letters")
    return ticker
```

```python
@router.post("/api/watchlist")
async def add_watch(body: WatchlistIn, request: Request):
    try:
        ticker = normalize_ticker(body.ticker)
    except ValueError as e:
        raise HTTPException(422, str(e))
    ...
```

(A Pydantic `field_validator` that calls `normalize_ticker` works just as well, and gives
the 422 for free.)

---

## 5. `cache.py`: PriceCache

```python
# backend/app/market/cache.py
"""Thread-safe in-memory price cache."""

from __future__ import annotations

import time
from threading import Lock

from .models import PriceUpdate


class PriceCache:
    """Latest PriceUpdate per ticker, plus a version counter for change detection.

    Writers: exactly one MarketDataSource (simulator or Massive).
    Readers: SSE stream, portfolio valuation, trade execution, chat context.
    """

    def __init__(self) -> None:
        self._prices: dict[str, PriceUpdate] = {}
        self._lock = Lock()
        self._version = 0  # bumped on every visible change (update or remove)

    def update(self, ticker: str, price: float, timestamp: float | None = None) -> PriceUpdate:
        """Record a new price. previous_price is the last cached price (== price the first time)."""
        with self._lock:
            ts = time.time() if timestamp is None else timestamp
            prev = self._prices.get(ticker)
            update = PriceUpdate(
                ticker=ticker,
                price=round(price, 2),
                previous_price=round(prev.price if prev else price, 2),
                timestamp=ts,
            )
            self._prices[ticker] = update
            self._version += 1
            return update

    def get(self, ticker: str) -> PriceUpdate | None:
        with self._lock:
            return self._prices.get(ticker)

    def get_price(self, ticker: str) -> float | None:
        update = self.get(ticker)
        return update.price if update else None

    def get_all(self) -> dict[str, PriceUpdate]:
        """Shallow copy; PriceUpdate is immutable so this is safe to hand out."""
        with self._lock:
            return dict(self._prices)

    def remove(self, ticker: str) -> None:
        """Drop a ticker. Bumps the version so SSE clients see it disappear promptly."""
        with self._lock:
            if self._prices.pop(ticker, None) is not None:
                self._version += 1

    @property
    def version(self) -> int:
        with self._lock:
            return self._version

    def __len__(self) -> int:
        with self._lock:
            return len(self._prices)

    def __contains__(self, ticker: object) -> bool:
        with self._lock:
            return ticker in self._prices
```

**Why a version counter?** The SSE loop checks it every 500 ms. A single integer
comparison tells it whether anything changed, without diffing prices. Each
`update()` bumps it, and so does each `remove()` that actually removed something.

**Why prices are rounded here.** Producers hand over full-precision floats. Rounding to
cents once, at the cache, means the SSE stream, the REST responses and the trade fills
all see the same number. The simulator keeps full precision internally, so rounding
never builds up drift.

**`previous_price`** is the price from the previous *update* for that ticker: about
500 ms ago with the simulator, and one poll ago with Massive. On a ticker's first update
it equals `price`, so `direction` is `"flat"`.

```python
cache = PriceCache()
cache.update("AAPL", 190.004)      # PriceUpdate(price=190.0, previous_price=190.0, direction='flat')
cache.update("AAPL", 190.126)      # price=190.13, previous_price=190.0, direction='up'
cache.get_price("AAPL")            # 190.13
cache.get_price("ZZZZ")            # None  -> trade returns 400, UI shows "--"
cache.remove("AAPL"); cache.version  # 3
```

---

## 6. `interface.py`: MarketDataSource

Unchanged except for its docstrings. Both implementations must meet the contract below,
and the contract test in §14.2 runs against both of them.

```python
# backend/app/market/interface.py
"""Abstract interface for market data sources."""

from __future__ import annotations

from abc import ABC, abstractmethod


class MarketDataSource(ABC):
    """Contract for market data providers.

    Implementations push price updates into a shared PriceCache on their own
    schedule. Downstream code never calls the data source directly for prices —
    it reads from the cache.

    Lifecycle:
        source = create_market_data_source(cache)
        await source.start(["AAPL", "GOOGL", ...])
        # ... app runs ...
        await source.add_ticker("TSLA")
        await source.remove_ticker("GOOGL")
        # ... app shutting down ...
        await source.stop()
    """

    @abstractmethod
    async def start(self, tickers: list[str]) -> None:
        """Begin producing price updates for the given tickers.

        Starts a background task that periodically writes to the PriceCache.
        Must be called exactly once. Calling start() twice is undefined behavior.
        """

    @abstractmethod
    async def stop(self) -> None:
        """Stop the background task and release resources.

        Safe to call multiple times. After stop(), the source will not write
        to the cache again.
        """

    @abstractmethod
    async def add_ticker(self, ticker: str) -> None:
        """Add a ticker to the active set. No-op if already present.

        The next update cycle will include this ticker.
        """

    @abstractmethod
    async def remove_ticker(self, ticker: str) -> None:
        """Remove a ticker from the active set. No-op if not present.

        Also removes the ticker from the PriceCache.
        """

    @abstractmethod
    def get_tickers(self) -> list[str]:
        """Return the current list of actively tracked tickers."""
```

| Method | Contract |
|---|---|
| `start(tickers)` | Called once. **When it returns, every ticker that can be priced is in the cache**: seeded (simulator) or after one poll (Massive). Then it starts the background task. An empty list is fine |
| `stop()` | Cancels the task and waits for it. Safe to call twice. No cache writes afterwards |
| `add_ticker(t)` | Trims and uppercases. Idempotent. The simulator prices it at once. Massive prices it at once from cached EOD data, or triggers an immediate poll in snapshot mode |
| `remove_ticker(t)` | Trims and uppercases. Idempotent. Also removes it from the cache, which bumps the version |
| `get_tickers()` | A copy of the tracked list, in insertion order |

---

## 7. `seed_prices.py`: tunables

Plain constants, unchanged. Keeping them out of `simulator.py` means you can retune the
market without touching the maths.

```python
# backend/app/market/seed_prices.py
"""Seed prices and per-ticker parameters for the market simulator."""

# Realistic starting prices for the default watchlist (as of project creation)
SEED_PRICES: dict[str, float] = {
    "AAPL": 190.00,
    "GOOGL": 175.00,
    "MSFT": 420.00,
    "AMZN": 185.00,
    "TSLA": 250.00,
    "NVDA": 800.00,
    "META": 500.00,
    "JPM": 195.00,
    "V": 280.00,
    "NFLX": 600.00,
}

# Per-ticker GBM parameters
# sigma: annualized volatility (higher = more price movement)
# mu: annualized drift / expected return
TICKER_PARAMS: dict[str, dict[str, float]] = {
    "AAPL": {"sigma": 0.22, "mu": 0.05},
    "GOOGL": {"sigma": 0.25, "mu": 0.05},
    "MSFT": {"sigma": 0.20, "mu": 0.05},
    "AMZN": {"sigma": 0.28, "mu": 0.05},
    "TSLA": {"sigma": 0.50, "mu": 0.03},  # High volatility
    "NVDA": {"sigma": 0.40, "mu": 0.08},  # High volatility, strong drift
    "META": {"sigma": 0.30, "mu": 0.05},
    "JPM": {"sigma": 0.18, "mu": 0.04},  # Low volatility (bank)
    "V": {"sigma": 0.17, "mu": 0.04},  # Low volatility (payments)
    "NFLX": {"sigma": 0.35, "mu": 0.05},
}

# Default parameters for tickers not in the list above (dynamically added)
DEFAULT_PARAMS: dict[str, float] = {"sigma": 0.25, "mu": 0.05}

# Correlation groups for the simulator's Cholesky decomposition
# Tickers in the same group have higher intra-group correlation
CORRELATION_GROUPS: dict[str, set[str]] = {
    "tech": {"AAPL", "GOOGL", "MSFT", "AMZN", "META", "NVDA", "NFLX"},
    "finance": {"JPM", "V"},
}

# Correlation coefficients
INTRA_TECH_CORR = 0.6  # Tech stocks move together
INTRA_FINANCE_CORR = 0.5  # Finance stocks move together
CROSS_GROUP_CORR = 0.3  # Between sectors / unknown tickers
TSLA_CORR = 0.3  # TSLA does its own thing
```

---

## 8. `simulator.py`: GBM simulator

### 8.1 Maths

Each tick, every price is multiplied by a log-normal factor:

```
S(t+dt) = S(t) · exp( (μ − σ²/2)·dt + σ·√dt·Z )        Z = L·z,  z ~ N(0, I),  C = L·Lᵀ
dt = update_interval / (252 · 6.5 · 3600)              = 8.48e-8 for 0.5 s
```

- `exp()` keeps prices positive, and moves scale with price.
- The `−σ²/2` term (Itô) means `E[S(t)] = S(0)·e^{μt}`, so `μ` really is the expected
  return.
- `dt` is real trading-time. For AAPL (σ = 0.22) one tick is a 1σ move of about 0.0064%
  (≈ 1.2 ¢), and one hour is about 0.54%. Prices look lively without being silly.
- **Correlation.** `C[i,j]` comes from `_pairwise_correlation`: 0.6 for two tech
  stocks, 0.5 for two finance stocks, 0.3 when either one is TSLA, and 0.3 for
  everything else, unknown tickers included. This block structure stays
  positive-definite as tickers are added (checked with 30). `L` is rebuilt only when a
  ticker is added or removed.
- **Events.** After the GBM step, each ticker has a probability `event_probability`
  (0.001) of being multiplied by `1 ± U(0.02, 0.05)`. With 10 tickers at 2 ticks/s,
  that is about one event every 50 s somewhere on the board.

### 8.2 Code

```python
# backend/app/market/simulator.py
"""GBM-based market simulator: a pure math engine plus its MarketDataSource adapter."""

from __future__ import annotations

import asyncio
import logging
import math
import random

import numpy as np

from .cache import PriceCache
from .interface import MarketDataSource
from .seed_prices import (
    CORRELATION_GROUPS,
    CROSS_GROUP_CORR,
    DEFAULT_PARAMS,
    INTRA_FINANCE_CORR,
    INTRA_TECH_CORR,
    SEED_PRICES,
    TICKER_PARAMS,
    TSLA_CORR,
)
from .tickers import clean_ticker

logger = logging.getLogger(__name__)

# 252 trading days * 6.5 h * 3600 s: one tick of `interval` seconds is interval / this.
TRADING_SECONDS_PER_YEAR = 252 * 6.5 * 3600  # 5,896,800


def seed_price_for(ticker: str) -> float:
    """Known tickers use SEED_PRICES; unknown ones get a price in [50, 300) that is
    stable per symbol (random.Random(str) seeds deterministically), so a made-up
    ticker restarts at the same price every time."""
    if ticker in SEED_PRICES:
        return SEED_PRICES[ticker]
    return round(random.Random(ticker).uniform(50.0, 300.0), 2)


class GBMSimulator:
    """Correlated Geometric Brownian Motion.

        S(t+dt) = S(t) * exp((mu - sigma^2/2) * dt + sigma * sqrt(dt) * Z)

    Z is a vector of standard normals correlated through the Cholesky factor of a
    sector-based correlation matrix. Synchronous and asyncio-free, so it is trivial to test.
    """

    DEFAULT_DT = 0.5 / TRADING_SECONDS_PER_YEAR  # ~8.48e-8 for 500 ms ticks

    def __init__(
        self,
        tickers: list[str] | None = None,
        dt: float = DEFAULT_DT,
        event_probability: float = 0.001,
    ) -> None:
        self._dt = dt
        self._sqrt_dt = math.sqrt(dt)
        self._event_prob = event_probability
        self._tickers: list[str] = []
        self._prices: dict[str, float] = {}
        self._params: dict[str, dict[str, float]] = {}
        self._cholesky: np.ndarray | None = None
        for t in tickers or []:
            self._add_internal(t)
        self._rebuild_cholesky()

    # --- public API ---

    def step(self) -> dict[str, float]:
        """Advance every ticker one tick. Returns {ticker: price rounded to 2 dp}."""
        n = len(self._tickers)
        if n == 0:
            return {}
        z = np.random.standard_normal(n)
        if self._cholesky is not None:
            z = self._cholesky @ z

        out: dict[str, float] = {}
        for i, ticker in enumerate(self._tickers):
            mu, sigma = self._params[ticker]["mu"], self._params[ticker]["sigma"]
            drift = (mu - 0.5 * sigma * sigma) * self._dt
            self._prices[ticker] *= math.exp(drift + sigma * self._sqrt_dt * z[i])

            if random.random() < self._event_prob:  # idiosyncratic "news" shock
                shock = random.uniform(0.02, 0.05) * random.choice((-1, 1))
                self._prices[ticker] *= 1 + shock
                logger.debug("Simulator event on %s: %+.1f%%", ticker, shock * 100)

            # Internal price keeps full precision; only the output is rounded.
            out[ticker] = round(self._prices[ticker], 2)
        return out

    def add_ticker(self, ticker: str) -> None:
        if ticker in self._prices:
            return
        self._add_internal(ticker)
        self._rebuild_cholesky()

    def remove_ticker(self, ticker: str) -> None:
        if ticker not in self._prices:
            return
        self._tickers.remove(ticker)
        del self._prices[ticker]
        del self._params[ticker]
        self._rebuild_cholesky()

    def get_price(self, ticker: str) -> float | None:
        return self._prices.get(ticker)

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    # --- internals ---

    def _add_internal(self, ticker: str) -> None:
        if ticker in self._prices:
            return
        self._tickers.append(ticker)
        self._prices[ticker] = seed_price_for(ticker)
        self._params[ticker] = dict(TICKER_PARAMS.get(ticker, DEFAULT_PARAMS))

    def _rebuild_cholesky(self) -> None:
        """O(n^2) to fill, run only on add/remove (n stays well under 50)."""
        n = len(self._tickers)
        if n <= 1:
            self._cholesky = None
            return
        corr = np.eye(n)
        for i in range(n):
            for j in range(i + 1, n):
                rho = self._pairwise_correlation(self._tickers[i], self._tickers[j])
                corr[i, j] = corr[j, i] = rho
        self._cholesky = np.linalg.cholesky(corr)

    @staticmethod
    def _pairwise_correlation(t1: str, t2: str) -> float:
        if t1 == "TSLA" or t2 == "TSLA":
            return TSLA_CORR  # in the tech set, but does its own thing
        tech, finance = CORRELATION_GROUPS["tech"], CORRELATION_GROUPS["finance"]
        if t1 in tech and t2 in tech:
            return INTRA_TECH_CORR
        if t1 in finance and t2 in finance:
            return INTRA_FINANCE_CORR
        return CROSS_GROUP_CORR


class SimulatorDataSource(MarketDataSource):
    """Steps a GBMSimulator every `update_interval` seconds and writes to the cache."""

    def __init__(
        self,
        price_cache: PriceCache,
        update_interval: float = 0.5,
        event_probability: float = 0.001,
    ) -> None:
        self._cache = price_cache
        self._interval = update_interval
        # dt always matches the real tick length, so volatility is right at any rate.
        self._sim = GBMSimulator(
            dt=update_interval / TRADING_SECONDS_PER_YEAR,
            event_probability=event_probability,
        )
        self._task: asyncio.Task | None = None

    async def start(self, tickers: list[str]) -> None:
        for t in tickers:
            await self.add_ticker(t)  # seeds the cache, so SSE has data at once
        self._task = asyncio.create_task(self._run_loop(), name="simulator-loop")
        logger.info("Simulator started with %d tickers", len(self._sim.get_tickers()))

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def add_ticker(self, ticker: str) -> None:
        ticker = clean_ticker(ticker)
        if ticker in self._sim.get_tickers():
            return
        self._sim.add_ticker(ticker)
        # Price is available immediately: a chat reply can add a ticker and trade it.
        self._cache.update(ticker, self._sim.get_price(ticker))

    async def remove_ticker(self, ticker: str) -> None:
        ticker = clean_ticker(ticker)
        self._sim.remove_ticker(ticker)
        self._cache.remove(ticker)

    def get_tickers(self) -> list[str]:
        return self._sim.get_tickers()

    async def _run_loop(self) -> None:
        while True:
            try:
                for ticker, price in self._sim.step().items():
                    self._cache.update(ticker, price)
            except Exception:
                logger.exception("Simulator step failed")  # never let the loop die
            await asyncio.sleep(self._interval)
```

### 8.3 Usage

```python
sim = GBMSimulator(["AAPL", "MSFT"])
sim.step()                 # {'AAPL': 190.01, 'MSFT': 419.97}
sim.add_ticker("PYPL")     # seed_price_for("PYPL") -> same value every run
sim.get_price("AAPL")      # 190.0137...  (unrounded internal price)

src = SimulatorDataSource(cache)                  # 0.5 s ticks
src = SimulatorDataSource(cache, update_interval=0.25, event_probability=0.0)  # calm, fast
await src.start(["AAPL", "MSFT"])                 # cache already has both on return
```

### 8.4 Tuning

| Want | Change |
|---|---|
| Calmer or wilder market | `sigma` in `TICKER_PARAMS` / `DEFAULT_PARAMS` |
| More or less drama | `event_probability` |
| Faster ticks | `update_interval` (`dt` follows it automatically) |
| Stronger sector moves | `INTRA_TECH_CORR` and friends (keep them < 1 so `C` stays positive-definite) |
| Repeatable tests | `np.random.seed(n)` (diffusion) and `random.seed(n)` (events) |

Known quirk, accepted: a cheap, low-σ ticker can show several `"flat"` ticks in a row,
because its per-tick move is under half a cent.

---

## 9. `massive_client.py`: Massive poller

### 9.1 What the API allows

| Plan | Snapshot endpoint | What we use | Interval | Calls per poll |
|---|---|---|---|---|
| Basic (free, 5 calls/min) | **403** | Grouped daily bars (`/v2/aggs/grouped/...`): the last close for **every** US stock | 900 s (`eod_poll_interval`) | 1 (up to 4 while it walks back over a weekend) |
| Starter / Developer (15-min delayed) | yes | Full Market Snapshot for our tickers | 15 s | 1 |
| Advanced / Business (real-time) | yes | Full Market Snapshot | 2-5 s if you want | 1 |

One poll is **one HTTP call for all tracked tickers**, whatever the plan.

### 9.2 Mode detection

```
            first poll with ≥1 ticker
                       │
          get_snapshot_all(tickers)
           ok │                │ BadResponse (403 on free plan)
              ▼                ▼
          SNAPSHOT  ◄────     EOD  (permanent for this process)
              │ later BadResponse/network error:
              └─ log, keep mode, retry next tick
```

- The mode is `None` until the first poll that has tickers. The app **never** calls the
  snapshot endpoint with an empty list, because the SDK would then return the whole
  market (10k+ rows).
- After a successful snapshot, an error is treated as transient (a 429, a network
  blip). Only a failure on the *first* attempt means "this plan has no snapshots".

### 9.3 Parsing (field names checked against SDK 2.2.0)

| Source | Price | Timestamp → Unix seconds |
|---|---|---|
| `TickerSnapshot` | `last_trade.price` → `day.close` → `prev_day.close` | `last_trade.sip_timestamp / 1e9` → `updated / 1e9` → now |
| `GroupedDailyAgg` | `close` | `timestamp / 1e3` |

**Nanoseconds** for trades and snapshots, **milliseconds** for aggregates. Mixing them up
is the commonest bug here. An unknown symbol is simply missing from the response, so it
never gets a cache entry, and the UI shows "--".

### 9.4 Code

```python
# backend/app/market/massive_client.py
"""Massive (formerly Polygon.io) REST poller."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, timedelta

from massive import RESTClient
from massive.exceptions import BadResponse
from massive.rest.models import SnapshotMarketType

from .cache import PriceCache
from .interface import MarketDataSource
from .tickers import clean_ticker

logger = logging.getLogger(__name__)

Quote = tuple[float, float]  # (price, unix_seconds)

SNAPSHOT = "snapshot"  # paid plans: Full Market Snapshot, near-live prices
EOD = "eod"  # free Basic plan: grouped daily bars, latest close
EOD_LOOKBACK_DAYS = 4  # covers a long weekend; at most 4 calls (free limit is 5/min)


def parse_snapshots(snaps: list) -> dict[str, Quote]:
    """TickerSnapshot list -> {ticker: (price, ts)}. Rows with no usable price are skipped.

    Price: last_trade.price, else day.close, else prev_day.close.
    Time:  last_trade.sip_timestamp (ns), else updated (ns), else now.
    """
    out: dict[str, Quote] = {}
    now = time.time()
    for s in snaps:
        if not s.ticker:
            continue
        lt = s.last_trade
        if lt is not None and lt.price:
            ts = lt.sip_timestamp or s.updated
            out[s.ticker] = (lt.price, ts / 1e9 if ts else now)
        elif s.day is not None and s.day.close:
            out[s.ticker] = (s.day.close, s.updated / 1e9 if s.updated else now)
        elif s.prev_day is not None and s.prev_day.close:
            out[s.ticker] = (s.prev_day.close, now)
    return out


def parse_grouped(rows: list) -> dict[str, Quote]:
    """GroupedDailyAgg list -> {ticker: (close, ts)}. Aggregate timestamps are ms."""
    now = time.time()
    return {
        r.ticker: (r.close, r.timestamp / 1e3 if r.timestamp else now)
        for r in rows
        if r.ticker and r.close
    }


class MassiveDataSource(MarketDataSource):
    """Polls Massive once per interval for ALL tracked tickers in a single API call.

    Mode is detected on the first poll that has tickers:
      * SNAPSHOT works       -> stay in snapshot mode (paid plans)
      * SNAPSHOT BadResponse -> switch for good to EOD mode (free plan returns 403)
    """

    def __init__(
        self,
        api_key: str,
        price_cache: PriceCache,
        poll_interval: float = 15.0,
        eod_poll_interval: float = 900.0,
        client: RESTClient | None = None,  # injectable for tests
    ) -> None:
        self._api_key = api_key
        self._cache = price_cache
        self._interval = poll_interval
        self._eod_interval = eod_poll_interval
        self._client = client
        self._tickers: list[str] = []
        self._mode: str | None = None  # None until detected
        self._eod_date: date | None = None  # trading day of the cached grouped bars
        self._eod_quotes: dict[str, Quote] = {}  # every US ticker's close for that day
        self._last: dict[str, Quote] = {}  # last value written per ticker (dedupe)
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None

    @property
    def mode(self) -> str | None:
        return self._mode

    # --- MarketDataSource ---

    async def start(self, tickers: list[str]) -> None:
        if self._client is None:
            # retries=0: the SDK's own 429/5xx retries would burn the 5 calls/min budget;
            # the poll loop already retries on its next tick.
            self._client = RESTClient(api_key=self._api_key, retries=0)
        self._tickers = list(dict.fromkeys(clean_ticker(t) for t in tickers))
        await self._poll_once()  # fill the cache before the app starts serving
        self._task = asyncio.create_task(self._poll_loop(), name="massive-poller")
        logger.info("Massive poller started: %d tickers, mode=%s", len(self._tickers), self._mode)

    async def stop(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self._task = None

    async def add_ticker(self, ticker: str) -> None:
        ticker = clean_ticker(ticker)
        if ticker in self._tickers:
            return
        self._tickers.append(ticker)
        if self._mode == EOD:
            # Grouped bars already hold every US ticker: price it now, no API call.
            if ticker in self._eod_quotes:
                self._write(ticker, *self._eod_quotes[ticker])
        else:
            self._wake.set()  # snapshot (or undetected) mode: poll now, not in 15s

    async def remove_ticker(self, ticker: str) -> None:
        ticker = clean_ticker(ticker)
        self._tickers = [t for t in self._tickers if t != ticker]
        self._last.pop(ticker, None)
        self._cache.remove(ticker)

    def get_tickers(self) -> list[str]:
        return list(self._tickers)

    # --- polling ---

    async def _poll_loop(self) -> None:
        while True:
            interval = self._eod_interval if self._mode == EOD else self._interval
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=interval)
            except TimeoutError:
                pass
            self._wake.clear()
            await self._poll_once()

    async def _poll_once(self) -> None:
        tickers = list(self._tickers)  # snapshot: the worker thread must not see mutations
        if not tickers:
            return  # never call the snapshot endpoint with no tickers (= whole market)
        try:
            quotes = await asyncio.to_thread(self._fetch, tickers)
        except Exception as e:  # BadResponse (401/403/429), network errors, ...
            logger.error("Massive poll failed (mode=%s): %s", self._mode, e)
            return
        for ticker, (price, ts) in quotes.items():
            if ticker in self._tickers:  # may have been removed while we were fetching
                self._write(ticker, price, ts)

    def _write(self, ticker: str, price: float, ts: float) -> None:
        if self._last.get(ticker) == (price, ts):
            return  # unchanged (market closed, EOD): no version bump, no SSE event
        self._last[ticker] = (price, ts)
        self._cache.update(ticker, price, ts)

    # --- synchronous SDK calls (run in a worker thread) ---

    def _fetch(self, tickers: list[str]) -> dict[str, Quote]:
        if self._mode in (None, SNAPSHOT):
            try:
                quotes = self._fetch_snapshot(tickers)
                self._mode = SNAPSHOT
                return quotes
            except BadResponse as e:
                if self._mode == SNAPSHOT:
                    raise  # worked before, so this is transient; retry next tick
                logger.warning("Snapshot endpoint unavailable (%s); using end-of-day mode", e)
                self._mode = EOD
        return self._fetch_eod(tickers)

    def _fetch_snapshot(self, tickers: list[str]) -> dict[str, Quote]:
        snaps = self._client.get_snapshot_all(SnapshotMarketType.STOCKS, tickers=tickers)
        return parse_snapshots(snaps)

    def _fetch_eod(self, tickers: list[str]) -> dict[str, Quote]:
        """Walk back from yesterday to the newest trading day we don't have yet.

        Weekends/holidays return [] and a day that hasn't closed returns 403; both mean
        "try the day before". Stops at the day already cached, so a steady-state poll
        costs 1 call.
        """
        day = date.today() - timedelta(days=1)
        for _ in range(EOD_LOOKBACK_DAYS):
            if self._eod_date is not None and day <= self._eod_date:
                break
            try:
                rows = self._client.get_grouped_daily_aggs(day.isoformat(), adjusted=True)
            except BadResponse as e:
                logger.debug("Grouped daily %s unavailable: %s", day, e)
                rows = []
            if rows:
                self._eod_date, self._eod_quotes = day, parse_grouped(rows)
                break
            day -= timedelta(days=1)
        return {t: self._eod_quotes[t] for t in tickers if t in self._eod_quotes}
```

### 9.5 Notable behaviour

- **`retries=0`.** The SDK retries 429 and 5xx three times by default. On the free plan
  that turns one failed poll into four calls out of a budget of five a minute. The poll
  loop is already a retry.
- **Immediate pricing on `add_ticker`.** In EOD mode, grouped bars already hold every US
  ticker, so a ticker added by the watchlist or the AI gets a price with no API call. In
  snapshot mode, `_wake` cuts the sleep short and polls at once (paid plans have no
  per-minute cap). PLAN.md §9 says "a ticker added in the same response may have no
  price yet". That is still true for the brief moment before the poll lands, so a chat
  trade in the same reply can still fail with "no price", as designed.
- **Dedupe.** While the market is closed, and always in EOD mode, every poll returns
  the same `(price, ts)`. `_write` skips those, so the SSE stream stays quiet instead of
  resending identical payloads. The heartbeat in §11 keeps the connection open.
- **Thread safety.** `_fetch` runs in a worker thread with a *copy* of the ticker list,
  and results for tickers removed during the call are dropped. `_mode`, `_eod_*` and
  `_last` are only written by the poll path, which never runs twice at once.
- **EOD date.** `date.today()` uses the container's local date (UTC in Docker). Asking
  for a day that hasn't closed yet just returns 403, and the walk-back goes on to the
  day before, so the timezone doesn't matter for correctness.

### 9.6 Usage

```python
src = MassiveDataSource(api_key, cache)                     # 15 s snapshot / 900 s EOD
src = MassiveDataSource(api_key, cache, poll_interval=3.0)  # real-time paid plan
await src.start(["AAPL", "MSFT"])
src.mode            # "snapshot" or "eod"
```

---

## 10. `factory.py`: picking a source

Unchanged. An empty or whitespace-only key means the simulator. The source it returns
has not been started.

```python
# backend/app/market/factory.py
"""Factory for creating market data sources."""

from __future__ import annotations

import logging
import os

from .cache import PriceCache
from .interface import MarketDataSource
from .massive_client import MassiveDataSource
from .simulator import SimulatorDataSource

logger = logging.getLogger(__name__)


def create_market_data_source(price_cache: PriceCache) -> MarketDataSource:
    """Create the appropriate market data source based on environment variables.

    - MASSIVE_API_KEY set and non-empty → MassiveDataSource (real market data)
    - Otherwise → SimulatorDataSource (GBM simulation)

    Returns an unstarted source. Caller must await source.start(tickers).
    """
    api_key = os.environ.get("MASSIVE_API_KEY", "").strip()

    if api_key:
        logger.info("Market data source: Massive API (real data)")
        return MassiveDataSource(api_key=api_key, price_cache=price_cache)
    else:
        logger.info("Market data source: GBM Simulator")
        return SimulatorDataSource(price_cache=price_cache)
```

---

## 11. `stream.py`: SSE endpoint

```python
# backend/app/market/stream.py
"""SSE endpoint: GET /api/stream/prices."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from .cache import PriceCache

logger = logging.getLogger(__name__)

POLL_INTERVAL = 0.5  # how often the cache version is checked
HEARTBEAT_EVERY = 15.0  # idle comment so proxies don't drop a quiet stream (Massive EOD)


def create_stream_router(price_cache: PriceCache) -> APIRouter:
    """A fresh router per call, so tests can build several apps safely."""
    router = APIRouter(prefix="/api/stream", tags=["streaming"])

    @router.get("/prices")
    async def stream_prices(request: Request) -> StreamingResponse:
        return StreamingResponse(
            generate_events(price_cache, request),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return router


def format_event(price_cache: PriceCache) -> str:
    """One SSE frame with every tracked ticker (PLAN.md section 6 shape)."""
    data = {t: u.to_dict() for t, u in price_cache.get_all().items()}
    return f"data: {json.dumps(data)}\n\n"


async def generate_events(
    price_cache: PriceCache,
    request: Request,
    interval: float = POLL_INTERVAL,
) -> AsyncGenerator[str, None]:
    yield "retry: 1000\n\n"  # EventSource reconnects 1 s after a drop
    last_version = -1
    idle = 0.0
    try:
        while not await request.is_disconnected():
            version = price_cache.version
            if version != last_version:
                last_version = version
                yield format_event(price_cache)  # may be "{}" after the last ticker is removed
                idle = 0.0
            elif idle >= HEARTBEAT_EVERY:
                yield ": ping\n\n"
                idle = 0.0
            await asyncio.sleep(interval)
            idle += interval
    except asyncio.CancelledError:
        pass
```

### What goes over the wire

```
retry: 1000

data: {"AAPL": {"ticker": "AAPL", "price": 190.13, "previous_price": 190.0, "timestamp": 1764230400.51, "change": 0.13, "change_percent": 0.0684, "direction": "up"}, "MSFT": {...}}

data: {"AAPL": {...}, "MSFT": {...}}

: ping
```

- The first frame is sent as soon as a client connects (`last_version = -1`), so a new
  page gets every price straight away.
- Each `data:` frame is the **full** map of tracked tickers, not a diff. A client
  doesn't need to merge anything: it replaces its state, which makes reconnects simple.
  A ticker missing from the map is no longer tracked.
- With the simulator, that is one frame every 500 ms (about 1.5 KB for 10 tickers).
  With Massive, there is one frame per poll that brought something new.
- `: ping` is an SSE comment. `EventSource` ignores it.

### Client side (frontend reference)

```ts
const es = new EventSource("/api/stream/prices");
es.onopen = () => setStatus("connected");
es.onerror = () => setStatus(es.readyState === EventSource.CLOSED ? "disconnected" : "reconnecting");
es.onmessage = (e) => {
  const prices: Record<string, PriceUpdate> = JSON.parse(e.data);
  // flash on direction, append to the sparkline buffer (cap 300), recompute total value
};
```

---

## 12. `tracking.py`: watchlist UNION positions

```python
# backend/app/market/tracking.py
"""Keeping the tracked set equal to watchlist UNION open positions (PLAN.md section 6)."""

from __future__ import annotations

from .interface import MarketDataSource


async def sync_tracking(source: MarketDataSource, ticker: str, *, watched: bool, held: bool) -> None:
    """Call after any change that can affect `ticker`'s tracked status.

    `watched` / `held` are the state AFTER the change, read from the database in
    the same request. Both add_ticker and remove_ticker are idempotent, so calling
    this when nothing changed is harmless.
    """
    if watched or held:
        await source.add_ticker(ticker)
    else:
        await source.remove_ticker(ticker)
```

Call it **after** the database write, passing the post-change state:

| Event | `watched` | `held` | Effect |
|---|---|---|---|
| Watchlist add `T` | `True` | any | tracked (add_ticker is idempotent) |
| Watchlist remove `T`, no position | `False` | `False` | untracked, leaves the cache and the SSE stream |
| Watchlist remove `T`, position open | `False` | `True` | stays tracked, so P&L keeps updating |
| Buy `T` (not watched) | any | `True` | tracked (it already was, since a trade needs a price) |
| Sell closes `T`, still watched | `True` | `False` | stays tracked |
| Sell closes `T`, not watched | `False` | `False` | untracked |

The startup set is computed the same way, in the lifespan (§13.1).

---

## 13. FastAPI integration and usage examples

The database helpers named here (`init_db`, `get_watchlist_tickers`,
`get_position_tickers`, `is_watched`, `get_position`, ...) belong to the backend agent.
Only their meaning matters to the market package.

### 13.1 Lifespan and app wiring (`backend/app/main.py`)

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.market import PriceCache, create_market_data_source, create_stream_router
from app import db

price_cache = PriceCache()  # module-level so routers can be built before startup


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()                                      # 1. tables + seed (PLAN §7)
    tracked = list(dict.fromkeys(                     # 2. watchlist UNION positions
        db.get_watchlist_tickers() + db.get_position_tickers()
    ))
    source = create_market_data_source(price_cache)   # 3. simulator or Massive
    await source.start(tracked)                       #    cache is filled on return
    app.state.price_cache = price_cache
    app.state.market = source
    snapshot_task = start_snapshot_task(app)          # 4. 60 s portfolio snapshots (backend)
    try:
        yield
    finally:
        snapshot_task.cancel()
        await source.stop()


app = FastAPI(lifespan=lifespan)
app.include_router(create_stream_router(price_cache))
# app.include_router(portfolio_router); app.include_router(watchlist_router); ...
# app.mount("/", StaticFiles(directory="static", html=True))   # last, after /api routes
```

### 13.2 Dependencies

```python
from fastapi import Request

from app.market import MarketDataSource, PriceCache


def get_cache(request: Request) -> PriceCache:
    return request.app.state.price_cache


def get_market(request: Request) -> MarketDataSource:
    return request.app.state.market
```

### 13.3 Watchlist endpoints

```python
@router.get("/api/watchlist")
def list_watchlist(cache: PriceCache = Depends(get_cache)):
    return [{"ticker": t, "price": cache.get_price(t)} for t in db.get_watchlist_tickers()]


@router.post("/api/watchlist")
async def add_watch(body: WatchlistIn, cache=Depends(get_cache), market=Depends(get_market)):
    ticker = normalize_ticker(body.ticker)            # ValueError -> 422 (validator)
    db.add_watchlist(ticker)                          # INSERT OR IGNORE: idempotent
    await sync_tracking(market, ticker, watched=True, held=db.get_position(ticker) is not None)
    return {"ticker": ticker, "price": cache.get_price(ticker)}  # None with Massive until polled


@router.delete("/api/watchlist/{ticker}", status_code=204)
async def remove_watch(ticker: str, market=Depends(get_market)):
    ticker = normalize_ticker(ticker)
    db.remove_watchlist(ticker)                       # no-op if absent
    await sync_tracking(market, ticker, watched=False, held=db.get_position(ticker) is not None)
```

### 13.4 Trade execution: reading a price

```python
async def execute_trade(ticker: str, side: str, quantity: float, cache, market) -> dict:
    ticker = normalize_ticker(ticker)
    price = cache.get_price(ticker)                   # read ONCE: this is the fill price
    if price is None:
        raise TradeError(f"No price available for {ticker}")      # -> 400
    trade = db.apply_trade(ticker, side, round(quantity, 6), price)  # one transaction (PLAN §7)
    await sync_tracking(
        market, ticker,
        watched=db.is_watched(ticker),
        held=db.get_position(ticker) is not None,
    )
    return trade
```

Read the price once and pass it down. Don't call `get_price` again inside the
transaction, or the cash check and the fill can see different prices.

### 13.5 Portfolio valuation

```python
def value_portfolio(cache: PriceCache) -> dict:
    cash = db.get_cash()
    rows, total = [], cash
    for p in db.get_positions():
        current = cache.get_price(p.ticker)
        mark = current if current is not None else p.avg_cost   # PLAN §7: fall back to avg_cost
        mv = p.quantity * mark
        pnl = (mark - p.avg_cost) * p.quantity
        total += mv
        rows.append({
            "ticker": p.ticker, "quantity": p.quantity, "avg_cost": p.avg_cost,
            "current_price": current, "market_value": round(mv, 2),
            "unrealized_pnl": round(pnl, 2),
            "pnl_percent": round((mark / p.avg_cost - 1) * 100, 2) if p.avg_cost else 0.0,
        })
    return {"cash_balance": round(cash, 2), "total_value": round(total, 2),
            "unrealized_pnl": round(sum(r["unrealized_pnl"] for r in rows), 2),
            "positions": rows}
```

The 60 s snapshot task calls the same function and stores `total_value`.

### 13.6 Chat context

```python
def market_context(cache: PriceCache) -> str:
    lines = [f"{t}: {'--' if (u := cache.get(t)) is None else f'${u.price:.2f}'}"
             for t in db.get_watchlist_tickers()]
    return "Watchlist prices:\n" + "\n".join(lines)
```

Chat actions run in the order PLAN §9 sets: watchlist changes first, then trades. Each
watchlist action calls `sync_tracking`. With the simulator, that prices a new ticker at
once, so a trade on it in the same reply succeeds. With Massive, it may still fail with
"no price", as described above.

---

## 14. Tests

Put these in `backend/tests/market/`. `asyncio_mode = "auto"` is already set in
`pyproject.toml`, so `async def test_*` works without decorators. Run them with
`cd backend && uv run pytest tests/market -q`.

### 14.1 Massive, using real SDK models (replaces the `MagicMock` snapshots)

Build fixtures with `TickerSnapshot.from_dict` and `GroupedDailyAgg.from_dict`, so a
wrong attribute name fails the test instead of quietly passing, which is how bug #1
slipped through.

```python
# backend/tests/market/test_massive_v2.py
"""MassiveDataSource tests built from real SDK models (attribute names are checked)."""

from datetime import date, timedelta
from unittest.mock import MagicMock

from massive.exceptions import BadResponse
from massive.rest.models import GroupedDailyAgg, TickerSnapshot

from app.market.cache import PriceCache
from app.market.massive_client import EOD, SNAPSHOT, MassiveDataSource

NS = 1_700_000_000_000_000_000  # 2023-11-14 in nanoseconds


def snap(ticker, price, ts_ns=NS):
    return TickerSnapshot.from_dict(
        {"ticker": ticker, "updated": ts_ns, "day": {"c": price},
         "lastTrade": {"p": price, "t": ts_ns}}
    )


def bar(ticker, close, ts_ms=1_700_000_000_000):
    return GroupedDailyAgg.from_dict({"T": ticker, "c": close, "t": ts_ms})


def make_source(client):
    cache = PriceCache()
    return cache, MassiveDataSource("k", cache, poll_interval=60, client=client)


async def test_snapshot_mode_fills_cache_with_seconds_timestamps():
    client = MagicMock()
    client.get_snapshot_all.return_value = [snap("AAPL", 190.5), snap("MSFT", 420.0)]
    cache, src = make_source(client)
    await src.start(["aapl", "MSFT"])
    try:
        assert src.mode == SNAPSHOT
        assert cache.get_price("AAPL") == 190.5
        assert cache.get("AAPL").timestamp == NS / 1e9
        client.get_snapshot_all.assert_called_once()
    finally:
        await src.stop()


async def test_snapshot_fallbacks_and_unknown_ticker():
    rows = [
        TickerSnapshot.from_dict({"ticker": "A", "day": {"c": 10.0}, "updated": NS}),
        TickerSnapshot.from_dict({"ticker": "B", "prevDay": {"c": 20.0}}),
        TickerSnapshot.from_dict({"ticker": "C"}),  # nothing usable
    ]
    client = MagicMock()
    client.get_snapshot_all.return_value = rows
    cache, src = make_source(client)
    await src.start(["A", "B", "C", "ZZZZ"])
    await src.stop()
    assert cache.get_price("A") == 10.0
    assert cache.get_price("B") == 20.0
    assert cache.get_price("C") is None and cache.get_price("ZZZZ") is None


async def test_free_plan_switches_to_eod_and_walks_back():
    client = MagicMock()
    client.get_snapshot_all.side_effect = BadResponse('{"status":"NOT_AUTHORIZED"}')
    yesterday = (date.today() - timedelta(days=1)).isoformat()

    def grouped(day, adjusted=True):
        if day == yesterday:
            raise BadResponse("403 not yet available")
        return [bar("AAPL", 225.0), bar("PYPL", 70.0)]

    client.get_grouped_daily_aggs.side_effect = grouped
    cache, src = make_source(client)
    await src.start(["AAPL"])
    try:
        assert src.mode == EOD
        assert cache.get_price("AAPL") == 225.0
        assert client.get_grouped_daily_aggs.call_count == 2
        await src.add_ticker("PYPL")  # served from the cached grouped bars, no API call
        assert cache.get_price("PYPL") == 70.0
        assert client.get_grouped_daily_aggs.call_count == 2
    finally:
        await src.stop()


async def test_failed_poll_keeps_cache_and_mode():
    client = MagicMock()
    client.get_snapshot_all.return_value = [snap("AAPL", 190.0)]
    cache, src = make_source(client)
    await src.start(["AAPL"])
    await src.stop()
    client.get_snapshot_all.side_effect = BadResponse("429")
    await src._poll_once()
    assert src.mode == SNAPSHOT and cache.get_price("AAPL") == 190.0


async def test_unchanged_quote_does_not_bump_version():
    client = MagicMock()
    client.get_snapshot_all.return_value = [snap("AAPL", 190.0)]
    cache, src = make_source(client)
    await src.start(["AAPL"])
    await src.stop()
    v = cache.version
    await src._poll_once()
    assert cache.version == v


async def test_empty_ticker_list_never_calls_api():
    client = MagicMock()
    _, src = make_source(client)
    await src.start([])
    await src.stop()
    client.get_snapshot_all.assert_not_called()


async def test_add_ticker_in_snapshot_mode_polls_immediately():
    import asyncio
    client = MagicMock()
    client.get_snapshot_all.side_effect = lambda *a, tickers, **k: [snap(t, 100.0) for t in tickers]
    cache, src = make_source(client)
    await src.start(["AAPL"])
    try:
        await src.add_ticker("NVDA")
        for _ in range(50):
            if "NVDA" in cache:
                break
            await asyncio.sleep(0.01)
        assert cache.get_price("NVDA") == 100.0
    finally:
        await src.stop()
```

### 14.2 Contract test (both sources)

```python
# backend/tests/market/test_contract.py
"""The same contract, run against both MarketDataSource implementations."""

from unittest.mock import MagicMock

import pytest
from massive.rest.models import TickerSnapshot

from app.market.cache import PriceCache
from app.market.massive_client import MassiveDataSource
from app.market.simulator import SimulatorDataSource


def _fake_massive_client():
    client = MagicMock()
    client.get_snapshot_all.side_effect = lambda *a, tickers, **k: [
        TickerSnapshot.from_dict({"ticker": t, "lastTrade": {"p": 100.0, "t": 1}}) for t in tickers
    ]
    return client


@pytest.fixture(params=["simulator", "massive"])
def source_and_cache(request):
    cache = PriceCache()
    if request.param == "simulator":
        return SimulatorDataSource(cache, update_interval=0.05), cache
    return MassiveDataSource("k", cache, poll_interval=60, client=_fake_massive_client()), cache


async def test_contract(source_and_cache):
    source, cache = source_and_cache
    await source.start(["AAPL", "MSFT"])
    try:
        assert set(source.get_tickers()) == {"AAPL", "MSFT"}
        assert cache.get_price("AAPL") is not None  # priced right after start()

        await source.add_ticker("aapl")  # normalised + idempotent
        assert source.get_tickers().count("AAPL") == 1

        await source.remove_ticker("MSFT")
        assert "MSFT" not in source.get_tickers() and "MSFT" not in cache
        await source.remove_ticker("MSFT")  # no-op
    finally:
        await source.stop()
        await source.stop()  # safe twice
```

### 14.3 SSE generator (no server needed)

```python
# backend/tests/market/test_stream_v2.py
import json

from app.market.cache import PriceCache
from app.market.stream import create_stream_router, generate_events


class FakeRequest:
    def __init__(self, polls_before_disconnect):
        self._left = polls_before_disconnect

    async def is_disconnected(self):
        self._left -= 1
        return self._left < 0


async def test_events_only_on_version_change():
    cache = PriceCache()
    cache.update("AAPL", 190.0)
    frames = [f async for f in generate_events(cache, FakeRequest(3), interval=0)]
    assert frames[0] == "retry: 1000\n\n"
    data = [f for f in frames if f.startswith("data: ")]
    assert len(data) == 1  # three polls, one version
    aapl = json.loads(data[0][6:])["AAPL"]
    assert set(aapl) == {"ticker", "price", "previous_price", "timestamp",
                         "change", "change_percent", "direction"}


async def test_remove_triggers_event():
    cache = PriceCache()
    cache.update("AAPL", 190.0)
    gen = generate_events(cache, FakeRequest(10), interval=0)
    await gen.__anext__()          # retry
    await gen.__anext__()          # initial data
    cache.remove("AAPL")
    assert await gen.__anext__() == "data: {}\n\n"
    await gen.aclose()


def test_router_factory_is_independent():
    r1, r2 = create_stream_router(PriceCache()), create_stream_router(PriceCache())
    assert r1 is not r2 and len(r1.routes) == len(r2.routes) == 1
```

### 14.4 Simulator statistics and normalisation

```python
# backend/tests/market/test_sim_v2.py
import math
import random

import numpy as np
import pytest

from app.market.cache import PriceCache
from app.market.simulator import (
    TRADING_SECONDS_PER_YEAR,
    GBMSimulator,
    SimulatorDataSource,
    seed_price_for,
)
from app.market.tickers import normalize_ticker


def _log_returns(sim: GBMSimulator, steps: int) -> dict[str, np.ndarray]:
    hist = {t: [sim.get_price(t)] for t in sim.get_tickers()}
    for _ in range(steps):
        sim.step()
        for t in hist:
            hist[t].append(sim.get_price(t))
    return {t: np.diff(np.log(v)) for t, v in hist.items()}


def test_gbm_volatility_matches_theory():
    np.random.seed(42)
    sim = GBMSimulator(["AAPL"], event_probability=0.0)
    rets = _log_returns(sim, 20_000)["AAPL"]
    expected = 0.22 * math.sqrt(GBMSimulator.DEFAULT_DT)
    assert abs(np.std(rets) / expected - 1) < 0.03


def test_tech_pair_more_correlated_than_cross_sector():
    np.random.seed(1)
    r = _log_returns(GBMSimulator(["AAPL", "MSFT", "JPM"], event_probability=0.0), 5000)
    assert 0.5 < np.corrcoef(r["AAPL"], r["MSFT"])[0, 1] < 0.7  # target 0.6
    assert 0.2 < np.corrcoef(r["AAPL"], r["JPM"])[0, 1] < 0.4  # target 0.3


def test_event_probability_one_gives_2_to_5_percent_move():
    random.seed(0)
    sim = GBMSimulator(["AAPL"], event_probability=1.0)
    p0 = sim.get_price("AAPL")
    sim.step()
    assert 0.019 < abs(sim.get_price("AAPL") / p0 - 1) < 0.051


def test_unknown_seed_is_stable_and_in_range():
    assert seed_price_for("ZZZZ") == seed_price_for("ZZZZ")
    assert 50 <= seed_price_for("ZZZZ") < 300
    assert seed_price_for("AAPL") == 190.0


def test_cholesky_holds_for_30_tickers():
    tickers = ["AAPL", "GOOGL", "MSFT", "AMZN", "TSLA", "NVDA", "META", "JPM", "V", "NFLX"]
    tickers += [f"X{c}" for c in "ABCDEFGHIJKLMNOPQRST"]
    assert GBMSimulator(tickers)._cholesky.shape == (30, 30)


async def test_dt_follows_interval_and_add_before_start_works():
    src = SimulatorDataSource(PriceCache(), update_interval=1.0)
    assert src._sim._dt == 1.0 / TRADING_SECONDS_PER_YEAR
    await src.add_ticker("pypl")
    assert src.get_tickers() == ["PYPL"]


@pytest.mark.parametrize(
    "raw,expected",
    [(" aapl ", "AAPL"), ("V", "V"), ("GOOGLE", None), ("BRK.B", None), ("", None)],
)
def test_normalize_ticker(raw, expected):
    if expected:
        assert normalize_ticker(raw) == expected
    else:
        with pytest.raises(ValueError):
            normalize_ticker(raw)
```

The existing `test_models.py`, `test_cache.py`, `test_simulator.py`,
`test_simulator_source.py` and `test_factory.py` keep passing unchanged against the new
code. In the old `test_massive.py`, the 7 tests that mock `_fetch_snapshots` or set
`last_trade.timestamp` are replaced by §14.1.

---

## 15. Edge cases and failure modes

| Situation | Behaviour |
|---|---|
| Empty watchlist and no positions at startup | `start([])` succeeds. The simulator idles. Massive makes no call and detects its mode on the first poll after a ticker is added. SSE sends nothing until there is a price |
| Unknown symbol (`ZZZZ`) with the simulator | Priced at a stable `U(50, 300)` seed with default params and correlation 0.3 |
| Unknown symbol with Massive | Missing from the response. No cache entry, so the API returns `price: null`, the UI shows "--", and a trade gets a 400 |
| Invalid symbol (`GOOGLE`, `BRK.B`, `""`) | `normalize_ticker` raises, giving a 422. It never reaches a source |
| Bad Massive key (401) | The first snapshot raises `BadResponse`, so the source switches to EOD, which also gets 401. Every poll logs an error and the cache stays empty. The app still runs and prices show "--". The fix is the key; to keep prices live, remove the key to fall back to the simulator |
| Rate limited (429) | Logged. The next tick retries. `retries=0` keeps it to one call |
| Network down | Same as 429. Cached prices stay; they are stale but usable |
| Simulator `step()` raises | Logged with its traceback. The loop goes on |
| Restart | Simulator prices reset to their seeds while positions persist, so P&L jumps (accepted in PLAN §7). Massive starts again from real data |
| Ticker removed while a Massive fetch is running | Its result is dropped (`if ticker in self._tickers`) |
| Client disconnects from SSE | `is_disconnected()` ends the generator within 500 ms, and uvicorn cancels it on shutdown |
| Last tracked ticker removed | The version bumps and SSE sends `data: {}`, so the client clears its board |
| Several uvicorn workers | **Not supported.** Each worker would have its own cache and simulator, with different prices. The Dockerfile CMD must use one worker |

---

## 16. Configuration

| Setting | Where | Default | Notes |
|---|---|---|---|
| `MASSIVE_API_KEY` | env | unset | Non-empty means Massive. Whitespace counts as unset |
| Simulator tick | `SimulatorDataSource(update_interval=)` | 0.5 s | `dt` follows it |
| Simulator events | `SimulatorDataSource(event_probability=)` | 0.001 | Use 0.0 in tests that need calm prices |
| Massive snapshot interval | `MassiveDataSource(poll_interval=)` | 15 s | 2-5 s on real-time plans |
| Massive EOD interval | `MassiveDataSource(eod_poll_interval=)` | 900 s | The data changes once a day |
| SSE check interval | `stream.POLL_INTERVAL` | 0.5 s | |
| SSE heartbeat | `stream.HEARTBEAT_EVERY` | 15 s | |
| Workers | Dockerfile `CMD uvicorn app.main:app --host 0.0.0.0 --port 8000` | 1 | Don't add `--workers` |

There are no new environment variables. The intervals are constructor arguments, so
tests and the demo can override them without touching the environment.

---

## 17. Implementation checklist

1. Add `tickers.py` and `tracking.py` (§4, §12).
2. Replace `cache.py` (§5).
3. Replace `simulator.py` (§8.2).
4. Replace `massive_client.py` (§9.4).
5. Replace `stream.py` (§11). `generate_events` was `_generate_events`; nothing outside
   the module imports it.
6. Update `__init__.py`:

```python
# backend/app/market/__init__.py
"""Market data subsystem. Everything outside this package imports from here."""

from .cache import PriceCache
from .factory import create_market_data_source
from .interface import MarketDataSource
from .models import PriceUpdate
from .stream import create_stream_router
from .tickers import TICKER_RE, normalize_ticker
from .tracking import sync_tracking

__all__ = [
    "PriceUpdate",
    "PriceCache",
    "MarketDataSource",
    "create_market_data_source",
    "create_stream_router",
    "normalize_ticker",
    "TICKER_RE",
    "sync_tracking",
]
```

7. Replace the 7 old Massive tests and add §14.1 to §14.4.
8. Run `uv run pytest tests -q` and `uv run ruff check app tests`. Both should be clean.
9. Smoke test it by hand: `uv run market_data_demo.py` (simulator). With a key set, run
   `MASSIVE_API_KEY=... uv run python -c "..."` to start `MassiveDataSource` and print
   `src.mode` and `cache.get_all()`.
10. Then update `MARKET_INTERFACE.md` §5 ("Changes needed"), because those changes are
    now done.
