/* NQ Autotrader dashboard. Vanilla JS; every value from the server is inserted with textContent. */
"use strict";

const LWC = window.LightweightCharts;
const CSS = getComputedStyle(document.documentElement);
const C = (name) => CSS.getPropertyValue(name).trim();
const COLORS = {
  text: C("--text"), text2: C("--text-2"), muted: C("--muted"), grid: C("--grid"), axis: C("--axis"),
  surface: C("--surface"), good: C("--good"), critical: C("--critical"), warning: C("--warning"),
  series: [C("--series-1"), C("--series-2"), C("--series-3"), C("--series-4"), C("--series-5")],
};
const POLL_MS = 2000;

const S = { state: null, page: "overview", charts: {}, lastEventId: 0, chartDay: null, research: null, researchPick: null };

// ------------------------------------------------------------------ helpers

function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "style") el.setAttribute("style", v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}
const $ = (id) => document.getElementById(id);
const fill = (id, ...kids) => { const el = typeof id === "string" ? $(id) : id; el.replaceChildren(...kids.flat().filter((k) => k !== null && k !== undefined && k !== false)); return el; };

const fmt = {
  usd(v, sign = false) {
    if (v === null || v === undefined || Number.isNaN(v)) return "—";
    const s = Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    if (!sign) return (v < 0 ? "−$" : "$") + s;
    return (v > 0 ? "+$" : v < 0 ? "−$" : "$") + s;
  },
  usd0(v) { return v === null || v === undefined ? "—" : (v < 0 ? "−$" : "$") + Math.abs(Math.round(v)).toLocaleString("en-US"); },
  px(v) { return v === null || v === undefined ? "—" : Number(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 }); },
  pct(v, d = 0) { return v === null || v === undefined ? "—" : (v * 100).toFixed(d) + "%"; },
  n(v, d = 2) { return v === null || v === undefined ? "—" : Number(v).toFixed(d); },
  hm(iso) { return iso ? iso.slice(11, 16) : "—"; },
  dt(iso) { return iso ? iso.slice(5, 10) + " " + iso.slice(11, 16) : "—"; },
};
const pnlClass = (v) => (v > 0 ? "pos" : v < 0 ? "neg" : "");
const pnlSpan = (v, signed = true) => h("span", { class: "num " + pnlClass(v), text: (v > 0 ? "▲ " : v < 0 ? "▼ " : "") + fmt.usd(v, signed) });
const sideBadge = (side) => h("span", { class: "side-badge " + (side > 0 ? "long" : "short"), text: side > 0 ? "LONG" : "SHORT" });

// chart time: ET wall clock rendered as if UTC, so the axis reads exchange time
const etTime = (iso) => Math.floor(Date.UTC(+iso.slice(0, 4), +iso.slice(5, 7) - 1, +iso.slice(8, 10), +iso.slice(11, 13), +iso.slice(14, 16)) / 1000);

async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json" };
  const tok = safeStorage.get("nqt_token");
  if (tok) headers.Authorization = "Bearer " + tok;
  const res = await fetch(path, { ...opts, headers });
  if (res.status === 401) {
    const ok = await askToken();
    if (ok) return api(path, opts);
    throw new Error("unauthorized");
  }
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch (e) { /* ignore */ }
    throw new Error(typeof msg === "string" ? msg : JSON.stringify(msg));
  }
  return res.json();
}

const safeStorage = {
  get(k) { try { return localStorage.getItem(k); } catch (e) { return null; } },
  set(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* private mode */ } },
};

function toast(msg, level = "info") {
  const t = h("div", { class: "toast " + level, role: "status", text: msg });
  $("toasts").append(t);
  setTimeout(() => t.remove(), 7000);
}

function dialog(id) {
  const d = $(id);
  return new Promise((resolve) => {
    d.addEventListener("close", () => resolve(d.returnValue === "ok"), { once: true });
    d.showModal();
  });
}
async function askToken() {
  $("token-text").value = "";
  if (!(await dialog("dlg-token"))) return false;
  safeStorage.set("nqt_token", $("token-text").value.trim());
  return true;
}

function baseChart(el, opts = {}) {
  return LWC.createChart(el, {
    autoSize: true,
    layout: { background: { type: "solid", color: COLORS.surface }, textColor: COLORS.muted, fontFamily: CSS.getPropertyValue("--font"), fontSize: 11 },
    grid: { vertLines: { color: COLORS.grid }, horzLines: { color: COLORS.grid } },
    rightPriceScale: { borderColor: COLORS.axis },
    timeScale: { borderColor: COLORS.axis, timeVisible: true, secondsVisible: false },
    crosshair: { mode: LWC.CrosshairMode.Normal, vertLine: { color: COLORS.muted, labelBackgroundColor: "#383835" }, horzLine: { color: COLORS.muted, labelBackgroundColor: "#383835" } },
    ...opts,
  });
}

function attachTooltip(chart, el, rowsFor) {
  const tip = h("div", { class: "tooltip", role: "presentation" });
  el.append(tip);
  chart.subscribeCrosshairMove((param) => {
    if (!param.point || !param.time || param.point.x < 0 || param.point.y < 0) { tip.style.display = "none"; return; }
    const content = rowsFor(param);
    if (!content) { tip.style.display = "none"; return; }
    tip.replaceChildren(...content);
    tip.style.display = "block";
    const w = tip.offsetWidth, hgt = tip.offsetHeight;
    let x = param.point.x + 14, y = param.point.y + 14;
    if (x + w > el.clientWidth) x = param.point.x - w - 14;
    if (y + hgt > el.clientHeight) y = Math.max(0, param.point.y - hgt - 14);
    tip.style.left = x + "px"; tip.style.top = y + "px";
  });
}

// ------------------------------------------------------------------ navigation

function showPage(page) {
  S.page = page;
  document.querySelectorAll(".page").forEach((p) => (p.hidden = p.id !== "page-" + page));
  document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.page === page));
  safeStorage.set("nqt_page", page);
  refreshPage(true);
}
window.addEventListener("hashchange", () => showPage(location.hash.slice(1) || "overview"));

// ------------------------------------------------------------------ global status

function renderStatus(st) {
  const health = st.health || {};
  const hb = st.heartbeat;
  const synthetic = (st.data_label || "").startsWith("synthetic");
  const items = [
    h("span", { class: "pill mode-" + (st.mode || "paper") }, h("strong", { text: (st.mode || "—").toUpperCase() }), "mode"),
    h("span", { class: "pill" }, h("span", { class: "dot " + (st.market?.state === "rth" ? "good" : st.market?.state === "globex" ? "warning" : "") }), st.market?.label || "Market"),
    h("span", { class: "pill" }, h("span", { class: "dot " + (health.broker_connected ? "good" : "critical") }), "Broker " + (st.broker || "—")),
    h("span", { class: "pill" }, h("span", { class: "dot " + (health.data_fresh ? "good" : "critical") }), "Data " + (st.data_label || "—")),
    h("span", { class: "pill" }, h("span", { class: "dot " + (health.protective_orders_confirmed ? "good" : "critical") }), "Stops " + (health.protective_orders_confirmed ? "confirmed" : "UNCONFIRMED")),
  ];
  if (st.contract) items.push(h("span", { class: "pill" }, h("strong", { text: st.contract.symbol }), `rolls ${st.contract.roll_date}`));
  fill("status-strip", items);

  const banners = [];
  if (st.kill_switch) banners.push(h("div", { class: "banner critical" }, h("span", { class: "icon", text: "■" }),
    "EMERGENCY STOP ENGAGED: positions flattened, no new entries.", h("button", { class: "btn btn-sm", type: "button", onclick: resume, text: "Release…" })));
  if (st.lockout) banners.push(h("div", { class: "banner warning" }, h("span", { class: "icon", text: "!" }), "Trading locked for today: " + st.lockout));
  if (st.news_blackout) banners.push(h("div", { class: "banner warning" }, h("span", { class: "icon", text: "!" }), "News blackout: " + st.news_blackout + ". No new entries."));
  if (health && st.as_of && (!health.positions_reconciled || !health.broker_connected)) banners.push(h("div", { class: "banner critical" }, h("span", { class: "icon", text: "!" }), "Broker state unconfirmed: no new positions until reconciliation succeeds."));
  if (synthetic) banners.push(h("div", { class: "banner info" }, h("span", { class: "icon", text: "i" }), "SYNTHETIC DATA: this is a replay of generated prices for testing. Nothing shown is real market data or real performance."));
  if (!st.as_of) banners.push(h("div", { class: "banner info" }, h("span", { class: "icon", text: "i" }), "The engine hasn't written any state yet. Start it with: python -m nqtrader paper (or demo)."));
  if (hb && st.market?.state === "rth" && Date.now() - Date.parse(hb.at) > 180000) banners.push(h("div", { class: "banner critical" }, h("span", { class: "icon", text: "!" }), "Engine heartbeat is stale (last " + fmt.dt(hb.at) + "). Is the engine running?"));
  fill("banners", banners);

  const kb = $("btn-kill");
  kb.classList.toggle("engaged", !!st.kill_switch);
  kb.textContent = st.kill_switch ? "Stop engaged" : "Emergency stop";
}

