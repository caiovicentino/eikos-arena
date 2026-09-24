"use strict";
// Eikos Arena: public, static page. The arena's snapshot comes from a public Hugging Face dataset (updated after every
// decision); prices and candles come straight from Hyperliquid's public API. Equity and PNL are recomputed here from
// live prices with the arena's own formula: size = side * notional / entry, PNL = size * (mark - entry).

const STATE_URL = "https://huggingface.co/datasets/caiovicentino1/eikos-arena/resolve/main/live/state.json";
const HL_WS = "wss://api.hyperliquid.xyz/ws";
const HL_INFO = "https://api.hyperliquid.xyz/info";
const MODELS = {
  eikos: { name: "Eikos-27B", short: "Eikos", sub: "Open weights (MIT) · one GPU", color: "#f2ab43" },
  jev: { name: "Jev", short: "Jev", sub: "TypeSafe's decision API", color: "#7aa5ff" },
};
const CAT = { crypto: "Crypto", stock: "Stock", index: "Index", commodity: "Commodity" };
// column tooltips (the models only answer long/flat/short and a probability; every price comes from Hyperliquid)
const TIP = {
  size: "Amount of the asset: $2,000 ÷ entry price",
  value: "Size × mark price",
  entry: "Price the position was opened at: Hyperliquid's real best bid or ask in that round. The models never set prices.",
  mark: "Hyperliquid's live market price right now",
  pnl: "Size × (mark price − entry price). The % is of the $2,000 position.",
  conf: "Probability the model gave to its choice (long, flat or short)",
  pup: "The model's probability that the price is higher 5 minutes after the decision",
  fill: "Hyperliquid's real best bid or ask in that round. The models choose long, flat or short, never a price.",
};
const COL = { bg: "#0f1d22", grid: "#15262c", line: "#1d3037", text: "#8a9a9b", long: "#2bb58b", short: "#ef6a7f", start: "#52666a" };
const LC = window.LightweightCharts;

const $ = (s) => document.querySelector(s);
let S = null;
const mids = {};
let lastTick = 0;
let chart = null, sE = null, sJ = null, sC = null;
let mode = "equity", coin = "BTC", range = 0, tab = "pos-eikos";
let priceLines = [], lastCandle = null, liveT = 0, histLast = 0, crossActive = false, tableDrawn = 0;

