"use strict";

const PALETTE = [
  "#4c9aff", "#3fb950", "#d29922", "#f85149", "#a371f7", "#39c5cf",
  "#db61a2", "#e3b341", "#7ee787", "#79c0ff", "#ffa657", "#d2a8ff",
];

let DATA = null; // full payload: { accounts, views }
let REPORT = null; // the currently selected view
let account = "ALL";
let tabKey = "by_symbol";
let basis = "pct_of_gross";
let exFutures = false;

// Preferences live on the server, not in localStorage: the app serves on a
// different port each launch when 8765 is taken, and localStorage is scoped to
// the origin *including the port*, so it would not survive a restart.
let PREFS = { account: "ALL", tab: "by_symbol", basis: "pct_of_gross",
              ex_futures: false, sort: "notional", sort_dir: "desc", collapsed: [] };
let sortKey = "notional";
let sortDir = "desc";
let collapsed = new Set();

async function loadPrefs() {
  try {
    const res = await fetch("/api/prefs");
    if (res.ok) PREFS = { ...PREFS, ...(await res.json()) };
  } catch { /* fall back to defaults */ }
  account = PREFS.account;
  tabKey = PREFS.tab;
  basis = PREFS.basis;
  exFutures = !!PREFS.ex_futures;
  sortKey = PREFS.sort || "notional";
  sortDir = PREFS.sort_dir === "asc" ? "asc" : "desc";
  collapsed = new Set(Array.isArray(PREFS.collapsed) ? PREFS.collapsed : []);
}

let saveTimer = null;
function savePrefs(patch) {
  PREFS = { ...PREFS, ...patch };
  clearTimeout(saveTimer);
  // Debounced: clicking through tabs should not fire a write per click.
  saveTimer = setTimeout(() => {
    fetch("/api/prefs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(PREFS),
    }).catch(() => { /* a failed save is not worth interrupting the user */ });
  }, 250);
}

const $ = (sel) => document.querySelector(sel);

const money = (n) => {
  if (n === null || n === undefined) return "—";
  const abs = Math.abs(n);
  const sign = n < 0 ? "-" : "";
  if (abs >= 1e6) return `${sign}$${(abs / 1e6).toFixed(2)}M`;
  if (abs >= 1e3) return `${sign}$${(abs / 1e3).toFixed(1)}k`;
  return `${sign}$${abs.toFixed(0)}`;
};
const exact = (n) =>
  n === null || n === undefined
    ? "—"
    : n.toLocaleString("en-US", { style: "currency", currency: "USD",
                                  maximumFractionDigits: 0 });
const pct = (n, digits = 1) =>
  n === null || n === undefined ? "—" : `${n.toFixed(digits)}%`;
const num = (n, digits = 2) =>
  n === null || n === undefined ? "—" : n.toFixed(digits);

const colorFor = (i) => PALETTE[i % PALETTE.length];
const signClass = (n) => (n > 0 ? "pos" : n < 0 ? "neg" : "dim");

// ---------------------------------------------------------------- rendering

function renderKpis(r) {
  const c = r.cash, e = r.exposure, k = r.risk, pf = r.performance || {};
  const items = [
    { label: "Net liquidating value", value: exact(c.net_liquidating_value) },
    { label: "Cash available", value: exact(c.cash_balance),
      note: `${pct(c.cash_pct_of_net_liq)} of net liq · ${exact(c.cash_available_to_withdraw)} withdrawable` },
    { label: "Gross notional", value: exact(e.gross_notional),
      note: `${num(k.gross_leverage)}x net liq` },
    { label: "Net notional", value: exact(e.net_notional),
      note: `${num(k.net_leverage)}x net liq`,
      cls: e.net_notional >= 0 ? "good" : "bad" },
    { label: "Margin used", value: exact(e.total_margin),
      note: `${pct(k.margin_utilization_pct)} of net liq` },
    { label: "Buying power", value: exact(c.derivative_buying_power),
      note: `equity: ${exact(c.equity_buying_power)}` },
    { label: "Beta-weighted exposure", value: exact(k.beta_weighted_notional),
      note: `${pct(k.beta_weighted_pct_of_net_liq)} of net liq · ${pct(k.beta_coverage_pct, 0)} covered` },
    { label: "Unrealized P&L", value: exact(pf.unrealized_pnl),
      note: `${pct(pf.unrealized_pnl_pct)} on ${exact(pf.cost_basis)} cost · ${pf.winners}W / ${pf.losers}L`,
      cls: pf.unrealized_pnl >= 0 ? "good" : "bad" },
    { label: "Day P&L", value: exact(pf.day_pnl),
      note: pf.best ? `best ${pf.best.symbol} ${exact(pf.best.pnl)} · worst ${pf.worst.symbol} ${exact(pf.worst.pnl)}` : "",
      cls: pf.day_pnl >= 0 ? "good" : "bad" },
    { label: "Positions", value: String(r.positions.length),
      note: `${k.distinct_underlyings} underlyings · ${k.option_position_count} options · ${k.future_position_count} futures` },
  ];
  $("#kpis").innerHTML = items.map((i) => `
    <div class="kpi ${i.cls || ""}">
      <div class="label">${i.label}</div>
      <div class="value">${i.value}</div>
      ${i.note ? `<div class="note">${i.note}</div>` : ""}
    </div>`).join("");
}

