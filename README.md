# Portfolio Statistics

A desktop dashboard for your tastytrade account. Run it, and it logs in, pulls your
positions and balances, and shows allocation by stock / sector / industry, cash, and
a set of portfolio risk figures — with futures counted at true contract notional.

## Setup

1. Create an OAuth application at
   [my.tastytrade.com → API Access → OAuth Applications](https://my.tastytrade.com/app.html#/manage/api-access/oauth-applications).
   Use `http://localhost:8000` as the callback URL and tick only the **read**
   scopes — this app never places orders. Save the client ID and secret (the
   secret is shown once), then use **Manage → Create Grant** to mint a refresh
   token, which does not expire.

2. Copy `.env.example` to `.env` and fill in all three:

   ```
   TASTYTRADE_CLIENT_ID=...
   TASTYTRADE_CLIENT_SECRET=...
   TASTYTRADE_REFRESH_TOKEN=...
   ```

   `.env` is gitignored. Leave `TASTYTRADE_ACCOUNT` blank to aggregate every open
   account, or set it to one account number (or a comma-separated list). Treat the
   secret and refresh token like a password — together they are account access.

3. Double-click `run.bat`. On first run it creates a virtual environment in
   `%LOCALAPPDATA%\PortfolioStatistics\venv` and installs dependencies, then opens
   the dashboard in a desktop window. The venv is kept outside the project folder
   so each computer the folder syncs to builds its own, and it is rebuilt
   automatically if its Python goes missing or `requirements.txt` changes.

Other ways to run it:

```
run.bat --browser     open in your default browser instead of a window
run.bat --json        print the raw report as JSON and exit
run.bat --demo        render stub data, without calling the API
python app.py         same thing, if the venv is already active
```

## What the numbers mean

Every position gets **two** exposure figures, because for derivatives they answer
different questions:

| | Meaning |
|---|---|
| **Market value** | What the position itself is worth — share price, or option premium. Futures show ~0 here, since they settle daily and hold no balance-sheet value. |
| **Notional** | The economic exposure the position gives you. |

Notional is computed per instrument type:

- **Stock** — quantity × price.
- **Equity option** — quantity × 100 × delta × *underlying* price: the share-equivalent
  directional exposure. Delta comes from tastytrade's option quote; if it is missing,
  the full nominal is used and the dashboard shows a warning.
- **Future** — quantity × price × the contract's point value, taken from tastytrade's
  `notional-multiplier` and cross-checked against the table in
  [portfolio/futures.py](portfolio/futures.py). So 2× `/ZN` at 110.5 is
  2 × 110.5 × $1,000 = **$221,000**, not $221. If the API and the local table disagree,
  the dashboard shows a warning rather than silently using the wrong one.
- **Future option** — quantity × delta × underlying future price × that future's point
  value.

Allocation percentages are shares of **gross** notional. Legs on the same underlying
are netted first, so a calendar, vertical or covered call counts as its net delta
rather than as several naked positions. The absolute values are then summed across
underlyings, so a hedged book never produces percentages over 100%. The **Margin**
toggle re-bases the same breakdown on actual margin requirement per underlying.
tastytrade's margin report covers equities only -- futures margin is reported once
per account as `futures-margin-requirement`, so it is attributed across that
account's futures positions in proportion to notional. Margin is tracked per
(account, underlying), since the same name held in two accounts is two separate
requirements.

### Three bases for allocation: Notional / Risk / Margin

Notional alone is misleading in a book that mixes futures with equities. A 10-year
note future has roughly 4% annualised volatility; a micro-cap can run over 100%. Rated
by notional, one `/ZN` contract looks like half the portfolio; rated by how much it can
actually move, it is under a tenth of it:

| | % notional | % risk | % margin | vol |
|---|---|---|---|---|
| `/ZN` | 53.8% | 8.1% | 6.9% | 4% |
| CRSR | 4.4% | **14.8%** | 17.7% | 91% |
| SNDK | 3.3% | **14.1%** | 6.5% | 116% |

**Risk** weights each position by notional × annualised volatility, using the same
daily returns the correlation matrix is built from. It is the honest denominator when
asset classes differ: it moves Technology from 18.8% of the book to **48.2%**, and
`Futures: Rates` from 53.8% down to 8.1%.

The **Equities only** checkbox hides futures entirely — useful for looking at just the
stock sleeve, but note it hides a real exposure rather than measuring it, so prefer the
Risk basis for judging concentration.

### Risk panel

Gross and net leverage against net liq, beta-weighted (SPY-equivalent) exposure, the
largest position and top-5 concentration, a Herfindahl concentration index, margin
utilisation and maintenance excess, futures notional as a share of the book, and
notional-weighted dividend yield, IV rank and SPY correlation. Beta, IV rank,
correlation and dividend yield come from tastytrade's `/market-metrics`; futures have
no beta there, so the beta-weighted figure reports what share of gross it covers.

## Performance and P&L

**Unrealized P&L** per position, rolled up by sector and industry and shown as a
total, from `average-open-price` against the current mark. Signed quantity makes it
correct for shorts: a short option whose premium has decayed shows a gain. Day P&L is
measured against the previous close, not the open price. No extra API calls -- the
fields are already in the positions response.

**Net liq history** comes from `/accounts/{n}/net-liq/history`, giving an equity curve,
peak-to-trough max drawdown with its dates, and period returns.

Two caveats on the curve, both deliberate:

- The endpoint returns one point per **calendar** day, so period returns are resolved
  by date rather than by counting rows -- counting trading days would make "1y" reach
  back only about eight months. A period is omitted entirely when the history does not
  span it, rather than quietly measured against the oldest point available.
- Net liquidating value moves with deposits and withdrawals, so these are *account
  value* changes, not time-weighted investment returns.

## Upcoming earnings

Holdings reporting within 60 days, nearest first, flagged when the position includes
options. The dates come from the `earnings` block of the `/market-metrics` response
that is already fetched for beta and IV rank.

## Correlation matrix

A heatmap of pairwise correlations, computed on **daily returns** over the last year
(correlating raw price levels mostly measures shared trend and would show almost
everything as highly correlated). Red means the pair moves together, teal means it
moves opposite.

The matrix is over **underlyings, not individual positions** — stock plus options on
the same name share one price series, so they would be perfectly correlated by
construction and tell you nothing. Below the grid: average pairwise correlation, and
the most and least correlated pairs.

History comes from Yahoo, since tastytrade only serves candles over its DXLink
websocket. Futures map to their continuous front-month series (`/ZNZ6` → `ZN=F`) and
micro contracts map to their full-size parent, which tracks the same underlying — see
`yahoo_symbol()` in [portfolio/futures.py](portfolio/futures.py). Anything with no
Yahoo series is listed as missing rather than silently dropped. Closes are cached in
`data/cache/price_history.json` for 12 hours.

## What-if: testing a prospective position

Type symbols into the **What if I added…** box (comma separated, futures roots welcome:
`NVDA, TLT, GLD, /ES`) to see how each would sit alongside what you already hold.

The headline number is correlation to the **portfolio's own return series** — the book
is collapsed into one daily return stream weighted by each holding's *net* notional, so
shorts subtract, and the comparison is against the portfolio as actually held rather
than an equal-weighted basket of the same names. Underneath: average correlation to
individual holdings, the closest and furthest holding, the candidate's annualised
volatility next to the book's, and a strip showing its correlation to every holding.

Symbols you already hold are skipped, and anything with no Yahoo history is reported
rather than silently dropped. The price cache is incremental, so testing a new symbol
downloads only that symbol.

## Fetching

Two things keep the load fast, both of which matter because request latency to
tastytrade dominates the runtime:

- **Only the selected account is fetched.** Picking one account skips the others'
  positions, margin and net-liq history -- 11 requests instead of 17. Balances are
  still fetched for every account, since the picker shows each one's net liq and a
  balance call is the cheapest of the four. Each account's report is cached
  separately, so switching back to one already viewed is instant.
- **Requests run concurrently.** Every per-account call is independent, as are the
  quote, metrics and futures-instrument lookups, so they are issued through a thread
  pool (`MAX_WORKERS` in [portfolio/analytics.py](portfolio/analytics.py)) instead of
  serially. Measured on a slow link, median wall clock:

  | | sequential | concurrent | |
  |---|---|---|---|
  | All accounts | 71.8s | 25.1s | 2.9x |
  | One account | 51.6s | 9.1s | 5.7x |

  Results are collected in account order, so the report is byte-identical to the
  sequential version. The OAuth token is minted under a double-checked lock, or every
  thread noticing an expiring token would start its own exchange.

## Sector and industry data

tastytrade's API does not return sector or industry, so equities are classified via
Yahoo Finance and cached in `data/cache/sector_cache.json` (fetched once per symbol,
refreshed monthly). Futures are classified from the local contract table instead.

Anything Yahoo gets wrong or leaves as `Unknown` — ETFs especially — goes in
**`data/overrides.csv`**, which always wins:

```csv
symbol,sector,industry
SPY,ETF / Fund,US Large Cap Blend
TLT,ETF / Fund,Long Government Bonds
```

The first run is the slow one, because it looks up every symbol. After that it's fast.

## Known limitations

- Option notional is delta-adjusted, which captures direction only. A delta-neutral
  spread (a calendar, an iron condor) shows near-zero exposure even though it carries
  gamma and vega risk. Check the **Margin** view for those.
- Beta-weighting covers equities and equity options only; futures carry no beta in
  tastytrade's market metrics.
- Prices are marks from tastytrade's REST quote endpoint, not a live stream — the
  dashboard is a snapshot, refreshed on demand via the Refresh button.
- Adding a futures contract that isn't in `CONTRACT_SPECS` still works (the API
  supplies the multiplier), but its sector/industry falls back to `Futures: Other`.

