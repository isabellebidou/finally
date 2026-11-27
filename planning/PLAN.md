# FinAlly — AI Trading Workstation

## Project Specification

## 1. Vision

FinAlly (Finance Ally) is a visually stunning AI-powered trading workstation that streams live market data, lets users trade a simulated portfolio, and integrates an LLM chat assistant that can analyze positions and execute trades on the user's behalf. It looks and feels like a modern Bloomberg terminal with an AI copilot.

This is the capstone project for an agentic AI coding course. It is built entirely by Coding Agents demonstrating how orchestrated AI agents can produce a production-quality full-stack application. Agents interact through files in `planning/`.

## 2. User Experience

### First Launch

The user runs a single Docker command (or a provided start script). A browser opens to `http://localhost:8000`. No login, no signup. They immediately see:

- A watchlist of 10 default tickers with live-updating prices in a grid
- $10,000 in virtual cash
- A dark, data-rich trading terminal aesthetic
- An AI chat panel ready to assist

### What the User Can Do

- **Watch prices stream** — prices flash green (uptick) or red (downtick) with subtle CSS animations that fade
- **View sparkline mini-charts** — price action beside each ticker in the watchlist, accumulated on the frontend from the SSE stream since page load (sparklines fill in progressively)
- **Click a ticker** to see a larger detailed chart in the main chart area
- **Buy and sell shares** — market orders only, instant fill at current price, no fees, no confirmation dialog
- **Monitor their portfolio** — a heatmap (treemap) showing positions sized by weight and colored by P&L, plus a P&L chart tracking total portfolio value over time
- **View a positions table** — ticker, quantity, average cost, current price, unrealized P&L, % change
- **Chat with the AI assistant** — ask about their portfolio, get analysis, and have the AI execute trades and manage the watchlist through natural language
- **Manage the watchlist** — add/remove tickers manually or via the AI chat

### Visual Design

- **Dark theme**: backgrounds around `#0d1117` or `#1a1a2e`, muted gray borders, no pure black
- **Price flash animations**: brief green/red background highlight on price change, fading over ~500ms via CSS transitions
- **Connection status indicator**: a small colored dot (green = connected, yellow = reconnecting, red = disconnected) visible in the header
- **Professional, data-dense layout**: inspired by Bloomberg/trading terminals — every pixel earns its place
- **Responsive but desktop-first**: optimized for wide screens, functional on tablet

### Color Scheme
- Accent Yellow: `#ecad0a`
- Blue Primary: `#209dd7`
- Purple Secondary: `#753991` (submit buttons)

## 3. Architecture Overview

### Single Container, Single Port

```
┌─────────────────────────────────────────────────┐
│  Docker Container (port 8000)                   │
│                                                 │
│  FastAPI (Python/uv)                            │
│  ├── /api/*          REST endpoints             │
│  ├── /api/stream/*   SSE streaming              │
│  └── /*              Static file serving        │
│                      (Next.js export)           │
│                                                 │
│  SQLite database (volume-mounted)               │
│  Background task: market data polling/sim       │
└─────────────────────────────────────────────────┘
```

- **Frontend**: Next.js with TypeScript, built as a static export (`output: 'export'`), served by FastAPI as static files
- **Backend**: FastAPI (Python), managed as a `uv` project
- **Database**: SQLite, single file at `db/finally.db`, volume-mounted for persistence
- **Real-time data**: Server-Sent Events (SSE) — simpler than WebSockets, one-way server→client push, works everywhere
- **AI integration**: LiteLLM → OpenRouter (Cerebras for fast inference), with structured outputs for trade execution
- **Market data**: Environment-variable driven — simulator by default, real data via Massive API if key provided

### Why These Choices

| Decision | Rationale |
|---|---|
| SSE over WebSockets | One-way push is all we need; simpler, no bidirectional complexity, universal browser support |
| Static Next.js export | Single origin, no CORS issues, one port, one container, simple deployment |
| SQLite over Postgres | No auth = no multi-user = no need for a database server; self-contained, zero config |
| Single Docker container | Students run one command; no docker-compose for production, no service orchestration |
| uv for Python | Fast, modern Python project management; reproducible lockfile; what students should learn |
| Market orders only | Eliminates order book, limit order logic, partial fills — dramatically simpler portfolio math |

---

## 4. Directory Structure