const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (ch) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));

function renderAllocation() {
  const rows = REPORT.allocation[tabKey] || [];
  const sorted = [...rows].sort((a, b) => b[basis] - a[basis]);
  const max = Math.max(...sorted.map((r) => r[basis]), 1);
  const showClassification = tabKey === "by_symbol";

  $("#stack").innerHTML = sorted
    .filter((r) => r[basis] > 0)
    .map((r, i) => `<div style="width:${r[basis]}%;background:${colorFor(i)}"
                         title="${esc(r.name)}: ${pct(r[basis])}"></div>`)
    .join("");

  const valueKey = basis === "pct_of_margin" ? "margin"
    : basis === "pct_of_risk" ? "risk" : "gross_notional";
  const alloc = $("#alloc");
  alloc.classList.toggle("with-classification", showClassification);

  const basisLabel = basis === "pct_of_margin" ? "Margin"
    : basis === "pct_of_risk" ? "Risk" : "Notional";
  const header = `
    <div class="row head">
      <div>${showClassification ? "Symbol" : tabKey === "by_sector" ? "Sector"
            : tabKey === "by_industry" ? "Industry" : "Type"}</div>
      ${showClassification ? "<div>Sector</div><div>Industry</div><div class=\"num\">Vol</div>" : ""}
      <div></div>
      <div class="num">% ${basisLabel.toLowerCase()}</div>
      <div class="num">${basisLabel}</div>
      <div></div>
    </div>`;

  alloc.innerHTML = header + sorted.map((r, i) => `
    <div class="row" data-index="${i}">
      <div class="name" title="${esc(r.name)}">
        <span class="swatch" style="background:${colorFor(i)}"></span>${esc(r.name)}
      </div>
      ${showClassification ? `
        <div class="dim clip" title="${esc(r.sector || "")}">${esc(r.sector || "—")}</div>
        <div class="dim clip" title="${esc(r.industry || "")}">${esc(r.industry || "—")}</div>
        <div class="num dim" title="annualised volatility">${r.volatility_pct === null || r.volatility_pct === undefined ? "—" : r.volatility_pct.toFixed(0) + "%"}</div>
      ` : ""}
      <div class="bar">
        <span style="width:${(r[basis] / max) * 100}%;background:${colorFor(i)}"></span>
      </div>
      <div class="num">${pct(r[basis])}</div>
      <div class="num dim">${exact(r[valueKey])}</div>
      <div><span class="badge ${r.direction}">${r.direction}</span></div>
    </div>`).join("");

  // Hover panel listing the underlyings inside a bucket. Pointless on the
  // by-symbol view, where each row is already a single underlying.
  alloc.__rows = showClassification ? null : sorted;
}

// ------------------------------------------------------- constituent tooltip

const tip = document.createElement("div");
tip.className = "tip";
tip.hidden = true;
document.body.appendChild(tip);

function showTip(row, evt) {
  const members = row.members || [];
  if (members.length === 0) return;
  tip.innerHTML = `
    <div class="tip-head">${esc(row.name)}
      <span class="dim">· ${members.length} ${members.length === 1 ? "symbol" : "symbols"}</span>
    </div>
    ${members.map((m) => `
      <div class="tip-row">
        <span class="tip-sym">${esc(m.name)}</span>
        <span class="tip-bar"><i style="width:${m.pct_of_parent}%"></i></span>
        <span class="num">${pct(m.pct_of_parent)}</span>
        <span class="num dim">${exact(m.gross_notional)}</span>
      </div>`).join("")}
    <div class="tip-foot dim">% of ${esc(row.name)} · ${pct(row.pct_of_gross)} of portfolio</div>`;
  tip.hidden = false;
  moveTip(evt);
}