function tickClock() {
  const t = new Date().toLocaleTimeString("en-US", { timeZone: "America/New_York", hour12: false });
  $("clock").textContent = t + " ET";
}

async function kill() {
  if (S.state?.kill_switch) return resume();
  if (!(await dialog("dlg-kill"))) return;
  try { await api("/api/control/kill", { method: "POST", body: JSON.stringify({ reason: $("kill-reason").value || "dashboard" }) }); toast("Emergency stop engaged", "critical"); poll(); }
  catch (e) { toast("Could not engage stop: " + e.message + ". Use the CLI: python -m nqtrader kill", "critical"); }
}
async function resume() {
  $("resume-text").value = "";
  if (!(await dialog("dlg-resume"))) return;
  try { await api("/api/control/resume", { method: "POST", body: JSON.stringify({ confirm: $("resume-text").value.trim() }) }); toast("Emergency stop released"); poll(); }
  catch (e) { toast(e.message, "warning"); }
}

// ------------------------------------------------------------------ overview

function kpi(label, value, hint, alarm) {
  return h("div", { class: "kpi" + (alarm ? " alarm" : "") }, h("div", { class: "label", text: label }),
    h("div", { class: "value" }, value), hint ? h("div", { class: "hint" }, hint) : null);
}

function renderOverview(st) {
  const b = st.budgets || { rows: [] };
  const day = b.rows[0] || {}, dd = b.rows[3] || {};
  const pos = st.position;
  const openRisk = pos?.risk_usd ?? 0;
  fill("kpis",
    kpi("Equity", fmt.usd(b.equity), "Balance " + fmt.usd(st.balance)),
    kpi("Day P&L", pnlSpan(day.pnl), `${fmt.usd0(day.remaining)} loss budget left`, day.used_pct >= 0.8),
    kpi("Unrealized", pnlSpan(st.unrealized ?? 0), pos ? `${pos.qty} × ${st.contract?.symbol}` : "Flat"),
    kpi("Drawdown", h("span", { class: dd.used > 0 ? "neg" : "", text: fmt.usd(dd.used ? -dd.used : 0) }), `limit ${fmt.usd0(dd.limit)}`, dd.used_pct >= 0.8),
    kpi("Open risk", fmt.usd(openRisk), `max ${fmt.usd0(st.limits?.risk_per_trade_usd)} / trade`),
    kpi("Trades today", `${st.strategy?.trades_today ?? 0} / ${st.strategy?.max_per_day ?? "—"}`, `${st.consecutive_losses ?? 0} losses in a row`),
  );
  $("chart-contract").textContent = st.contract ? `${st.contract.symbol} · last ${fmt.px(st.last_price)}` : "";
  renderPosition(st);
  renderSignal(st);
  renderTimers(st);
}

function renderPosition(st) {
  const p = st.position, el = $("position-card");
  const head = h("div", { class: "card-head" }, h("h2", { text: "Position & exit plan" }), p ? sideBadge(p.side) : null);
  if (!p) {
    const pend = st.pending_order;
    return fill(el, head, h("div", { class: "flat", text: pend ? "Entry order working…" : "Flat. No open position." }),
      h("p", { class: "note", text: "Every entry is sent with a resting stop-loss and take-profit attached. The engine never widens or removes a stop." }));
  }
  const ladder = h("div", { class: "ladder" }, h("h2", { text: "Exit ladder" }),
    ...(p.levels || []).map((l) => h("div", { class: "ladder-row" }, h("span", { class: "lbl", text: l.label }), h("span", { text: fmt.px(l.price) }), pnlSpan(l.pnl))));
  fill(el, head,
    h("div", { class: "pos-head" }, h("span", { class: "big num", text: `${p.qty} @ ${fmt.px(p.entry)}` }), pnlSpan(p.unrealized),
      p.r_now !== null ? h("span", { class: "muted num", text: `${p.r_now > 0 ? "+" : ""}${fmt.n(p.r_now)}R` }) : null),
    h("dl", { class: "kv" },
      h("dt", { text: "Stop-loss" }), h("dd", { class: "neg", text: fmt.px(p.stop) }),
      h("dt", { text: "Take-profit" }), h("dd", { class: "pos", text: p.target ? fmt.px(p.target) : "time exit" }),
      h("dt", { text: "Risk to stop" }), h("dd", { text: fmt.usd(p.risk_usd) }),
      h("dt", { text: "Reward at target" }), h("dd", { text: p.reward_usd ? fmt.usd(p.reward_usd) : "—" }),
      h("dt", { text: "Time exit" }), h("dd", { text: p.time_exit + " ET" }),
      h("dt", { text: "Strategy" }), h("dd", { class: "mono", text: p.strategy })),
    ladder,
    h("ul", { class: "rules" }, ...(p.rules || []).map((r) => h("li", { text: r }))));
}

function renderSignal(st) {
  const s = st.last_signal, el = $("signal-card");
  const head = h("div", { class: "card-head" }, h("h2", { text: "Strategy & last signal" }), h("span", { class: "badge info", text: `v${st.strategy?.version ?? "?"}` }));
  const strat = h("div", { class: "mono", text: st.strategy?.key || "—" });
  if (!s) return fill(el, head, strat, h("div", { class: "flat", text: "No signal yet this session." }));
  const ok = s.allowed;
  fill(el, head, strat,
    h("div", { class: "pos-head", style: "margin-top:10px" }, sideBadge(s.side), h("span", { class: "num", text: `${fmt.hm(s.time)} @ ${fmt.px(s.price)}` }),
      h("span", { class: "badge " + (ok ? "good" : "warning"), text: ok ? `✓ sent · ${s.qty} contract${s.qty === 1 ? "" : "s"}` : "✕ blocked by risk" })),
    h("dl", { class: "kv" }, h("dt", { text: "Stop" }), h("dd", { text: fmt.px(s.stop) }), h("dt", { text: "Target" }), h("dd", { text: s.target ? fmt.px(s.target) : "time exit" })),
    !ok ? h("ul", { class: "rules" }, ...s.reasons.map((r) => h("li", { text: r }))) : null);
}

function engineClock(st) {
  // During a replay the engine's clock is the replayed bar time, not the wall clock.
  const synthetic = (st.data_label || "").startsWith("synthetic") || (st.data_label || "").startsWith("replay");
  return synthetic && st.as_of ? Date.parse(st.as_of) : Date.now();
}

