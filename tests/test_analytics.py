"""Offline checks for the exposure math, using a stubbed API client.

Run with: .venv/Scripts/python.exe -m pytest tests/ -q
(or plain `python tests/test_analytics.py` for a quick pass/fail summary)
"""

from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from portfolio.analytics import PortfolioBuilder  # noqa: E402
from portfolio.classify import Classifier  # noqa: E402
from portfolio.correlation import PriceHistory, to_yahoo  # noqa: E402


class StubPrices(PriceHistory):
    """Keeps the test offline: no Yahoo download, a canned matrix."""

    def load(self, symbols):
        self.loaded = sorted(set(symbols))

    def matrix_for(self, symbols):
        syms = sorted(set(symbols))
        return {
            "symbols": syms,
            "matrix": [[1.0 if i == j else 0.5 for j in range(len(syms))]
                       for i in range(len(syms))],
            "observations": 250,
            "period": "1y",
            "missing": [],
            "average_correlation": 0.5,
            "most_correlated": None,
            "least_correlated": None,
        }


class StubClassifier(Classifier):
    """Skips disk cache and Yahoo entirely; futures still use the local table."""

    def __init__(self, mapping: dict[str, tuple[str, str]]):
        self._cache = {}
        self._overrides = dict(mapping)
        self._dirty = False

    def save(self) -> None:
        pass

    @staticmethod
    def _fetch_yahoo(symbol: str):
        return "Unknown", "Unknown"


class FakeClient:
    def accounts(self, include_closed: bool = False):
        return [{"account_number": "5WT00001", "nickname": "Main", "is_closed": False,
                 "account_type_name": "Margin", "margin_or_cash": "Margin"}]

    def balances(self, account_number):
        return {
            "account_number": account_number,
            "net_liquidating_value": "250000",
            "cash_balance": "50000",
            "cash_available_to_withdraw": "40000",
            "equity_buying_power": "80000",
            "derivative_buying_power": "75000",
            "maintenance_requirement": "60000",
            "maintenance_excess": "190000",
            "futures_margin_requirement": "3000",
            "used_derivative_buying_power": "25000",
            "pending_cash": "0",
        }

    def positions(self, account_number):
        return [
            {  # 100 shares AAPL @ 200 -> $20,000 long
                "symbol": "AAPL", "underlying_symbol": "AAPL",
                "instrument_type": "Equity", "quantity": "100",
                "quantity_direction": "Long", "multiplier": "1",
                "close_price": "198", "mark_price": "200",
                "average_open_price": "180",
            },
            {  # short 2 XOM puts -> notional 2 * 100 * 110 = $22,000 short
                "symbol": "XOM  250117P00100000", "underlying_symbol": "XOM",
                "instrument_type": "Equity Option", "quantity": "2",
                "quantity_direction": "Short", "multiplier": "100",
                "close_price": "3.60", "mark_price": "3.50",
                "average_open_price": "5.00",
                "expires_at": "2025-01-17T21:00:00Z",
            },
            {  # long 2 /ZN @ 110.5 -> notional 2 * 110.5 * 1000 = $221,000
                "symbol": "/ZNZ4", "underlying_symbol": "/ZN",
                "instrument_type": "Future", "quantity": "2",
                "quantity_direction": "Long", "multiplier": "1",
                "close_price": "110.0", "average_open_price": "110.0",
            },
        ]

    def margin_requirements(self, account_number):
        # Mirrors the real API: the margin report covers equities only. Futures
        # margin arrives separately as futures_margin_requirement on balances.
        return {"groups": [
            {"underlying_symbol": "AAPL", "margin_requirement": "10000"},
            {"underlying_symbol": "XOM", "margin_requirement": "4000"},
        ]}

    def net_liq_history(self, account_number, time_back="1y"):
        """One point per calendar day, as the real endpoint returns.

        Ends today so the date-based period returns can be exercised: 120 days
        of history means 1m and 3m resolve but 6m and 1y are correctly omitted.
        Values dip to a 110k peak then a 99k trough -> -10% max drawdown.
        """
        today = date.today()
        series = []
        for offset in range(119, -1, -1):
            day = today - timedelta(days=offset)
            if offset == 119:
                close = 100000
            elif offset == 100:
                close = 110000
            elif offset == 91:  # exactly the 3-month lookback point
                close = 99000
            else:
                close = 105000
            series.append({"time": f"{day.isoformat()} 00:00:00+00", "close": str(close)})
        return series

    def market_metrics(self, symbols):
        soon = (date.today() + timedelta(days=10)).isoformat()
        return [
            {"symbol": "AAPL", "beta": "1.25", "dividend_yield": "0.005",
             "implied_volatility_index_rank": "0.30", "corr_spy_3month": "0.80",
             "earnings": {"expected_report_date": soon}},
            {"symbol": "XOM", "beta": "0.85", "dividend_yield": "0.033",
             "implied_volatility_index_rank": "0.45", "corr_spy_3month": "0.55"},
        ]

    def market_data(self, equities=None, options=None, futures=None,
                    future_options=None, indices=None, cryptocurrencies=None):
        return [
            {"symbol": "AAPL", "mark": "200"},
            {"symbol": "XOM", "mark": "110"},
            {"symbol": "/ZNZ4", "mark": "110.5"},
        ]

    def future(self, symbol):
        return {"symbol": symbol, "notional_multiplier": "1000", "product_code": "ZN"}


