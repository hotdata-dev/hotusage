'use strict';

// ---------------------------------------------------------------------------
// State & series definitions. Color follows the token type everywhere:
// output=blue, input=orange, cache write=aqua, cache read=yellow.
// ---------------------------------------------------------------------------
const SERIES = [
  { key: 'out', ckey: 'cout', label: 'Output',      cssVar: '--s-out' },
  { key: 'in',  ckey: 'cin',  label: 'Input',       cssVar: '--s-in'  },
  { key: 'cw',  ckey: 'ccw',  label: 'Cache write', cssVar: '--s-cw'  },
  { key: 'cr',  ckey: 'ccr',  label: 'Cache read',  cssVar: '--s-cr'  },
];

const PROVIDER_LABEL = { claude: 'Claude Code', codex: 'Codex', opencode: 'OpenCode' };

const state = {
  data: null,
  provider: 'all',
  user: 'all',
  project: 'all',
  range: '30',        // the first load only fetches this window
  loadedDays: 30,     // what the server has actually sent us so far
  metric: 'tok',
  stackBy: 'type',    // 'type' = token types, 'user' = one band per person
  sort: { key: 'end', dir: -1 },
  page: 0,            // sessions table page (PAGE_SIZE rows each)
  expanded: null,
  dailyView: 'chart',
  detailView: 'chart',
  detailCache: new Map(),
  cwdCache: new Map(),   // cwd rides with the detail fetch, not the list
};

// ---------------------------------------------------------------------------
// Small DOM / formatting helpers
// ---------------------------------------------------------------------------
const $ = (sel) => document.querySelector(sel);

function el(tag, attrs, ...children) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === 'class') n.className = v;
    else if (k === 'text') n.textContent = v;
    else if (k.startsWith('on')) n.addEventListener(k.slice(2), v);
    else n.setAttribute(k, v);
  }
  for (const c of children) if (c != null) n.append(c);
  return n;
}

const SVG_NS = 'http://www.w3.org/2000/svg';
function svg(tag, attrs, ...children) {
  const n = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === 'text') n.textContent = v;
    else n.setAttribute(k, v);
  }
  for (const c of children) if (c != null) n.append(c);
  return n;
}

function metaTail(s) {
  return `${timeShort(s.start)} to ${timeShort(s.end)} · ` +
    `${fmtInt(s.requests)} requests · peak context ${fmtInt(s.peakCtx)} tokens · ` +
    `${fmtMoney(s.cost)} list`;
}

function fmtTok(n) {
  if (n == null) return '-';
  const f = (v) => (Math.round(v * 10) / 10).toString().replace(/\.0$/, '');
  if (n >= 1e9) return f(n / 1e9) + 'B';
  if (n >= 1e6) return f(n / 1e6) + 'M';
  if (n >= 1e3) return f(n / 1e3) + 'K';
  return String(n);
}
const fmtInt = (n) => Number(n).toLocaleString('en-US');
function fmtMoney(v) {
  if (v > 0 && v < 0.005) return '<$0.01';
  if (v >= 1000) return '$' + Math.round(v).toLocaleString('en-US');
  return '$' + v.toFixed(2);
}
const fmtMetric = (v) => (state.metric === 'tok' ? fmtTok(v) : fmtMoney(v));
const fmtMetricExact = (v) => (state.metric === 'tok' ? fmtInt(v) : fmtMoney(v));