function renderTimers(st) {
  const now = engineClock(st);
  const rows = st.timers || [];
  const nextIdx = rows.findIndex((r) => Date.parse(r.at) > now);
  const replay = now !== Date.now();
  fill("timer-card",
    h("div", { class: "card-head" }, h("h2", { text: "Entry timer" }), h("span", { class: "muted", text: replay ? "replay clock " + fmt.hm(st.as_of) : "today, ET" })),
    rows.length ? h("div", { class: "timers" }, ...rows.map((r, i) => {
      const t = Date.parse(r.at), diff = t - now;
      const cd = diff <= 0 ? "done" : countdown(diff);
      return h("div", { class: "timer" + (i < nextIdx || nextIdx === -1 ? " past" : "") + (i === nextIdx ? " next" : "") + (r.news ? " news" : "") },
        h("span", { class: "at", text: fmt.hm(r.at) }), h("span", { class: "lbl", text: r.label }), h("span", { class: "cd", text: cd }));
    })) : h("div", { class: "empty", text: "No session schedule yet." }));
}
function countdown(ms) {
  const s = Math.floor(ms / 1000), hh = Math.floor(s / 3600), mm = Math.floor((s % 3600) / 60), ss = s % 60;
  if (hh >= 24) return `in ${Math.floor(hh / 24)}d ${hh % 24}h`;
  return hh > 0 ? `in ${hh}h ${String(mm).padStart(2, "0")}m` : `in ${mm}:${String(ss).padStart(2, "0")}`;
}

async function renderPriceChart(force) {
  const st = S.state || {};
  const day = S.chartDay;
  const data = await api("/api/bars" + (day ? "?day=" + encodeURIComponent(day) : ""));
  const sel = $("chart-day");
  if (force || sel.options.length !== data.days.length) {
    sel.replaceChildren(...data.days.map((d) => h("option", { value: d, text: d, selected: d === data.day })));
  }
  if (!S.charts.price) {
    const el = $("price-chart");
    const chart = baseChart(el);
    const candles = chart.addCandlestickSeries({ upColor: COLORS.good, downColor: COLORS.critical, borderVisible: false, wickUpColor: COLORS.good, wickDownColor: COLORS.critical, priceLineVisible: true, priceFormat: { type: "price", precision: 2, minMove: 0.25 } });
    const vwap = chart.addLineSeries({ color: COLORS.series[0], lineWidth: 2, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false });
    S.charts.price = { chart, candles, vwap, lines: [], day: null, n: 0 };
    attachTooltip(chart, el, (param) => {
      const c = param.seriesData.get(candles), v = param.seriesData.get(vwap);
      if (!c) return null;
      const t = new Date(param.time * 1000).toISOString().slice(11, 16);
      return [h("div", { class: "t-head", text: t + " ET" }),
        ...[["Open", c.open], ["High", c.high], ["Low", c.low], ["Close", c.close]].map(([k, x]) => h("div", { class: "t-row" }, h("span", { text: k }), h("span", { class: "v", text: fmt.px(x) }))),
        v ? h("div", { class: "t-row" }, h("span", { class: "ln", style: `background:${COLORS.series[0]}` }), h("span", { text: "VWAP" }), h("span", { class: "v", text: fmt.px(v.value) })) : null];
    });
  }
  const P = S.charts.price;
  const bars = data.bars.map((b) => ({ time: etTime(b.ts), open: b.open, high: b.high, low: b.low, close: b.close }));
  P.candles.setData(bars);
  P.vwap.setData(data.bars.map((b) => ({ time: etTime(b.ts), value: b.vwap })));
  if (P.day !== data.day) { P.chart.timeScale().fitContent(); P.day = data.day; }

  // levels: position entry/stop/target, alerts
  P.lines.forEach((l) => P.candles.removePriceLine(l));
  P.lines = [];
  const isToday = data.day === (st.bar_time || "").slice(0, 10);
  const addLine = (price, color, title, style = LWC.LineStyle.Dashed) => price && P.lines.push(P.candles.createPriceLine({ price, color, lineWidth: 1, lineStyle: style, axisLabelVisible: true, title }));
  const legend = [h("span", { class: "key" }, h("span", { class: "swatch", style: `background:${COLORS.series[0]}` }), "VWAP")];
  if (isToday && st.position) {
    addLine(st.position.entry, COLORS.text2, "Entry", LWC.LineStyle.Solid);
    addLine(st.position.stop, COLORS.critical, "Stop");
    addLine(st.position.target, COLORS.good, "Target");
    legend.push(h("span", { class: "key", text: "Dashed: stop (red) and target (green)" }));
  }
  if (isToday) {
    try {
      const alerts = await api("/api/alerts");
      alerts.filter((a) => a.status === "active").forEach((a) => addLine(a.price, COLORS.warning, "Alert", LWC.LineStyle.Dotted));
      if (alerts.some((a) => a.status === "active")) legend.push(h("span", { class: "key" }, h("span", { class: "swatch dashed", style: `color:${COLORS.warning}` }), "Price alert"));
    } catch (e) { /* non-fatal */ }
  }
  const markers = [];
  for (const t of data.trades) {
    markers.push({ time: etTime(t.entry_time), position: t.side > 0 ? "belowBar" : "aboveBar", color: COLORS.text2, shape: t.side > 0 ? "arrowUp" : "arrowDown", text: (t.side > 0 ? "Buy " : "Sell ") + fmt.px(t.entry) });
    markers.push({ time: etTime(t.exit_time), position: t.side > 0 ? "aboveBar" : "belowBar", color: t.pnl_usd > 0 ? COLORS.good : COLORS.critical, shape: "circle", text: `${t.reason} ${fmt.usd(t.pnl_usd, true)}` });
  }
  markers.sort((a, b) => a.time - b.time);
  P.candles.setMarkers(markers);
  if (data.trades.length) legend.push(h("span", { class: "key", text: "▲▼ entries · ● exits (P&L)" }));
  fill("chart-legend", legend);
}

function renderLog(rows, target) {
  fill(target, rows.length ? rows.map((e) => h("div", { class: "log-row" },
    h("span", { class: "ts", text: fmt.dt(e.ts) }), h("span", { class: "lvl " + e.level, text: e.level }),
    h("span", { class: "comp", text: e.component }), h("span", { class: "msg", text: e.message }))) : h("div", { class: "empty", text: "No events yet." }));
}

// ------------------------------------------------------------------ scanner