def approx(a, b, tol=0.01):
    return abs(a - b) <= tol


def run() -> int:
    builder = PortfolioBuilder(
        FakeClient(),
        StubClassifier({
            "AAPL": ("Technology", "Consumer Electronics"),
            "XOM": ("Energy", "Oil & Gas Integrated"),
        }),
        StubPrices(),
    )
    payload = builder.build()
    report = payload["views"]["ALL"]
    failures: list[str] = []

    def check(name, got, want, tol=0.01):
        if not approx(got, want, tol):
            failures.append(f"{name}: got {got}, want {want}")

    pos = {p["symbol"]: p for p in report["positions"]}

    # --- exposure math -----------------------------------------------------
    check("AAPL notional", pos["AAPL"]["notional"], 20_000)
    check("AAPL market value", pos["AAPL"]["market_value"], 20_000)

    xom = pos["XOM  250117P00100000"]
    check("short XOM put notional", xom["notional"], -22_000)  # 2 x 100 x 110, short
    check("short XOM put market value", xom["market_value"], -700)  # premium only

    zn = pos["/ZNZ4"]
    check("/ZN notional", zn["notional"], 221_000)  # 2 x 110.5 x $1,000 point value
    check("/ZN market value", zn["market_value"], 0)  # futures carry no cash value
    check("/ZN multiplier", zn["multiplier"], 1000)

    # --- aggregates --------------------------------------------------------
    exp = report["exposure"]
    check("gross notional", exp["gross_notional"], 263_000)  # 20k + 22k + 221k
    check("net notional", exp["net_notional"], 219_000)  # 20k - 22k + 221k
    # 10k AAPL + 4k XOM from the margin report, + 3k futures margin attributed
    # from balances (the margin report never includes futures).
    check("total margin", exp["total_margin"], 17_000)
    zn_row = {r["name"]: r for r in report["allocation"]["by_symbol"]}["/ZN"]
    check("futures margin attributed from balances", zn_row["margin"], 3_000)

    check("cash", report["cash"]["cash_balance"], 50_000)
    check("cash pct of net liq", report["cash"]["cash_pct_of_net_liq"], 20.0)

    sectors = {r["name"]: r for r in report["allocation"]["by_sector"]}
    check("rates sector pct", sectors["Futures: Rates"]["pct_of_gross"],
          221_000 / 263_000 * 100)
    check("tech sector pct", sectors["Technology"]["pct_of_gross"],
          20_000 / 263_000 * 100)
    if "US Treasuries" not in {r["name"] for r in report["allocation"]["by_industry"]}:
        failures.append("industry: /ZN did not map to 'US Treasuries'")

    risk = report["risk"]
    check("gross leverage", risk["gross_leverage"], 263_000 / 250_000)
    check("beta weighted", risk["beta_weighted_notional"],
          20_000 * 1.25 + (-22_000) * 0.85)  # /ZN has no beta, excluded
    check("largest position pct", risk["largest_position_pct"],
          221_000 / 263_000 * 100)
    if risk["largest_position"] != "/ZN":
        failures.append(f"largest position: got {risk['largest_position']}, want /ZN")
    check("futures pct of gross", risk["futures_pct_of_gross"],
          221_000 / 263_000 * 100)

    # --- P&L ---------------------------------------------------------------
    perf = report["performance"]
    check("unrealized pnl", perf["unrealized_pnl"], 3_300)   # 2000 + 300 + 1000
    check("cost basis", perf["cost_basis"], 239_000)          # 18000 + 1000 + 220000
    check("day pnl", perf["day_pnl"], 1_220)                  # 200 + 20 + 1000
    check("short option pnl", pos["XOM  250117P00100000"]["unrealized_pnl"], 300)
    if perf["winners"] != 3 or perf["losers"] != 0:
        failures.append(f"winners/losers: {perf['winners']}/{perf['losers']}")
    check("max drawdown", perf["max_drawdown_pct"], -10.0)    # 110k -> 99k
    if len(perf["history"]) != 120:
        failures.append(f"history points: {len(perf['history'])}")

    # Periods are resolved by date, not by row count. With 120 calendar days of
    # history, 1m and 3m are answerable and 6m/1y must be omitted rather than
    # silently reported against the oldest available point.
    if set(perf["returns"]) != {"1m", "3m"}:
        failures.append(f"returns periods: got {sorted(perf['returns'])}, want 1m+3m")
    check("3m return", perf["returns"].get("3m", 0), (105000 / 99000 - 1) * 100)
    check("1m return", perf["returns"].get("1m", 0), 0.0)

    # --- earnings ----------------------------------------------------------
    earnings = report["earnings"]
    if [e["symbol"] for e in earnings] != ["AAPL"]:
        failures.append(f"earnings: got {earnings}")
    elif earnings[0]["days_away"] != 10:
        failures.append(f"earnings days_away: {earnings[0]['days_away']}")

    if report["warnings"]:
        failures.append(f"unexpected warnings: {report['warnings']}")

    # --- per-account views ------------------------------------------------
    if set(payload["views"]) != {"ALL", "5WT00001"}:
        failures.append(f"views: got {sorted(payload['views'])}")
    solo = payload["views"].get("5WT00001", {})
    # Only one account holds anything, so its view must match the combined one.
    check("single-account gross matches combined",
          solo.get("exposure", {}).get("gross_notional", -1),
          report["exposure"]["gross_notional"])
    if len(solo.get("positions", [])) != len(report["positions"]):
        failures.append("single-account view has a different position count")

    meta = payload["accounts"][0]
    check("account net liq", meta["net_liquidating_value"], 250_000)
    if meta["position_count"] != 3:
        failures.append(f"account position_count: got {meta['position_count']}, want 3")

    # --- allocation rows carry classification and constituents ------------
    by_symbol = {r["name"]: r for r in report["allocation"]["by_symbol"]}
    if by_symbol["AAPL"]["sector"] != "Technology":
        failures.append(f"AAPL sector: got {by_symbol['AAPL']['sector']}")
    if by_symbol["AAPL"]["industry"] != "Consumer Electronics":
        failures.append(f"AAPL industry: got {by_symbol['AAPL']['industry']}")

    # --- correlation wiring ------------------------------------------------
    corr = report["correlation"]
    if corr["symbols"] != ["/ZN", "AAPL", "XOM"]:
        failures.append(f"correlation symbols: got {corr['symbols']}")
    check("correlation diagonal", corr["matrix"][0][0], 1.0)
    check("risk average correlation", report["risk"]["average_correlation"], 0.5)

    # Futures and crypto map to their Yahoo series; equities pass through.
    for sym, want in [("/ZNZ6", "ZN=F"), ("/MESU6", "ES=F"), ("AAPL", "AAPL"),
                      ("BTC/USD", "BTC-USD")]:
        if to_yahoo(sym) != want:
            failures.append(f"to_yahoo({sym}): got {to_yahoo(sym)}, want {want}")

    tech = {r["name"]: r for r in report["allocation"]["by_sector"]}["Technology"]
    if [m["name"] for m in tech["members"]] != ["AAPL"]:
        failures.append(f"Technology members: got {tech['members']}")
    check("member pct of parent", tech["members"][0]["pct_of_parent"], 100.0)

    if failures:
        print("FAILED:")
        for f in failures:
            print("  -", f)
        return 1
    print(f"All checks passed ({len(report['positions'])} positions).")
    return 0


def test_analytics():
    assert run() == 0


if __name__ == "__main__":
    raise SystemExit(run())