function localDay(d) {
  const p = (x) => String(x).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
function dayLabel(iso) { // '2026-08-19' -> 'Aug 19'
  const [, m, d] = iso.split('-').map(Number);
  return `${MONTHS[m - 1]} ${d}`;
}
function fullDay(iso) {
  const [y, m, d] = iso.split('-').map(Number);
  const dt = new Date(y, m - 1, d);
  return dt.toLocaleDateString('en-US', { weekday: 'short', month: 'short', day: 'numeric', year: 'numeric' });
}
function timeShort(ts) {
  const d = new Date(ts);
  return `${MONTHS[d.getMonth()]} ${d.getDate()}, ` +
    d.toLocaleTimeString('en-US', { hour: 'numeric', minute: '2-digit' });
}
function durationLabel(startTs, endTs) {
  const ms = new Date(endTs) - new Date(startTs);
  const min = Math.round(ms / 60000);
  if (min < 1) return '<1m';
  if (min < 60) return min + 'm';
  const h = Math.floor(min / 60);
  if (h < 48) return h + 'h ' + (min % 60) + 'm';
  return Math.round(h / 24) + 'd';
}
const shortModel = (m) => m.replace(/^claude-/, '');
const seriesColor = (s) => `var(${s.cssVar})`;

// --- stacking by person ----------------------------------------------------
// Slots are assigned from the whole loaded dataset in sorted order, NOT from
// whatever survives the current filters: colour has to follow the person, or
// filtering to a subset would repaint everyone who remained. Beyond the eight
// validated hues the tail folds into one "Other" band rather than inventing a
// ninth colour nobody can tell from the others.
const CAT_SLOTS = 8;

let userSlotCache = null;
function userSlots() {
  if (userSlotCache) return userSlotCache;
  const all = [...new Set((state.data?.sessions || []).map((s) => s.user).filter(Boolean))].sort();
  const short = new Map();
  for (const email of all) {
    const local = email.split('@')[0];
    // keep the local part unless two people share it, then disambiguate
    const clash = all.some((o) => o !== email && o.split('@')[0] === local);
    short.set(email, clash ? email : local);
  }
  userSlotCache = { all, short };
  return userSlotCache;
}

function userSeries() {
  const { all, short } = userSlots();
  const named = all.slice(0, CAT_SLOTS).map((email, i) => ({
    key: email, label: short.get(email), color: `var(--cat-${i + 1})`,
  }));
  const rest = all.slice(CAT_SLOTS);
  if (rest.length) {
    named.push({ key: '__other', label: `Other (${rest.length})`,
                 color: 'var(--muted)', members: new Set(rest) });
  }
  return named;
}

/// What the daily chart, its table and its legend are stacking right now.
function activeSeries() {
  if (state.stackBy === 'user') return userSeries();
  return SERIES.map((sr) => ({ key: sr.key, label: sr.label, color: seriesColor(sr) }));
}

function niceTicks(max, count) {
  if (max <= 0) return { ticks: [0, 1], top: 1 };
  const raw = max / count;
  const mag = Math.pow(10, Math.floor(Math.log10(raw)));
  let step = 10 * mag;
  for (const m of [1, 2, 2.5, 5]) { if (m * mag >= raw) { step = m * mag; break; } }
  const top = Math.ceil(max / step) * step;
  const ticks = [];
  for (let v = 0; v <= top + step / 2; v += step) ticks.push(v);
  return { ticks, top };
}

// ---------------------------------------------------------------------------
// Tooltip (one instance; values lead, labels follow; textContent only)
// ---------------------------------------------------------------------------
const tipEl = () => $('#tooltip');
function tipRow(color, isLine, value, label, extraClass) {
  const key = el('span', { class: 'tt-key' + (isLine ? ' line' : '') });
  if (color) key.style.background = color; else key.style.visibility = 'hidden';
  return el('div', { class: 'tt-row' + (extraClass ? ' ' + extraClass : '') },
    key,
    el('span', { class: 'tt-val', text: value }),
    el('span', { class: 'tt-label', text: label }));
}
function showTip(build, x, y) {
  const t = tipEl();
  t.replaceChildren();
  build(t);
  t.style.display = 'block';
  const r = t.getBoundingClientRect();
  let left = x + 14, top = y + 14;
  if (left + r.width > window.innerWidth - 8) left = x - r.width - 14;
  if (top + r.height > window.innerHeight - 8) top = y - r.height - 14;
  t.style.left = Math.max(4, left) + 'px';
  t.style.top = Math.max(4, top) + 'px';
}
function hideTip() { tipEl().style.display = 'none'; }

// ---------------------------------------------------------------------------
// Filtering
// ---------------------------------------------------------------------------
function cutoffDay() {
  if (state.range === 'all') return null;
  const d = new Date();
  d.setDate(d.getDate() - (Number(state.range) - 1));
  return localDay(d);
}

function filteredSessions() {
  const cut = cutoffDay();
  return state.data.sessions.filter((s) => {
    if (state.provider !== 'all' && s.provider !== state.provider) return false;
    if (state.user !== 'all' && s.user !== state.user) return false;
    if (state.project !== 'all' && s.project !== state.project) return false;
    if (cut && localDay(new Date(s.end)) < cut) return false;
    return true;
  });
}

const zeroDay = () => ({ in: 0, out: 0, cr: 0, cw: 0, cin: 0, cout: 0, ccr: 0, ccw: 0, u: new Map() });

function dailyAgg(sessions) {
  const ids = new Set(sessions.map((s) => s.id));
  // daily rows carry a session id but no person, so the owner is joined here
  const owner = new Map(sessions.map((s) => [s.id, s.user]));
  const cut = cutoffDay();
  const byDay = new Map();
  for (const r of state.data.daily) {
    if (!ids.has(r.s)) continue;
    if (cut && r.d < cut) continue;
    let d = byDay.get(r.d);
    if (!d) { d = zeroDay(); byDay.set(r.d, d); }
    for (const k of Object.keys(d)) { if (k !== 'u') d[k] += r[k] || 0; }
    // both metrics per person, so the metric toggle needs no re-aggregation
    const who = owner.get(r.s) || '(unknown)';
    const cur = d.u.get(who) || { tok: 0, cost: 0 };
    cur.tok += (r.in || 0) + (r.out || 0) + (r.cr || 0) + (r.cw || 0);
    cur.cost += (r.cin || 0) + (r.cout || 0) + (r.ccr || 0) + (r.ccw || 0);
    d.u.set(who, cur);
  }
  if (!byDay.size) return [];
  const daysSorted = [...byDay.keys()].sort();
  const first = cut || daysSorted[0];
  const last = daysSorted[daysSorted.length - 1];
  const out = [];
  const d = new Date(first + 'T12:00:00');
  for (let day = first; day <= last; d.setDate(d.getDate() + 1), day = localDay(d)) {
    // a fresh zero row per gap day: they carry a Map, which a shared object
    // would alias across every empty day
    out.push({ d: day, ...(byDay.get(day) || zeroDay()) });
    if (out.length > 5000) break; // safety
  }
  return out;
}

const sessionTotal = (s) => (state.metric === 'tok' ? s.in + s.out + s.cr + s.cw : s.cost);
const sessionVals = (s) => SERIES.map((sr) => (state.metric === 'tok' ? s[sr.key] : s[sr.ckey]));
function dayVals(row) {
  if (state.stackBy !== 'user') {
    return SERIES.map((sr) => (state.metric === 'tok' ? row[sr.key] : row[sr.ckey]));
  }
  const pick = (v) => (state.metric === 'tok' ? v.tok : v.cost);
  return activeSeries().map((s) => {
    if (s.members) {
      let sum = 0;
      for (const [who, v] of row.u || []) if (s.members.has(who)) sum += pick(v);
      return sum;
    }
    const v = (row.u || new Map()).get(s.key);
    return v ? pick(v) : 0;
  });
}

// ---------------------------------------------------------------------------
// Stacked daily column chart
// ---------------------------------------------------------------------------
function roundTopRect(x, y, w, h, r) {
  r = Math.min(r, w / 2, h);
  return `M${x},${y + h} L${x},${y + r} Q${x},${y} ${x + r},${y} L${x + w - r},${y} ` +
    `Q${x + w},${y} ${x + w},${y + r} L${x + w},${y + h} Z`;
}

function bucketWeekly(days) {
  const buckets = new Map();
  for (const row of days) {
    const [y, m, d] = row.d.split('-').map(Number);
    const dt = new Date(y, m - 1, d);
    dt.setDate(dt.getDate() - ((dt.getDay() + 6) % 7)); // back to Monday
    const wk = localDay(dt);
    let b = buckets.get(wk);
    if (!b) { b = { d: wk, week: true, in: 0, out: 0, cr: 0, cw: 0, cin: 0, cout: 0, ccr: 0, ccw: 0 }; buckets.set(wk, b); }
    for (const k of ['in', 'out', 'cr', 'cw', 'cin', 'cout', 'ccr', 'ccw']) b[k] += row[k];
  }
  return [...buckets.values()].sort((a, b) => (a.d < b.d ? -1 : 1));
}

function renderDailyChart(container, days) {
  container.replaceChildren();
  if (!days.length) { container.append(el('div', { class: 'empty', text: 'No usage in this range.' })); return; }

  const availW = Math.max(container.clientWidth || 640, 320);
  const ml = 46, mr = 8, mt = 12, mb = 26, plotH = 230;
  const w = availW;
  const plotW = w - ml - mr;
  // long ranges: one column per day gets unreadable - bucket into weeks instead
  if (days.length > plotW / 7) days = bucketWeekly(days);
  const h = plotH + mt + mb;
  const slot = plotW / days.length;
  const barW = Math.max(1.5, Math.min(24, slot * 0.72));
  const weekly = !!days[0].week;

  const totals = days.map((r) => dayVals(r).reduce((a, b) => a + b, 0));
  const { ticks, top } = niceTicks(Math.max(...totals), 4);
  const yOf = (v) => mt + plotH - (v / top) * plotH;
  const snap = (y) => Math.round(y) + 0.5; // crisp 1px hairlines

  const root = svg('svg', { viewBox: `0 0 ${w} ${h}`, role: 'img', 'aria-label': 'Daily usage stacked by token type' });

  for (const t of ticks) {
    if (t > 0) root.append(svg('line', { class: 'gridline', x1: ml, x2: ml + plotW, y1: snap(yOf(t)), y2: snap(yOf(t)) }));
    root.append(svg('text', {
      class: 'tick', x: ml - 7, y: yOf(t) + 3.5, 'text-anchor': 'end',
      text: state.metric === 'tok' ? fmtTok(t) : fmtMoney(t),
    }));
  }
  root.append(svg('line', { class: 'baseline', x1: ml, x2: ml + plotW, y1: snap(yOf(0)), y2: snap(yOf(0)) }));

  const labelStep = Math.max(1, Math.ceil(days.length / Math.max(2, Math.floor(plotW / 56))));
  days.forEach((row, i) => {
    if (i % labelStep !== 0) return;
    root.append(svg('text', {
      x: ml + i * slot + slot / 2, y: mt + plotH + 16, 'text-anchor': 'middle', text: dayLabel(row.d),
    }));
  });

  const active = activeSeries();
  days.forEach((row, i) => {
    const vals = dayVals(row);
    const g = svg('g', { class: 'day' });
    const x = ml + i * slot + (slot - barW) / 2;
    let cum = 0, firstDrawn = true, topSegIdx = -1;
    vals.forEach((v, k) => { if (v > 0) topSegIdx = k; });
    vals.forEach((v, k) => {
      if (v <= 0) { return; }
      const yTop = yOf(cum + v);
      let yBot = yOf(cum);
      cum += v;
      // 2px surface gap carved from this segment's own bottom (stack top stays honest)
      if (!firstDrawn && yBot - yTop > 3) yBot -= 2;
      firstDrawn = false;
      const hh = Math.max(1, yBot - yTop);
      const fill = `fill: ${active[k].color}`;
      if (k === topSegIdx && hh > 3) {
        g.append(svg('path', { class: 'segment', d: roundTopRect(x, yTop, barW, hh, 4), style: fill }));
      } else {
        g.append(svg('rect', { class: 'segment', x, y: yTop, width: barW, height: hh, style: fill }));
      }
    });

    const headTxt = weekly ? 'Week of ' + fullDay(row.d) : fullDay(row.d);
    const hit = svg('rect', {
      class: 'hitcol', x: ml + i * slot, y: mt, width: slot, height: plotH, tabindex: '0',
      'aria-label': `${headTxt}: ${fmtMetricExact(totals[i])} total`,
    });
    const build = (t) => {
      t.append(el('div', { class: 'tt-head', text: headTxt }));
      // zero bands are dropped from the tooltip: with a person per band most
      // days touch only a few, and listing six zeroes buries the ones that matter
      active.forEach((sr, k) => {
        if (state.stackBy !== 'user' || vals[k] > 0) {
          t.append(tipRow(sr.color, false, fmtMetricExact(vals[k]), sr.label));
        }
      });
      t.append(tipRow(null, false, fmtMetricExact(totals[i]), 'Total', 'total'));
    };
    hit.addEventListener('pointermove', (e) => { g.classList.add('lift'); showTip(build, e.clientX, e.clientY); });
    hit.addEventListener('pointerleave', () => { g.classList.remove('lift'); hideTip(); });
    hit.addEventListener('focus', () => {
      const r = hit.getBoundingClientRect();
      g.classList.add('lift'); showTip(build, r.left + r.width / 2, r.top + 30);
    });
    hit.addEventListener('blur', () => { g.classList.remove('lift'); hideTip(); });
    g.append(hit);
    root.append(g);
  });

  container.append(root);
}

function renderDailyTable(container, days) {
  container.replaceChildren();
  const table = el('table', { class: 'data' });
  const active = activeSeries();
  const trh = el('tr', null, el('th', { text: 'Date' }));
  for (const sr of active) trh.append(el('th', { class: 'num', text: sr.label }));
  trh.append(el('th', { class: 'num', text: 'Total' }));
  table.append(el('thead', null, trh));
  const tb = el('tbody');
  for (const row of [...days].reverse()) {
    const vals = dayVals(row);
    const tr = el('tr', null, el('td', { text: fullDay(row.d) }));
    vals.forEach((v) => tr.append(el('td', { class: 'num', text: fmtMetricExact(v) })));
    tr.append(el('td', { class: 'num', text: fmtMetricExact(vals.reduce((a, b) => a + b, 0)) }));
    tb.append(tr);
  }
  table.append(tb);
  const box = el('div', { class: 'scroller' });
  box.style.maxHeight = '320px';
  box.style.overflowY = 'auto';
  box.append(table);
  container.append(box);
}

// ---------------------------------------------------------------------------
// Session detail: context-window line + output columns, shared x + crosshair
// ---------------------------------------------------------------------------
function renderDetailChart(container, points) {
  container.replaceChildren();
  const n = points.length;
  if (!n) { container.append(el('div', { class: 'empty', text: 'No requests.' })); return; }

  const availW = Math.max(container.clientWidth || 640, 320);
  const ml = 50, mr = 14, mt = 18, gap = 34, ctxH = 140, outH = 84, mb = 24;
  const w = availW, plotW = w - ml - mr;
  const outTop = mt + ctxH + gap;
  const h = outTop + outH + mb;
  const slot = plotW / n;
  const xOf = (i) => ml + (i + 0.5) * slot;

  const ctxMaxRaw = Math.max(...points.map((p) => p.ctx));
  const outMaxRaw = Math.max(...points.map((p) => p.out), 1);
  const ctxT = niceTicks(ctxMaxRaw, 3);
  const outT = niceTicks(outMaxRaw, 2);
  const yCtx = (v) => mt + ctxH - (v / ctxT.top) * ctxH;
  const yOut = (v) => outTop + outH - (v / outT.top) * outH;

  const root = svg('svg', { viewBox: `0 0 ${w} ${h}`, role: 'img', 'aria-label': 'Context window and output tokens per request' });

  // panel titles (single series each: the title names it, no legend box)
  root.append(svg('text', { x: ml, y: mt - 7, class: 'direct-label', text: 'Context window per request' }));
  root.append(svg('text', { x: ml, y: outTop - 7, class: 'direct-label', text: 'Output tokens per request' }));

  const snap = (y) => Math.round(y) + 0.5;
  for (const t of ctxT.ticks) {
    if (t > 0) root.append(svg('line', { class: 'gridline', x1: ml, x2: ml + plotW, y1: snap(yCtx(t)), y2: snap(yCtx(t)) }));
    root.append(svg('text', { class: 'tick', x: ml - 7, y: yCtx(t) + 3.5, 'text-anchor': 'end', text: fmtTok(t) }));
  }
  root.append(svg('line', { class: 'baseline', x1: ml, x2: ml + plotW, y1: snap(yCtx(0)), y2: snap(yCtx(0)) }));
  for (const t of outT.ticks) {
    if (t > 0) root.append(svg('line', { class: 'gridline', x1: ml, x2: ml + plotW, y1: snap(yOut(t)), y2: snap(yOut(t)) }));
    root.append(svg('text', { class: 'tick', x: ml - 7, y: yOut(t) + 3.5, 'text-anchor': 'end', text: fmtTok(t) }));
  }
  root.append(svg('line', { class: 'baseline', x1: ml, x2: ml + plotW, y1: snap(yOut(0)), y2: snap(yOut(0)) }));

  // x labels: request index
  const labelStep = Math.max(1, Math.ceil(n / Math.max(2, Math.floor(plotW / 44))));
  for (let i = 0; i < n; i += labelStep) {
    root.append(svg('text', { x: xOf(i), y: outTop + outH + 16, 'text-anchor': 'middle', text: '#' + (i + 1) }));
  }

  // output columns (Output keeps its blue everywhere)
  const barW = Math.max(1.5, Math.min(24, slot * 0.7));
  points.forEach((p, i) => {
    if (p.out <= 0) return;
    const x = xOf(i) - barW / 2;
    const yTop = yOut(p.out);
    const hh = Math.max(1, yOut(0) - yTop);
    const fill = 'fill: var(--s-out)';
    if (hh > 3 && barW > 3) root.append(svg('path', { d: roundTopRect(x, yTop, barW, hh, Math.min(4, barW / 2.5)), style: fill }));
    else root.append(svg('rect', { x, y: yTop, width: barW, height: hh, style: fill }));
  });

  // context line + 10% area wash
  const linePts = points.map((p, i) => `${xOf(i)},${yCtx(p.ctx)}`);
  if (n > 1) {
    root.append(svg('path', {
      d: `M${xOf(0)},${yCtx(0)} L` + linePts.join(' L') + ` L${xOf(n - 1)},${yCtx(0)} Z`,
      style: 'fill: var(--s-ctx); fill-opacity: 0.1',
    }));
    root.append(svg('path', {
      d: 'M' + linePts.join(' L'),
      style: 'fill: none; stroke: var(--s-ctx); stroke-width: 2; stroke-linejoin: round; stroke-linecap: round',
    }));
  }
  // end marker: 8px dot with a 2px surface ring
  root.append(svg('circle', {
    cx: xOf(n - 1), cy: yCtx(points[n - 1].ctx), r: 4,
    style: 'fill: var(--s-ctx); stroke: var(--surface); stroke-width: 2',
  }));
  // direct-label the extreme (the peak), nothing else
  let peakI = 0;
  points.forEach((p, i) => { if (p.ctx > points[peakI].ctx) peakI = i; });
  const px = Math.min(Math.max(xOf(peakI), ml + 18), ml + plotW - 18);
  root.append(svg('text', {
    class: 'direct-label', x: px, y: Math.max(yCtx(points[peakI].ctx) - 8, mt + 9),
    'text-anchor': 'middle', text: fmtTok(points[peakI].ctx),
  }));

  // crosshair across both plots; nearest-index hit, whole plot is the target
  const cross = svg('line', { class: 'crosshair', x1: 0, x2: 0, y1: mt, y2: outTop + outH, visibility: 'hidden' });
  root.append(cross);
  const hit = svg('rect', { class: 'hitcol', x: ml, y: mt, width: plotW, height: outTop + outH - mt, tabindex: '0' });
  let curI = -1;
  const showAt = (i, cx, cy) => {
    const p = points[i];
    cross.setAttribute('x1', xOf(i)); cross.setAttribute('x2', xOf(i));
    cross.setAttribute('visibility', 'visible');
    showTip((t) => {
      t.append(el('div', { class: 'tt-head', text: `Request ${i + 1} of ${n} - ${timeShort(p.t)}` }));
      t.append(tipRow('var(--s-ctx)', true, fmtInt(p.ctx), 'Context'));
      t.append(tipRow('var(--s-out)', false, fmtInt(p.out), 'Output'));
    }, cx, cy);
  };
  hit.addEventListener('pointermove', (e) => {
    const r = root.getBoundingClientRect();
    const sx = w / r.width;
    const i = Math.min(n - 1, Math.max(0, Math.round(((e.clientX - r.left) * sx - ml) / slot - 0.5)));
    curI = i;
    showAt(i, e.clientX, e.clientY);
  });
  hit.addEventListener('pointerleave', () => { cross.setAttribute('visibility', 'hidden'); hideTip(); });
  hit.addEventListener('focus', () => {
    curI = curI < 0 ? n - 1 : curI;
    const r = hit.getBoundingClientRect();
    showAt(curI, r.left + r.width / 2, r.top + 40);
  });
  hit.addEventListener('blur', () => { cross.setAttribute('visibility', 'hidden'); hideTip(); });
  hit.addEventListener('keydown', (e) => {
    if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
    e.preventDefault();
    curI = Math.min(n - 1, Math.max(0, (curI < 0 ? n - 1 : curI) + (e.key === 'ArrowRight' ? 1 : -1)));
    const r = hit.getBoundingClientRect();
    showAt(curI, r.left + ((xOf(curI) - ml) / plotW) * r.width, r.top + 40);
  });
  root.append(hit);
  container.append(root);
}

function renderDetailTable(container, points) {
  container.replaceChildren();
  const table = el('table', { class: 'data' });
  table.append(el('thead', null, el('tr', null,
    el('th', { class: 'num', text: '#' }), el('th', { text: 'Time' }),
    el('th', { class: 'num', text: 'Context' }), el('th', { class: 'num', text: 'Output' }))));
  const tb = el('tbody');
  points.forEach((p, i) => {
    tb.append(el('tr', null,
      el('td', { class: 'num', text: String(i + 1) }),
      el('td', { text: timeShort(p.t) }),
      el('td', { class: 'num', text: fmtInt(p.ctx) }),
      el('td', { class: 'num', text: fmtInt(p.out) })));
  });
  table.append(tb);
  const box = el('div');
  box.style.maxHeight = '300px';
  box.style.overflowY = 'auto';
  box.append(table);
  container.append(box);
}

// ---------------------------------------------------------------------------
// Sessions table
// ---------------------------------------------------------------------------
const PAGE_SIZE = 100;

const COLS = [
  { key: 'title',    label: 'Session' },
  { key: 'models',   label: 'Model',        sortKey: 'models' },
  { key: 'bar',      label: 'Mix' },
  { key: 'requests', label: 'Requests',     num: true, sortKey: 'requests' },
  { key: 'out',      label: 'Output',       num: true, sortKey: 'out' },
  { key: 'total',    label: 'Total tokens', num: true, sortKey: 'total' },
  { key: 'peakCtx',  label: 'Peak context', num: true, sortKey: 'peakCtx' },
  { key: 'cost',     label: 'List $',       num: true, sortKey: 'cost' },
];

function sortVal(s, key) {
  if (key === 'total') return sessionTotal(s);
  if (key === 'end') return s.end || '';
  if (key === 'models') return (s.models || []).map(shortModel).sort().join(', ');
  return s[key] || 0;
}

function renderSessions(container, sessions) {
  container.replaceChildren();
  $('#sessCount').textContent = sessions.length + (sessions.length === 1 ? ' session' : ' sessions');
  if (!sessions.length) { container.append(el('div', { class: 'empty', text: 'No sessions match the current filters.' })); return; }

  const sorted = [...sessions].sort((a, b) => {
    const va = sortVal(a, state.sort.key), vb = sortVal(b, state.sort.key);
    return (va < vb ? -1 : va > vb ? 1 : 0) * state.sort.dir;
  });
  const table = el('table', { class: 'data' });
  const trh = el('tr');
  for (const c of COLS) {
    const th = el('th', { class: (c.num ? 'num' : '') + (c.sortKey ? ' sortable' : ''), text: c.label });
    if (c.sortKey) {
      if (state.sort.key === c.sortKey) th.append(el('span', { class: 'arrow', text: state.sort.dir < 0 ? '▼' : '▲' }));
      th.addEventListener('click', () => {
        state.sort = { key: c.sortKey, dir: state.sort.key === c.sortKey ? -state.sort.dir : -1 };
        state.page = 0;
        render();
      });
    }
    trh.append(th);
  }
  table.append(el('thead', null, trh));

  // an expanded session (deep link or a sort change) must stay visible
  const expandedAt = state.expanded ? sorted.findIndex((s) => s.id === state.expanded) : -1;
  const pages = Math.max(1, Math.ceil(sorted.length / PAGE_SIZE));
  if (expandedAt >= 0) state.page = Math.floor(expandedAt / PAGE_SIZE);
  state.page = Math.min(Math.max(0, state.page), pages - 1);
  const start = state.page * PAGE_SIZE;
  const pageRows = sorted.slice(start, start + PAGE_SIZE);

  const tb = el('tbody');
  for (const s of pageRows) {
    const tr = el('tr', { class: 'sess', tabindex: '0' });
    const meta = [
      s.user,
      PROVIDER_LABEL[s.provider] || s.provider,
      s.project,
      dayLabel(localDay(new Date(s.start))),
      durationLabel(s.start, s.end),
    ].filter(Boolean).join(' · ');
    tr.append(el('td', null,
      el('div', { class: 'title', text: s.title }),
      el('div', { class: 'meta', text: meta })));

    const models = (s.models || []).map(shortModel);
    tr.append(el('td', null,
      el('div', { class: 'models', text: models.join(', ') || '\u2014', title: models.join(', ') })));

    // composition bar: share of the session by token type (magnitude lives in the columns)
    const bar = el('div', { class: 'minibar', role: 'img', 'aria-label': `${fmtMetricExact(sessionTotal(s))} total` });
    const vals = sessionVals(s);
    const tot = vals.reduce((a, b) => a + b, 0) || 1;
    vals.forEach((v, k) => {
      if (v <= 0) return;
      const sp = el('span');
      sp.style.background = seriesColor(SERIES[k]);
      sp.style.flex = String(v / tot);
      bar.append(sp);
    });
    const barTd = el('td', null, bar);
    barTd.addEventListener('pointermove', (e) => showTip((t) => {
      t.append(el('div', { class: 'tt-head', text: s.title }));
      SERIES.forEach((sr, k) => t.append(tipRow(seriesColor(sr), false, fmtMetricExact(vals[k]),
        `${sr.label} · ${Math.round((vals[k] / tot) * 100)}%`)));
      t.append(tipRow(null, false, fmtMetricExact(sessionTotal(s)), 'Total', 'total'));
    }, e.clientX, e.clientY));
    barTd.addEventListener('pointerleave', hideTip);
    tr.append(barTd);

    tr.append(el('td', { class: 'num', text: fmtInt(s.requests) }));
    tr.append(el('td', { class: 'num', text: fmtTok(s.out) }));
    tr.append(el('td', { class: 'num', text: fmtTok(s.in + s.out + s.cr + s.cw) }));
    tr.append(el('td', { class: 'num', text: fmtTok(s.peakCtx) }));
    tr.append(el('td', { class: 'num', text: fmtMoney(s.cost) }));

    const toggle = () => {
      state.expanded = state.expanded === s.id ? null : s.id;
      history.replaceState(null, '', state.expanded ? '#s=' + state.expanded : location.pathname);
      render();
    };
    tr.addEventListener('click', toggle);
    tr.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); } });
    tb.append(tr);

    if (state.expanded === s.id) tb.append(buildDetailRow(s));
  }
  table.append(tb);
  const scroller = el('div', { class: 'scroller' });
  scroller.style.overflowX = 'auto';
  scroller.append(table);
  container.append(scroller);
  if (pages > 1) container.append(buildPager(pages, start, pageRows.length, sorted.length));
}