// ---------- formatting ----------
const MINUS = "−";
function usd(x, sign) {
  if (x == null || !isFinite(x)) return "—";
  const s = "$" + Math.abs(x).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return sign ? (x >= 0 ? "+" : MINUS) + s : (x < 0 ? MINUS : "") + s;
}
const pct = (x, d = 2) => (x == null || !isFinite(x) ? "—" : (x > 0 ? "+" : x < 0 ? MINUS : "") + Math.abs(x).toFixed(d) + "%");
const prec = (x) => Math.min(6, Math.max(0, 5 - Math.floor(Math.log10(Math.abs(x) || 1))));
const fpx = (x) => (!x ? "—" : x.toLocaleString("en-US", { minimumFractionDigits: prec(x), maximumFractionDigits: prec(x) }));
const fsize = (x) => Math.abs(x).toLocaleString("en-US", { maximumSignificantDigits: 5 });
const cls = (x) => (x > 0.005 ? "pos" : x < -0.005 ? "neg" : "zero");
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const utc = (t, o) => new Date(t * 1000).toLocaleString("en-US", { timeZone: "UTC", hour12: false, ...o });
const hhmm = (t) => utc(t, { hour: "2-digit", minute: "2-digit" });
const mmss = (s) => `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
const sideWord = (s) => (s > 0 ? "Long" : s < 0 ? "Short" : "Flat");
const sideCls = (s) => (s > 0 ? "side-l" : s < 0 ? "side-s" : "side-f");
const mkt = (c) => S.markets.find((m) => m.coin === c);
const markOf = (m) => mids[m.coin] ?? m.mid;

// ---------- live book (same formula as the arena) ----------
function positions(p) {
  const out = [];
  for (const m of S.markets) {
    const x = m[p];
    if (!x.side || !x.entry) continue;
    const size = (x.side * S.arena.notional) / x.entry, mark = markOf(m);
    const pnl = size * (mark - x.entry);
    out.push({ m, x, size, mark, value: Math.abs(size) * mark, pnl, roe: (100 * pnl) / S.arena.notional });
  }
  return out;
}
function account(p) {
  const pos = positions(p), upnl = pos.reduce((a, r) => a + r.pnl, 0), eq = S.players[p].balance + upnl;
  // positions share one account (cross margin): leverage = total position value / equity
  const expo = pos.reduce((a, r) => a + r.value, 0);
  return { pos, upnl, eq, pl: eq - S.arena.start_equity, expo, lev: eq > 0 ? expo / eq : null };
}

// ---------- header, strip, accounts, round ----------
function renderNav() {
  const a = S.arena, now = Date.now() / 1000;
  $("#nav-round").textContent = `${a.cycle}/${a.cycles_total}`;
  $("#nav-next").textContent = a.finished ? "done" : now >= a.next ? "deciding" : mmss(a.next - now);
  const stale = !a.finished && now - S.published > 12 * 60;
  $("#live").classList.toggle("on", !stale && !a.finished);
  $("#live").classList.toggle("stale", stale);
  $("#live-text").textContent = a.finished ? "Finished" : stale ? "Delayed" : "Live";
  let b = $("#banner");
  if (stale && !b) {
    b = document.createElement("p");
    b.id = "banner";
    b.className = "banner";
    b.textContent = `Arena updates are delayed (last snapshot ${hhmm(S.published)} UTC). Prices keep streaming live.`;
    $(".intro").after(b);
  } else if (!stale && b) b.remove();
}

function buildStrip() {
  $("#strip").innerHTML = S.markets.map((m) =>
    `<div class="tk" data-coin="${esc(m.coin)}" id="tk-${esc(m.label)}"><span class="s">${esc(m.label)}</span>` +
    `<span class="p" id="tkp-${esc(m.label)}">${fpx(markOf(m))}</span><span class="c" id="tkc-${esc(m.label)}"></span></div>`).join("");
  $("#msel").innerHTML = S.markets.map((m) => `<option value="${esc(m.coin)}">${esc(m.label)} · ${CAT[m.cat] || ""}</option>`).join("");
  $("#msel").value = coin;
}

function updateStrip() {
  for (const m of S.markets) {
    const p = document.getElementById(`tkp-${m.label}`), c = document.getElementById(`tkc-${m.label}`);
    if (!p) continue;
    const mark = markOf(m);
    p.textContent = fpx(mark);
    if (m.chg24 != null) {
      const ch = (mark / (m.mid / (1 + m.chg24 / 100)) - 1) * 100;
      c.textContent = pct(ch);
      c.className = `c ${cls(ch)}`;
    }
    document.getElementById(`tk-${m.label}`).classList.toggle("sel", mode === "market" && m.coin === coin);
  }
}

function renderAccounts() {
  const A = { eikos: account("eikos"), jev: account("jev") };
  const lead = A.eikos.eq >= A.jev.eq ? "eikos" : "jev";
  for (const p of ["eikos", "jev"]) {
    const P = S.players[p], M = MODELS[p], a = A[p];
    const lat = P.lat_med == null ? "—" : (P.lat_med / 1000).toFixed(P.lat_med < 1000 ? 2 : 1) + " s";
    $(`#acct-${p}`).innerHTML =
      `<div class="acct-h"><span class="acct-name"><i></i>${M.name}</span>${p === lead ? '<span class="lead">Leading</span>' : ""}</div>` +
      `<div class="acct-sub">${M.sub}</div>` +
      `<div class="acct-v">${usd(a.eq)}</div>` +
      `<div class="acct-pl ${cls(a.pl)}">${usd(a.pl, true)} (${pct((100 * a.pl) / S.arena.start_equity)})</div>` +
      `<dl><dt>Unrealized PNL</dt><dd class="${cls(a.upnl)}">${usd(a.upnl, true)}</dd>` +
      `<dt class="x">Realized PNL</dt><dd class="x ${cls(P.realized)}">${usd(P.realized, true)}</dd>` +
      `<dt class="x">Fees · funding</dt><dd class="x">${usd(-P.fees, true)} · ${usd(-P.funding, true)}</dd>` +
      `<dt>Open positions</dt><dd>${a.pos.length}</dd>` +
      `<dt class="x">Position value</dt><dd class="x">${usd(a.expo)}</dd>` +
      `<dt>Leverage<span class="hint">value ÷ equity</span></dt><dd>${a.lev == null ? "—" : a.lev.toFixed(2) + "x"}</dd>` +
      `<dt>Forecast error<span class="hint">Brier</span></dt><dd>${P.brier == null ? "—" : P.brier.toFixed(3)}</dd>` +
      `<dt class="x">Answer time (28 q.)</dt><dd class="x">${lat}</dd>` +
      `<dt class="x">Missed rounds</dt><dd class="x">${P.missed}</dd></dl>`;
    const n = document.getElementById(`n-${p}`);
    if (n) n.textContent = a.pos.length;
  }
}

