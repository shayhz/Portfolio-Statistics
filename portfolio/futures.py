"""Futures contract specs and notional math.

tastytrade returns a ``notional-multiplier`` on the Future instrument, and that is
what we use whenever the API gives it to us. The table below is a fallback for when
the instrument lookup fails, and -- more usefully -- a sanity check: if the API
multiplier and our table disagree, the discrepancy is surfaced in the UI rather than
silently producing a wrong notional.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

MONTH_CODES = "FGHJKMNQUVXZ"
_ROOT_RE = re.compile(rf"^/?([A-Z0-9]+?)[{MONTH_CODES}]\d{{1,2}}$")


@dataclass(frozen=True)
class ContractSpec:
    root: str
    name: str
    point_value: Decimal  # dollars per 1.00 move in the quoted price
    asset_class: str
    sector: str


def _s(root, name, pv, asset_class, sector):
    return ContractSpec(root, name, Decimal(str(pv)), asset_class, sector)


# Dollars per full point of the *displayed* price.
CONTRACT_SPECS: dict[str, ContractSpec] = {
    s.root: s
    for s in [
        # --- Equity index ---
        _s("ES", "E-mini S&P 500", 50, "Equity Index", "US Large Cap"),
        _s("MES", "Micro E-mini S&P 500", 5, "Equity Index", "US Large Cap"),
        _s("NQ", "E-mini Nasdaq 100", 20, "Equity Index", "US Tech"),
        _s("MNQ", "Micro E-mini Nasdaq 100", 2, "Equity Index", "US Tech"),
        _s("RTY", "E-mini Russell 2000", 50, "Equity Index", "US Small Cap"),
        _s("M2K", "Micro E-mini Russell 2000", 5, "Equity Index", "US Small Cap"),
        _s("YM", "E-mini Dow", 5, "Equity Index", "US Large Cap"),
        _s("MYM", "Micro E-mini Dow", 0.5, "Equity Index", "US Large Cap"),
        _s("EMD", "E-mini S&P MidCap 400", 100, "Equity Index", "US Mid Cap"),
        _s("NKD", "Nikkei 225 (USD)", 5, "Equity Index", "Intl Equity"),
        # --- Interest rates / bonds ---
        _s("ZT", "2-Year T-Note", 2000, "Rates", "US Treasuries"),
        _s("Z3N", "3-Year T-Note", 2000, "Rates", "US Treasuries"),
        _s("ZF", "5-Year T-Note", 1000, "Rates", "US Treasuries"),
        _s("ZN", "10-Year T-Note", 1000, "Rates", "US Treasuries"),
        _s("TN", "Ultra 10-Year T-Note", 1000, "Rates", "US Treasuries"),
        _s("ZB", "30-Year T-Bond", 1000, "Rates", "US Treasuries"),
        _s("UB", "Ultra T-Bond", 1000, "Rates", "US Treasuries"),
        _s("ZQ", "30-Day Fed Funds", 4167, "Rates", "Short-Term Rates"),
        _s("SR3", "3-Month SOFR", 2500, "Rates", "Short-Term Rates"),
        _s("SR1", "1-Month SOFR", 4167, "Rates", "Short-Term Rates"),
        # --- Energy ---
        _s("CL", "Crude Oil (WTI)", 1000, "Energy", "Crude Oil"),
        _s("MCL", "Micro Crude Oil", 100, "Energy", "Crude Oil"),
        _s("QM", "E-mini Crude Oil", 500, "Energy", "Crude Oil"),
        _s("BZ", "Brent Crude", 1000, "Energy", "Crude Oil"),
        _s("NG", "Natural Gas", 10000, "Energy", "Natural Gas"),
        _s("QG", "E-mini Natural Gas", 2500, "Energy", "Natural Gas"),
        _s("RB", "RBOB Gasoline", 42000, "Energy", "Refined Products"),
        _s("HO", "NY Harbor ULSD", 42000, "Energy", "Refined Products"),
        # --- Metals ---
        _s("GC", "Gold", 100, "Metals", "Precious Metals"),
        _s("MGC", "Micro Gold", 10, "Metals", "Precious Metals"),
        _s("SI", "Silver", 5000, "Metals", "Precious Metals"),
        _s("SIL", "Micro Silver", 1000, "Metals", "Precious Metals"),
        _s("PL", "Platinum", 50, "Metals", "Precious Metals"),
        _s("PA", "Palladium", 100, "Metals", "Precious Metals"),
        _s("HG", "Copper", 25000, "Metals", "Industrial Metals"),
        _s("MHG", "Micro Copper", 2500, "Metals", "Industrial Metals"),
        # --- Agriculture ---
        _s("ZC", "Corn", 50, "Agriculture", "Grains"),
        _s("ZS", "Soybeans", 50, "Agriculture", "Grains"),
        _s("ZW", "Chicago Wheat", 50, "Agriculture", "Grains"),
        _s("KE", "KC Wheat", 50, "Agriculture", "Grains"),
        _s("ZM", "Soybean Meal", 100, "Agriculture", "Grains"),
        _s("ZL", "Soybean Oil", 600, "Agriculture", "Grains"),
        _s("ZO", "Oats", 50, "Agriculture", "Grains"),
        _s("ZR", "Rough Rice", 2000, "Agriculture", "Grains"),
        _s("LE", "Live Cattle", 400, "Agriculture", "Livestock"),
        _s("GF", "Feeder Cattle", 500, "Agriculture", "Livestock"),
        _s("HE", "Lean Hogs", 400, "Agriculture", "Livestock"),
        _s("KC", "Coffee", 37500, "Agriculture", "Softs"),
        _s("SB", "Sugar #11", 112000, "Agriculture", "Softs"),
        _s("CC", "Cocoa", 10, "Agriculture", "Softs"),
        _s("CT", "Cotton", 500, "Agriculture", "Softs"),
        _s("OJ", "Orange Juice", 150, "Agriculture", "Softs"),
        # --- Currencies ---
        _s("6E", "Euro FX", 125000, "Currency", "EUR"),
        _s("M6E", "Micro Euro FX", 12500, "Currency", "EUR"),
        _s("6J", "Japanese Yen", 12500000, "Currency", "JPY"),
        _s("6B", "British Pound", 62500, "Currency", "GBP"),
        _s("M6B", "Micro British Pound", 6250, "Currency", "GBP"),
        _s("6A", "Australian Dollar", 100000, "Currency", "AUD"),
        _s("M6A", "Micro Australian Dollar", 10000, "Currency", "AUD"),
        _s("6C", "Canadian Dollar", 100000, "Currency", "CAD"),
        _s("6S", "Swiss Franc", 125000, "Currency", "CHF"),
        _s("6N", "New Zealand Dollar", 100000, "Currency", "NZD"),
        _s("6M", "Mexican Peso", 500000, "Currency", "MXN"),
        _s("DX", "US Dollar Index", 1000, "Currency", "USD Index"),
        # --- Volatility ---
        _s("VX", "VIX Futures", 1000, "Volatility", "VIX"),
        _s("VXM", "Mini VIX Futures", 100, "Volatility", "VIX"),
        # --- Crypto ---
        _s("BTC", "Bitcoin", 5, "Crypto", "Bitcoin"),
        _s("MBT", "Micro Bitcoin", 0.1, "Crypto", "Bitcoin"),
        _s("ETH", "Ether", 50, "Crypto", "Ethereum"),
        _s("MET", "Micro Ether", 0.1, "Crypto", "Ethereum"),
    ]
}


def root_symbol(symbol: str) -> str:
    """'/ZNZ4' -> 'ZN'. Falls back to the symbol minus its leading slash."""
    sym = (symbol or "").upper().strip()
    match = _ROOT_RE.match(sym)
    if match:
        return match.group(1)
    return sym.lstrip("/")


def spec_for(symbol: str) -> ContractSpec | None:
    return CONTRACT_SPECS.get(root_symbol(symbol))


# Micro and mini contracts track the same underlying as their full-size parent,
# so for correlation purposes they map to the parent's price history.
_MICRO_PARENT = {
    "MES": "ES", "MNQ": "NQ", "M2K": "RTY", "MYM": "YM",
    "MGC": "GC", "SIL": "SI", "MHG": "HG", "MCL": "CL", "QM": "CL",
    "QG": "NG", "M6E": "6E", "M6A": "6A", "M6B": "6B",
    "MBT": "BTC", "MET": "ETH", "VXM": "VX", "TN": "ZN", "Z3N": "ZT",
}

# Roots whose Yahoo ticker is not simply <root>=F.
_YAHOO_OVERRIDES = {
    "BTC": "BTC-USD",
    "ETH": "ETH-USD",
    "VX": "^VIX",
    "SR3": "^IRX",   # no Yahoo future; 13-week T-bill yield is the closest proxy
    "SR1": "^IRX",
    "ZQ": "^IRX",
    "UB": "ZB=F",    # Ultra Bond has no Yahoo series; 30Y bond is the proxy
    "KE": "KE=F",
}


def yahoo_symbol(symbol: str) -> str | None:
    """Yahoo Finance ticker for a futures symbol, for price history.

    Continuous front-month series (e.g. '/ZNZ6' -> 'ZN=F'). Returns None when
    the root is unknown, so the caller can report it as missing rather than
    silently correlating against the wrong thing.
    """
    root = root_symbol(symbol)
    root = _MICRO_PARENT.get(root, root)
    if root in _YAHOO_OVERRIDES:
        return _YAHOO_OVERRIDES[root]
    if root in CONTRACT_SPECS or _MICRO_PARENT.get(root):
        return f"{root}=F"
    return None