function renderScanner(st) {
  const sc = st.scanner;
  if (!sc) { fill("scanner-table", h("div", { class: "empty", text: "Scanner results appear after the engine processes its first bar." })); fill("levels"); return; }
  const stateBadge = { signal: "good", "in trade": "info", watching: "neutral", waiting: "neutral", done: "neutral", closed: "neutral" };
  fill("scanner-table", h("div", { class: "card-head" }, h("h2", { text: "Setups" }), h("span", { class: "muted", text: "as of " + fmt.dt(sc.as_of) })),
    h("div", { class: "table-wrap" }, h("table", {},
      h("thead", {}, h("tr", {}, ...["Strategy", "State", "Window (ET)", "Signals today", "Sim P&L today"].map((x, i) => h("th", { class: i === 4 ? "r" : "", text: x })))),
      h("tbody", {}, ...sc.strategies.map((r) => h("tr", {},
        h("td", {}, h("div", {}, h("strong", { text: r.name }), r.production ? h("span", { class: "badge info", style: "margin-left:8px", text: "production" }) : null),
          h("div", { class: "muted", style: "font-size:12px", text: r.description })),
        h("td", {}, h("span", { class: "badge " + (stateBadge[r.state] || "neutral"), text: r.state })),
        h("td", { class: "num", text: `${r.window_start}–${r.window_end}` }),
        h("td", {}, r.signals_today.length ? r.signals_today.map((s) => h("div", { class: "num" }, sideBadge(s.side), ` ${s.time} @ ${fmt.px(s.price)}`)) : h("span", { class: "muted", text: "—" })),
        h("td", { class: "r" }, pnlSpan(r.sim_pnl_today))))))),
    h("p", { class: "note", text: `Sim P&L is each strategy's own replay of today, per 1 ${st.instrument?.root || ""} contract after costs. It's for comparison only; no orders were placed.` }));
  const last = st.last_price;
  const L = sc.levels || {};
  const rows = [["Prior high", L.prior_high], ["VWAP +2σ", L.vwap_upper], ["10:00 range high", L.orb30_high], ["VWAP", L.vwap], ["10:00 range low", L.orb30_low], ["VWAP −2σ", L.vwap_lower], ["Prior low", L.prior_low], ["Prior close", L.prior_close]]
    .filter(([, v]) => v !== undefined && v !== null).sort((a, b) => b[1] - a[1]);
  fill("levels", h("table", {}, h("thead", {}, h("tr", {}, h("th", { text: "Level" }), h("th", { class: "r", text: "Price" }), h("th", { class: "r", text: "From last" }))),
    h("tbody", {}, ...rows.map(([k, v]) => h("tr", {}, h("td", { text: k }), h("td", { class: "r", text: fmt.px(v) }), h("td", { class: "r " + pnlClass(last - v), text: (last - v > 0 ? "+" : "") + fmt.n(last - v) }))))),
    h("p", { class: "note", text: `Last ${fmt.px(last)} · ATR(14) ${fmt.n(L.atr)} pts` }));
}

// ------------------------------------------------------------------ risk & budget

function meter(row) {
  const pct = row.used_pct || 0;
  const cls = pct >= 0.8 ? "critical" : pct >= 0.5 ? "warning" : "";
  return h("div", { class: "meter-row" },
    h("div", { class: "meter-top" }, h("span", { text: row.name }), h("span", { class: "r", text: `${fmt.usd0(row.used)} used of ${fmt.usd0(row.limit)} · ${fmt.usd0(row.remaining)} left` })),
    h("div", { class: "meter", role: "meter", "aria-valuenow": Math.round(pct * 100), "aria-valuemin": 0, "aria-valuemax": 100, "aria-label": row.name }, h("span", { class: cls, style: `width:${Math.max(pct * 100, pct > 0 ? 2 : 0)}%` })),
    row.name.endsWith("loss") ? h("div", { class: "muted", style: "font-size:12px;margin-top:4px" }, "P&L this period: ", pnlSpan(row.pnl)) : null);
}

function renderRisk(st) {
  const b = st.budgets;
  if (!b) { fill("budgets", h("div", { class: "empty", text: "Waiting for the engine." })); return; }
  fill("budgets", h("dl", { class: "kv", style: "margin:0 0 8px" }, h("dt", { text: "Trading capital" }), h("dd", { text: fmt.usd(b.capital) }),
    h("dt", { text: "Equity" }), h("dd", { text: fmt.usd(b.equity) }), h("dt", { text: "High-water mark" }), h("dd", { text: fmt.usd(b.equity_peak) }),
    h("dt", { text: "Risk budget per trade" }), h("dd", { text: fmt.usd(b.risk_per_trade) })), ...b.rows.map(meter));
  const hl = st.health || {};
  const checks = [
    ["Emergency stop released", !st.kill_switch, st.kill_switch ? "engaged" : "released"],
    ["No daily lockout", !st.lockout, st.lockout || "none"],
    ["Broker connected", hl.broker_connected, st.broker],
    ["Positions reconciled with broker", hl.positions_reconciled, ""],
    ["Protective stop confirmed", hl.protective_orders_confirmed, st.position ? "on open position" : "flat"],
    ["Market data fresh", hl.data_fresh, `max ${st.limits?.max_data_staleness_seconds}s old`],
    ["Front-month contract confirmed", hl.contract_ok, st.contract?.symbol],
    ["Outside news blackout", !st.news_blackout, st.news_blackout || "clear"],
    ["Consecutive losses today", (st.consecutive_losses ?? 0) < (st.limits?.max_consecutive_losses ?? 99), `${st.consecutive_losses ?? 0} of ${st.limits?.max_consecutive_losses}`],
    ["Trades today", (st.strategy?.trades_today ?? 0) < (st.limits?.max_trades_per_day ?? 99), `${st.strategy?.trades_today ?? 0} of ${st.limits?.max_trades_per_day} (strategy max ${st.strategy?.max_per_day})`],
  ];
  const allOk = checks.every((c) => c[1]);
  fill("gate", h("div", { class: "callout " + (allOk ? "good" : "warning"), style: "margin-bottom:10px" },
    h("strong", { text: allOk ? "✓ New entries allowed (within the entry window)" : "✕ New entries blocked" }),
    h("span", { class: "muted", text: "Every check must pass before any order is sent." })),
    h("ul", { class: "checks" }, ...checks.map(([n, ok, d]) => h("li", {}, h("span", { class: "ic " + (ok ? "good" : "bad"), text: ok ? "✓" : "✕" }),
      h("div", {}, h("div", { text: n }), d ? h("div", { class: "detail", text: d }) : null)))));
  renderSizer(st);
  const lim = st.limits || {};
  $("limits-digest").textContent = "digest " + (st.limits_digest || "");
  const label = (k) => k.replace(/_usd$/, "").replace(/_/g, " ").replace(/^max /, "max ");
  fill("limits", h("table", {}, h("tbody", {}, ...Object.entries(lim).map(([k, v]) => h("tr", {}, h("td", { text: label(k) }),
    h("td", { class: "r num", text: v === null ? "off" : k.endsWith("_usd") || k === "account_capital" ? fmt.usd0(v) : String(v) }))))),
    h("p", { class: "note", text: "The system can't change these values. Edit config/risk.json (validated on load) and restart the engine." }));
}

function renderSizer(st) {
  const el = $("sizer");
  if (!st.instrument || !st.limits) return fill(el, h("div", { class: "empty", text: "Waiting for the engine." }));
  if (el.dataset.ready) return calcSize();
  el.dataset.ready = "1";
  fill(el, h("div", { class: "form" },
    h("label", {}, "Stop distance (points)", h("input", { id: "sz-stop", type: "number", min: "0.25", step: "0.25", value: "40", oninput: calcSize })),
    h("div", { id: "sz-out" })));
  calcSize();
}
function calcSize() {
  const st = S.state, inst = st.instrument, lim = st.limits;
  const stop = parseFloat($("sz-stop")?.value) || 0;
  const per = (stop + lim.max_slippage_ticks * inst.tick_size) * inst.point_value + inst.commission_rt;
  const qty = stop > 0 ? Math.max(0, Math.min(Math.floor(lim.risk_per_trade_usd / per), lim.max_order_size, lim.max_contracts)) : 0;
  fill("sz-out", h("dl", { class: "kv" },
    h("dt", { text: "Instrument" }), h("dd", { text: `${inst.root} ($${inst.point_value}/pt)` }),
    h("dt", { text: "Worst-case loss / contract" }), h("dd", { text: fmt.usd(per) }),
    h("dt", { text: "Risk budget" }), h("dd", { text: fmt.usd(lim.risk_per_trade_usd) }),
    h("dt", { text: "Contracts allowed" }), h("dd", {}, h("strong", { class: qty ? "" : "neg", text: qty ? String(qty) : "0 (trade blocked)" }))),
    h("p", { class: "note", text: `Worst case = stop + ${lim.max_slippage_ticks} ticks slippage + commission. Size never increases after losses.` }));
}

// ------------------------------------------------------------------ performance