function renderRound() {
  const a = S.arena, now = Date.now() / 1000, step = a.cycle_min * 60;
  const left = a.next - now, deciding = !a.finished && left <= 0;
  const day = Math.min(3, Math.floor((Math.min(now, a.end) - a.start) / 86400) + 1);
  const lastT = a.next - step;
  const lat = (p) => (S.players[p].lat_last == null ? "—" : (S.players[p].lat_last / 1000).toFixed(2) + " s");
  $("#roundp").innerHTML =
    `<div class="round-top"><span>Round <b>${a.cycle}</b> of ${a.cycles_total}</span><span>Day <b>${day}</b> of 3</span></div>` +
    `<div class="bar"><i id="barfill"></i></div>` +
    `<div class="round-next">${a.finished ? "Experiment finished" : deciding ? "Deciding now…" : `Next decision in ${mmss(left)}`}</div>` +
    `<div class="round-sub">Last round ${hhmm(lastT)} UTC · answers in ${lat("eikos")} (Eikos) and ${lat("jev")} (Jev)</div>`;
  $("#barfill").style.width = `${a.finished || deciding ? 100 : Math.max(0, Math.min(100, 100 * (1 - left / step)))}%`;
}

// ---------- chart ----------
function makeChart() {
  chart = LC.createChart($("#chart"), {
    autoSize: true,
    layout: { background: { type: "solid", color: COL.bg }, textColor: COL.text, fontFamily: "Inter, sans-serif", fontSize: 11 },
    grid: { vertLines: { color: COL.grid }, horzLines: { color: COL.grid } },
    rightPriceScale: { borderColor: COL.line, scaleMargins: { top: 0.12, bottom: 0.1 } },
    timeScale: { borderColor: COL.line, timeVisible: true, secondsVisible: false, rightOffset: 4 },
    crosshair: { mode: LC.CrosshairMode.Normal },
  });
  chart.subscribeCrosshairMove((param) => {
    crossActive = !!param.time;
    renderLegend(param);
  });
}

function clearSeries() {
  for (const s of [sE, sJ, sC]) if (s) chart.removeSeries(s);
  sE = sJ = sC = null;
  priceLines = [];
  lastCandle = null;
}

function showEquity() {
  clearSeries();
  const money = { type: "custom", minMove: 0.01, formatter: (v) => "$" + Math.round(v).toLocaleString("en-US") };
  sJ = chart.addLineSeries({ color: MODELS.jev.color, lineWidth: 2, priceLineVisible: false, title: "Jev", priceFormat: money });
  sE = chart.addLineSeries({ color: MODELS.eikos.color, lineWidth: 2, priceLineVisible: false, title: "Eikos", priceFormat: money });
  sE.createPriceLine({ price: S.arena.start_equity, color: COL.start, lineWidth: 1, lineStyle: LC.LineStyle.Dashed, axisLabelVisible: true, title: "Start" });
  setEquityData();
  applyRange();
}

function setEquityData() {
  if (!sE) return;
  const h = S.history;
  sE.setData(h.map((r) => ({ time: r[0], value: r[1] })));
  sJ.setData(h.map((r) => ({ time: r[0], value: r[2] })));
  histLast = h.length ? h[h.length - 1][0] : 0;
  liveT = 0;
}

function liveEquity() {
  if (mode !== "equity" || !sE || S.arena.finished) return;
  const bucket = Math.floor(Date.now() / 30000) * 30;
  liveT = Math.max(bucket, histLast + 1, liveT);
  sE.update({ time: liveT, value: account("eikos").eq });
  sJ.update({ time: liveT, value: account("jev").eq });
}

async function fetchCandles(c, fromMs, toMs) {
  const r = await fetch(HL_INFO, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ type: "candleSnapshot", req: { coin: c, interval: "5m", startTime: Math.floor(fromMs), endTime: Math.floor(toMs) } }),
  });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return (await r.json()).map((k) => ({ time: Math.floor(k.t / 1000), open: +k.o, high: +k.h, low: +k.l, close: +k.c }));
}

