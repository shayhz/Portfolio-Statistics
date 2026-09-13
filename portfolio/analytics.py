"""Turns raw tastytrade API responses into a portfolio report.

Two different exposure numbers are computed for every position, because for
derivatives they answer different questions:

  market_value -- what the position itself is worth (an option's premium, a
                  share's price). For futures this is ~0, since futures are
                  cash-settled daily and carry no balance-sheet value.

  notional     -- the economic exposure the position gives you. For an equity
                  that equals market value; for an option it is
                  100 x underlying price (NOT delta-adjusted); for a future it
                  is price x the contract's point value, so one /ZN counts as
                  its full ~$110k of 10-year note exposure.

Allocation percentages are computed against *gross* notional (the sum of
absolute exposures), so a hedged book does not produce percentages over 100%.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal

from portfolio.classify import Classifier
from portfolio.correlation import PriceHistory
from portfolio.futures import root_symbol, spec_for
from portfolio.tasty import TastyClient, dec

log = logging.getLogger(__name__)

ZERO = Decimal(0)

OPTION_LIKE = {"Equity Option"}
FUTURE_LIKE = {"Future"}
FUTURE_OPTION_LIKE = {"Future Option"}


@dataclass
class Position:
    account: str
    symbol: str
    underlying: str
    group_key: str  # what allocation rolls up to: ticker, or futures root
    instrument_type: str
    quantity: Decimal  # signed: negative when short
    multiplier: Decimal
    price: Decimal  # per-unit mark
    underlying_price: Decimal | None
    market_value: Decimal
    notional: Decimal  # signed
    sector: str = "Unknown"
    industry: str = "Unknown"
    beta: Decimal | None = None
    margin: Decimal = ZERO
    expires_at: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def gross_notional(self) -> Decimal:
        return abs(self.notional)


def _f(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def _pct(part: Decimal, whole: Decimal) -> float:
    return float(part / whole * 100) if whole else 0.0


def _signed_quantity(raw: dict) -> Decimal:
    qty = dec(raw.get("quantity"))
    direction = (raw.get("quantity_direction") or "").lower()
    if direction == "short":
        return -abs(qty)
    if direction == "zero":
        return ZERO
    return abs(qty)


def _position_price(raw: dict) -> Decimal:
    """Prefer a live mark; fall back to the previous close."""
    for key in ("mark_price", "mark", "close_price", "average_open_price"):
        if raw.get(key) is not None:
            value = dec(raw.get(key), Decimal(-1))
            if value >= 0:
                return value
    return ZERO


class PortfolioBuilder:
    def __init__(
        self,
        client: TastyClient,
        classifier: Classifier | None = None,
        prices: PriceHistory | None = None,
    ):
        self.client = client
        self.classifier = classifier or Classifier()
        self.prices = prices if prices is not None else PriceHistory()
        self._quotes: dict[str, dict] = {}
        self._metrics: dict[str, dict] = {}
        self._futures: dict[str, dict] = {}

    # ------------------------------------------------------------- fetching

    def build(self, account_numbers: list[str] | None = None) -> dict:
        accounts = self.client.accounts()
        if account_numbers:
            wanted = {a.strip() for a in account_numbers if a.strip()}
            accounts = [a for a in accounts if a.get("account_number") in wanted]
        if not accounts:
            raise RuntimeError("No open tastytrade accounts found for this login.")

        raw_positions: list[dict] = []
        balances: list[dict] = []
        margin_by_account: dict[str, dict] = {}

        for acct in accounts:
            number = acct["account_number"]
            bal = self.client.balances(number)
            bal["nickname"] = acct.get("nickname") or number
            balances.append(bal)
            for pos in self.client.positions(number):
                pos["_account"] = number
                raw_positions.append(pos)
            margin_by_account[number] = self.client.margin_requirements(number)

        self._load_reference_data(raw_positions)
        margins = self._margin_by_underlying(margin_by_account)

        positions = [self._build_position(raw, margins) for raw in raw_positions]
        positions = [p for p in positions if p.quantity != 0]

        self.classifier.save()

        # Price history is downloaded once for every underlying across all
        # accounts, then sliced per view.
        try:
            self.prices.load([p.group_key for p in positions])
        except Exception as exc:  # noqa: BLE001 - correlation is not worth failing over
            log.warning("Correlation data unavailable: %s", exc)

        # One fully-computed view per account plus a combined one, so the UI can
        # switch accounts without re-querying the API. Allocation percentages are
        # only meaningful within a single view, so they cannot be derived on the
        # client by filtering the combined one.
        views = {"ALL": self._assemble(accounts, balances, positions)}
        for acct in accounts:
            number = acct["account_number"]
            views[number] = self._assemble(
                [acct],
                [b for b in balances if b.get("account_number") == number],
                [p for p in positions if p.account == number],
            )

        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "accounts": [
                {
                    "account_number": a.get("account_number"),
                    "nickname": a.get("nickname") or a.get("account_number"),
                    "type": a.get("account_type_name"),
                    "margin_or_cash": a.get("margin_or_cash"),
                    "net_liquidating_value": next(
                        (
                            float(dec(b.get("net_liquidating_value")))
                            for b in balances
                            if b.get("account_number") == a.get("account_number")
                        ),
                        0.0,
                    ),
                    "position_count": sum(
                        1 for p in positions if p.account == a.get("account_number")
                    ),
                }
                for a in accounts
            ],
            "views": views,
        }

    def _load_reference_data(self, raw_positions: list[dict]) -> None:
        equity_syms: set[str] = set()
        future_syms: set[str] = set()
        metric_syms: set[str] = set()

        for raw in raw_positions:
            itype = raw.get("instrument_type") or ""
            underlying = (raw.get("underlying_symbol") or "").strip()
            symbol = (raw.get("symbol") or "").strip()
            if itype in FUTURE_LIKE:
                future_syms.add(symbol)
            elif itype in FUTURE_OPTION_LIKE:
                if underlying:
                    future_syms.add(underlying)
            elif underlying:
                equity_syms.add(underlying)
                metric_syms.add(underlying)

        quotes = self.client.market_data(
            equities=sorted(equity_syms), futures=sorted(future_syms)
        )
        for quote in quotes:
            self._quotes[quote.get("symbol", "")] = quote

        for metric in self.client.market_metrics(sorted(metric_syms)):
            self._metrics[metric.get("symbol", "")] = metric

        for symbol in sorted(future_syms):
            try:
                self._futures[symbol] = self.client.future(symbol)
            except Exception as exc:  # noqa: BLE001 - proceed without the instrument
                log.warning("Future instrument lookup failed for %s: %s", symbol, exc)

    @staticmethod
    def _margin_by_underlying(margin_by_account: dict[str, dict]) -> dict[str, Decimal]:
        """Flatten the per-account margin reports into underlying -> requirement."""
        out: dict[str, Decimal] = defaultdict(Decimal)
        for report in margin_by_account.values():
            for group in report.get("groups") or []:
                if not isinstance(group, dict):
                    continue
                underlying = (group.get("underlying_symbol") or "").strip()
                if not underlying:
                    continue
                out[underlying] += abs(dec(group.get("margin_requirement")))
        return dict(out)

    # ------------------------------------------------------------ per position

    def _quote_price(self, symbol: str) -> Decimal | None:
        quote = self._quotes.get(symbol)
        if not quote:
            return None
        for key in ("mark", "last", "mid", "close", "prev_close"):
            if quote.get(key) is not None:
                value = dec(quote.get(key), Decimal(-1))
                if value > 0:
                    return value
        return None

    def _notional_multiplier(self, symbol: str, warnings: list[str]) -> Decimal:
        """Point value for a futures contract, preferring the API's own figure."""
        api_value: Decimal | None = None
        instrument = self._futures.get(symbol)
        if instrument:
            candidate = dec(instrument.get("notional_multiplier"), ZERO)
            if candidate > 0:
                api_value = candidate

        spec = spec_for(symbol)
        table_value = spec.point_value if spec else None

        if api_value and table_value and api_value != table_value:
            warnings.append(
                f"Point value mismatch for {symbol}: API says {api_value}, "
                f"local table says {table_value}. Using the API value."
            )
        if api_value:
            return api_value
        if table_value:
            warnings.append(f"No API point value for {symbol}; used local table.")
            return table_value
        warnings.append(
            f"Unknown point value for {symbol}; notional treated as 1x price. "
            "Add the contract to portfolio/futures.py to fix this."
        )
        return Decimal(1)

    def _build_position(self, raw: dict, margins: dict[str, Decimal]) -> Position:
        warnings: list[str] = []
        itype = raw.get("instrument_type") or "Unknown"
        symbol = (raw.get("symbol") or "").strip()
        underlying = (raw.get("underlying_symbol") or "").strip() or symbol
        qty = _signed_quantity(raw)
        price = _position_price(raw)
        multiplier = dec(raw.get("multiplier"), Decimal(1)) or Decimal(1)

        underlying_price: Decimal | None = None
        group_key = underlying

        if itype in FUTURE_LIKE:
            point_value = self._notional_multiplier(symbol, warnings)
            live = self._quote_price(symbol)
            if live:
                price = live
            market_value = ZERO  # futures carry no balance-sheet value
            notional = qty * price * point_value
            multiplier = point_value
            underlying_price = price
            group_key = "/" + root_symbol(symbol)

        elif itype in FUTURE_OPTION_LIKE:
            market_value = qty * price * multiplier
            underlying_price = self._quote_price(underlying)
            point_value = self._notional_multiplier(underlying, warnings)
            if underlying_price:
                notional = qty * underlying_price * point_value
            else:
                notional = market_value
                warnings.append(
                    f"No underlying quote for {underlying}; used premium as notional."
                )
            group_key = "/" + root_symbol(underlying)

        elif itype in OPTION_LIKE:
            market_value = qty * price * multiplier
            underlying_price = self._quote_price(underlying)
            if underlying_price:
                notional = qty * underlying_price * multiplier
            else:
                notional = market_value
                warnings.append(
                    f"No underlying quote for {underlying}; used premium as notional."
                )

        else:  # equity, crypto, bonds, equity offerings
            market_value = qty * price * multiplier
            notional = market_value
            underlying_price = price

        sector, industry = self.classifier.classify(group_key, itype)

        beta = None
        metric = self._metrics.get(underlying)
        if metric and metric.get("beta") is not None:
            beta = dec(metric["beta"], ZERO)
        elif self._quotes.get(underlying, {}).get("beta") is not None:
            beta = dec(self._quotes[underlying]["beta"], ZERO)

        expires = raw.get("expires_at")

        return Position(
            account=raw.get("_account", ""),
            symbol=symbol,
            underlying=underlying,
            group_key=group_key,
            instrument_type=itype,
            quantity=qty,
            multiplier=multiplier,
            price=price,
            underlying_price=underlying_price,
            market_value=market_value,
            notional=notional,
            sector=sector,
            industry=industry,
            beta=beta,
            margin=margins.get(underlying, ZERO),
            expires_at=str(expires) if expires else None,
            warnings=warnings,
        )

    # -------------------------------------------------------------- assembly

    def _assemble(
        self, accounts: list[dict], balances: list[dict], positions: list[Position]
    ) -> dict:
        totals = _totals(balances)
        gross = sum((p.gross_notional for p in positions), ZERO)
        net = sum((p.notional for p in positions), ZERO)

        # Margin is reported per underlying, so credit it once per group.
        margin_by_group: dict[str, Decimal] = {}
        for pos in positions:
            margin_by_group.setdefault(pos.group_key, ZERO)
            margin_by_group[pos.group_key] = max(margin_by_group[pos.group_key], pos.margin)
        total_margin = sum(margin_by_group.values(), ZERO)

        by_symbol = _group(positions, lambda p: p.group_key, gross, margin_by_group, total_margin)
        by_sector = _group(positions, lambda p: p.sector, gross, margin_by_group, total_margin)
        by_industry = _group(positions, lambda p: p.industry, gross, margin_by_group, total_margin)
        by_type = _group(
            positions, lambda p: p.instrument_type, gross, margin_by_group, total_margin
        )

        warnings = sorted({w for p in positions for w in p.warnings})

        try:
            correlation = self.prices.matrix_for([p.group_key for p in positions])
        except Exception as exc:  # noqa: BLE001
            log.warning("Correlation matrix failed: %s", exc)
            correlation = {"symbols": [], "matrix": [], "missing": [], "observations": 0}

        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "accounts": [
                {
                    "account_number": a.get("account_number"),
                    "nickname": a.get("nickname"),
                    "type": a.get("account_type_name"),
                    "margin_or_cash": a.get("margin_or_cash"),
                }
                for a in accounts
            ],
            "cash": totals,
            "exposure": {
                "gross_notional": float(gross),
                "net_notional": float(net),
                "long_notional": float(sum((p.notional for p in positions if p.notional > 0), ZERO)),
                "short_notional": float(sum((p.notional for p in positions if p.notional < 0), ZERO)),
                "total_margin": float(total_margin),
            },
            "risk": {
                **_risk(positions, totals, gross, net, total_margin, self._metrics, self._quotes),
                "average_correlation": correlation.get("average_correlation"),
            },
            "correlation": correlation,
            "allocation": {
                "by_symbol": by_symbol,
                "by_sector": by_sector,
                "by_industry": by_industry,
                "by_instrument_type": by_type,
            },
            "positions": [
                {
                    "account": p.account,
                    "symbol": p.symbol,
                    "underlying": p.underlying,
                    "group": p.group_key,
                    "instrument_type": p.instrument_type,
                    "quantity": _f(p.quantity),
                    "multiplier": _f(p.multiplier),
                    "price": _f(p.price),
                    "underlying_price": _f(p.underlying_price),
                    "market_value": _f(p.market_value),
                    "notional": _f(p.notional),
                    "pct_of_gross": _pct(p.gross_notional, gross),
                    "sector": p.sector,
                    "industry": p.industry,
                    "beta": _f(p.beta),
                    "margin": _f(p.margin),
                    "expires_at": p.expires_at,
                }
                for p in sorted(positions, key=lambda p: -p.gross_notional)
            ],
            "warnings": warnings,
        }