function statTable(rows, keyLabel) {
  if (!rows.length) return h("div", { class: "empty", text: "No trades yet." });
  const max = Math.max(...rows.map((r) => Math.abs(r.net_pnl)), 1);
  return h("div", { class: "hbars" }, ...rows.map((r) => {
    const w = (Math.abs(r.net_pnl) / max) * 50;
    return h("div", { class: "hbar", title: `${r.key}: ${r.trades} trades, ${fmt.usd(r.net_pnl, true)}, win ${fmt.pct(r.win_rate)}` },
      h("span", { class: "mono hlabel", text: r.key }),
      h("div", { class: "track" }, h("span", { class: "zero" }), h("span", { class: "fill " + (r.net_pnl >= 0 ? "up" : "down"), style: `width:${w}%` })),
      h("span", { class: "v" }, pnlSpan(r.net_pnl), h("span", { class: "muted", text: ` · ${r.trades}` })));
  }), h("p", { class: "note", text: `Net P&L by ${keyLabel}; number after the dot = trades. Hover for win rate.` }));
}

async function renderPerformance() {
  const p = await api("/api/performance");
  const synthetic = (p.data_label || "").startsWith("synthetic");
  $("perf-sub").textContent = `${(p.mode || "paper").toUpperCase()} trading · ${p.sessions_traded} sessions with trades` + (synthetic ? " · SYNTHETIC replay: not real performance" : " · simulated fills, not verified live performance");
  fill("perf-kpis",
    kpi("Net P&L", pnlSpan(p.net_pnl), `${p.trades} trades`),
    kpi("Win rate", fmt.pct(p.win_rate, 1), `avg R ${fmt.n(p.avg_r)}`),
    kpi("Expectancy", p.expectancy === null ? "—" : pnlSpan(p.expectancy), "per trade"),
    kpi("Profit factor", fmt.n(p.profit_factor), "gross win / gross loss"),
    kpi("Avg win / loss", h("span", {}, h("span", { class: "pos", text: fmt.usd0(p.avg_win) }), " / ", h("span", { class: "neg", text: fmt.usd0(p.avg_loss) })), ""),
    kpi("Max drawdown", h("span", { class: p.max_drawdown ? "neg" : "", text: fmt.usd(p.max_drawdown ? -p.max_drawdown : 0) }), "closed-trade equity"));
  if (!S.charts.equity) {
    const el = $("equity-chart");
    const chart = baseChart(el, { timeScale: { borderColor: COLORS.axis, timeVisible: false } });
    const series = chart.addAreaSeries({ lineColor: COLORS.series[0], topColor: "rgba(57,135,229,0.25)", bottomColor: "rgba(57,135,229,0.02)", lineWidth: 2, priceFormat: { type: "custom", formatter: (v) => fmt.usd0(v) } });
    S.charts.equity = { chart, series };
    attachTooltip(chart, el, (param) => {
      const d = param.seriesData.get(series);
      if (!d) return null;
      const row = (S.perfCurve || []).find((r) => r.time === param.time || etDay(param.time) === r.time);
      return [h("div", { class: "t-head", text: typeof param.time === "string" ? param.time : etDay(param.time) }),
        h("div", { class: "t-row" }, h("span", { class: "ln", style: `background:${COLORS.series[0]}` }), h("span", { text: "Equity" }), h("span", { class: "v", text: fmt.usd(d.value, true) })),
        row ? h("div", { class: "t-row" }, h("span", { text: "Day" }), h("span", { class: "v", text: fmt.usd(row.day_pnl, true) })) : null];
    });
    const el2 = $("daypnl-chart");
    const chart2 = baseChart(el2, { timeScale: { borderColor: COLORS.axis, timeVisible: false } });
    const hist = chart2.addHistogramSeries({ priceFormat: { type: "custom", formatter: (v) => fmt.usd0(v) } });
    S.charts.daypnl = { chart: chart2, series: hist };
    attachTooltip(chart2, el2, (param) => {
      const d = param.seriesData.get(hist);
      return d ? [h("div", { class: "t-head", text: etDay(param.time) }), h("div", { class: "t-row" }, h("span", { text: "Day P&L" }), h("span", { class: "v", text: fmt.usd(d.value, true) }))] : null;
    });
  }
  S.perfCurve = p.equity_curve;
  S.charts.equity.series.setData(p.equity_curve.map((r) => ({ time: r.time, value: r.value })));
  S.charts.daypnl.series.setData(p.equity_curve.map((r) => ({ time: r.time, value: r.day_pnl, color: r.day_pnl >= 0 ? COLORS.good : COLORS.critical })));
  S.charts.equity.chart.timeScale().fitContent(); S.charts.daypnl.chart.timeScale().fitContent();
  fill("perf-hours", statTable(p.by_hour, "entry hour (ET)"));
  fill("perf-strats", statTable(p.by_strategy, "strategy"));
  const trades = await api("/api/trades?limit=500");
  fill("trade-table", trades.length ? h("table", {},
    h("thead", {}, h("tr", {}, ...["Day", "Side", "Qty", "Entry", "Exit", "Entry px", "Exit px", "Stop", "Target", "Exit reason", "R", "P&L"].map((x, i) => h("th", { class: i >= 5 && i !== 9 ? "r" : "", text: x })))),
    h("tbody", {}, ...trades.map((t) => h("tr", {},
      h("td", { text: t.day }), h("td", {}, sideBadge(t.side)), h("td", { text: t.qty }), h("td", { text: fmt.hm(t.entry_time) }), h("td", { text: fmt.hm(t.exit_time) }),
      h("td", { class: "r", text: fmt.px(t.entry) }), h("td", { class: "r", text: fmt.px(t.exit) }), h("td", { class: "r", text: fmt.px(t.stop) }),
      h("td", { class: "r", text: t.target ? fmt.px(t.target) : "—" }), h("td", { text: t.reason }), h("td", { class: "r", text: fmt.n(t.r_multiple) }), h("td", { class: "r" }, pnlSpan(t.pnl_usd))))))
    : h("div", { class: "empty", text: "No closed trades yet." }));
}
const etDay = (t) => (typeof t === "string" ? t : t && t.year ? `${t.year}-${String(t.month).padStart(2, "0")}-${String(t.day).padStart(2, "0")}` : new Date(t * 1000).toISOString().slice(0, 10));

// ------------------------------------------------------------------ research

const VERDICT = { candidate: ["good", "✓ candidate"], rejected: ["critical", "✕ rejected"], insufficient: ["warning", "? insufficient"] };