async function showMarket(c) {
  coin = c;
  $("#msel").value = c;
  clearSeries();
  const m = mkt(c), pr = prec(markOf(m));
  sC = chart.addCandlestickSeries({
    upColor: COL.long, downColor: COL.short, borderVisible: false, wickUpColor: COL.long, wickDownColor: COL.short,
    priceFormat: { type: "price", precision: pr, minMove: 1 / 10 ** pr },
  });
  const series = sC;
  try {
    const now = Date.now();
    const data = await fetchCandles(c, S.arena.start * 1000 - 6 * 3600e3, now);
    if (series !== sC) return;  // switched again meanwhile
    sC.setData(data);
    lastCandle = data.length ? { ...data[data.length - 1] } : null;
  } catch (e) { /* stays empty; the next poll retries */ }
  setMarkers();
  setEntryLines();
  applyRange(range || 6 * 3600);
  renderLegend({});
}

async function pollCandles() {
  if (mode !== "market" || !sC) return;
  try {
    const series = sC, now = Date.now();
    const data = await fetchCandles(coin, now - 15 * 60e3, now);
    if (series !== sC) return;
    for (const k of data) sC.update(k);
    if (data.length) lastCandle = { ...data[data.length - 1] };
  } catch (e) { /* keep the last candles */ }
}

function liveCandle() {
  if (mode !== "market" || !sC || !lastCandle) return;
  const px = mids[coin];
  if (!px) return;
  const bucket = Math.floor(Date.now() / 300000) * 300;
  if (bucket > lastCandle.time) lastCandle = { time: bucket, open: lastCandle.close, high: px, low: px, close: px };
  lastCandle.close = px;
  lastCandle.high = Math.max(lastCandle.high, px);
  lastCandle.low = Math.min(lastCandle.low, px);
  sC.update({ ...lastCandle });
}

function setMarkers() {
  if (!sC) return;
  const mk = S.fills.filter((f) => f.coin === coin).map((f) => {
    const M = MODELS[f.who], open = f.action === "open", long = f.side > 0;
    return {
      time: Math.floor(f.t / 300) * 300,
      position: open ? (long ? "belowBar" : "aboveBar") : "inBar",
      color: M.color,
      shape: open ? (long ? "arrowUp" : "arrowDown") : "circle",
      text: `${M.short} ${open ? (long ? "long" : "short") : "close"}`,
    };
  }).sort((a, b) => a.time - b.time);
  sC.setMarkers(mk);
}

function setEntryLines() {
  if (!sC) return;
  for (const pl of priceLines) sC.removePriceLine(pl);
  priceLines = [];
  const m = mkt(coin);
  for (const p of ["eikos", "jev"]) {
    const x = m[p];
    if (!x.side || !x.entry) continue;
    priceLines.push(sC.createPriceLine({ price: x.entry, color: MODELS[p].color, lineWidth: 1, lineStyle: LC.LineStyle.Dashed,
      axisLabelVisible: true, title: `${MODELS[p].short} ${x.side > 0 ? "long" : "short"} entry` }));
  }
}

function applyRange(r = range) {
  if (!chart) return;
  const ts = chart.timeScale(), now = Date.now() / 1000;
  if (!r) { ts.fitContent(); return; }
  try { ts.setVisibleRange({ from: Math.max(S.arena.start - 6 * 3600, now - r), to: now }); } catch (e) { ts.fitContent(); }
}