function buildPager(pages, start, shown, total) {
  const go = (p) => {
    state.page = p;
    // the row toggle keeps #s= and state.expanded in sync; collapsing here must too,
    // or a later load() would restore the old id from the hash and yank the page back
    state.expanded = null;
    history.replaceState(null, '', location.pathname);
    render();
  };
  const bar = el('div', { class: 'pager' });
  bar.append(el('span', { class: 'flabel', text: `${start + 1}\u2013${start + shown} of ${total}` }));
  bar.append(el('span', { class: 'spacer' }));
  const prev = el('button', { type: 'button', text: 'Previous', onclick: () => go(state.page - 1) });
  prev.disabled = state.page === 0;
  bar.append(prev);
  bar.append(el('span', { class: 'flabel', text: `Page ${state.page + 1} of ${pages}` }));
  const next = el('button', { type: 'button', text: 'Next', onclick: () => go(state.page + 1) });
  next.disabled = state.page >= pages - 1;
  bar.append(next);
  return bar;
}

function buildDetailRow(s) {
  const td = el('td', { colspan: String(COLS.length) });
  const head = el('div', { class: 'card-head' },
    el('h3', { text: 'Session detail' }),
    el('span', { class: 'spacer' }));
  const toggleBox = el('div', { class: 'viewtoggle' });
  for (const [v, lbl] of [['chart', 'Chart'], ['table', 'Table']]) {
    toggleBox.append(el('button', {
      type: 'button', text: lbl, 'aria-pressed': String(state.detailView === v),
      onclick: (e) => { e.stopPropagation(); state.detailView = v; render(); },
    }));
  }
  head.append(toggleBox);
  const metaText = (cwd) => (s.user ? `${s.user}${s.host ? ' on ' + s.host : ''} · ` : '') +
    (cwd ? `${cwd} · ` : '') + metaTail(s);
  const meta = el('div', { class: 'dmeta', text: metaText('') });
  const box = el('div', { class: 'chartbox' });
  td.append(head, meta, box);
  td.addEventListener('click', (e) => e.stopPropagation());

  const knownCwd = state.cwdCache.get(s.id);
  if (knownCwd) meta.textContent = metaText(knownCwd);

  const cached = state.detailCache.get(s.id);
  const draw = (points) => {
    if (state.detailView === 'chart') renderDetailChart(box, points);
    else renderDetailTable(box, points);
  };
  if (cached) {
    // defer so the container has a measurable width
    requestAnimationFrame(() => draw(cached));
  } else {
    box.append(el('div', { class: 'empty', text: 'Loading detail...' }));
    fetch('/api/session/' + encodeURIComponent(s.id))
      .then((r) => {
        if (r.status === 401) { location.href = '/login'; }
        return r.json();
      })
      .then((d) => {
        state.detailCache.set(s.id, d.detail || []);
        if (d.cwd) {
          state.cwdCache.set(s.id, d.cwd);
          meta.textContent = metaText(d.cwd);
        }
        if (state.expanded === s.id) draw(d.detail || []);
      })
      .catch(() => box.replaceChildren(el('div', { class: 'empty', text: 'Failed to load session detail.' })));
  }
  return el('tr', { class: 'detailrow' }, td);
}