```
finally/
├── frontend/                 # Next.js TypeScript project (static export)
├── backend/                  # FastAPI uv project (Python)
│   └── app/db/               # Schema definitions and seed data
├── planning/                 # Project-wide documentation for agents
│   ├── PLAN.md               # This document
│   └── ...                   # Additional agent reference docs
├── scripts/
│   ├── start_mac.sh          # Launch Docker container (macOS/Linux)
│   ├── stop_mac.sh           # Stop Docker container (macOS/Linux)
│   ├── start_windows.ps1     # Launch Docker container (Windows PowerShell)
│   └── stop_windows.ps1      # Stop Docker container (Windows PowerShell)
├── test/                     # Playwright E2E tests (run from the host)
├── db/                       # Volume mount target (SQLite file lives here at runtime)
│   └── .gitkeep              # Directory exists in repo; finally.db is gitignored
├── Dockerfile                # Multi-stage build (Node → Python)
├── .env                      # Environment variables (gitignored, .env.example committed)
└── .gitignore
```

### Key Boundaries

- **`frontend/`** is a self-contained Next.js project. It knows nothing about Python. It talks to the backend via `/api/*` endpoints and `/api/stream/*` SSE endpoints. Internal structure is up to the Frontend Engineer agent.
- **`backend/`** is a self-contained uv project with its own `pyproject.toml`. It owns all server logic including database initialization, schema, seed data, API routes, SSE streaming, market data, and LLM integration. Internal structure is up to the Backend/Market Data agents.
- **`backend/app/db/`** contains schema SQL definitions and seed logic. The backend creates tables and seeds default data once, in the FastAPI lifespan startup. (It is not `backend/db/`, which would collide with the `/app/db` volume mount.)
- **`db/`** at the top level holds the SQLite file in local development. In Docker, the named volume `finally-data` is mounted at `/app/db` instead.
- **`planning/`** contains project-wide documentation, including this plan. All agents reference files here as the shared contract.
- **`test/`** contains Playwright E2E tests, run from the host against the container. Unit tests live within `frontend/` and `backend/` respectively, following each framework's conventions.
- **`scripts/`** contains start/stop scripts that wrap Docker commands.

---

## 5. Environment Variables

```bash
# Required: OpenRouter API key for LLM chat functionality
OPENROUTER_API_KEY=your-openrouter-api-key-here

# Optional: Massive (Polygon.io) API key for real market data
# If not set, the built-in market simulator is used (recommended for most users)
MASSIVE_API_KEY=

# Optional: Set to "true" for deterministic mock LLM responses (testing)
LLM_MOCK=false

# Optional: SQLite path (default db/finally.db at the project root; /app/db/finally.db in Docker)
DB_PATH=
```

### Behavior

- If `MASSIVE_API_KEY` is set and non-empty → backend uses Massive REST API for market data
- If `MASSIVE_API_KEY` is absent or empty → backend uses the built-in market simulator
- If `LLM_MOCK=true` → backend returns deterministic mock LLM responses (for E2E tests)
- The backend reads `.env` from the project root (read via docker `--env-file`)
- If `OPENROUTER_API_KEY` is absent and `LLM_MOCK` is not true → the app runs normally and only chat is unavailable
- The committed `.env.example` documents all variables

---

## 6. Market Data

### Two Implementations, One Interface

Both the simulator and the Massive client implement the same abstract interface. The backend selects which to use based on the environment variable. All downstream code (SSE streaming, price cache, frontend) is agnostic to the source.

### Simulator (Default)

- Generates prices using geometric Brownian motion (GBM) with configurable drift and volatility per ticker
- Updates at ~500ms intervals
- Correlated moves across tickers (e.g., tech stocks move together)
- Occasional random "events" — sudden 2-5% moves on a ticker for drama
- Starts from realistic seed prices (e.g., AAPL ~$190, GOOGL ~$175, etc.)
- Runs as an in-process background task — no external dependencies

### Massive API (Optional)

- REST API polling (not WebSocket) — simpler, works on all tiers
- Polls for all tracked tickers (see Tracked Tickers) on a configurable interval
- Free tier (5 calls/min): poll every 15 seconds
- Paid tiers: poll every 2-15 seconds depending on tier
- Parses REST response into the same format as the simulator

### Shared Price Cache

- A single background task (simulator or Massive poller) writes to an in-memory price cache
- The cache holds the latest price, previous price, and timestamp for each ticker
- SSE streams read from this cache and push updates to connected clients

### Tracked Tickers