function renderLegend(param) {
  const el = $("#legend");
  if (!S) return;
  if (mode === "equity") {
    let e, j;
    if (param && param.time && sE && param.seriesData) {
      e = param.seriesData.get(sE)?.value;
      j = param.seriesData.get(sJ)?.value;
    }
    if (e == null) e = account("eikos").eq;
    if (j == null) j = account("jev").eq;
    const d = (v) => `${usd(v)} <span class="${cls(v - S.arena.start_equity)}">${pct((100 * (v - S.arena.start_equity)) / S.arena.start_equity)}</span>`;
    el.innerHTML = `<span><i class="sw e"></i>Eikos-27B<b>${d(e)}</b></span><span><i class="sw j"></i>Jev<b>${d(j)}</b></span>` +
      `<span class="note">Account value in simulated dollars · start $10,000</span>`;
  } else {
    const m = mkt(coin);
    let k = null;
    if (param && param.time && sC && param.seriesData) k = param.seriesData.get(sC);
    if (!k) k = lastCandle;
    const ohlc = k ? `O <b>${fpx(k.open)}</b> H <b>${fpx(k.high)}</b> L <b>${fpx(k.low)}</b> C <b>${fpx(k.close)}</b>` : "";
    const pos = (p) => { const x = m[p]; return x.side ? `<span class="${sideCls(x.side)}">${sideWord(x.side)}</span> @ ${fpx(x.entry)}` : `<span class="side-f">Flat</span>`; };
    el.innerHTML = `<span><b>${esc(m.label)}</b> · 5m · ${CAT[m.cat]}</span><span>${ohlc}</span>` +
      `<span><i class="sw e"></i>Eikos: ${pos("eikos")}</span><span><i class="sw j"></i>Jev: ${pos("jev")}</span>`;
  }
}

function setMode(newMode, c) {
  mode = newMode;
  for (const b of document.querySelectorAll("#modes button")) b.classList.toggle("on", b.dataset.mode === mode);
  if (mode === "equity") showEquity();
  else showMarket(c || coin);
  updateStrip();
  renderLegend({});
}

// ---------- bottom tables ----------
function tableHTML(head, rows, empty) {
  if (!rows.length) return `<div class="empty">${empty}</div>`;
  const th = (h) => `<th class="${h[1] || ""}${h[2] ? " tip" : ""}"${h[2] ? ` title="${esc(h[2])}"` : ""}>${h[0]}</th>`;
  return `<table><thead><tr>${head.map(th).join("")}</tr></thead><tbody>${rows.join("")}</tbody></table>`;
}

function positionsTable(p) {
  const pos = positions(p).sort((a, b) => Math.abs(b.pnl) - Math.abs(a.pnl));
  const rows = pos.map((r) => {
    const s = r.x.side, pp = r.x.p_pos || {}, conf = pp[s > 0 ? "long" : "short"];
    return `<tr><td><span class="coin ${s > 0 ? "long" : "short"}">${esc(r.m.label)}</span><span class="cat">${CAT[r.m.cat]}</span></td>` +
      `<td class="${s > 0 ? "pos" : "neg"}">${s > 0 ? "" : MINUS}${fsize(r.size)} ${esc(r.m.label)}</td>` +
      `<td>${usd(r.value)}</td><td>${fpx(r.x.entry)}</td><td>${fpx(r.mark)}</td>` +
      `<td class="${cls(r.pnl)}">${usd(r.pnl, true)} (${pct(r.roe)})</td>` +
      `<td>${conf == null ? "—" : Math.round(conf * 100) + "%"}</td><td>${r.x.p_up == null ? "—" : Math.round(r.x.p_up * 100) + "%"}</td></tr>`;
  });
  return tableHTML([["Coin"], ["Size", "", TIP.size], ["Position value", "", TIP.value], ["Entry price", "", TIP.entry], ["Mark price", "", TIP.mark],
    ["PNL (% of position)", "", TIP.pnl], ["Model's confidence", "", TIP.conf], ["P(higher in 5 min)", "", TIP.pup]],
    rows, `${MODELS[p].name} has no open positions right now.`);
}

function compareTable() {
  const cell = (m, p) => {
    const x = m[p], pp = x.p_pos || {}, conf = pp[x.side > 0 ? "long" : x.side < 0 ? "short" : "flat"];
    const pnl = x.side && x.entry ? ((x.side * S.arena.notional) / x.entry) * (markOf(m) - x.entry) : null;
    return `<td class="l"><span class="${sideCls(x.side)}">${sideWord(x.side)}</span></td><td>${conf == null ? "—" : Math.round(conf * 100) + "%"}</td>` +
      `<td>${x.p_up == null ? "—" : Math.round(x.p_up * 100) + "%"}</td><td class="${pnl == null ? "" : cls(pnl)}">${pnl == null ? "" : usd(pnl, true)}</td>`;
  };
  const rows = S.markets.map((m) => {
    const ch = m.chg24 == null ? null : (markOf(m) / (m.mid / (1 + m.chg24 / 100)) - 1) * 100;
    return `<tr><td><span class="coin">${esc(m.label)}</span><span class="cat">${CAT[m.cat]}</span></td><td>${fpx(markOf(m))}</td>` +
      `<td class="${cls(ch)}">${pct(ch)}</td>${cell(m, "eikos")}${cell(m, "jev")}</tr>`;
  });
  const E = '<span class="who-e">Eikos</span>', J = '<span class="who-j">Jev</span>';
  return tableHTML([["Market"], ["Mark price", "", TIP.mark], ["24h"], [`${E} position`, "l"], [`${E} confidence`, "", TIP.conf],
    [`${E} P(higher)`, "", TIP.pup], [`${E} PNL`], [`${J} position`, "l"], [`${J} confidence`, "", TIP.conf], [`${J} P(higher)`, "", TIP.pup],
    [`${J} PNL`]], rows, "No data yet.");
}