// ---------------------------------------------------------------------------
// Tiles, legend, top-level render
// ---------------------------------------------------------------------------
function renderTiles(sessions) {
  const box = $('#tiles');
  box.replaceChildren();  // clears the skeleton tiles
  const sum = (f) => sessions.reduce((a, s) => a + f(s), 0);
  // member count lives on the admin page, not here: this row is about usage
  const tiles = [
    { label: 'Sessions', value: fmtInt(sessions.length) },
    { label: 'API requests', value: fmtInt(sum((s) => s.requests)) },
    { label: 'Total tokens', value: fmtTok(sum((s) => s.in + s.out + s.cr + s.cw)), hint: 'incl. cache reads' },
    { label: 'Output tokens', value: fmtTok(sum((s) => s.out)) },
    { label: 'Max context', value: fmtTok(sessions.length ? Math.max(...sessions.map((s) => s.peakCtx)) : 0), hint: 'largest single request' },
    { label: 'List-price equiv.', value: fmtMoney(sum((s) => s.cost)),
      hint: 'API list prices, not a bill' },
  ];
  for (const t of tiles) {
    box.append(el('div', { class: 'tile' },
      el('div', { class: 'label', text: t.label }),
      el('div', { class: 'value', text: t.value }),
      t.hint ? el('div', { class: 'hint', text: t.hint }) : null));
  }
}