- The tracked set is the union of the watchlist and open positions. It is loaded at startup
- Removing a held ticker from the watchlist keeps it tracked; tracking stops only when a ticker is neither watched nor held
- Tickers are uppercased and must be 1-5 letters. Unknown symbols are accepted; the simulator invents a price, and with Massive they show "--" because no price arrives

### SSE Streaming

- Endpoint: `GET /api/stream/prices`
- Long-lived SSE connection; client uses native `EventSource` API
- Server checks every ~500ms and sends an event only when the cache has changed
- Each event is a JSON map of all tracked tickers: `{"AAPL": {"ticker", "price", "previous_price", "timestamp", "change", "change_percent", "direction"}, ...}`
- `timestamp` is Unix seconds (the database uses ISO strings). `change` and `change_percent` are tick-to-tick, not daily
- Client handles reconnection automatically (EventSource has built-in retry)

---

## 7. Database

### SQLite Initialization

In the FastAPI lifespan startup, before the market data source starts, the backend creates any missing tables and seeds defaults if the database is new. There is no migration step or manual setup.

### Schema

Single user; no `user_id` columns. Timestamps are ISO 8601 UTC strings.

**users_profile** — Cash balance (single row)
- `id` INTEGER PRIMARY KEY (always `1`)
- `cash_balance` REAL (default: `10000.0`)
- `created_at` TEXT

**watchlist** — Tickers the user is watching
- `ticker` TEXT PRIMARY KEY
- `added_at` TEXT

**positions** — Current holdings
- `ticker` TEXT PRIMARY KEY
- `quantity` REAL (fractional shares supported)
- `avg_cost` REAL
- `updated_at` TEXT

**trades** — Trade history (append-only log)
- `id` INTEGER PRIMARY KEY
- `ticker` TEXT
- `side` TEXT (`"buy"` or `"sell"`)
- `quantity` REAL
- `price` REAL
- `executed_at` TEXT

**portfolio_snapshots** — Portfolio value over time. Recorded every 60 seconds by a background task, and immediately after each trade.
- `id` INTEGER PRIMARY KEY
- `total_value` REAL
- `recorded_at` TEXT

**chat_messages** — Conversation history with LLM
- `id` INTEGER PRIMARY KEY
- `role` TEXT (`"user"` or `"assistant"`)
- `content` TEXT
- `actions` TEXT (JSON — per-action results; null for user messages)
- `created_at` TEXT

### Trade Rules

- A trade requires a positive quantity and a cached price; the ticker need not be on the watchlist, only tracked
- Average cost is quantity-weighted on buys and unchanged on sells. Realized P&L is out of scope
- Quantities round to 6 decimals. Sells tolerate 1e-6 shares; a position at or below that is deleted
- Cash check, cash, position, trade and snapshot are written in one transaction, so concurrent trades cannot double-spend
- A held ticker without a price is valued at `avg_cost`
- Simulator prices reset on restart while positions persist, so P&L may jump. This is accepted

### Default Seed Data

- One user profile: `id=1`, `cash_balance=10000.0`
- Ten watchlist entries: AAPL, GOOGL, MSFT, AMZN, TSLA, NVDA, META, JPM, V, NFLX

---

## 8. API Endpoints

### Market Data
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/stream/prices` | SSE stream of live price updates |

### Portfolio
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/portfolio` | Current positions, cash balance, total value, unrealized P&L |
| POST | `/api/portfolio/trade` | Execute a trade: `{ticker, quantity, side}` |
| GET | `/api/portfolio/history` | Latest 500 snapshots, oldest first (for P&L chart) |

### Watchlist
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/watchlist` | Current watchlist tickers with latest prices |
| POST | `/api/watchlist` | Add a ticker: `{ticker}` |
| DELETE | `/api/watchlist/{ticker}` | Remove a ticker |

### Chat
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/chat` | Latest 50 messages, oldest first (restores the panel on reload) |
| POST | `/api/chat` | Send a message, receive complete JSON response (message + executed actions) |

### System
| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/health` | Health check (for Docker/deployment) |

### Response Contract

```
GET /api/portfolio
  {"cash_balance", "total_value", "unrealized_pnl",
   "positions": [{"ticker", "quantity", "avg_cost", "current_price",
                  "market_value", "unrealized_pnl", "pnl_percent"}]}

POST /api/portfolio/trade    {"ticker", "quantity", "side"}
  {"trade": {"id", "ticker", "side", "quantity", "price", "executed_at"},
   "portfolio": <GET /api/portfolio>}