function moveTip(evt) {
  if (tip.hidden) return;
  const pad = 14;
  const rect = tip.getBoundingClientRect();
  let x = evt.clientX + pad;
  let y = evt.clientY + pad;
  if (x + rect.width > window.innerWidth - 8) x = evt.clientX - rect.width - pad;
  if (y + rect.height > window.innerHeight - 8) y = evt.clientY - rect.height - pad;
  tip.style.left = `${Math.max(8, x)}px`;
  tip.style.top = `${Math.max(8, y)}px`;
}

document.addEventListener("mouseover", (e) => {
  const rowEl = e.target.closest("#alloc .row:not(.head)");
  const rows = $("#alloc").__rows;
  const row = rowEl && rows ? rows[Number(rowEl.dataset.index)] : null;
  if (!row) {
    tip.hidden = true;
    return;
  }
  showTip(row, e);
});
document.addEventListener("mousemove", (e) => {
  if (!tip.hidden) moveTip(e);
});
document.addEventListener("mouseout", (e) => {
  if (!e.relatedTarget || !e.relatedTarget.closest?.("#alloc .row:not(.head)")) {
    tip.hidden = true;
  }
});

function renderRisk(r) {
  const k = r.risk, c = r.cash, e = r.exposure;
  const items = [
    ["Gross leverage", `${num(k.gross_leverage)}x`,
     "Gross notional ÷ net liq. Above 1x means leveraged exposure."],
    ["Net leverage", `${num(k.net_leverage)}x`,
     "Directional exposure after longs and shorts offset."],
    ["Beta-weighted exposure", exact(k.beta_weighted_notional),
     `Equivalent SPY exposure. Covers ${pct(k.beta_coverage_pct, 0)} of gross (futures lack beta).`],
    ["Largest position", `${k.largest_position || "—"} · ${pct(k.largest_position_pct)}`,
     "Single biggest underlying by gross notional."],
    ["Top 5 concentration", pct(k.top5_pct),
     "Share of gross notional in the five largest underlyings."],
    ["Concentration (HHI)", num(k.concentration_hhi, 0),
     "Herfindahl index, 0–10000. Above 2500 is concentrated; 10000 is a single position."],
    ["Margin utilisation", pct(k.margin_utilization_pct),
     `${exact(e.total_margin)} required against ${exact(c.net_liquidating_value)} net liq.`],
    ["Maintenance excess", exact(c.maintenance_excess),
     "Cushion before a margin call. Negative means a call."],
    ["Futures exposure", `${exact(k.futures_notional)} · ${pct(k.futures_pct_of_gross)}`,
     "Full contract notional, not margin posted."],
    ["Weighted dividend yield", pct(k.weighted_dividend_yield_pct, 2),
     "Notional-weighted across underlyings that pay one."],
    ["Weighted IV rank", pct(k.weighted_iv_rank_pct, 0),
     "Where implied vol sits in its 1-year range. High favours selling premium."],
    ["Weighted SPY correlation", num(k.weighted_spy_correlation),
     "3-month correlation. Near 1 means little diversification."],
    ["Long / short notional",
     `${money(e.long_notional)} / ${money(e.short_notional)}`,
     "Gross exposure on each side."],
    ["Nearest expiration", (k.nearest_expiration || "—").slice(0, 10),
     "Soonest expiring option or futures contract."],
  ];
  $("#risk").innerHTML = items.map(([label, value, hint]) => `
    <div class="riskitem">
      <div class="label">${label}</div>
      <div class="value">${value}</div>
      <div class="hint">${hint}</div>
    </div>`).join("");
}

// Diverging scale: teal for negative, neutral grey at zero, red for positive.
function corrColor(v) {
  if (v === null || v === undefined) return "var(--panel-2)";
  const t = Math.max(-1, Math.min(1, v));
  const alpha = 0.12 + Math.abs(t) * 0.78;
  return t >= 0
    ? `rgba(248, 81, 73, ${alpha})`
    : `rgba(57, 197, 207, ${alpha})`;
}