## Layout

```
app.py                  FastAPI server + desktop window
portfolio/tasty.py      thin tastytrade REST client (login, positions, balances, quotes)
portfolio/analytics.py  exposure math and aggregation
portfolio/futures.py    futures contract specs and point values
portfolio/classify.py   sector/industry lookup, cache and overrides
web/                    dashboard UI (no CDN dependencies; works offline)
tests/                  offline checks of the exposure math
```

Run the tests with `%LOCALAPPDATA%\PortfolioStatistics\venv\Scripts\python.exe tests\test_analytics.py`.

Your selected account, allocation tab, allocation basis, equities-only toggle,
positions-table sort and which panels you have collapsed are remembered in
`data/ui_state.json`. They are stored server-side on purpose: the app falls back to a
random port when 8765 is taken, and browser `localStorage` is scoped to the origin
*including the port*, so a browser-side preference would not survive a restart.

## Authentication

OAuth2: the refresh token is exchanged for a 15-minute access token via
`POST /oauth/token`, sent as `Authorization: Bearer`. Because a first run can spend
several minutes on sector lookups, [tasty.py](portfolio/tasty.py) re-mints the access
token whenever it is within two minutes of expiry, and retries once on a `401` — a
long run cannot die halfway through on an expired token.

Password login (`POST /sessions`) is deliberately not supported: tastytrade has
removed it from their public documentation in favour of OAuth, and it does not work
cleanly for accounts with 2FA enabled.

The API host is `api.tastytrade.com` — the `iss` claim in the refresh token. The
legacy `api.tastyworks.com` hostname that the official SDK still ships also resolves,
but OAuth tokens are issued by the former.

This project does not use the `tastytrade` PyPI package — only about eight endpoints
are needed, so a small client avoids the dependency entirely. It also does not use
`requests`: on this machine urllib3 (which `requests` sits on) fails its TLS handshake
against tastytrade with `SSLEOFError`, while raw sockets and `http.client` connect
fine — the signature of a TLS-inspecting security product. The client uses stdlib
`urllib.request`, which is unaffected. If you ever see `SSLEOFError` from another
library here, that is the same cause.