GET /api/portfolio/history   [{"total_value", "recorded_at"}]

GET /api/watchlist           [{"ticker", "price"}]
POST /api/watchlist          {"ticker"} -> {"ticker", "price"}
DELETE /api/watchlist/{t}    204

GET /api/chat                [{"id", "role", "content", "actions", "created_at"}]
POST /api/chat               {"message"} -> {"message", "actions"}
  actions: [{"type": "trade" | "watchlist", "ticker", "status": "ok" | "error",
             "error": str | null, "side", "quantity", "price", "action"}]
  (side/quantity/price are null for watchlist; action is null for trade)

GET /api/health              {"status": "ok"}
```

- `current_price` and `price` may be null when no price is cached yet, except for an executed trade
- Errors are `{"detail": ...}`: 400 for business rules (insufficient cash or shares, no price), 422 for invalid input
- Watchlist add and remove are idempotent and succeed when there is nothing to change

---

## 9. LLM Integration

When writing code to make calls to LLMs, use cerebras-inference skill to use LiteLLM via OpenRouter to the `openrouter/openai/gpt-oss-120b` model with Cerebras as the inference provider. Structured Outputs should be used to interpret the results.

There is an OPENROUTER_API_KEY in the .env file in the project root.

### How It Works

When the user sends a chat message, the backend:

1. Loads the user's current portfolio context (cash, positions with P&L, watchlist with live prices, total portfolio value)
2. Loads the last 20 messages from `chat_messages`, including action results
3. Constructs a prompt with a system message, portfolio context, conversation history, and the user's new message
4. Calls the LLM once via LiteLLM → OpenRouter, requesting structured output, using the cerebras-inference skill. The call is synchronous, so run it in a thread. Provider fallback is acceptable
5. Parses the complete structured JSON response
6. Auto-executes watchlist changes, then trades, in order. One failure does not stop the rest
7. Stores the message and per-action results in `chat_messages`
8. Returns the complete JSON response to the frontend (no token-by-token streaming — Cerebras inference is fast enough that a loading indicator is sufficient)

### Structured Output Schema

The LLM is instructed to respond with JSON matching this schema:

```json
{
  "message": "Your conversational response to the user",
  "trades": [
    {"ticker": "AAPL", "side": "buy", "quantity": 10}
  ],
  "watchlist_changes": [
    {"ticker": "PYPL", "action": "add"}
  ]
}
```

- `message` (required): The conversational text shown to the user
- `trades` (optional): Array of trades to auto-execute. Each trade goes through the same validation as manual trades (sufficient cash for buys, sufficient shares for sells)
- `watchlist_changes` (optional): Array of watchlist modifications

### Auto-Execution

Trades specified by the LLM execute automatically — no confirmation dialog. This is a deliberate design choice:
- It's a simulated environment with fake money, so the stakes are zero
- It creates an impressive, fluid demo experience
- It demonstrates agentic AI capabilities — the core theme of the course

The reply is written before actions run, so the LLM cannot describe failures in it. Each result is returned in `actions` and rendered inline by the frontend; results also appear in later history so the LLM knows what happened. A ticker added in the same response may have no price yet with Massive, in which case its trade fails normally.

On timeout (30s), a missing key, or invalid output, `/api/chat` returns 200 with a short friendly message and no actions.

### System Prompt Guidance

The LLM should be prompted as "FinAlly, an AI trading assistant" with instructions to:
- Analyze portfolio composition, risk concentration, and P&L
- Suggest trades with reasoning
- Execute trades when the user asks or agrees
- Change the watchlist when the user asks or agrees
- Be concise and data-driven in responses
- Always respond with valid structured JSON

### LLM Mock Mode

When `LLM_MOCK=true`, the backend returns deterministic mock responses instead of calling OpenRouter. Matching is on the lowercased message, first match wins:

| Message contains | Response |
|---|---|
| `buy` | Buys 1 AAPL |
| `sell` | Sells 1 AAPL |
| `add` | Adds PYPL to the watchlist |
| `remove` | Removes PYPL from the watchlist |
| anything else | Message only, no actions |

This enables:
- Fast, free, reproducible E2E tests
- Development without an API key
- CI/CD pipelines

---

## 10. Frontend Design

### Layout

The frontend is a single-page application with a dense, terminal-inspired layout. The specific component architecture and layout system is up to the Frontend Engineer, but the UI should include these elements:

- **Watchlist panel** — grid/table of watched tickers with: ticker symbol, current price (flashing green/red on change), change % since page load (labelled as such), and a sparkline mini-chart (accumulated from SSE since page load)
- **Main chart area** — larger chart for the currently selected ticker, showing price accumulated from SSE since page load (it starts empty). Clicking a ticker in the watchlist selects it here.
- **Portfolio heatmap** — treemap visualization where each rectangle is a position, sized by portfolio weight, colored by P&L (green = profit, red = loss)
- **P&L chart** — line chart showing total portfolio value over time, using data from `portfolio_snapshots`
- **Positions table** — tabular view of all positions: ticker, quantity, avg cost, current price, unrealized P&L, % change
- **Trade bar** — simple input area: ticker field, quantity field, buy button, sell button. Market orders, instant fill.
- **AI chat panel** — docked/collapsible sidebar. Message input, scrolling conversation history, loading indicator while waiting for LLM response. Trade executions and watchlist changes shown inline as confirmations.
- **Header** — portfolio total value (updating live), connection status indicator, cash balance

### Technical Notes

- Use `EventSource` for SSE connection to `/api/stream/prices`
- Use Recharts for all charts, including the treemap. Cap accumulated history at 300 points per ticker
- Recompute total value from SSE prices and positions rather than polling
- Price flash effect: on receiving a new price, briefly apply a CSS class with background color transition, then remove it
- All API calls go to the same origin (`/api/*`) — no CORS configuration needed
- Tailwind CSS for styling with a custom dark theme

---

## 11. Docker & Deployment

### Multi-Stage Dockerfile

```
Stage 1: Node 24 slim
  - Copy frontend/
  - npm ci && npm run build (produces static export)

Stage 2: Python 3.12 slim
  - Install uv
  - Copy backend/
  - uv sync (install Python dependencies from lockfile)
  - Copy frontend build output into a static/ directory
  - Expose port 8000
  - CMD: uvicorn serving FastAPI app (single worker: cache and tasks are per process)
```

FastAPI serves the static frontend files and all API routes on port 8000.

### Docker Volume

The SQLite database persists via a named Docker volume:

```bash
docker run -v finally-data:/app/db -p 8000:8000 --env-file .env finally
```

The named volume is mounted at `/app/db`, where the backend writes `finally.db`.

### Start/Stop Scripts

**`scripts/start_mac.sh`** (macOS/Linux):
- Always builds the Docker image (the layer cache keeps this fast)
- Creates `.env` from `.env.example` if it is missing
- Runs the container with the volume mount, port mapping, and `.env` file
- Prints the URL to access the app
- Optionally opens the browser

**`scripts/stop_mac.sh`** (macOS/Linux):
- Stops and removes the running container
- Does NOT remove the volume (data persists)

**`scripts/start_windows.ps1`** / **`scripts/stop_windows.ps1`**: PowerShell equivalents for Windows.

All scripts should be idempotent — safe to run multiple times.

### Cloud Deployment

Out of scope. The app has no login and auto-executes AI trades, so add access control before exposing it publicly.

---

## 12. Testing Strategy

### Unit Tests (within `frontend/` and `backend/`)

**Backend (pytest)**:
- Market data: simulator generates valid prices, GBM math is correct, Massive API response parsing works, both implementations conform to the abstract interface
- Portfolio: trade execution logic, P&L calculations, edge cases (selling more than owned, buying with insufficient cash, selling at a loss)
- LLM: structured output parsing handles all valid schemas, graceful handling of malformed responses, trade validation within chat flow
- API routes: correct status codes, response shapes, error handling

**Frontend (React Testing Library or similar)**:
- Component rendering with mock data
- Price flash animation triggers correctly on price changes
- Watchlist CRUD operations
- Portfolio display calculations
- Chat message rendering and loading state

### E2E Tests (in `test/`)

**Infrastructure**: Playwright runs from the host against the app container, keeping browser dependencies out of the production image. Each run starts with a fresh volume.

**Environment**: Tests run with `LLM_MOCK=true`. Simulated prices are random, so assert direction and presence, not exact values.

**Key Scenarios**:
- Fresh start: default watchlist appears, $10k balance shown, prices are streaming
- Add and remove a ticker from the watchlist
- Buy shares: cash decreases, position appears, portfolio updates
- Sell shares: cash increases, position updates or disappears
- Portfolio visualization: heatmap renders with correct colors, P&L chart has data points
- AI chat (mocked): send a message, receive a response, trade execution appears inline
- SSE resilience: disconnect and verify reconnection