async function renderResearch(force) {
  if (!S.research || force) S.research = await api("/api/research");
  const R = S.research;
  if (!R.available) {
    fill("research-conclusion", h("div", { class: "callout" }, h("strong", { text: "No research run yet" }), "Run: python -m nqtrader research --data your_nq_1m.csv (or --synthetic)."));
    return;
  }
  const rep = R.report;
  const synthetic = rep.data.synthetic;
  $("research-sub").textContent = `${rep.data.label} · ${rep.data.sessions} sessions (${rep.data.first} → ${rep.data.last}) · ${rep.total_configs_tested} configurations · code ${rep.git_sha} · ${rep.created.slice(0, 16).replace("T", " ")} UTC`;
  const passing = Object.values(rep.results).filter((r) => !r.is_baseline && r.verdict.status === "candidate");
  fill("research-conclusion", h("div", { class: "callout " + (passing.length ? "good" : "warning") },
    h("strong", { text: rep.conclusion }),
    synthetic ? h("div", { text: "This run used SYNTHETIC data. It tests the pipeline only and says nothing about real markets." }) : null,
    h("div", { class: "muted", text: `Costs: ${rep.costs.instrument} $${rep.costs.commission_rt} round trip + ${rep.costs.slippage_ticks_per_market_or_stop_fill} tick slippage per market/stop fill; limit fills need a 1-tick trade-through. Walk-forward ${rep.walk_forward.train_sessions}/${rep.walk_forward.test_sessions} sessions; final ${Math.round(rep.walk_forward.holdout_frac * 100)}% held out.` })));

  const names = Object.keys(rep.results);
  const cands = names.filter((n) => !rep.results[n].is_baseline);
  const bases = names.filter((n) => rep.results[n].is_baseline);
  const row = (n) => {
    const r = rep.results[n], o = r.oos, hd = r.holdout, [cls, label] = VERDICT[r.verdict.status] || ["neutral", r.verdict.status];
    return h("tr", { class: "clickable" + (r.is_baseline ? " baseline" : ""), tabindex: "0", onclick: () => pickResearch(n), onkeydown: (e) => e.key === "Enter" && pickResearch(n) },
      h("td", {}, h("strong", { text: n })), h("td", {}, r.is_baseline ? h("span", { class: "badge neutral", text: "baseline" }) : h("span", { class: "badge " + cls, text: label })),
      h("td", { class: "r", text: o.trades }), h("td", { class: "r" }, pnlSpan(o.net_pnl)), h("td", { class: "r", text: fmt.n(o.profit_factor) }),
      h("td", { class: "r", text: fmt.pct(o.win_rate) }), h("td", { class: "r" }, pnlSpan(o.expectancy), h("div", { class: "muted", style: "font-size:11px", text: `95% CI ${fmt.usd0(o.expectancy_ci95[0])} … ${fmt.usd0(o.expectancy_ci95[1])}` })),
      h("td", { class: "r" + (o.max_drawdown ? " neg" : ""), text: fmt.usd0(-o.max_drawdown) }), h("td", { class: "r", text: fmt.n(o.sharpe_annual) }), h("td", { class: "r", text: fmt.pct(o.exposure, 1) }), h("td", { class: "r" }, pnlSpan(hd.net_pnl)));
  };
  fill("research-table", h("table", {},
    h("thead", {}, h("tr", {}, ...["Strategy", "Verdict", "Trades", "Net P&L", "PF", "Win %", "Expectancy", "Max DD", "Sharpe", "Exposure", "Holdout P&L"].map((x, i) => h("th", { class: i >= 2 ? "r" : "", text: x })))),
    h("tbody", {}, h("tr", { class: "group" }, h("td", { colspan: 11, text: "Candidates" })), ...cands.map(row),
      h("tr", { class: "group" }, h("td", { colspan: 11, text: "Baselines (what a strategy must beat)" })), ...bases.map(row))),
    h("p", { class: "note", text: "Out-of-sample = parameters chosen on a rolling training window, then traded on the next unseen window. Holdout = most recent data, never used for selection. Per 1 contract, after costs. Click a row for detail." }));

  // equity chart (OOS), candidates colored in fixed order; baselines muted + dashed
  if (!S.charts.research) {
    const el = $("research-chart");
    const chart = baseChart(el, { timeScale: { borderColor: COLORS.axis, timeVisible: false } });
    S.charts.research = { chart, series: {} };
    attachTooltip(chart, el, (param) => {
      const rows = Object.entries(S.charts.research.series).map(([n, s]) => [n, s, param.seriesData.get(s)]).filter((x) => x[2] && x[1].options().visible);
      if (!rows.length) return null;
      rows.sort((a, b) => b[2].value - a[2].value);
      return [h("div", { class: "t-head", text: etDay(param.time) }), ...rows.map(([n, s, d]) => h("div", { class: "t-row" },
        h("span", { class: "ln", style: `background:${s.options().color}` }), h("span", { text: n }), h("span", { class: "v", text: fmt.usd0(d.value) })))];
    });
  }
  const RC = S.charts.research;
  Object.values(RC.series).forEach((s) => RC.chart.removeSeries(s));
  RC.series = {};
  const legend = [];
  const plotNames = [...cands, ...bases.filter((n) => n !== "baseline_no_trade")];
  plotNames.forEach((n, i) => {
    const r = rep.results[n];
    if (!r.oos_equity.length) return;
    const isBase = r.is_baseline;
    const color = isBase ? (n === "baseline_random" ? "#898781" : "#c3c2b7") : COLORS.series[cands.indexOf(n) % 5];
    const s = RC.chart.addLineSeries({ color, lineWidth: 2, lineStyle: isBase ? LWC.LineStyle.Dashed : LWC.LineStyle.Solid, priceLineVisible: false, lastValueVisible: !isBase, priceFormat: { type: "custom", formatter: (v) => fmt.usd0(v) } });
    s.setData(r.oos_days.map((d, k) => ({ time: d, value: r.oos_equity[k] })));
    RC.series[n] = s;
    const btn = h("button", { class: "key", type: "button", "aria-pressed": "true", onclick: () => { const v = btn.getAttribute("aria-pressed") !== "true"; btn.setAttribute("aria-pressed", String(v)); s.applyOptions({ visible: v }); } },
      h("span", { class: "swatch" + (isBase ? " dashed" : ""), style: isBase ? `color:${color}` : `background:${color}` }), n);
    legend.push(btn);
  });
  RC.chart.timeScale().fitContent();
  fill("research-legend", legend);

  const pick = $("research-pick");
  pick.replaceChildren(...names.map((n) => h("option", { value: n, text: n, selected: n === (S.researchPick || cands[0]) })));
  pick.onchange = () => pickResearch(pick.value);
  pickResearch(S.researchPick || cands[0], false);
  fill("experiments", h("table", {}, h("thead", {}, h("tr", {}, ...["Experiment", "Created (UTC)", "Data", "Code"].map((x) => h("th", { text: x })))),
    h("tbody", {}, ...R.experiments.map((e) => h("tr", { class: "clickable", onclick: async () => { S.research = await api("/api/research?exp_id=" + encodeURIComponent(e.id)); renderResearch(false); } },
      h("td", { class: "mono", text: e.id }), h("td", { text: e.created.slice(0, 16).replace("T", " ") }), h("td", { text: e.data_label }), h("td", { class: "mono", text: e.git_sha }))))));
}