function renderPerformance(r) {
  const pf = r.performance || {};
  const pts = pf.history || [];
  const host = $("#curve");
  const meta = $("#perfmeta");

  if (pts.length < 2) {
    meta.textContent = "";
    host.innerHTML = `<div class="empty">No net liquidating value history for this account.</div>`;
    host.__pts = null;
  } else {
    const W = 1000, H = 190, pad = 6;
    const vals = pts.map((p) => p.value);
    const lo = Math.min(...vals), hi = Math.max(...vals);
    const span = hi - lo || 1;
    const x = (i) => (i / (pts.length - 1)) * W;
    const y = (v) => pad + (1 - (v - lo) / span) * (H - pad * 2);
    const line = pts.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(p.value).toFixed(1)}`).join("");
    const up = vals[vals.length - 1] >= vals[0];
    const stroke = up ? "var(--long)" : "var(--short)";

    meta.textContent = `${pts[0].date} → ${pts[pts.length - 1].date} · ${pts.length} days`;
    // Roughly six evenly spaced date ticks, always including both ends.
    const ticks = [];
    const want = Math.min(6, pts.length);
    for (let t = 0; t < want; t++) {
      const i = Math.round((t / (want - 1)) * (pts.length - 1));
      ticks.push({ i, date: pts[i].date, left: (i / (pts.length - 1)) * 100 });
    }

    host.innerHTML = `
      <div class="curve-wrap" id="curvewrap">
        <svg class="curve" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img"
             aria-label="Net liquidating value over time">
          <defs><linearGradient id="cg" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stop-color="${stroke}" stop-opacity="0.28"/>
            <stop offset="100%" stop-color="${stroke}" stop-opacity="0"/>
          </linearGradient></defs>
          <path d="${line}L${W},${H}L0,${H}Z" fill="url(#cg)"/>
          <path d="${line}" fill="none" stroke="${stroke}" stroke-width="2"
                vector-effect="non-scaling-stroke"/>
        </svg>
        <div class="crosshair" id="crosshair" hidden></div>
        <div class="cdot" id="cdot" hidden></div>
        <div class="ylab hi dim">${exact(hi)}</div>
        <div class="ylab lo dim">${exact(lo)}</div>
      </div>
      <div class="xaxis">
        ${ticks.map((t) => `<span style="left:${t.left}%">${t.date.slice(2)}</span>`).join("")}
      </div>`;

    // Kept for the hover handler: value lookup plus the y-scale to place the dot.
    host.__pts = pts;
    host.__scale = { lo, span, pad, H };
  }

  const ret = pf.returns || {};
  const retCell = (k) => ret[k] === undefined ? "—"
    : `<span class="${ret[k] >= 0 ? "pos" : "neg"}">${ret[k] >= 0 ? "+" : ""}${ret[k].toFixed(1)}%</span>`;
  $("#perfstats").innerHTML = `
    <div class="riskitem">
      <div class="label">Net liq return</div>
      <div class="value" style="font-size:15px">
        1m ${retCell("1m")} · 3m ${retCell("3m")} · 6m ${retCell("6m")} · 1y ${retCell("1y")}
      </div>
      <div class="hint">Change in net liquidating value, which includes deposits and withdrawals.</div>
    </div>
    <div class="riskitem">
      <div class="label">Max drawdown</div>
      <div class="value ${pf.max_drawdown_pct !== null && pf.max_drawdown_pct < -10 ? "neg" : ""}">
        ${pf.max_drawdown_pct === null || pf.max_drawdown_pct === undefined ? "—" : pf.max_drawdown_pct.toFixed(1) + "%"}</div>
      <div class="hint">${pf.max_drawdown_peak_date
        ? `Peak ${pf.max_drawdown_peak_date} → trough ${pf.max_drawdown_trough_date}` : "Largest peak-to-trough fall."}</div>
    </div>
    <div class="riskitem">
      <div class="label">Unrealized P&L</div>
      <div class="value ${pf.unrealized_pnl >= 0 ? "pos" : "neg"}">${exact(pf.unrealized_pnl)}</div>
      <div class="hint">${pct(pf.unrealized_pnl_pct)} on ${exact(pf.cost_basis)} of cost basis.</div>
    </div>`;
}

// Hovering the curve reports the exact day rather than making you eyeball it.
document.addEventListener("mousemove", (e) => {
  const host = $("#curve");
  const wrap = e.target.closest ? e.target.closest("#curvewrap") : null;
  const cross = $("#crosshair"), dot = $("#cdot");
  if (!wrap || !host.__pts) {
    if (cross) cross.hidden = true;
    if (dot) dot.hidden = true;
    if (tip.dataset.owner === "curve") tip.hidden = true;
    return;
  }
  const pts = host.__pts;
  const rect = wrap.getBoundingClientRect();
  const ratio = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
  const i = Math.round(ratio * (pts.length - 1));
  const point = pts[i];
  const { lo, span, pad, H } = host.__scale;
  const yFrac = (pad + (1 - (point.value - lo) / span) * (H - pad * 2)) / H;

  cross.hidden = false;
  cross.style.left = `${(i / (pts.length - 1)) * 100}%`;
  dot.hidden = false;
  dot.style.left = `${(i / (pts.length - 1)) * 100}%`;
  dot.style.top = `${yFrac * 100}%`;

  const prev = i > 0 ? pts[i - 1].value : point.value;
  const chg = point.value - prev;
  tip.dataset.owner = "curve";
  tip.innerHTML = `
    <div class="tip-head">${esc(point.date)}</div>
    <div class="tip-row" style="grid-template-columns:auto 1fr">
      <span class="dim">Net liq</span><span class="num"><b>${exact(point.value)}</b></span>
    </div>
    <div class="tip-row" style="grid-template-columns:auto 1fr">
      <span class="dim">Day change</span>
      <span class="num ${chg >= 0 ? "pos" : "neg"}">${chg >= 0 ? "+" : ""}${exact(chg)}</span>
    </div>`;
  tip.hidden = false;
  moveTip(e);
});

function renderEarnings(r) {
  const rows = r.earnings || [];
  $("#earnpanel").hidden = rows.length === 0;
  if (!rows.length) return;
  $("#earnings").innerHTML = rows.map((e) => `
    <div class="earn ${e.days_away <= 7 ? "urgent" : e.days_away <= 14 ? "near" : ""}">
      <span class="earn-sym">${esc(e.symbol)}</span>
      <span class="earn-when">${e.date}</span>
      <span>${e.days_away === 0 ? '<span class="badge Short">TODAY</span>'
        : e.days_away <= 7 ? `<span class="badge Short">in ${e.days_away}d</span>`
        : `<span class="dim">in ${e.days_away} days</span>`}</span>
      <span class="num dim">${exact(e.gross_notional)}</span>
      <span>${e.has_options ? '<span class="badge Short">has options</span>' : ""}</span>
    </div>`).join("");
}

function renderCorrelation(r) {
  const c = r.correlation || {};
  const meta = $("#corrmeta");
  const host = $("#corr");

  if (!c.symbols || c.symbols.length < 2) {
    meta.textContent = "";
    const why = c.observations && c.observations > 0
      ? `only ${c.observations} overlapping days of history`
      : "not enough price history";
    host.innerHTML = `<div class="empty">Correlation needs at least two underlyings with
      daily history — ${why}.${c.missing && c.missing.length
        ? ` No series for: ${c.missing.map(esc).join(", ")}.` : ""}</div>`;
    return;
  }

  const n = c.symbols.length;
  meta.textContent = `daily returns · ${c.observations} days · ${c.period}`
    + (c.missing && c.missing.length ? ` · no data for ${c.missing.join(", ")}` : "");

  const head = `<div class="ch corner"></div>` +
    c.symbols.map((s) => `<div class="ch" title="${esc(s)}"><span>${esc(s)}</span></div>`).join("");

  const body = c.symbols.map((rowSym, i) =>
    `<div class="rh" title="${esc(rowSym)}">${esc(rowSym)}</div>` +
    c.matrix[i].map((v, j) => {
      const self = i === j;
      return `<div class="cell${self ? " self" : ""}"
        style="background:${self ? "var(--line)" : corrColor(v)}"
        title="${esc(rowSym)} vs ${esc(c.symbols[j])}: ${v === null ? "n/a" : v.toFixed(2)}"
        >${self ? "" : v === null ? "" : v.toFixed(2)}</div>`;
    }).join("")
  ).join("");

  const pair = (p, label) => p
    ? `<div class="riskitem"><div class="label">${label}</div>
         <div class="value">${esc(p.a)} / ${esc(p.b)}</div>
         <div class="hint">${p.value.toFixed(2)}</div></div>`
    : "";

  host.innerHTML = `
    <div class="heat" style="grid-template-columns: 116px repeat(${n}, minmax(46px, 1fr))">
      ${head}${body}
    </div>
    <div class="legend">
      <span class="dim">-1</span>
      <i style="background:linear-gradient(90deg,
         rgba(57,197,207,.9), rgba(57,197,207,.12),
         rgba(248,81,73,.12), rgba(248,81,73,.9))"></i>
      <span class="dim">+1</span>
      <span class="dim legend-note">red = move together · teal = move opposite</span>
    </div>
    <div class="riskgrid" style="margin-top:14px">
      <div class="riskitem">
        <div class="label">Average pairwise correlation</div>
        <div class="value">${c.average_correlation === null ? "—" : c.average_correlation.toFixed(2)}</div>
        <div class="hint">Across ${n * (n - 1) / 2} pairs. Near 1 means the book is one bet.</div>
      </div>
      ${pair(c.most_correlated, "Most correlated pair")}
      ${pair(c.least_correlated, "Least correlated pair")}
    </div>`;
}

function sortPositions(rows) {
  const dir = sortDir === "asc" ? 1 : -1;
  return [...rows].sort((a, b) => {
    const x = a[sortKey], y = b[sortKey];
    // Missing values always sink, whichever way the column is sorted.
    if (x === null || x === undefined) return 1;
    if (y === null || y === undefined) return -1;
    if (typeof x === "string" || typeof y === "string") {
      return String(x).localeCompare(String(y)) * dir;
    }
    return (x - y) * dir;
  });
}

function syncSortHeaders() {
  document.querySelectorAll("#positions th[data-sort]").forEach((th) => {
    const active = th.dataset.sort === sortKey;
    th.classList.toggle("sorted", active);
    th.classList.toggle("asc", active && sortDir === "asc");
  });
}

function renderPositions(r) {
  const rows = sortPositions(r.positions);
  const meta = $("#posmeta");
  if (meta) meta.textContent = `${rows.length} positions · click a column to sort`;
  syncSortHeaders();
  $("#positions tbody").innerHTML = rows.map((p) => `
    <tr>
      <td><strong>${p.symbol}</strong></td>
      <td class="dim">${p.instrument_type}</td>
      <td class="dim">${p.sector}</td>
      <td class="dim">${p.industry}</td>
      <td class="num ${signClass(p.quantity)}">${num(p.quantity, 0)}</td>
      <td class="num">${num(p.price)}</td>
      <td class="num dim">${num(p.multiplier, 0)}</td>
      <td class="num ${signClass(p.market_value)}">${exact(p.market_value)}</td>
      <td class="num ${signClass(p.notional)}">${exact(p.notional)}</td>
      <td class="num">${pct(p.pct_of_gross)}</td>
      <td class="num ${signClass(p.unrealized_pnl)}">${exact(p.unrealized_pnl)}</td>
      <td class="num ${signClass(p.unrealized_pnl)}">${pct(p.unrealized_pnl_pct)}</td>
      <td class="num dim">${num(p.beta)}</td>
    </tr>`).join("");
}

document.querySelectorAll("#positions th[data-sort]").forEach((th) => {
  th.addEventListener("click", () => {
    const key = th.dataset.sort;
    if (key === sortKey) {
      sortDir = sortDir === "asc" ? "desc" : "asc";
    } else {
      sortKey = key;
      // Text reads naturally A-Z; numbers are almost always wanted largest first.
      sortDir = th.classList.contains("num") ? "desc" : "asc";
    }
    savePrefs({ sort: sortKey, sort_dir: sortDir });
    if (REPORT) renderPositions(REPORT);
  });
});

// ------------------------------------------------------------- collapsing

function applyCollapsed() {
  document.querySelectorAll("section.panel[data-panel]").forEach((panel) => {
    panel.classList.toggle("collapsed", collapsed.has(panel.dataset.panel));
  });
}

document.querySelectorAll("section.panel[data-panel]").forEach((panel) => {
  const head = panel.querySelector(".panel-head");
  if (!head) return;
  const btn = document.createElement("button");
  btn.className = "collapse";
  btn.type = "button";
  btn.setAttribute("aria-label", "Collapse or expand this section");
  head.prepend(btn);
  // The whole header is the hit target, but controls inside it must still work.
  head.addEventListener("click", (e) => {
    if (e.target.closest("button:not(.collapse), select, input, form, a")) return;
    const id = panel.dataset.panel;
    if (collapsed.has(id)) collapsed.delete(id); else collapsed.add(id);
    savePrefs({ collapsed: [...collapsed] });
    applyCollapsed();
  });
});

function renderAccountPicker() {
  const sel = $("#account");
  const total = DATA.accounts.reduce((s, a) => s + a.net_liquidating_value, 0);
  const opts = [
    `<option value="ALL">All accounts (${DATA.accounts.length}) — ${money(total)}</option>`,
    ...DATA.accounts.map((a) =>
      `<option value="${esc(a.account_number)}">${esc(a.nickname)} · ${esc(a.account_number)} — ${money(a.net_liquidating_value)}</option>`),
  ];
  sel.innerHTML = opts.join("");
  if (!viewSet()[account]) {
    // The remembered account is gone (closed, or excluded by TASTYTRADE_ACCOUNT).
    account = "ALL";
    savePrefs({ account });
  }
  sel.value = account;
}

function viewSet() {
  return exFutures && DATA.views_ex_futures ? DATA.views_ex_futures : DATA.views;
}

function renderView() {
  const set = viewSet();
  const r = set[account] || set[DATA.selected] || set.ALL;
  if (!r) return;
  REPORT = r;

  const meta = DATA.accounts.find((a) => a.account_number === account);
  $("#accounts").textContent = meta
    ? `${meta.nickname} · ${meta.account_number} · ${meta.type || meta.margin_or_cash} · ${meta.position_count} positions`
    : DATA.accounts
        .map((a) => `${a.nickname} (${a.account_number})`)
        .join("  ·  ");

  if (!r.positions.length) {
    $("#empty").hidden = false;
    $("#empty").textContent =
      "This account holds no open positions. Cash and balances are shown below.";
  } else {
    $("#empty").hidden = true;
  }

  render(r);
}

function render(r) {
  REPORT = r;
  $("#stamp").textContent = `Updated ${new Date(DATA.generated_at).toLocaleTimeString()}`;

  const warn = $("#warnings");
  if (r.warnings && r.warnings.length) {
    warn.innerHTML = `<strong>Check these figures:</strong><ul>${
      r.warnings.map((w) => `<li>${w}</li>`).join("")}</ul>`;
    warn.hidden = false;
  } else {
    warn.hidden = true;
  }

  applyCollapsed();
  renderKpis(r);
  renderPerformance(r);
  renderEarnings(r);
  renderAllocation();
  renderCorrelation(r);
  renderRisk(r);
  renderPositions(r);

  $("#status").hidden = true;
  $("#main").hidden = false;
}

// ---------------------------------------------------------------- loading

async function load(refresh = false) {
  const btn = $("#refresh");
  btn.disabled = true;
  $("#account").disabled = true;
  const status = $("#status");
  if (!REPORT) {
    status.className = "status";
    status.textContent = "Querying tastytrade… (first run also looks up sectors, which can take a minute)";
    status.hidden = false;
  } else {
    // Keep the page on screen while the new account loads, rather than
    // blanking everything the user was just looking at.
    $("#stamp").textContent = "Loading…";
    $("#main").classList.add("loading");
  }
  try {
    const res = await fetch(
      `/api/report?refresh=${refresh ? "true" : "false"}&account=${encodeURIComponent(account)}`);
    const body = await res.json();
    if (!res.ok) throw new Error(body.detail || `HTTP ${res.status}`);
    DATA = body;
    renderAccountPicker();
    syncControls();
    renderView();
  } catch (err) {
    status.className = "status error";
    status.textContent = `Could not load the portfolio:\n\n${err.message}`;
    status.hidden = false;
    $("#main").hidden = true;
  } finally {
    btn.disabled = false;
    $("#account").disabled = false;
    $("#main").classList.remove("loading");
  }
}

$("#refresh").addEventListener("click", () => load(true));

// ---------------------------------------------------------------- what-if

function verdict(v) {
  // Framed as diversification benefit, which is the decision being made.
  if (v === null || v === undefined) return ["—", ""];
  if (v >= 0.7) return ["Duplicates the book", "bad"];
  if (v >= 0.4) return ["Adds little diversification", "warn"];
  if (v >= 0.15) return ["Partial diversifier", ""];
  if (v >= -0.15) return ["Strong diversifier", "good"];
  return ["Hedges the book", "good"];
}

function renderWhatIf(data) {
  const host = $("#whatif");
  const rows = data.candidates || [];
  const notes = [];
  if (data.unknown?.length) notes.push(`No price history for: ${data.unknown.map(esc).join(", ")}`);
  if (data.already_held?.length) notes.push(`Already held (skipped): ${data.already_held.map(esc).join(", ")}`);

  if (!rows.length) {
    host.innerHTML = `<div class="empty">${notes.join(" · ") || "Nothing to analyse."}</div>`;
    return;
  }

  const cards = rows.map((c) => {
    const [label, cls] = verdict(c.correlation_to_portfolio);
    const strip = (c.per_holding || []).map((p) => `
      <div class="hstrip-cell" style="background:${corrColor(p.value)}"
           title="${esc(c.symbol)} vs ${esc(p.symbol)}: ${p.value.toFixed(2)}">
        <span>${esc(p.symbol)}</span>
      </div>`).join("");
    return `
      <div class="wcard">
        <div class="wcard-head">
          <div>
            <div class="wsym">${esc(c.symbol)}</div>
            <div class="sub">${esc(c.ticker)} · ${c.observations} days</div>
          </div>
          <div class="wmain">
            <div class="wval ${cls}">${c.correlation_to_portfolio === null ? "—" : c.correlation_to_portfolio.toFixed(2)}</div>
            <div class="sub">vs portfolio</div>
          </div>
        </div>
        <div class="wverdict ${cls}">${label}</div>
        <div class="wstats">
          <span>avg vs holdings <b>${c.average_correlation === null ? "—" : c.average_correlation.toFixed(2)}</b></span>
          <span>volatility <b>${c.annualized_volatility_pct.toFixed(1)}%</b>
            <span class="dim">(book ${data.portfolio_volatility_pct?.toFixed(1) ?? "—"}%)</span></span>
          ${c.most_correlated ? `<span>closest <b>${esc(c.most_correlated.symbol)}</b> ${c.most_correlated.value.toFixed(2)}</span>` : ""}
          ${c.least_correlated ? `<span>furthest <b>${esc(c.least_correlated.symbol)}</b> ${c.least_correlated.value.toFixed(2)}</span>` : ""}
        </div>
        <div class="hstrip">${strip}</div>
      </div>`;
  }).join("");

  host.innerHTML = cards + (notes.length ? `<div class="empty">${notes.join(" · ")}</div>` : "");
}

async function runWhatIf() {
  const input = $("#whatif-input");
  const host = $("#whatif");
  const symbols = input.value.trim();
  if (!symbols) return;
  host.innerHTML = `<div class="empty">Fetching price history…</div>`;
  try {
    const res = await fetch(
      `/api/whatif?account=${encodeURIComponent(account)}`
      + `&ex_futures=${exFutures}`
      + `&symbols=${encodeURIComponent(symbols)}`);
    const body = await res.json();
    if (!res.ok) throw new Error(body.detail || `HTTP ${res.status}`);
    renderWhatIf(body);
  } catch (err) {
    host.innerHTML = `<div class="empty" style="color:var(--short)">${esc(err.message)}</div>`;
  }
}

$("#whatif-form").addEventListener("submit", (e) => {
  e.preventDefault();
  runWhatIf();
});

function syncControls() {
  [...$("#tabs").children].forEach((b) =>
    b.classList.toggle("is-active", b.dataset.key === tabKey));
  [...$("#basis").children].forEach((b) =>
    b.classList.toggle("is-active", b.dataset.basis === basis));
  // The equities-only toggle is meaningless without futures in the book.
  $("#futwrap").hidden = !DATA?.has_futures;
  $("#exfutures").checked = exFutures;
  $("#futwrap").classList.toggle("is-on", exFutures);
  document.body.classList.toggle("ex-futures", exFutures);
}

$("#exfutures").addEventListener("change", (e) => {
  exFutures = e.target.checked;
  savePrefs({ ex_futures: exFutures });
  renderAccountPicker();
  renderView();
});

$("#account").addEventListener("change", (e) => {
  account = e.target.value;
  savePrefs({ account });
  // Only the selected account's data is fetched, so switching needs a round
  // trip. The server caches per account, so coming back to one is instant.
  load(false);
});

$("#tabs").addEventListener("click", (e) => {
  const btn = e.target.closest("button");
  if (!btn) return;
  tabKey = btn.dataset.key;
  savePrefs({ tab: tabKey });
  syncControls();
  renderAllocation();
});

$("#basis").addEventListener("click", (e) => {
  const btn = e.target.closest("button");
  if (!btn) return;
  basis = btn.dataset.basis;
  savePrefs({ basis });
  syncControls();
  renderAllocation();
});

loadPrefs().then(load);