def _totals(balances: list[dict]) -> dict:
    def total(key: str) -> Decimal:
        return sum((dec(b.get(key)) for b in balances), ZERO)

    net_liq = total("net_liquidating_value")
    cash = total("cash_balance")
    return {
        "net_liquidating_value": float(net_liq),
        "cash_balance": float(cash),
        "cash_available_to_withdraw": float(total("cash_available_to_withdraw")),
        "equity_buying_power": float(total("equity_buying_power")),
        "derivative_buying_power": float(total("derivative_buying_power")),
        "maintenance_requirement": float(total("maintenance_requirement")),
        "maintenance_excess": float(total("maintenance_excess")),
        "futures_margin_requirement": float(total("futures_margin_requirement")),
        "used_derivative_buying_power": float(total("used_derivative_buying_power")),
        "pending_cash": float(total("pending_cash")),
        "cash_pct_of_net_liq": _pct(cash, net_liq),
        "per_account": [
            {
                "account_number": b.get("account_number"),
                "nickname": b.get("nickname"),
                "net_liquidating_value": float(dec(b.get("net_liquidating_value"))),
                "cash_balance": float(dec(b.get("cash_balance"))),
                "maintenance_requirement": float(dec(b.get("maintenance_requirement"))),
            }
            for b in balances
        ],
    }