function renderLegend() {
  const box = $('#dailyLegend');
  box.replaceChildren();
  // always present: identity must never be carried by colour alone
  for (const sr of activeSeries()) {
    const sw = el('span', { class: 'swatch' });
    sw.style.background = sr.color;
    box.append(el('span', { class: 'item' }, sw, el('span', { text: sr.label })));
  }
}

function renderDailyToggle() {
  const box = $('#dailyToggle');
  box.replaceChildren();
  for (const [v, lbl] of [['chart', 'Chart'], ['table', 'Table']]) {
    box.append(el('button', {
      type: 'button', text: lbl, 'aria-pressed': String(state.dailyView === v),
      onclick: () => { state.dailyView = v; render(); },
    }));
  }
}

function render() {
  if (!state.data) return;
  const sessions = filteredSessions();
  const days = dailyAgg(sessions);
  const unit = state.metric === 'tok' ? 'tokens' : 'list-price equivalent';
  $('#dailyTitle').textContent = state.stackBy === 'user'
    ? `Daily usage by person (${unit})` : `Daily usage (${unit})`;
  renderTiles(sessions);
  renderLegend();
  renderDailyToggle();
  const dc = $('#dailyChart');
  if (state.dailyView === 'chart') renderDailyChart(dc, days);
  else renderDailyTable(dc, days);
  renderSessions($('#sessions'), sessions);
}

