"""Sector / industry classification.

tastytrade's API does not expose sector or industry, so equities are classified
via Yahoo Finance (yfinance) and cached to disk -- each symbol is looked up once,
not once per run. Futures are classified from the local contract table instead,
since Yahoo has nothing sensible to say about /ZN.

`data/overrides.csv` always wins. Use it to correct anything Yahoo gets wrong and
to classify ETFs, ADRs and anything else the lookup leaves as Unknown.
"""

from __future__ import annotations

import csv
import json
import logging
import time
from pathlib import Path

from portfolio.futures import spec_for

log = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CACHE_FILE = DATA_DIR / "cache" / "sector_cache.json"
OVERRIDES_FILE = DATA_DIR / "overrides.csv"

UNKNOWN = "Unknown"
CACHE_TTL_SECONDS = 30 * 24 * 3600  # sectors change rarely; refresh monthly


class Classifier:
    def __init__(self) -> None:
        self._cache: dict[str, dict] = self._load_cache()
        self._overrides: dict[str, tuple[str, str]] = self._load_overrides()
        self._dirty = False

    # ------------------------------------------------------------- loading

    def _load_cache(self) -> dict[str, dict]:
        if not CACHE_FILE.exists():
            return {}
        try:
            return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Could not read sector cache, starting fresh: %s", exc)
            return {}

    def _load_overrides(self) -> dict[str, tuple[str, str]]:
        if not OVERRIDES_FILE.exists():
            return {}
        out: dict[str, tuple[str, str]] = {}
        try:
            with OVERRIDES_FILE.open(newline="", encoding="utf-8-sig") as fh:
                for row in csv.DictReader(fh):
                    symbol = (row.get("symbol") or "").strip().upper()
                    if not symbol or symbol.startswith("#"):
                        continue
                    out[symbol] = (
                        (row.get("sector") or "").strip() or UNKNOWN,
                        (row.get("industry") or "").strip() or UNKNOWN,
                    )
        except (OSError, csv.Error) as exc:
            log.warning("Could not read %s: %s", OVERRIDES_FILE, exc)
        return out

    def save(self) -> None:
        if not self._dirty:
            return
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        try:
            CACHE_FILE.write_text(json.dumps(self._cache, indent=2), encoding="utf-8")
            self._dirty = False
        except OSError as exc:
            log.warning("Could not write sector cache: %s", exc)

    # ------------------------------------------------------------ lookup

    def classify(self, symbol: str, instrument_type: str = "") -> tuple[str, str]:
        """Return (sector, industry) for an underlying symbol."""
        symbol = (symbol or "").strip().upper()
        if not symbol:
            return UNKNOWN, UNKNOWN

        # 1. Manual override wins over everything.
        if symbol in self._overrides:
            return self._overrides[symbol]

        # 2. Futures come from the local contract table.
        if symbol.startswith("/") or "Future" in instrument_type:
            spec = spec_for(symbol)
            if spec:
                return f"Futures: {spec.asset_class}", spec.sector
            return "Futures: Other", UNKNOWN

        if "Cryptocurrency" in instrument_type:
            return "Crypto", symbol.replace("/USD", "")

        # 3. Cached Yahoo lookup.
        entry = self._cache.get(symbol)
        if entry and (time.time() - entry.get("fetched_at", 0)) < CACHE_TTL_SECONDS:
            return entry.get("sector", UNKNOWN), entry.get("industry", UNKNOWN)

        sector, industry = self._fetch_yahoo(symbol)
        self._cache[symbol] = {
            "sector": sector,
            "industry": industry,
            "fetched_at": time.time(),
        }
        self._dirty = True
        return sector, industry

    @staticmethod
    def _fetch_yahoo(symbol: str) -> tuple[str, str]:
        try:
            import yfinance

            info = yfinance.Ticker(symbol).get_info() or {}
        except Exception as exc:  # yfinance raises a wide variety of errors
            log.warning("Yahoo lookup failed for %s: %s", symbol, exc)
            return UNKNOWN, UNKNOWN

        quote_type = (info.get("quoteType") or "").upper()
        if quote_type in ("ETF", "MUTUALFUND"):
            category = info.get("category") or info.get("fundFamily") or UNKNOWN
            return "ETF / Fund", category
        if quote_type == "INDEX":
            return "Index", info.get("shortName") or UNKNOWN

        return (
            info.get("sector") or UNKNOWN,
            info.get("industry") or UNKNOWN,
        )