function pickResearch(name, scroll = true) {
  S.researchPick = name;
  const r = S.research.report.results[name];
  $("research-pick").value = name;
  const v = r.verdict;
  const checkNames = { enough_trades: "≥ 100 out-of-sample trades", profit_factor: "Profit factor ≥ 1.15", sharpe: "Sharpe ≥ 0.75", expectancy_significant: "Expectancy 95% CI above $0",
    consistent_folds: "≥ 55% of walk-forward windows profitable", holdout_positive: "Holdout period profitable", beats_random: "Beats random-entry baseline" };
  const fold = (f) => h("tr", {}, h("td", { class: "num", text: (f.test_period || ["?", "?"]).join(" → ") }), h("td", { class: "mono", text: f.chosen || "no trade (nothing qualified)" }),
    h("td", { class: "r", text: f.test.trades }), h("td", { class: "r" }, pnlSpan(f.test.net_pnl)), h("td", { class: "r", text: fmt.n(f.test.profit_factor) }));
  const regimeRows = Object.entries(r.regimes);
  fill("research-detail",
    h("p", { class: "sub", text: r.description }),
    r.is_baseline ? null : h("div", { class: "grid-2", style: "margin-top:12px" },
      h("div", {}, h("h3", { text: "Validation gates" }), h("ul", { class: "checks" }, ...Object.entries(v.checks).map(([k, ok]) => h("li", {}, h("span", { class: "ic " + (ok ? "good" : "bad"), text: ok ? "✓" : "✕" }), h("div", { text: checkNames[k] || k })))),
        h("p", { class: "note", text: v.summary + ` Holdout used: ${r.holdout_params || "none qualified"}.` })),
      h("div", {}, h("h3", { text: "Market regimes (out-of-sample)" }), regimeRows.length ? h("table", {}, h("thead", {}, h("tr", {}, ...["Regime", "Trades", "Net", "PF", "Win %"].map((x, i) => h("th", { class: i ? "r" : "", text: x })))),
        h("tbody", {}, ...regimeRows.map(([k, m]) => h("tr", {}, h("td", { text: k.replace("_", " ") }), h("td", { class: "r", text: m.trades }), h("td", { class: "r" }, pnlSpan(m.net_pnl)), h("td", { class: "r", text: fmt.n(m.profit_factor) }), h("td", { class: "r", text: fmt.pct(m.win_rate) })))))
        : h("div", { class: "empty", text: "No out-of-sample trades." }),
        h("p", { class: "note", text: "Regimes are labelled after the fact from each session's own range and trend, so they explain results; they aren't a tradable filter." }))),
    h("h3", { text: "Walk-forward windows" }),
    h("div", { class: "table-wrap" }, h("table", {}, h("thead", {}, h("tr", {}, ...["Test window", "Parameters chosen on prior window", "Trades", "Net", "PF"].map((x, i) => h("th", { class: i >= 2 ? "r" : "", text: x })))), h("tbody", {}, ...r.folds.map(fold)))),
    h("h3", { text: "Entry timing (out-of-sample)" }),
    statTable(r.by_entry_hour.map((x) => ({ key: x.hour, trades: x.trades, net_pnl: x.net_pnl, win_rate: x.win_rate })), "entry hour"),
    r.by_earnings ? h("div", {}, h("h3", { text: "Earnings reaction days" }), h("table", {}, h("tbody", {}, ...Object.entries(r.by_earnings).map(([k, m]) => h("tr", {}, h("td", { text: k.replace(/_/g, " ") }), h("td", { class: "r", text: `${m.trades} trades` }), h("td", { class: "r" }, pnlSpan(m.net_pnl)), h("td", { class: "r" }, "exp ", pnlSpan(m.expectancy))))))) : null,
    h("h3", { text: "Parameter sensitivity (development period, in-sample)" }),
    h("div", { class: "table-wrap" }, h("table", {}, h("thead", {}, h("tr", {}, ...["Parameters", "Trades", "Net", "PF", "Win %", "Sharpe", "Max DD"].map((x, i) => h("th", { class: i ? "r" : "", text: x })))),
      h("tbody", {}, ...r.sensitivity.map((s) => h("tr", {}, h("td", { class: "mono", text: s.key }), h("td", { class: "r", text: s.trades }), h("td", { class: "r" }, pnlSpan(s.net_pnl)), h("td", { class: "r", text: fmt.n(s.profit_factor) }),
        h("td", { class: "r", text: fmt.pct(s.win_rate) }), h("td", { class: "r", text: fmt.n(s.sharpe_annual) }), h("td", { class: "r neg", text: fmt.usd0(-s.max_drawdown) })))))),
    h("p", { class: "note", text: "A robust strategy holds up across neighbouring parameters. If only one combination works, that's a sign of overfitting." }));
  if (scroll) $("research-detail").scrollIntoView({ behavior: "smooth", block: "start" });
}

// ------------------------------------------------------------------ news & earnings