// ---------------------------------------------------------------------------
// Filters wiring + load
// ---------------------------------------------------------------------------
function wireSeg(id, attr, apply) {
  const seg = document.getElementById(id);
  seg.addEventListener('click', (e) => {
    const btn = e.target.closest('button');
    if (!btn) return;
    for (const b of seg.querySelectorAll('button')) b.setAttribute('aria-pressed', String(b === btn));
    state.page = 0;
    apply(btn.dataset[attr]);
    render();
  });
}

function populateProjects() {
  const sel = $('#projectSel');
  const counts = new Map();
  for (const s of state.data.sessions) {
    if (state.provider !== 'all' && s.provider !== state.provider) continue;
    counts.set(s.project, (counts.get(s.project) || 0) + 1);
  }
  const opts = [...counts.entries()].sort((a, b) => b[1] - a[1]);
  sel.replaceChildren(el('option', { value: 'all', text: 'All projects' }));
  for (const [name, n] of opts) sel.append(el('option', { value: name, text: `${name} (${n})` }));
  if (![...counts.keys()].includes(state.project)) state.project = 'all';
  sel.value = state.project;
  sel.onchange = () => { state.project = sel.value; state.page = 0; render(); };
}

function populateUsers() {
  const sel = $('#userSel');
  const counts = new Map();
  for (const s of state.data.sessions) {
    if (!s.user) continue;
    counts.set(s.user, (counts.get(s.user) || 0) + 1);
  }
  const opts = [...counts.entries()].sort((a, b) => b[1] - a[1]);
  sel.replaceChildren(el('option', { value: 'all', text: 'All users' }));
  for (const [name, n] of opts) sel.append(el('option', { value: name, text: `${name} (${n})` }));
  if (![...counts.keys()].includes(state.user)) state.user = 'all';
  sel.value = state.user;
  sel.onchange = () => { state.user = sel.value; state.page = 0; render(); };
  // hide the filter until more than one user reports in -- the whole group, so
  // the label never outlives the control it names
  $('#userGroup').hidden = counts.size <= 1;
}