function tradesTable() {
  const rows = S.fills.slice().reverse().map((f) => {
    const M = MODELS[f.who], open = f.action === "open", long = f.side > 0;
    const dir = `${open ? "Open" : "Close"} ${long ? "Long" : "Short"}`;
    return `<tr><td>${esc(utc(f.t, { month: "short", day: "numeric" }))} ${hhmm(f.t)}</td>` +
      `<td class="l"><span class="${f.who === "eikos" ? "who-e" : "who-j"}">${M.short}</span></td>` +
      `<td class="l"><span class="coin">${esc(f.label)}</span></td>` +
      `<td class="l ${open === long ? "pos" : "neg"}">${dir}</td><td>${fpx(f.px)}</td>` +
      `<td>${f.size == null ? "—" : fsize(f.size) + " " + esc(f.label)}</td><td>${usd(f.fee)}</td>` +
      `<td class="${f.pnl == null ? "" : cls(f.pnl)}">${f.pnl == null ? "—" : usd(f.pnl, true)}</td></tr>`;
  });
  return tableHTML([["Time (UTC)"], ["Model", "l"], ["Coin", "l"], ["Direction", "l"], ["Price", "", TIP.fill], ["Size"], ["Fee"], ["Closed PNL"]],
    rows, "No trades yet.");
}

function decisionsTable() {
  const rounds = new Map();
  for (const e of S.feed) {
    const key = Math.round(e.t / 60);
    if (!rounds.has(key)) rounds.set(key, { t: e.t, sys: [] });
    const r = rounds.get(key);
    if (e.who === "eikos" || e.who === "jev") r[e.who] = e.text;
    else r.sys.push(e.text);
  }
  const part = (text) => {
    if (text == null) return `<td class="l muted">—</td><td class="muted">—</td>`;
    const m = text.match(/^(.*) · ([\d.]+ s)$/);
    const what = (m ? m[1] : text).replace(/^no changes$/, "no change");
    return `<td class="l">${esc(what)}</td><td class="muted">${m ? esc(m[2]) : ""}</td>`;
  };
  const rows = [];
  for (const r of [...rounds.values()].sort((a, b) => b.t - a.t)) {
    if (r.eikos != null || r.jev != null) rows.push(`<tr><td>${hhmm(r.t)}</td>${part(r.eikos)}${part(r.jev)}</tr>`);
    for (const s of r.sys) rows.push(`<tr><td>${hhmm(r.t)}</td><td class="l muted" colspan="4">${esc(s)}</td></tr>`);
  }
  const E = '<span class="who-e">Eikos-27B</span>', J = '<span class="who-j">Jev</span>';
  return tableHTML([["Time (UTC)"], [`${E}: what changed`, "l"], ["Answer time"], [`${J}: what changed`, "l"], ["Answer time"]], rows, "No decisions yet.");
}

function renderTable(force) {
  if (!S) return;
  const live = tab === "pos-eikos" || tab === "pos-jev" || tab === "compare";
  if (!force && (!live || Date.now() - tableDrawn < 2000)) return;
  const html = tab === "pos-eikos" ? positionsTable("eikos") : tab === "pos-jev" ? positionsTable("jev")
    : tab === "compare" ? compareTable() : tab === "trades" ? tradesTable() : decisionsTable();
  const w = $("#btable"), top = w.scrollTop, left = w.scrollLeft;
  w.innerHTML = html;
  w.scrollTop = top;
  w.scrollLeft = left;
  tableDrawn = Date.now();
  $("#n-trades").textContent = S.fills.length;
}