def _group(
    positions: list[Position],
    key_fn,
    gross: Decimal,
    margin_by_group: dict[str, Decimal],
    total_margin: Decimal,
) -> list[dict]:
    buckets: dict[str, list[Position]] = defaultdict(list)
    for pos in positions:
        buckets[key_fn(pos) or "Unknown"].append(pos)

    rows = []
    for name, group in buckets.items():
        group_gross = sum((p.gross_notional for p in group), ZERO)
        group_net = sum((p.notional for p in group), ZERO)
        # Sum margin over the distinct underlyings represented in this bucket.
        margin = sum(
            (margin_by_group.get(k, ZERO) for k in {p.group_key for p in group}), ZERO
        )

        # The underlyings inside this bucket, largest first. Drives the hover
        # panel on the sector and industry views.
        member_totals: dict[str, Decimal] = defaultdict(Decimal)
        member_net: dict[str, Decimal] = defaultdict(Decimal)
        for pos in group:
            member_totals[pos.group_key] += pos.gross_notional
            member_net[pos.group_key] += pos.notional
        members = [
            {
                "name": key,
                "gross_notional": float(value),
                "pct_of_parent": _pct(value, group_gross),
                "pct_of_gross": _pct(value, gross),
                "direction": "Long" if member_net[key] > 0
                else "Short" if member_net[key] < 0 else "Flat",
            }
            for key, value in sorted(member_totals.items(), key=lambda kv: -kv[1])
        ]

        rows.append(
            {
                "name": name,
                "gross_notional": float(group_gross),
                "net_notional": float(group_net),
                "market_value": float(sum((p.market_value for p in group), ZERO)),
                "pct_of_gross": _pct(group_gross, gross),
                "margin": float(margin),
                "pct_of_margin": _pct(margin, total_margin),
                "position_count": len(group),
                "direction": "Long" if group_net > 0 else "Short" if group_net < 0 else "Flat",
                # Present when every position in the bucket agrees, which is
                # always the case on the by-symbol view.
                "sector": _uniform(p.sector for p in group),
                "industry": _uniform(p.industry for p in group),
                "members": members,
            }
        )
    return sorted(rows, key=lambda r: -r["gross_notional"])