function populateProviders() {
  const seg = $('#providerSeg');
  const counts = new Map();
  for (const s of state.data.sessions) counts.set(s.provider, (counts.get(s.provider) || 0) + 1);
  const providers = [...counts.entries()].sort((a, b) => b[1] - a[1]);
  seg.replaceChildren(el('button', {
    type: 'button', text: 'All', 'data-provider': 'all',
    'aria-pressed': String(state.provider === 'all'),
  }));
  for (const [p] of providers) {
    seg.append(el('button', {
      type: 'button', text: PROVIDER_LABEL[p] || p, 'data-provider': p,
      'aria-pressed': String(state.provider === p),
    }));
  }
  // a single tool needs no filter
  $('#providerGroup').hidden = providers.length <= 1;
}

const TILE_COUNT = 6; // must match renderTiles(), so nothing reflows on arrival

function renderSkeleton() {
  const tiles = $('#tiles');
  if (!tiles.childElementCount) {
    for (let i = 0; i < TILE_COUNT; i++) {
      tiles.append(el('div', { class: 'tile' },
        el('div', { class: 'skel skel-label' }),
        el('div', { class: 'skel skel-value' })));
    }
  }
  const chart = $('#dailyChart');
  if (!chart.childElementCount) chart.append(el('div', { class: 'skel skel-chart' }));
  const sess = $('#sessions');
  if (!sess.childElementCount) {
    for (let i = 0; i < 6; i++) sess.append(el('div', { class: 'skel skel-row' }));
  }
}