// ---------- data ----------
async function loadState() {
  try {
    const r = await fetch(`${STATE_URL}?v=${Math.floor(Date.now() / 10000)}`, { cache: "no-store" });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const d = await r.json();
    if (!d || d.schema !== 1) throw new Error("unexpected snapshot");
    if (S && d.published <= S.published) return;
    const first = !S;
    S = d;
    for (const m of S.markets) if (mids[m.coin] == null) mids[m.coin] = m.mid;
    if (first) {
      buildStrip();
      if (S.arena.rules_sha256) $("#rules-hash").textContent = `Rules SHA-256: ${S.arena.rules_sha256}`;
      setMode("equity");
    } else if (mode === "equity") {
      setEquityData();
    } else {
      setMarkers();
      setEntryLines();
    }
    renderTable(true);
    tick();
  } catch (e) {
    if (!S) $("#live-text").textContent = "Loading…";
  }
}

function onMids(d) {
  if (!S) return;
  for (const m of S.markets) {
    if (!(m.coin in d)) continue;
    const v = +d[m.coin], prev = mids[m.coin];
    if (prev != null && v !== prev) {
      const el = document.getElementById(`tkp-${m.label}`);
      if (el) {
        el.classList.remove("up", "dn");
        void el.offsetWidth;
        el.classList.add(v > prev ? "up" : "dn");
        setTimeout(() => el.classList.remove("up", "dn"), 700);
      }
    }
    mids[m.coin] = v;
  }
  lastTick = Date.now();
}

function connect() {
  let ws, ping;
  try { ws = new WebSocket(HL_WS); } catch (e) { return; }
  ws.onopen = () => {
    ws.send(JSON.stringify({ method: "subscribe", subscription: { type: "allMids" } }));
    ws.send(JSON.stringify({ method: "subscribe", subscription: { type: "allMids", dex: "xyz" } }));
    ping = setInterval(() => ws.readyState === 1 && ws.send('{"method":"ping"}'), 30000);
  };
  ws.onmessage = (ev) => {
    let m;
    try { m = JSON.parse(ev.data); } catch (e) { return; }
    if (m && m.channel === "allMids" && m.data && m.data.mids) onMids(m.data.mids);
  };
  ws.onclose = () => { clearInterval(ping); setTimeout(connect, 4000 + Math.random() * 3000); };
  ws.onerror = () => ws.close();
}

async function pollMids() {
  if (Date.now() - lastTick < 15000) return;
  try {
    for (const body of [{ type: "allMids" }, { type: "allMids", dex: "xyz" }]) {
      const r = await fetch(HL_INFO, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      if (r.ok) onMids(await r.json());
    }
  } catch (e) { /* keep the last prices */ }
}

function tick() {
  if (!S) return;
  renderNav();
  updateStrip();
  renderAccounts();
  renderRound();
  liveEquity();
  liveCandle();
  if (!crossActive) renderLegend({});
  renderTable(false);
}

function stateLoop() {
  const waiting = S && !S.arena.finished && Date.now() / 1000 > S.arena.next;
  loadState().finally(() => setTimeout(stateLoop, waiting ? 8000 : 30000));
}

// ---------- UI ----------
for (const b of document.querySelectorAll("#modes button")) b.addEventListener("click", () => S && setMode(b.dataset.mode));
$("#msel").addEventListener("change", (e) => S && setMode("market", e.target.value));
$("#strip").addEventListener("click", (e) => {
  const t = e.target.closest(".tk");
  if (t && S) setMode("market", t.dataset.coin);
});
for (const b of document.querySelectorAll("#tf button")) {
  b.addEventListener("click", () => {
    range = +b.dataset.r;
    for (const x of document.querySelectorAll("#tf button")) x.classList.toggle("on", x === b);
    applyRange();
  });
}
for (const b of document.querySelectorAll("#btabs button")) {
  b.addEventListener("click", () => {
    tab = b.dataset.t;
    for (const x of document.querySelectorAll("#btabs button")) x.classList.toggle("on", x === b);
    renderTable(true);
  });
}
for (const a of document.querySelectorAll("[data-go]")) {
  a.addEventListener("click", () => document.querySelector(`#btabs button[data-t="${a.dataset.go}"]`)?.click());
}

makeChart();
stateLoop();
connect();
setInterval(tick, 1000);
setInterval(pollMids, 5000);
setInterval(pollCandles, 10000);