def _uniform(values) -> str | None:
    """The single shared value, or None when the group disagrees."""
    distinct = set(values)
    return distinct.pop() if len(distinct) == 1 else None


def _risk(
    positions: list[Position],
    totals: dict,
    gross: Decimal,
    net: Decimal,
    total_margin: Decimal,
    metrics: dict[str, dict],
    quotes: dict[str, dict],
) -> dict:
    net_liq = Decimal(str(totals["net_liquidating_value"]))

    # Beta-weighted exposure: each position's notional scaled by its beta to SPY.
    beta_weighted = ZERO
    beta_covered = ZERO
    for pos in positions:
        if pos.beta is not None:
            beta_weighted += pos.notional * pos.beta
            beta_covered += pos.gross_notional

    # Herfindahl-Hirschman concentration index on gross notional (0-10000).
    hhi = ZERO
    by_group: dict[str, Decimal] = defaultdict(Decimal)
    for pos in positions:
        by_group[pos.group_key] += pos.gross_notional
    if gross:
        for value in by_group.values():
            share = value / gross * 100
            hhi += share * share

    largest = max(by_group.items(), key=lambda kv: kv[1], default=("", ZERO))

    # Dividend yield and IV rank, weighted by gross notional of the underlying.
    div_weighted = ZERO
    div_base = ZERO
    ivr_weighted = ZERO
    ivr_base = ZERO
    corr_weighted = ZERO
    corr_base = ZERO
    for pos in positions:
        metric = metrics.get(pos.underlying)
        if not metric:
            continue
        weight = pos.gross_notional
        if metric.get("dividend_yield") is not None:
            div_weighted += dec(metric["dividend_yield"]) * weight
            div_base += weight
        if metric.get("implied_volatility_index_rank") is not None:
            ivr_weighted += dec(metric["implied_volatility_index_rank"]) * weight
            ivr_base += weight
        if metric.get("corr_spy_3month") is not None:
            corr_weighted += dec(metric["corr_spy_3month"]) * weight
            corr_base += weight

    option_positions = [p for p in positions if "Option" in p.instrument_type]
    future_positions = [p for p in positions if p.instrument_type in FUTURE_LIKE]

    return {
        "gross_leverage": float(gross / net_liq) if net_liq else None,
        "net_leverage": float(net / net_liq) if net_liq else None,
        "margin_utilization_pct": _pct(total_margin, net_liq),
        "beta_weighted_notional": float(beta_weighted),
        "beta_weighted_pct_of_net_liq": _pct(beta_weighted, net_liq),
        "beta_coverage_pct": _pct(beta_covered, gross),
        "concentration_hhi": float(hhi),
        "largest_position": largest[0],
        "largest_position_pct": _pct(largest[1], gross),
        "top5_pct": _pct(
            sum(sorted(by_group.values(), reverse=True)[:5], ZERO), gross
        ),
        "distinct_underlyings": len(by_group),
        "weighted_dividend_yield_pct": float(div_weighted / div_base * 100)
        if div_base
        else None,
        "weighted_iv_rank_pct": float(ivr_weighted / ivr_base * 100) if ivr_base else None,
        "weighted_spy_correlation": float(corr_weighted / corr_base) if corr_base else None,
        "option_position_count": len(option_positions),
        "future_position_count": len(future_positions),
        "futures_notional": float(sum((p.gross_notional for p in future_positions), ZERO)),
        "futures_pct_of_gross": _pct(
            sum((p.gross_notional for p in future_positions), ZERO), gross
        ),
        "nearest_expiration": min(
            (p.expires_at for p in positions if p.expires_at), default=None
        ),
    }


def build_report(client: TastyClient, account_numbers: list[str] | None = None) -> dict:
    return PortfolioBuilder(client).build(account_numbers)