let loadSeq = 0;
let deepLinkPending = /^#s=[\w-]+$/.test(location.hash);

async function load(fresh, days) {
  renderSkeleton();
  const want = days === undefined ? state.range : days;
  const seq = ++loadSeq;
  const boxes = document.querySelectorAll('.chartbox');
  boxes.forEach((b) => b.classList.add('loading'));
  try {
    const params = new URLSearchParams();
    if (want && want !== 'all') params.set('days', String(want));
    if (fresh) params.set('fresh', '1');
    const q = params.toString();
    const r = await fetch('/api/data' + (q ? '?' + q : ''));
    if (r.status === 401) { location.href = '/login'; return; }
    const body = await r.json();
    if (!r.ok) throw new Error(body.error || r.statusText);
    if (seq !== loadSeq) return;  // a later request already answered
    state.data = body;
    // slot assignment is derived from the whole dataset, so a wider window or a
    // different org must re-derive it rather than keep the old ordering
    userSlotCache = null;
    state.loadedDays = body.windowDays || Infinity;
    if (body.viewer && window.setViewer) {
      window.setViewer(body.viewer.email, body.viewer.org);
    }
    state.detailCache.clear();
    // short enough to sit in the filter row; the counts and the source ride in
    // the title, where they are one hover away rather than a page away
    const nSess = state.data.sessions.length;
    const note = $('#srcNote');
    note.textContent = 'fetched ' +
      new Date(state.data.generatedAt).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
    note.title = `${nSess} sessions from ${state.data.source || 'hotdata'}`;
    populateProviders();
    populateUsers();
    populateProjects();
    const m = location.hash.match(/^#s=([\w-]+)$/);
    if (m) state.expanded = m[1];
    // a deep link can point at a session older than the loaded window;
    // widen ONCE rather than rendering with nothing expanded and no hint why.
    // One-shot: after the user touches the range themselves, their choice
    // wins, even with the #s= hash still in the URL.
    if (deepLinkPending && state.expanded && state.loadedDays !== Infinity &&
        !state.data.sessions.some((x) => x.id === state.expanded)) {
      deepLinkPending = false;
      state.range = 'all';
      for (const b of document.querySelectorAll('#rangeSeg button')) {
        b.setAttribute('aria-pressed', String(b.dataset.range === 'all'));
      }
      load(false, 'all');
      return;
    }
    render();
  } catch (e) {
    if (seq !== loadSeq) return;  // superseded; the newer load owns the UI
    // clear every skeleton, not just the list: render() bails while
    // state.data is null, so the shimmer would run forever under the error
    $('#tiles').replaceChildren();
    $('#dailyChart').replaceChildren();
    $('#sessions').replaceChildren(el('div', {
      class: 'empty',
      text: 'Failed to load data from hotdata: ' + (e && e.message ? e.message : 'is the server running?'),
    }));
  } finally {
    if (seq === loadSeq) boxes.forEach((b) => b.classList.remove('loading'));
  }
}

wireSeg('rangeSeg', 'range', (v) => {
  deepLinkPending = false;
  state.range = v;
  const want = v === 'all' ? Infinity : Number(v);
  if (want > state.loadedDays) load(false, v);  // fetch the wider window
});
wireSeg('metricSeg', 'metric', (v) => { state.metric = v; });
wireSeg('stackSeg', 'stack', (v) => { state.stackBy = v; });
wireSeg('providerSeg', 'provider', (v) => { state.provider = v; populateProjects(); });
$('#refresh').addEventListener('click', () => load(true));
let resizeTimer = null;
window.addEventListener('resize', () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(render, 150);
});
load();