async function renderNews() {
  const n = await api("/api/news");
  const now = Date.now();  // calendar events are real-world times, even during a replay
  fill("econ", n.econ.length ? h("table", {}, h("thead", {}, h("tr", {}, ...["When (ET)", "Event", "Impact", ""].map((x) => h("th", { text: x })))),
    h("tbody", {}, ...n.econ.map((e) => { const d = Date.parse(e.at) - now; return h("tr", {}, h("td", { class: "num", text: fmt.dt(e.at) }), h("td", { text: e.event }),
      h("td", {}, h("span", { class: "badge " + (e.impact === "high" ? "warning" : "neutral"), text: e.impact })), h("td", { class: "muted", text: d > 0 ? countdown(d) : "now / passed" })); })))
    : h("div", { class: "callout warning" }, h("strong", { text: "No upcoming events in the calendar" }), "The news blackout can't protect you without data. Add releases to config/economic_calendar.csv (FOMC, CPI, NFP, PPI, retail sales…)."),
    h("p", { class: "note", text: `High-impact events block new entries from ${S.state?.limits?.news_blackout_minutes_before ?? 5} min before to ${S.state?.limits?.news_blackout_minutes_after ?? 10} min after release.` }));
  const E = n.earnings;
  const ef = n.earnings_fetch;
  fill("earnings", E && E.reports.length ? h("div", {},
    h("div", { class: "callout " + (E.today_flag === "elevated" ? "warning" : ""), style: "margin-bottom:10px" },
      h("strong", { text: E.today_flag === "elevated" ? `Earnings reaction day: ${E.today_weight}% of index weight` : "No major earnings reaction today" }),
      h("span", { class: "muted", text: "A mega-cap report after the close moves NQ in the next session's open." })),
    h("table", {}, h("thead", {}, h("tr", {}, ...["Company", "Index wt", "Reports", "Timing", "NQ reaction"].map((x, i) => h("th", { class: i === 1 ? "r" : "", text: x })))),
      h("tbody", {}, ...E.reports.map((r) => h("tr", {}, h("td", {}, h("strong", { text: r.symbol })), h("td", { class: "r", text: r.weight + "%" }), h("td", { text: r.date }),
        h("td", { text: r.timing === "bmo" ? "before open" : "after close" }), h("td", { text: r.reaction_day })))))) :
    h("div", { class: "callout" }, h("strong", { text: "No earnings dates loaded" }), ef?.error ? "Last fetch: " + ef.error + ". " : "", "Run python -m nqtrader refresh (uses Yahoo), or list dates in config/earnings.csv."));
  const news = n.news;
  fill("headlines", news.items.length ? news.items.map((it) => h("div", { class: "headline" },
    h("a", { href: /^https?:\/\//.test(it.link) ? it.link : "#", target: "_blank", rel: "noopener noreferrer", text: it.title }),
    h("div", { class: "meta" }, h("span", { text: it.source }), h("span", { text: it.published }), ...it.tags.map((t) => h("span", { class: "tag" + (it.high_impact && ["Fed & rates", "Inflation", "Jobs"].includes(t) ? " hi" : ""), text: t })))))
    : h("div", { class: "empty", text: news.error ? "Headlines unavailable: " + news.error : "No headlines loaded yet. Press Refresh." }));
}

// ------------------------------------------------------------------ alerts

async function renderAlerts() {
  const rows = await api("/api/alerts");
  const last = S.state?.last_price;
  if (last) fill("alert-quick", [-1, -0.5, -0.25, 0.25, 0.5, 1].map((p) => h("button", { class: "btn btn-sm", type: "button", text: (p > 0 ? "+" : "") + p + "%", onclick: () => {
    const v = Math.round(last * (1 + p / 100) * 4) / 4; $("alert-price").value = v; $("alert-dir").value = p > 0 ? "above" : "below"; } })));
  fill("alert-list", rows.length ? h("div", { class: "table-wrap" }, h("table", {}, h("thead", {}, h("tr", {}, ...["Price", "Condition", "Note", "Status", "From last", ""].map((x) => h("th", { text: x })))),
    h("tbody", {}, ...rows.map((a) => h("tr", {}, h("td", { class: "num", text: fmt.px(a.price) }), h("td", { text: a.direction === "above" ? "at or above" : "at or below" }), h("td", { text: a.note || "" }),
      h("td", {}, h("span", { class: "badge " + (a.status === "active" ? "info" : "neutral"), text: a.status === "active" ? "active" : "triggered " + fmt.dt(a.triggered_at) })),
      h("td", { class: "num muted", text: last ? (a.price - last > 0 ? "+" : "") + fmt.n(a.price - last) : "" }),
      h("td", {}, h("button", { class: "btn btn-sm", type: "button", text: "Delete", "aria-label": "Delete alert at " + fmt.px(a.price), onclick: async () => { await api("/api/alerts/" + a.id, { method: "DELETE" }); renderAlerts(); } })))))))
    : h("div", { class: "empty", text: "No alerts yet." }));
}

// ------------------------------------------------------------------ system

async function renderSystem(st) {
  const hl = st.health || {}, hb = st.heartbeat;
  const item = (n, ok, d, warn) => h("li", {}, h("span", { class: "ic " + (ok ? "good" : warn ? "warn" : "bad"), text: ok ? "✓" : warn ? "!" : "✕" }), h("div", {}, h("div", { text: n }), d ? h("div", { class: "detail", text: d }) : null));
  fill("health", h("ul", { class: "checks" },
    item("Engine state", !!st.as_of, st.as_of ? "last update " + fmt.dt(st.as_of) + " (bar " + fmt.hm(st.bar_time) + ")" : "no state yet"),
    item("Engine heartbeat", !!hb, hb ? `${fmt.dt(hb.at)} · feed ${hb.feed}${hb.feed_error ? " · error: " + hb.feed_error : ""}` : "no live engine running (replay/demo, or stopped)", !hb),
    item("Broker", hl.broker_connected, `${st.broker} (${st.mode})`),
    item("Position reconciliation", hl.positions_reconciled, "broker positions = engine positions"),
    item("Protective stops", hl.protective_orders_confirmed, (st.working_orders || []).map((o) => `${o.kind} ${o.side > 0 ? "buy" : "sell"} ${o.qty} @ ${o.price ?? "mkt"}`).join(" · ") || "no working orders"),
    item("Market data", hl.data_fresh, st.data_label),
    item("Dashboard auth", st.auth_required, st.auth_required ? "token required for controls" : "localhost only, no token", !st.auth_required)));
  if (st.contract) fill("contract", h("dl", { class: "kv" }, h("dt", { text: "Trading" }), h("dd", { class: "mono", text: st.contract.symbol }),
    h("dt", { text: "Expiration" }), h("dd", { text: st.contract.expiration }), h("dt", { text: "Roll date" }), h("dd", { text: st.contract.roll_date }),
    h("dt", { text: "Days to roll" }), h("dd", { class: st.contract.days_to_roll <= 5 ? "neg" : "", text: String(st.contract.days_to_roll) })),
    h("p", { class: "note", text: "The engine switches to the next quarterly contract on the roll date (8 days before expiry) and won't trade a contract it can't confirm." }));
  try {
    const [pf, vs] = await Promise.all([api("/api/preflight"), api("/api/versions")]);
    fill("preflight", h("ul", { class: "checks" }, ...pf.map((c) => item(c.name, c.ok, c.detail))),
      h("p", { class: "note", text: "Live trading stays off until every item passes and you authorize it from the CLI. The dashboard can't enable it." }));
    fill("versions", h("table", {}, h("thead", {}, h("tr", {}, ...["Ver", "Strategy", "Parameters", "Status", "Validated by"].map((x) => h("th", { text: x })))),
      h("tbody", {}, ...vs.history.map((v) => h("tr", {}, h("td", { text: "v" + v.version }), h("td", { text: v.strategy }), h("td", { class: "mono", text: JSON.stringify(v.params) }),
        h("td", {}, h("span", { class: "badge " + (v.status === "production" ? "good" : v.status === "paper-only" ? "info" : "neutral"), text: v.status })), h("td", { class: "mono", text: v.experiment_id || "—" }))))),
      h("p", { class: "note", text: "Promote: python -m nqtrader promote <strategy> --params '{…}' --experiment <id>. Roll back: python -m nqtrader rollback." }));
  } catch (e) { /* shown elsewhere */ }
  const level = $("log-level").value;
  renderLog(await api("/api/events?limit=400" + (level ? "&level=" + level : "")), "log-full");
}

// ------------------------------------------------------------------ polling

async function checkNewEvents() {
  const ev = await api("/api/events?limit=30");
  renderLog(ev.slice(0, 25), "log-mini");
  const newest = ev.length ? ev[0].id : 0;
  if (S.lastEventId) {
    ev.filter((e) => e.id > S.lastEventId && (e.level === "alert" || e.level === "critical")).reverse().forEach((e) => {
      toast(e.message, e.level === "critical" ? "critical" : "info");
      if (e.level === "alert" && "Notification" in window && Notification.permission === "granted") new Notification("NQ alert", { body: e.message });
    });
  }
  S.lastEventId = Math.max(S.lastEventId, newest);
}

let pollBusy = false;
async function poll() {
  if (pollBusy) return;
  pollBusy = true;
  try {
    S.state = await api("/api/state");
    renderStatus(S.state);
    await refreshPage(false);
    await checkNewEvents();
  } catch (e) {
    fill("banners", h("div", { class: "banner critical" }, h("span", { class: "icon", text: "!" }), "Lost connection to the dashboard server: " + e.message));
  } finally { pollBusy = false; }
}

let slowTick = 0;
async function refreshPage(force) {
  const st = S.state;
  if (!st) return;
  slowTick = force ? 0 : slowTick + 1;
  const slow = force || slowTick % 5 === 0;  // ~10s for heavier panels
  const main = $("main");
  try {
    if (S.page === "overview") { renderOverview(st); await renderPriceChart(force); }
    else if (S.page === "scanner") renderScanner(st);
    else if (S.page === "risk") renderRisk(st);
    else if (S.page === "performance" && slow) await renderPerformance();
    else if (S.page === "research" && force) await renderResearch(false);
    else if (S.page === "news" && slow) await renderNews();
    else if (S.page === "alerts" && slow) await renderAlerts();
    else if (S.page === "system" && slow) await renderSystem(st);
  } catch (e) { console.error(e); }
  main.classList.remove("refreshing");
}

function init() {
  document.querySelectorAll("#nav a").forEach((a) => a.addEventListener("click", (e) => { e.preventDefault(); location.hash = a.dataset.page; }));
  $("btn-kill").addEventListener("click", kill);
  $("chart-day").addEventListener("change", (e) => { S.chartDay = e.target.value; renderPriceChart(false); });
  $("log-level").addEventListener("change", () => renderSystem(S.state || {}));
  $("btn-refresh-news").addEventListener("click", async (e) => {
    e.target.disabled = true; e.target.textContent = "Refreshing…";
    try { const r = await api("/api/news/refresh", { method: "POST" }); toast(`Loaded ${r.news} headlines, ${r.earnings} earnings dates` + (r.news_error ? ` (${r.news_error})` : "")); await renderNews(); }
    catch (err) { toast("Refresh failed: " + err.message, "warning"); }
    finally { e.target.disabled = false; e.target.textContent = "Refresh"; }
  });
  $("alert-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const price = parseFloat($("alert-price").value);
    if (!(price > 0)) return toast("Enter a valid price", "warning");
    try { await api("/api/alerts", { method: "POST", body: JSON.stringify({ price, direction: $("alert-dir").value, note: $("alert-note").value }) }); $("alert-note").value = ""; toast("Alert created"); renderAlerts(); }
    catch (err) { toast(err.message, "warning"); }
  });
  $("btn-notify").addEventListener("click", async () => {
    if (!("Notification" in window)) return toast("This browser doesn't support notifications", "warning");
    const p = await Notification.requestPermission(); toast(p === "granted" ? "Browser notifications on" : "Notifications not allowed", p === "granted" ? "info" : "warning");
  });
  tickClock(); setInterval(tickClock, 1000);
  setInterval(() => { if (S.state && S.page === "overview") renderTimers(S.state); }, 1000);
  const start = location.hash.slice(1) || safeStorage.get("nqt_page") || "overview";
  poll().then(() => showPage(document.getElementById("page-" + start) ? start : "overview"));
  setInterval(poll, POLL_MS);
}
document.addEventListener("DOMContentLoaded", init);
