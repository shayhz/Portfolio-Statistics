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
    volatility: Decimal | None = None  # annualised, as a fraction
    average_open_price: Decimal = ZERO
    prev_close: Decimal | None = None
    earnings_date: str | None = None
    expires_at: str | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def gross_notional(self) -> Decimal:
        return abs(self.notional)

    @property
    def risk(self) -> Decimal:
        """Volatility-adjusted exposure: notional scaled by how much the
        underlying actually moves. Without this a Treasury future and a
        micro-cap look equivalent per dollar of notional, which they are not."""
        if self.volatility is None:
            return ZERO
        return self.gross_notional * self.volatility

    @property
    def cost_basis(self) -> Decimal:
        """What the position cost to put on, always positive."""
        return abs(self.average_open_price * self.quantity * self.multiplier)

    @property
    def unrealized_pnl(self) -> Decimal:
        """Signed quantity makes this work for shorts too: a short gains when
        the mark falls below the price it was opened at."""
        if not self.average_open_price:
            return ZERO
        return (self.price - self.average_open_price) * self.quantity * self.multiplier

    @property
    def day_pnl(self) -> Decimal:
        """Change since the previous close, not since the position was opened."""
        if self.prev_close is None:
            return ZERO
        return (self.price - self.prev_close) * self.quantity * self.multiplier

    @property
    def is_future(self) -> bool:
        return self.instrument_type in FUTURE_LIKE or self.instrument_type in FUTURE_OPTION_LIKE


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
        history_by_account: dict[str, list] = {}

        for acct in accounts:
            number = acct["account_number"]
            bal = self.client.balances(number)
            bal["nickname"] = acct.get("nickname") or number
            balances.append(bal)
            for pos in self.client.positions(number):
                pos["_account"] = number
                raw_positions.append(pos)
            margin_by_account[number] = self.client.margin_requirements(number)
            history_by_account[number] = self.client.net_liq_history(number)

        self._load_reference_data(raw_positions)
        margins = self._margin_by_underlying(margin_by_account, balances, raw_positions)

        positions = [self._build_position(raw, margins) for raw in raw_positions]
        positions = [p for p in positions if p.quantity != 0]

        self.classifier.save()

        # Price history is downloaded once for every underlying across all
        # accounts, then sliced per view.
        try:
            self.prices.load([p.group_key for p in positions])
        except Exception as exc:  # noqa: BLE001 - correlation is not worth failing over
            log.warning("Correlation data unavailable: %s", exc)

        # Volatility drives the risk basis, so attach it once per position.
        vols: dict[str, Decimal | None] = {}
        for pos in positions:
            if pos.group_key not in vols:
                try:
                    value = self.prices.volatility_for(pos.group_key)
                except Exception:  # noqa: BLE001
                    value = None
                vols[pos.group_key] = Decimal(str(value)) if value else None
            pos.volatility = vols[pos.group_key]

        # One fully-computed view per account plus a combined one, so the UI can
        # switch accounts without re-querying the API. Allocation percentages are
        # only meaningful within a single view, so they cannot be derived on the
        # client by filtering the combined one.
        equities_only = [p for p in positions if not p.is_future]

        all_history = _combine_history(history_by_account)
        views = {"ALL": self._assemble(accounts, balances, positions, all_history)}
        views_ex_futures = {
            "ALL": self._assemble(accounts, balances, equities_only, all_history)
        }
        for acct in accounts:
            number = acct["account_number"]
            acct_balances = [b for b in balances if b.get("account_number") == number]
            acct_history = _combine_history({number: history_by_account.get(number, [])})
            views[number] = self._assemble(
                [acct], acct_balances, [p for p in positions if p.account == number],
                acct_history,
            )
            views_ex_futures[number] = self._assemble(
                [acct], acct_balances,
                [p for p in equities_only if p.account == number], acct_history,
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
            # Same shape, with futures and futures options removed, for the
            # "equities only" toggle in the UI.
            "views_ex_futures": views_ex_futures,
            "has_futures": bool(positions) and len(equities_only) != len(positions),
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
    def _margin_by_underlying(
        margin_by_account: dict[str, dict],
        balances: list[dict],
        raw_positions: list[dict],
    ) -> dict[tuple[str, str], Decimal]:
        """Margin requirement keyed by (account, underlying).

        Keyed per account because the same underlying can be held in several
        accounts with different requirements; a single underlying -> margin map
        would credit one account with another's margin.

        tastytrade's margin report covers equities only -- futures margin is
        reported once per account as `futures-margin-requirement`, so it is
        attributed across that account's futures positions in proportion to
        notional. With a single futures position the split is exact.
        """
        out: dict[tuple[str, str], Decimal] = defaultdict(Decimal)

        for account, report in margin_by_account.items():
            for group in report.get("groups") or []:
                if not isinstance(group, dict):
                    continue
                underlying = (group.get("underlying_symbol") or "").strip()
                if not underlying:
                    continue
                out[(account, underlying)] += abs(dec(group.get("margin_requirement")))

        for balance in balances:
            account = balance.get("account_number") or ""
            futures_margin = abs(dec(balance.get("futures_margin_requirement")))
            if not futures_margin:
                continue
            futures = [
                p for p in raw_positions
                if p.get("_account") == account
                and (p.get("instrument_type") or "") in (FUTURE_LIKE | FUTURE_OPTION_LIKE)
            ]
            weights: dict[str, Decimal] = defaultdict(Decimal)
            for pos in futures:
                underlying = (pos.get("underlying_symbol") or pos.get("symbol") or "").strip()
                weights[underlying] += abs(dec(pos.get("quantity"))) or Decimal(1)
            total = sum(weights.values(), ZERO)
            if not total:
                continue
            for underlying, weight in weights.items():
                out[(account, underlying)] += futures_margin * weight / total

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

    def _build_position(
        self, raw: dict, margins: dict[tuple[str, str], Decimal]
    ) -> Position:
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

        earnings_date = None
        if metric and isinstance(metric.get("earnings"), dict):
            earnings_date = metric["earnings"].get("expected_report_date")

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
            margin=margins.get((raw.get("_account", ""), underlying), ZERO),
            average_open_price=dec(raw.get("average_open_price")),
            prev_close=dec(raw.get("close_price")) if raw.get("close_price") is not None else None,
            earnings_date=earnings_date,
            expires_at=str(expires) if expires else None,
            warnings=warnings,
        )

    # -------------------------------------------------------------- assembly

    def _assemble(
        self,
        accounts: list[dict],
        balances: list[dict],
        positions: list[Position],
        history: list[dict] | None = None,
    ) -> dict:
        totals = _totals(balances)
        gross = sum((p.gross_notional for p in positions), ZERO)
        net = sum((p.notional for p in positions), ZERO)

        # Margin is reported once per (account, underlying), so de-duplicate on
        # that pair before summing -- stock and its options share one figure,
        # but the same underlying in two accounts is two separate requirements.
        per_pair: dict[tuple[str, str], Decimal] = {}
        for pos in positions:
            per_pair[(pos.account, pos.group_key)] = pos.margin
        margin_by_group: dict[str, Decimal] = defaultdict(Decimal)
        for (_account, key), value in per_pair.items():
            margin_by_group[key] += value
        margin_by_group = dict(margin_by_group)
        total_margin = sum(margin_by_group.values(), ZERO)

        total_risk = sum((p.risk for p in positions), ZERO)

        def grp(key_fn):
            return _group(positions, key_fn, gross, margin_by_group, total_margin, total_risk)

        by_symbol = grp(lambda p: p.group_key)
        by_sector = grp(lambda p: p.sector)
        by_industry = grp(lambda p: p.industry)
        by_type = grp(lambda p: p.instrument_type)

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
                "total_risk": float(total_risk),
            },
            "risk": {
                **_risk(positions, totals, gross, net, total_margin, self._metrics, self._quotes),
                "average_correlation": correlation.get("average_correlation"),
                "total_risk": float(total_risk),
                "risk_pct_of_net_liq": _pct(total_risk, Decimal(str(totals["net_liquidating_value"]))),
            },
            "correlation": correlation,
            "performance": _performance(history or [], positions),
            "earnings": _upcoming_earnings(positions),
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
                    "volatility_pct": float(p.volatility * 100) if p.volatility is not None else None,
                    "risk": _f(p.risk),
                    "average_open_price": _f(p.average_open_price),
                    "cost_basis": _f(p.cost_basis),
                    "unrealized_pnl": _f(p.unrealized_pnl),
                    "unrealized_pnl_pct": _pct(p.unrealized_pnl, p.cost_basis),
                    "day_pnl": _f(p.day_pnl),
                    "earnings_date": p.earnings_date,
                    "pct_of_risk": _pct(p.risk, total_risk),
                    "is_future": p.is_future,
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
    total_risk: Decimal,
) -> list[dict]:
    buckets: dict[str, list[Position]] = defaultdict(list)
    for pos in positions:
        buckets[key_fn(pos) or "Unknown"].append(pos)

    rows = []
    for name, group in buckets.items():
        group_gross = sum((p.gross_notional for p in group), ZERO)
        group_net = sum((p.notional for p in group), ZERO)
        group_risk = sum((p.risk for p in group), ZERO)
        group_cost = sum((p.cost_basis for p in group), ZERO)
        group_pnl = sum((p.unrealized_pnl for p in group), ZERO)
        # Notional-weighted volatility of the bucket's underlyings.
        group_vol = (group_risk / group_gross) if group_gross else None
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
                "risk": float(group_risk),
                "pct_of_risk": _pct(group_risk, total_risk),
                "volatility_pct": float(group_vol * 100) if group_vol is not None else None,
                "unrealized_pnl": float(group_pnl),
                "unrealized_pnl_pct": _pct(group_pnl, group_cost),
                "day_pnl": float(sum((p.day_pnl for p in group), ZERO)),
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


