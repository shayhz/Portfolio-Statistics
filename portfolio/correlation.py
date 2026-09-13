"""Correlation matrix across the portfolio's underlyings.

tastytrade only exposes price history over its DXLink websocket, so daily closes
come from Yahoo Finance instead. Futures map to their continuous front-month
series (`/ZNZ6` -> `ZN=F`), and micro contracts map to their full-size parent,
which tracks the same underlying.

Correlations are computed on *daily returns*, not price levels -- correlating raw
prices mostly measures shared trend and would report almost everything as highly
correlated.

The matrix is over underlyings rather than individual positions: stock plus
options on the same name share one price series, so they would be perfectly
correlated by construction and add nothing.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from portfolio.futures import yahoo_symbol

log = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CACHE_FILE = DATA_DIR / "cache" / "price_history.json"
CACHE_TTL_SECONDS = 12 * 3600  # intraday reruns reuse the same closes

DEFAULT_PERIOD = "1y"
MIN_OBSERVATIONS = 20  # below this a correlation is noise, not signal


def to_yahoo(symbol: str) -> str | None:
    """Map a portfolio group key to a Yahoo ticker."""
    if symbol.startswith("/"):
        return yahoo_symbol(symbol)
    if "/" in symbol:  # crypto pairs such as BTC/USD
        base = symbol.split("/")[0]
        return f"{base}-USD"
    return symbol or None


class PriceHistory:
    """Fetches and caches daily returns, then slices correlation matrices."""

    def __init__(self, period: str = DEFAULT_PERIOD):
        self.period = period
        self._returns = None  # pandas DataFrame of daily returns, or None
        self._mapping: dict[str, str] = {}  # portfolio symbol -> yahoo ticker
        self._unmapped: list[str] = []

    def load(self, symbols: list[str]) -> None:
        self._mapping = {}
        self._unmapped = []
        for sym in sorted(set(symbols)):
            ticker = to_yahoo(sym)
            if ticker:
                self._mapping[sym] = ticker
            else:
                self._unmapped.append(sym)

        tickers = sorted(set(self._mapping.values()))
        if len(tickers) < 2:
            return

        closes = self._fetch_closes(tickers)
        if closes is None or closes.empty:
            return
        returns = closes.pct_change().dropna(how="all")
        self._returns = returns

    # ------------------------------------------------------------- fetching

    def _fetch_closes(self, tickers: list[str]):
        """Closes for `tickers`, downloading only what the cache is missing.

        The cache holds every ticker seen so far, so a what-if on a new symbol
        downloads just that symbol rather than the whole portfolio again.
        """
        import pandas as pd

        cached = self._read_cache()
        have = set(cached.columns) if cached is not None else set()
        needed = [t for t in tickers if t not in have]

        fetched = None
        if needed:
            fetched = self._download(needed)

        if cached is None:
            merged = fetched
        elif fetched is None:
            merged = cached
        else:
            merged = pd.concat([cached, fetched], axis=1)
            merged = merged.loc[:, ~merged.columns.duplicated()]

        if merged is None or merged.empty:
            return None
        self._write_cache(merged)
        available = [t for t in tickers if t in merged.columns]
        return merged[available] if available else None

    @staticmethod
    def _download(tickers: list[str]):
        import pandas as pd

        try:
            import warnings

            import yfinance as yf

            log.info("Downloading price history for %s.", ", ".join(tickers))
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                raw = yf.download(
                    tickers,
                    period=DEFAULT_PERIOD,
                    interval="1d",
                    auto_adjust=True,
                    progress=False,
                )
        except Exception as exc:  # noqa: BLE001 - never let this break the report
            log.warning("Price history download failed: %s", exc)
            return None

        if raw is None or raw.empty:
            return None
        closes = raw["Close"] if "Close" in raw else raw
        if isinstance(closes, pd.Series):
            closes = closes.to_frame(name=tickers[0])
        return closes.dropna(axis=1, how="all")  # drop tickers Yahoo had nothing for

    def _read_cache(self):
        if not CACHE_FILE.exists():
            return None
        try:
            blob = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        if time.time() - blob.get("fetched_at", 0) > CACHE_TTL_SECONDS:
            return None
        if blob.get("period") != self.period:
            return None

        import pandas as pd

        try:
            return pd.DataFrame(blob["data"], index=pd.to_datetime(blob["index"]))
        except (KeyError, ValueError) as exc:
            log.warning("Price cache unreadable (%s); refetching.", exc)
            return None

    def _write_cache(self, closes) -> None:
        try:
            CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            CACHE_FILE.write_text(
                json.dumps(
                    {
                        "fetched_at": time.time(),
                        "period": self.period,
                        "columns": [str(c) for c in closes.columns],
                        "index": [str(d.date()) for d in closes.index],
                        "data": {
                            str(c): [
                                None if v != v else float(v)  # NaN -> null
                                for v in closes[c].tolist()
                            ]
                            for c in closes.columns
                        },
                    }
                ),
                encoding="utf-8",
            )
        except (OSError, TypeError, ValueError) as exc:
            log.warning("Could not write price cache: %s", exc)

    # -------------------------------------------------------------- slicing

    def matrix_for(self, symbols: list[str]) -> dict:
        """Correlation matrix restricted to the given portfolio symbols."""
        wanted = sorted(set(symbols))
        empty = {
            "symbols": [],
            "matrix": [],
            "observations": 0,
            "period": self.period,
            "missing": wanted,
            "average_correlation": None,
            "most_correlated": None,
            "least_correlated": None,
        }
        if self._returns is None:
            return empty

        usable = [
            s for s in wanted
            if s in self._mapping and self._mapping[s] in self._returns.columns
        ]
        missing = [s for s in wanted if s not in usable]
        if len(usable) < 2:
            return {**empty, "missing": missing}

        cols = [self._mapping[s] for s in usable]
        sub = self._returns[cols].dropna()
        if len(sub) < MIN_OBSERVATIONS:
            return {**empty, "missing": missing, "observations": len(sub)}

        corr = sub.corr()

        matrix = [
            [
                None if corr.iloc[i, j] != corr.iloc[i, j] else round(float(corr.iloc[i, j]), 4)
                for j in range(len(usable))
            ]
            for i in range(len(usable))
        ]

        # Summary stats over the unique off-diagonal pairs.
        pairs = [
            (usable[i], usable[j], matrix[i][j])
            for i in range(len(usable))
            for j in range(i + 1, len(usable))
            if matrix[i][j] is not None
        ]
        average = round(sum(p[2] for p in pairs) / len(pairs), 4) if pairs else None
        most = max(pairs, key=lambda p: p[2]) if pairs else None
        least = min(pairs, key=lambda p: p[2]) if pairs else None

        return {
            "symbols": usable,
            "tickers": [self._mapping[s] for s in usable],
            "matrix": matrix,
            "observations": len(sub),
            "period": self.period,
            "missing": missing,
            "average_correlation": average,
            "most_correlated": {"a": most[0], "b": most[1], "value": most[2]} if most else None,
            "least_correlated": {"a": least[0], "b": least[1], "value": least[2]} if least else None,
        }

    # -------------------------------------------------------------- what-if

    def returns_for(self, symbol: str):
        """Daily return series for one portfolio symbol, or None."""
        if self._returns is None:
            return None
        ticker = self._mapping.get(symbol)
        if ticker is None or ticker not in self._returns.columns:
            return None
        return self._returns[ticker]

    def portfolio_series(self, weights: dict[str, float]):
        """Daily return series of the book, weighted by signed exposure.

        Weights are net notional, so a short position contributes negatively --
        the series reflects how the portfolio actually moves, not how a
        long-only basket of the same names would.
        """
        if self._returns is None:
            return None
        total = sum(abs(v) for v in weights.values())
        if not total:
            return None

        series = None
        for symbol, notional in weights.items():
            col = self.returns_for(symbol)
            if col is None:
                continue
            contribution = col * (notional / total)
            series = contribution if series is None else series.add(contribution, fill_value=0.0)
        return series

    def what_if(self, weights: dict[str, float], candidates: list[str]) -> dict:
        """How each candidate symbol relates to the existing book.

        The headline number is each candidate's correlation to the *portfolio's*
        return series, which is what actually matters when sizing a new
        position; correlations to individual holdings are reported underneath.
        """
        holdings = sorted(weights)
        wanted = [c.strip().upper() for c in candidates if c and c.strip()]
        wanted = list(dict.fromkeys(wanted))  # de-duplicate, keep order

        result = {
            "candidates": [],
            "holdings": holdings,
            "period": self.period,
            "unknown": [],
            "already_held": [c for c in wanted if c in weights],
        }
        if not wanted:
            return result

        fresh = [c for c in wanted if c not in weights]
        # Reload history so the candidates are present alongside the holdings.
        self.load(holdings + fresh)

        portfolio = self.portfolio_series(weights)
        if portfolio is None:
            result["unknown"] = fresh
            return result

        import math

        for symbol in fresh:
            series = self.returns_for(symbol)
            if series is None:
                result["unknown"].append(symbol)
                continue

            joined = portfolio.to_frame("portfolio").join(
                series.rename("candidate"), how="inner"
            ).dropna()
            if len(joined) < MIN_OBSERVATIONS:
                result["unknown"].append(symbol)
                continue

            corr_portfolio = joined["portfolio"].corr(joined["candidate"])

            pairs = []
            for held in holdings:
                other = self.returns_for(held)
                if other is None:
                    continue
                both = series.to_frame("a").join(other.rename("b"), how="inner").dropna()
                if len(both) < MIN_OBSERVATIONS:
                    continue
                value = both["a"].corr(both["b"])
                if value == value:  # not NaN
                    pairs.append({"symbol": held, "value": round(float(value), 4)})

            pairs.sort(key=lambda p: -p["value"])
            values = [p["value"] for p in pairs]
            vol = float(joined["candidate"].std()) * math.sqrt(252) * 100

            result["candidates"].append({
                "symbol": symbol,
                "ticker": self._mapping.get(symbol, symbol),
                "correlation_to_portfolio": round(float(corr_portfolio), 4)
                if corr_portfolio == corr_portfolio else None,
                "average_correlation": round(sum(values) / len(values), 4) if values else None,
                "most_correlated": pairs[0] if pairs else None,
                "least_correlated": pairs[-1] if pairs else None,
                "annualized_volatility_pct": round(vol, 2),
                "observations": len(joined),
                "per_holding": pairs,
            })

        portfolio_vol = float(portfolio.dropna().std()) * math.sqrt(252) * 100
        result["portfolio_volatility_pct"] = round(portfolio_vol, 2)
        return result