def _combine_history(history_by_account: dict[str, list]) -> list[dict]:
    """Merge per-account net-liq series into one portfolio curve.

    Accounts can start on different dates and skip days, so each account's last
    known value is carried forward and the accounts are summed per date. Without
    the carry-forward, a day missing from one account would look like a crash in
    the combined curve.
    """
    per_account: dict[str, dict[str, Decimal]] = {}
    all_dates: set[str] = set()
    for account, points in history_by_account.items():
        series: dict[str, Decimal] = {}
        for point in points or []:
            stamp = (point.get("time") or "")[:10]
            if not stamp:
                continue
            series[stamp] = dec(point.get("close"))
            all_dates.add(stamp)
        if series:
            per_account[account] = series

    if not per_account:
        return []

    out: list[dict] = []
    last: dict[str, Decimal] = {}
    for stamp in sorted(all_dates):
        total = ZERO
        for account, series in per_account.items():
            if stamp in series:
                last[account] = series[stamp]
            if account in last:
                total += last[account]
        out.append({"date": stamp, "value": float(total)})
    return out


def _performance(history: list[dict], positions: list[Position]) -> dict:
    """Equity curve statistics plus current unrealized P&L."""
    cost = sum((p.cost_basis for p in positions), ZERO)
    unrealized = sum((p.unrealized_pnl for p in positions), ZERO)
    day = sum((p.day_pnl for p in positions), ZERO)

    winners = [p for p in positions if p.unrealized_pnl > 0]
    losers = [p for p in positions if p.unrealized_pnl < 0]
    best = max(positions, key=lambda p: p.unrealized_pnl, default=None)
    worst = min(positions, key=lambda p: p.unrealized_pnl, default=None)

    stats = {
        "cost_basis": float(cost),
        "unrealized_pnl": float(unrealized),
        "unrealized_pnl_pct": _pct(unrealized, cost),
        "day_pnl": float(day),
        "winners": len(winners),
        "losers": len(losers),
        "best": {"symbol": best.symbol, "pnl": _f(best.unrealized_pnl)} if best else None,
        "worst": {"symbol": worst.symbol, "pnl": _f(worst.unrealized_pnl)} if worst else None,
        "history": history,
    }

    values = [h["value"] for h in history if h["value"] > 0]
    if len(values) < 2:
        return {**stats, "max_drawdown_pct": None, "returns": {}}

    # Peak-to-trough on the daily closes.
    peak = values[0]
    max_dd = 0.0
    peak_date = trough_date = None
    running_peak_date = history[0]["date"]
    for point in history:
        value = point["value"]
        if value <= 0:
            continue
        if value > peak:
            peak = value
            running_peak_date = point["date"]
        drawdown = (value / peak - 1) * 100 if peak else 0.0
        if drawdown < max_dd:
            max_dd = drawdown
            peak_date, trough_date = running_peak_date, point["date"]

    current = values[-1]

    # tastytrade returns one point per *calendar* day, so periods are looked up
    # by date rather than by counting rows -- counting trading days here would
    # make "1y" reach back only about eight months.
    from datetime import date as _date, timedelta as _timedelta

    usable = [h for h in history if h["value"] > 0]
    try:
        end = _date.fromisoformat(usable[-1]["date"])
    except (ValueError, IndexError):
        end = None

    returns = {}
    if end:
        for label, days in (("1m", 30), ("3m", 91), ("6m", 182), ("1y", 365)):
            target = end - _timedelta(days=days)
            prior = [h for h in usable if _date.fromisoformat(h["date"]) <= target]
            if not prior:
                continue
            # Only report a period the history actually spans: the oldest point
            # must be close to the target, not months short of it.
            gap = (target - _date.fromisoformat(prior[-1]["date"])).days
            if gap > 10:
                continue
            start = prior[-1]["value"]
            if start:
                returns[label] = round((current / start - 1) * 100, 2)

    return {
        **stats,
        "start_value": values[0],
        "current_value": current,
        "peak_value": max(values),
        "max_drawdown_pct": round(max_dd, 2),
        "max_drawdown_peak_date": peak_date,
        "max_drawdown_trough_date": trough_date,
        "returns": returns,
    }


def _upcoming_earnings(positions: list[Position], days_ahead: int = 60) -> list[dict]:
    """Holdings reporting earnings soon, nearest first.

    Relevant for short premium: an earnings print inside the life of a short
    option is the main scheduled gap risk in an equity book.
    """
    from datetime import date as _date

    today = _date.today()
    by_symbol: dict[str, dict] = {}
    for pos in positions:
        if not pos.earnings_date:
            continue
        try:
            when = _date.fromisoformat(pos.earnings_date)
        except ValueError:
            continue
        delta = (when - today).days
        if delta < 0 or delta > days_ahead:
            continue
        row = by_symbol.setdefault(
            pos.group_key,
            {
                "symbol": pos.group_key,
                "date": pos.earnings_date,
                "days_away": delta,
                "gross_notional": 0.0,
                "has_options": False,
            },
        )
        row["gross_notional"] += float(pos.gross_notional)
        if "Option" in pos.instrument_type:
            row["has_options"] = True
    return sorted(by_symbol.values(), key=lambda r: (r["days_away"], -r["gross_notional"]))
