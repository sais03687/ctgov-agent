/* TASK: Draws the charts of the demo page from the response of POST /query.
 * GRADING: Input/Output Design. The page uses only the documented response, so it shows that
 *          a frontend can draw each chart type without a guess. Optional demo.
 *
 * The page draws each chart from the response only:
 *   - `visualization.type` selects the renderer.
 *   - `visualization.encoding` tells which keys of `visualization.data` go on which channel.
 *   - `citations[datum id]` lists the trials of the value that the user clicks.
 * This file has no code for a specific question. Thus the response specification is
 * sufficient to make a renderer.
 *
 * All text from the response goes into the page through textContent only.
 * The page does not make HTML from response text.
 */
"use strict";

const $ = (id) => document.getElementById(id);
const SVG_NS = "http://www.w3.org/2000/svg";
const state = { request: null, response: null };
let current = { id: null, label: null };  // The data point whose trials are in the list.

const EXAMPLES = [
  { what: "See a trend over time", ask: "How has the number of trials for semaglutide changed per year since 2015?", kind: "Line chart" },
  { what: "Compare two treatments", ask: "Compare phases for trials involving nivolumab vs ipilimumab", kind: "Side-by-side bars" },
  { what: "Find where trials happen", ask: "Which countries have the most recruiting trials for melanoma?", kind: "Bar chart" },
  { what: "Break it down two ways", ask: "Break down hepatitis C trials by phase and sponsor category", kind: "Stacked bars" },
  { what: "See how big trials are", ask: "What does the distribution of enrollment sizes look like for cystic fibrosis trials?", kind: "Histogram" },
  { what: "Look for a relationship", ask: "Do bigger phase 3 Alzheimer's trials take longer?", kind: "Scatter plot" },
  { what: "Put trials on a timeline", ask: "Show a timeline of the most recent osimertinib trials", kind: "Timeline" },
  { what: "Map drugs used together", ask: "Which drugs frequently co-occur in combination studies for glioblastoma?", kind: "Network" },
  { what: "Map sponsors and their drugs", ask: "Show a network of sponsors and drugs for ovarian cancer trials", kind: "Network" },
];

/* ---- small helpers ------------------------------------------------------------------------- */

function el(tag, props = {}, parent) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === "text") node.textContent = value;
    else if (key === "class") node.className = value;
    else node.setAttribute(key, value);
  }
  if (parent) parent.appendChild(node);
  return node;
}

function svg(tag, attrs = {}, parent) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === "text") node.textContent = value;
    else node.setAttribute(key, value);
  }
  if (parent) parent.appendChild(node);
  return node;
}

const unique = (values) => [...new Set(values)];
const color = (index) => `var(--series-${(index % 8) + 1})`;
const fmt = (n) => (typeof n === "number" ? n.toLocaleString("en-US", { maximumFractionDigits: 1 }) : String(n));
const truncate = (text, max) => (String(text).length > max ? String(text).slice(0, max - 1) + "…" : String(text));

function compact(n) {
  const abs = Math.abs(n);
  if (abs >= 1e6) return +(n / 1e6).toFixed(1) + "M";
  if (abs >= 1e3) return +(n / 1e3).toFixed(1) + "K";
  return String(+n.toFixed(1));
}

/* Returns round axis values from 0 to `max` or more. */
function ticks(max, target = 5) {
  if (!(max > 0)) return [0, 1];
  const raw = max / target;
  const magnitude = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 5, 10].map((m) => m * magnitude).find((s) => s >= raw);
  const out = [];
  for (let v = 0; v < max + step * 0.999; v += step) out.push(+v.toPrecision(12));
  return out;
}

function chartWidth() {
  return Math.max(560, Math.min(1000, $("chart").clientWidth || 860));
}

/* Returns a bar that is square at the baseline and round at the data end. */
function barRight(x, y, w, h, r = 4) {
  r = Math.max(0, Math.min(r, w, h / 2));
  return `M${x},${y} h${w - r} a${r},${r} 0 0 1 ${r},${r} v${h - 2 * r} a${r},${r} 0 0 1 ${-r},${r} h${-(w - r)} z`;
}
function barUp(x, y, w, h, r = 4) {
  r = Math.max(0, Math.min(r, h, w / 2));
  return `M${x},${y + h} v${-(h - r)} a${r},${r} 0 0 1 ${r},${-r} h${w - 2 * r} a${r},${r} 0 0 1 ${r},${r} v${h - r} z`;
}

/* ---- tooltip, selection, legend ------------------------------------------------------------ */

const tip = $("tooltip");

function showTip(event, title, rows, foot = "Click to see the trials") {
  tip.replaceChildren();
  if (title) el("div", { class: "tt-title", text: title }, tip);
  for (const row of rows) {
    const line = el("div", { class: "tt-row" }, tip);
    if (row.color) el("i", { class: "tt-key" }, line).style.background = row.color;
    el("b", { text: row.value }, line);
    if (row.label) el("span", { text: row.label }, line);
  }
  if (foot) el("div", { class: "tt-foot", text: foot }, tip);
  tip.hidden = false;
  const x = Math.min(event.clientX + 14, window.innerWidth - tip.offsetWidth - 8);
  const y = Math.min(event.clientY + 14, window.innerHeight - tip.offsetHeight - 8);
  tip.style.left = `${Math.max(8, x)}px`;
  tip.style.top = `${Math.max(8, y)}px`;
}
const hideTip = () => { tip.hidden = true; };

/* Makes a mark react to the pointer, the keyboard, and a click. A click shows the trials of the mark. */
function bind(node, id, title, rows) {
  node.classList.add("mark");
  node.dataset.id = id;
  node.setAttribute("tabindex", "0");
  node.addEventListener("pointermove", (event) => showTip(event, title, rows));
  node.addEventListener("pointerleave", hideTip);
  node.addEventListener("focus", () => {
    const box = node.getBoundingClientRect();
    showTip({ clientX: box.right, clientY: box.top }, title, rows);
  });
  node.addEventListener("blur", hideTip);
  node.addEventListener("click", () => select(id, title));
  node.addEventListener("keydown", (event) => { if (event.key === "Enter") select(id, title); });
}

function select(id, label, scroll = true) {
  document.querySelectorAll("#chart [data-id]").forEach((node) => node.classList.toggle("selected", node.dataset.id === id));
  renderCitations(id, label, scroll);
}

function drawLegend(items) {
  const legend = $("legend");
  legend.replaceChildren();
  for (const item of items) {
    const entry = el("span", {}, legend);
    el("i", { class: item.shape || "" }, entry).style.background = item.color;
    el("span", { text: item.label }, entry);
  }
}

function axisTitle(parent, text, x, y, anchor = "middle", rotate = false) {
  const node = svg("text", { x, y, "text-anchor": anchor, class: "muted", text }, parent);
  if (rotate) node.setAttribute("transform", `rotate(-90 ${x} ${y})`);
  return node;
}
const withUnit = (channel) =>
  channel.unit && channel.unit.toLowerCase() !== channel.title.toLowerCase() ? `${channel.title} (${channel.unit})` : channel.title;

/* ---- bar, grouped bar, stacked bar (horizontal, so long names stay readable) ---------------- */

function renderBars(viz, root) {
  const enc = viz.encoding;
  const xf = enc.x.field, yf = enc.y.field;
  const groupField = enc.series ? enc.series.field : enc.stack ? enc.stack.field : null;
  const stacked = Boolean(enc.stack);
  const cats = unique(viz.data.map((row) => row[xf]));
  const groups = groupField ? unique(viz.data.map((row) => row[groupField])) : [null];
  if (groupField) drawLegend(groups.map((g, i) => ({ label: String(g), color: color(i) })));

  const W = chartWidth();
  const labelW = Math.min(250, Math.max(60, ...cats.map((c) => String(c).length * 6.6 + 14)));
  const barH = groupField && !stacked ? 12 : 18;
  const rowH = groupField && !stacked ? groups.length * (barH + 2) + 14 : 32;
  const left = labelW, right = 70, top = 6, bottom = 40;
  const plotW = W - left - right;
  const totals = cats.map((c) => viz.data.filter((r) => r[xf] === c).reduce((sum, r) => sum + r[yf], 0));
  const maxV = stacked ? Math.max(0, ...totals) : Math.max(0, ...viz.data.map((r) => r[yf]));
  const tk = ticks(maxV);
  const scale = (v) => (v / tk[tk.length - 1]) * plotW;
  const H = top + cats.length * rowH + bottom;
  const g = svg("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: "img", "aria-label": viz.title }, root);

  for (const t of tk) {
    const x = left + scale(t);
    svg("line", { x1: x, x2: x, y1: top, y2: H - bottom, class: t === 0 ? "axis" : "grid" }, g);
    svg("text", { x, y: H - bottom + 16, "text-anchor": "middle", class: "muted", text: compact(t) }, g);
  }
  axisTitle(g, withUnit(enc.y), left + plotW / 2, H - 6);

  cats.forEach((cat, i) => {
    const y0 = top + i * rowH;
    const label = svg("text", { x: left - 8, y: y0 + rowH / 2 + 4, "text-anchor": "end", text: truncate(cat, 36) }, g);
    svg("title", { text: String(cat) }, label);
    const rows = viz.data.filter((r) => r[xf] === cat);
    if (stacked) {
      let offset = 0;
      rows.forEach((row) => {
        const w = scale(row[yf]);
        if (w > 0) {
          const j = groups.indexOf(row[groupField]);
          const last = offset + w >= scale(totals[i]) - 0.01;
          const shape = last
            ? svg("path", { d: barRight(left + offset, y0 + 7, Math.max(1, w - 2), barH), fill: color(j) }, g)
            : svg("rect", { x: left + offset, y: y0 + 7, width: Math.max(1, w - 2), height: barH, fill: color(j) }, g);
          bind(shape, row.id, `${cat} · ${row[groupField]}`, [{ value: fmt(row[yf]), label: enc.y.unit || "", color: color(j) }]);
        }
        offset += w;
      });
      svg("text", { x: left + offset + 6, y: y0 + rowH / 2 + 4, class: "value", text: fmt(totals[i]) }, g);
    } else {
      rows.forEach((row) => {
        const j = groupField ? groups.indexOf(row[groupField]) : 0;
        const y = groupField ? y0 + 7 + j * (barH + 2) : y0 + (rowH - barH) / 2;
        const w = scale(row[yf]);
        const shape = svg("path", { d: barRight(left, y, Math.max(w, row[yf] ? 1.5 : 0), barH), fill: color(j) }, g);
        const title = groupField ? `${cat} · ${row[groupField]}` : String(cat);
        bind(shape, row.id, title, [{ value: fmt(row[yf]), label: enc.y.unit || "", color: color(j) }]);
        svg("text", { x: left + w + 6, y: y + barH / 2 + 4, class: "value", text: fmt(row[yf]) }, g);
      });
    }
  });
}

/* ---- time series ---------------------------------------------------------------------------- */

function renderTimeSeries(viz, root) {
  const enc = viz.encoding;
  const xf = enc.x.field, yf = enc.y.field, sf = enc.series ? enc.series.field : null;
  const names = sf ? unique(viz.data.map((r) => r[sf])) : [null];
  if (sf) drawLegend(names.map((n, i) => ({ label: String(n), color: color(i), shape: "line" })));
  const years = unique(viz.data.map((r) => r[xf])).sort((a, b) => a - b);
  const W = chartWidth(), H = 340;
  const m = { l: 56, r: sf && names.length <= 4 ? 120 : 24, t: 14, b: 44 };
  const plotW = W - m.l - m.r, plotH = H - m.t - m.b;
  const tk = ticks(Math.max(0, ...viz.data.map((r) => r[yf])));
  const span = Math.max(1, years[years.length - 1] - years[0]);
  const x = (year) => m.l + (years.length === 1 ? plotW / 2 : ((year - years[0]) / span) * plotW);
  const y = (v) => m.t + plotH - (v / tk[tk.length - 1]) * plotH;
  const g = svg("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: "img", "aria-label": viz.title }, root);

  for (const t of tk) {
    svg("line", { x1: m.l, x2: m.l + plotW, y1: y(t), y2: y(t), class: t === 0 ? "axis" : "grid" }, g);
    svg("text", { x: m.l - 8, y: y(t) + 4, "text-anchor": "end", class: "muted", text: compact(t) }, g);
  }
  const every = Math.ceil(years.length / 14);
  years.forEach((year, i) => {
    if (i % every === 0) svg("text", { x: x(year), y: H - m.b + 18, "text-anchor": "middle", class: "muted", text: String(year) }, g);
  });
  axisTitle(g, enc.x.title, m.l + plotW / 2, H - 6);
  axisTitle(g, withUnit(enc.y), 14, m.t + plotH / 2, "middle", true);

  const cross = svg("line", { y1: m.t, y2: m.t + plotH, class: "crosshair", visibility: "hidden" }, g);
  const readout = (year) => names.map((n, i) => {
    const row = viz.data.find((r) => r[xf] === year && (!sf || r[sf] === n));
    return { value: row ? fmt(row[yf]) : "–", label: sf ? String(n) : enc.y.unit || "", color: color(i) };
  });
  const hit = svg("rect", { x: m.l, y: m.t, width: plotW, height: plotH, fill: "transparent" }, g);
  hit.addEventListener("pointermove", (event) => {
    const box = g.getBoundingClientRect();
    const px = ((event.clientX - box.left) / box.width) * W;
    const year = years.reduce((best, c) => (Math.abs(x(c) - px) < Math.abs(x(best) - px) ? c : best), years[0]);
    cross.setAttribute("x1", x(year)); cross.setAttribute("x2", x(year)); cross.setAttribute("visibility", "visible");
    showTip(event, String(year), readout(year), "Click a dot to see the trials");
  });
  hit.addEventListener("pointerleave", () => { cross.setAttribute("visibility", "hidden"); hideTip(); });

  names.forEach((name, i) => {
    const rows = viz.data.filter((r) => !sf || r[sf] === name).sort((a, b) => a[xf] - b[xf]);
    const d = rows.map((r, k) => `${k ? "L" : "M"}${x(r[xf])},${y(r[yf])}`).join(" ");
    svg("path", { d, fill: "none", stroke: color(i), "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round", "pointer-events": "none" }, g);
    rows.forEach((r) => {
      const dot = svg("circle", { cx: x(r[xf]), cy: y(r[yf]), r: 4, fill: color(i), class: "ring" }, g);
      bind(dot, r.id, sf ? `${r[xf]} · ${name}` : String(r[xf]), readout(r[xf]));
    });
    const last = rows[rows.length - 1];
    if (sf && names.length <= 4 && last) svg("text", { x: x(last[xf]) + 10, y: y(last[yf]) + 4, text: truncate(name, 16) }, g);
  });
}

/* ---- histogram ------------------------------------------------------------------------------ */

function renderHistogram(viz, root, options = {}) {
  const enc = viz.encoding, rows = viz.data;
  const log = Boolean(options.log);
  // One very tall bar makes all other bars flat. A log count axis makes them visible again.
  const controls = el("label", { class: "chart-controls" }, root);
  const box = el("input", { type: "checkbox" }, controls);
  box.checked = log;
  controls.appendChild(document.createTextNode(" Make the small bars visible (logarithmic count axis)"));
  box.addEventListener("change", () => { root.replaceChildren(); renderHistogram(viz, root, { log: box.checked }); });

  const W = chartWidth(), H = 320, m = { l: 56, r: 16, t: 14, b: 52 };
  const plotW = W - m.l - m.r, plotH = H - m.t - m.b;
  const band = plotW / Math.max(1, rows.length);
  const max = Math.max(0, ...rows.map((r) => r.trial_count));
  const top = Math.ceil(Math.log10(max + 1)) || 1;
  const tk = log ? [0, ...Array.from({ length: top }, (_, k) => 10 ** (k + 1))] : ticks(max);
  const frac = (v) => (log ? Math.log10(v + 1) / top : v / tk[tk.length - 1]);
  const y = (v) => m.t + plotH - frac(v) * plotH;
  const g = svg("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: "img", "aria-label": viz.title }, root);
  for (const t of tk) {
    svg("line", { x1: m.l, x2: m.l + plotW, y1: y(t), y2: y(t), class: t === 0 ? "axis" : "grid" }, g);
    svg("text", { x: m.l - 8, y: y(t) + 4, "text-anchor": "end", class: "muted", text: compact(t) }, g);
  }
  const every = Math.ceil(rows.length / 10);
  const unit = enc.x.unit ? ` ${enc.x.unit}` : "";
  rows.forEach((row, i) => {
    const x = m.l + i * band + 1;
    const h = m.t + plotH - y(row.trial_count);
    const range = row.is_overflow ? `${fmt(row.bin_start)}${unit} or more` : `${fmt(row.bin_start)}–${fmt(row.bin_end)}${unit}`;
    const shape = svg("path", { d: barUp(x, y(row.trial_count), Math.max(1, band - 2), Math.max(h, row.trial_count ? 1.5 : 0)), fill: color(0) }, g);
    bind(shape, row.id, range, [{ value: fmt(row.trial_count), label: enc.y.unit || "" }]);
    if (row.id === current.id) shape.classList.add("selected");
    if (row.is_overflow) svg("text", { x: x + band / 2, y: H - m.b + 16, "text-anchor": "middle", class: "muted", text: `${compact(row.bin_start)}+` }, g);
    else if (i % every === 0) svg("text", { x, y: H - m.b + 16, "text-anchor": "middle", class: "muted", text: compact(row.bin_start) }, g);
  });
  axisTitle(g, withUnit(enc.x), m.l + plotW / 2, H - 8);
  axisTitle(g, withUnit(enc.y), 14, m.t + plotH / 2, "middle", true);
}

/* ---- heatmap -------------------------------------------------------------------------------- */

function renderHeatmap(viz, root) {
  const enc = viz.encoding;
  const xf = enc.x.field, yf = enc.y.field, cf = enc.color.field;
  const xs = unique(viz.data.map((r) => r[xf])), ys = unique(viz.data.map((r) => r[yf]));
  const W = chartWidth();
  const left = Math.min(230, Math.max(60, ...ys.map((v) => String(v).length * 6.6 + 14)));
  const top = 8, bottom = 110, cellH = 26;
  const cellW = (W - left - 16) / xs.length;
  const H = top + ys.length * cellH + bottom;
  const maxV = Math.max(1, ...viz.data.map((r) => r[cf]));
  const step = (v) => (v === 0 ? 0 : Math.min(6, 1 + Math.floor((v / maxV) * 5.999)));
  const g = svg("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: "img", "aria-label": viz.title }, root);
  ys.forEach((yv, j) => {
    const label = svg("text", { x: left - 8, y: top + j * cellH + cellH / 2 + 4, "text-anchor": "end", text: truncate(yv, 32) }, g);
    svg("title", { text: String(yv) }, label);
  });
  xs.forEach((xv, i) => {
    const cx = left + i * cellW + cellW / 2, cy = top + ys.length * cellH + 14;
    svg("text", { x: cx, y: cy, "text-anchor": "end", transform: `rotate(-40 ${cx} ${cy})`, text: truncate(xv, 22) }, g);
  });
  for (const row of viz.data) {
    const i = xs.indexOf(row[xf]), j = ys.indexOf(row[yf]), s = step(row[cf]);
    const cell = svg("rect", { x: left + i * cellW + 1, y: top + j * cellH + 1, width: Math.max(1, cellW - 2), height: cellH - 2, rx: 2, fill: `var(--seq-${s})` }, g);
    bind(cell, row.id, `${row[xf]} · ${row[yf]}`, [{ value: fmt(row[cf]), label: enc.color.unit || "" }]);
    if (cellW > 34 && row[cf] > 0) {
      svg("text", { x: left + i * cellW + cellW / 2, y: top + j * cellH + cellH / 2 + 4, "text-anchor": "middle", "pointer-events": "none",
        style: `fill: ${s >= 4 ? "#fff" : "var(--text-primary)"}`, text: compact(row[cf]) }, g);
    }
  }
  drawLegend([{ label: "0", color: "var(--seq-0)" }, { label: "fewer", color: "var(--seq-2)" }, { label: `more (up to ${fmt(maxV)} ${enc.color.unit || ""})`, color: "var(--seq-6)" }]);
}

/* ---- zooming --------------------------------------------------------------------------------
 * Dense charts (scatter, network) can be zoomed and dragged. The zoom is "semantic": zooming in
 * spreads the marks apart while dots, lines and text keep their size, so crowded areas actually
 * become readable (enlarging the picture would enlarge the overlaps with it).
 *
 * A view is the visible window of the full chart, in fractions: {u0, u1} across, {v0, v1} down.
 */

const fullView = () => ({ u0: 0, u1: 1, v0: 0, v1: 1 });
const isZoomed = (view) => view.u1 - view.u0 < 0.999;

function zoomAt(view, cu, cv, factor) {
  const w = Math.min(1, Math.max(0.01, (view.u1 - view.u0) * factor));
  const h = Math.min(1, Math.max(0.01, (view.v1 - view.v0) * factor));
  const ru = (cu - view.u0) / (view.u1 - view.u0), rv = (cv - view.v0) / (view.v1 - view.v0);
  view.u0 = cu - ru * w; view.u1 = view.u0 + w;
  view.v0 = cv - rv * h; view.v1 = view.v0 + h;
  clampView(view);
}

function clampView(view) {
  const w = view.u1 - view.u0, h = view.v1 - view.v0;
  view.u0 = Math.min(1 - w, Math.max(0, view.u0)); view.u1 = view.u0 + w;
  view.v0 = Math.min(1 - h, Math.max(0, view.v0)); view.v1 = view.v0 + h;
}

/* Adds zoom buttons above `node`. Makes `node` react to pinch or Ctrl + scroll, double-click,
 * and drag. `box` is the plot area in the SVG ({left, top, width, height, W} in SVG units).
 * A plain scroll is not changed. Thus the page scrolls when the pointer is on a chart. */
function attachZoom(node, box, view, redraw) {
  const bar = el("div", { class: "zoombar" });
  node.before(bar);
  const button = (text, label, action) => {
    const b = el("button", { type: "button", class: "ghost", "aria-label": label, title: label, text }, bar);
    b.addEventListener("click", () => { action(); redraw(); });
  };
  const centre = () => [(view.u0 + view.u1) / 2, (view.v0 + view.v1) / 2];
  button("+", "Zoom in", () => zoomAt(view, ...centre(), 0.6));
  button("−", "Zoom out", () => zoomAt(view, ...centre(), 1 / 0.6));
  button("Reset view", "Show the whole chart", () => Object.assign(view, fullView()));
  el("span", { class: "hint", text: "Zoom with the buttons, a double-click, a pinch or Ctrl + scroll · drag to move around" }, bar);
  node.classList.add("zoomable");

  const at = (event) => {
    const rect = node.getBoundingClientRect();
    const px = ((event.clientX - rect.left) / rect.width) * box.W;
    const py = ((event.clientY - rect.top) / rect.height) * (box.W * rect.height / rect.width);
    return [
      view.u0 + ((px - box.left) / box.width) * (view.u1 - view.u0),
      view.v0 + ((py - box.top) / box.height) * (view.v1 - view.v0),
    ];
  };
  node.addEventListener("wheel", (event) => {
    if (!event.ctrlKey) return;  // A trackpad pinch arrives as Ctrl + wheel.
    event.preventDefault();
    zoomAt(view, ...at(event), event.deltaY < 0 ? 0.8 : 1.25);
    redraw();
  }, { passive: false });
  node.addEventListener("dblclick", (event) => { zoomAt(view, ...at(event), 0.5); redraw(); });

  let start = null, dragged = false;
  node.addEventListener("pointerdown", (event) => { start = { x: event.clientX, y: event.clientY, ...view }; dragged = false; });
  node.addEventListener("pointermove", (event) => {
    if (!start || !(event.buttons & 1) || !isZoomed(view)) return;
    const dx = event.clientX - start.x, dy = event.clientY - start.y;
    if (!dragged && Math.hypot(dx, dy) < 4) return;
    if (!dragged) { dragged = true; node.setPointerCapture(event.pointerId); hideTip(); }
    const rect = node.getBoundingClientRect(), w = start.u1 - start.u0, h = start.v1 - start.v0;
    view.u0 = start.u0 - (dx / (rect.width * (box.width / box.W))) * w; view.u1 = view.u0 + w;
    view.v0 = start.v0 - (dy / (rect.height * (box.height / (box.W * rect.height / rect.width)))) * h; view.v1 = view.v0 + h;
    clampView(view);
    redraw();
  });
  const end = () => { start = null; };
  node.addEventListener("pointerup", end);
  node.addEventListener("pointercancel", end);
  // A drag must not also be a click on the item below the pointer.
  node.addEventListener("click", (event) => { if (dragged) { event.stopPropagation(); dragged = false; } }, true);
}

function niceStep(raw) {
  if (!(raw > 0)) return 1;
  const magnitude = 10 ** Math.floor(Math.log10(raw));
  return [1, 2, 5, 10].map((m) => m * magnitude).find((step) => step >= raw);
}

/* Returns the axis values to label for the visible part [f0, f1] of a scale (fractions of its full length). */
function axisTicks(scale, f0, f1) {
  const a = scale.value(Math.max(0, f0)), b = scale.value(Math.min(1, f1));
  if (scale.log) {
    const all = a <= 0 ? [{ v: 0, power: true }] : [];
    for (let p = 0; p <= 12; p++) for (const m of [1, 2, 5]) {
      const v = m * 10 ** p;
      if (v >= a && v <= b) all.push({ v, power: m === 1 });
    }
    const chosen = all.length > 8 ? all.filter((t) => t.power) : all;
    if (chosen.length >= 2) return chosen.map((t) => t.v);
  }
  const step = niceStep((b - a) / 6), out = [];
  for (let v = Math.ceil(a / step) * step; v <= b + step * 1e-9; v += step) out.push(+v.toPrecision(12));
  return out;
}

/* ---- scatter -------------------------------------------------------------------------------- */

function renderScatter(viz, root, options = {}) {
  const enc = viz.encoding;
  const xf = enc.x.field, yf = enc.y.field, sf = enc.series ? enc.series.field : null;
  const rows = viz.data;
  const names = sf ? unique(rows.map((r) => r[sf])) : [null];
  if (sf) drawLegend(names.map((n, i) => ({ label: String(n), color: color(i), shape: "dot" })));

  // Trial sizes have a very long tail. A log scale keeps most of the dots apart.
  const skewed = (field) => {
    const values = rows.map((r) => r[field]).sort((a, b) => a - b);
    return values.length > 5 && values[values.length - 1] > 20 * Math.max(1, values[Math.floor(values.length / 2)]);
  };
  const log = options.log === undefined ? skewed(xf) || skewed(yf) : options.log;
  const controls = el("label", { class: "chart-controls" }, root);
  const box = el("input", { type: "checkbox" }, controls);
  box.checked = log;
  controls.appendChild(document.createTextNode(" Spread out the small values (logarithmic axes)"));
  box.addEventListener("change", () => { root.replaceChildren(); renderScatter(viz, root, { log: box.checked }); });

  const W = chartWidth(), H = 440, m = { l: 64, r: 20, t: 12, b: 48 };
  const plotW = W - m.l - m.r, plotH = H - m.t - m.b;
  const scaleFor = (field) => {
    const max = Math.max(1, ...rows.map((r) => r[field]));
    if (!log) {
      const top = ticks(max).pop();
      return { log: false, frac: (v) => v / top, value: (f) => f * top };
    }
    const top = Math.ceil(Math.log10(max + 1));
    return { log: true, frac: (v) => Math.log10(v + 1) / top, value: (f) => 10 ** (f * top) - 1 };
  };
  const sx = scaleFor(xf), sy = scaleFor(yf);
  const view = fullView();
  const X = (cu) => m.l + ((cu - view.u0) / (view.u1 - view.u0)) * plotW;
  const Y = (cv) => m.t + ((cv - view.v0) / (view.v1 - view.v0)) * plotH;
  const g = svg("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: "img", "aria-label": viz.title }, root);
  const status = el("p", { class: "hint" }, root);

  function draw() {
    g.replaceChildren();
    for (const t of axisTicks(sy, 1 - view.v1, 1 - view.v0)) {
      const y = Y(1 - sy.frac(t));
      svg("line", { x1: m.l, x2: m.l + plotW, y1: y, y2: y, class: "grid" }, g);
      svg("text", { x: m.l - 8, y: y + 4, "text-anchor": "end", class: "muted", text: compact(t) }, g);
    }
    for (const t of axisTicks(sx, view.u0, view.u1)) {
      const x = X(sx.frac(t));
      svg("line", { x1: x, x2: x, y1: m.t, y2: m.t + plotH, class: "grid" }, g);
      svg("text", { x, y: H - m.b + 16, "text-anchor": "middle", class: "muted", text: compact(t) }, g);
    }
    svg("line", { x1: m.l, x2: m.l, y1: m.t, y2: m.t + plotH, class: "axis" }, g);
    svg("line", { x1: m.l, x2: m.l + plotW, y1: m.t + plotH, y2: m.t + plotH, class: "axis" }, g);
    axisTitle(g, withUnit(enc.x), m.l + plotW / 2, H - 8);
    axisTitle(g, withUnit(enc.y), 16, m.t + plotH / 2, "middle", true);
    let visible = 0;
    for (const row of rows) {
      const cu = sx.frac(row[xf]), cv = 1 - sy.frac(row[yf]);
      if (cu < view.u0 || cu > view.u1 || cv < view.v0 || cv > view.v1) continue;
      visible += 1;
      const i = sf ? names.indexOf(row[sf]) : 0;
      const dot = svg("g", {}, g);
      svg("circle", { cx: X(cu), cy: Y(cv), r: 12, fill: "transparent" }, dot);  // The click area is larger than the dot.
      svg("circle", { cx: X(cu), cy: Y(cv), r: 4, fill: color(i), "fill-opacity": 0.8, class: "ring" }, dot);
      bind(dot, row.id, row.nct_id + (sf ? ` · ${row[sf]}` : ""), [
        { value: fmt(row[xf]), label: withUnit(enc.x) },
        { value: fmt(row[yf]), label: withUnit(enc.y) },
      ]);
      if (row.id === current.id) dot.classList.add("selected");
    }
    status.textContent = isZoomed(view) ? `Zoomed in: ${fmt(visible)} of ${fmt(rows.length)} trials are in view.` : "";
  }
  attachZoom(g, { left: m.l, top: m.t, width: plotW, height: plotH, W }, view, draw);
  draw();
}

/* ---- gantt (one bar per trial) -------------------------------------------------------------- */

function renderGantt(viz, root) {
  const enc = viz.encoding, rows = viz.data;
  const time = (text) => Date.parse(text.length === 7 ? `${text}-01` : text);
  const W = chartWidth(), left = 300, right = 20, top = 8, bottom = 34, rowH = 26;
  const plotW = W - left - right;
  const H = top + rows.length * rowH + bottom;
  const lo = Math.min(...rows.map((r) => time(r.start))), hi = Math.max(...rows.map((r) => time(r.end)));
  const x = (t) => left + (hi === lo ? 0 : ((t - lo) / (hi - lo)) * plotW);
  const g = svg("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: "img", "aria-label": viz.title }, root);
  const firstYear = new Date(lo).getUTCFullYear(), lastYear = new Date(hi).getUTCFullYear();
  const every = Math.max(1, Math.ceil((lastYear - firstYear + 1) / 10));
  for (let year = firstYear; year <= lastYear + 1; year += every) {
    const t = Date.UTC(year, 0, 1);
    if (t < lo || t > hi) continue;
    svg("line", { x1: x(t), x2: x(t), y1: top, y2: H - bottom, class: "grid" }, g);
    svg("text", { x: x(t), y: H - bottom + 16, "text-anchor": "middle", class: "muted", text: String(year) }, g);
  }
  rows.forEach((row, i) => {
    const yMid = top + i * rowH + rowH / 2;
    const label = svg("text", { x: left - 10, y: yMid + 4, "text-anchor": "end", text: truncate(row.label, 44) }, g);
    svg("title", { text: row.label }, label);
    const x0 = x(time(row.start)), x1 = x(time(row.end));
    const bar = svg("rect", { x: x0, y: yMid - 6, width: Math.max(3, x1 - x0), height: 12, rx: 4, fill: color(0) }, g);
    bind(bar, row.id, row.label, [{ value: `${row.start} → ${row.end}`, label: row.nct_id }]);
  });
  axisTitle(g, `${enc.start.title} to ${enc.end.title.toLowerCase()}`, left + plotW / 2, H - 4);
}

/* ---- network graph -------------------------------------------------------------------------- */

function layout(nodes, edges, W, H) {
  // A small force layout: nodes push apart, linked nodes pull together, and all nodes are pulled
  // to the center. The start circle is fixed, so the same answer always draws the same graph.
  const n = nodes.length, k = Math.sqrt((W * H) / Math.max(1, n));
  const index = new Map(nodes.map((node, i) => [node.id, i]));
  nodes.forEach((node, i) => {
    node.x = W / 2 + Math.cos((2 * Math.PI * i) / n) * W * 0.35;
    node.y = H / 2 + Math.sin((2 * Math.PI * i) / n) * H * 0.35;
  });
  const maxW = Math.max(1, ...edges.map((e) => e.shared_trials));
  for (let step = 0; step < 320; step++) {
    const heat = (1 - step / 320) * W * 0.08;
    const dx = new Float64Array(n), dy = new Float64Array(n);
    for (let a = 0; a < n; a++) {
      for (let b = a + 1; b < n; b++) {
        let ux = nodes[a].x - nodes[b].x, uy = nodes[a].y - nodes[b].y;
        const d = Math.max(1, Math.hypot(ux, uy)), f = (k * k) / d;
        ux /= d; uy /= d;
        dx[a] += ux * f; dy[a] += uy * f; dx[b] -= ux * f; dy[b] -= uy * f;
      }
    }
    for (const edge of edges) {
      const a = index.get(edge.source), b = index.get(edge.target);
      let ux = nodes[a].x - nodes[b].x, uy = nodes[a].y - nodes[b].y;
      // A weak spring: strong links pull a little more, but not sufficiently to collapse a hub.
      const d = Math.max(1, Math.hypot(ux, uy)), f = ((d * d) / k) * (0.12 + 0.1 * (edge.shared_trials / maxW));
      ux /= d; uy /= d;
      dx[a] -= ux * f; dy[a] -= uy * f; dx[b] += ux * f; dy[b] += uy * f;
    }
    nodes.forEach((node, i) => {
      // Gravity keeps small separate groups near the main group, and not at the edge of the frame.
      dx[i] += (W / 2 - node.x) * 0.3; dy[i] += (H / 2 - node.y) * 0.3;
      const d = Math.max(1, Math.hypot(dx[i], dy[i]));
      node.x += (dx[i] / d) * Math.min(d, heat);
      node.y += (dy[i] / d) * Math.min(d, heat);
    });
  }
  // Scale the result to fill the frame.
  const pad = 70;
  const minX = Math.min(...nodes.map((v) => v.x)), maxX = Math.max(...nodes.map((v) => v.x));
  const minY = Math.min(...nodes.map((v) => v.y)), maxY = Math.max(...nodes.map((v) => v.y));
  nodes.forEach((node) => {
    node.x = pad + ((node.x - minX) / Math.max(1, maxX - minX)) * (W - 2 * pad - 60);
    node.y = pad / 2 + ((node.y - minY) / Math.max(1, maxY - minY)) * (H - pad);
  });
}

function renderNetwork(viz, root) {
  const nodes = viz.data.nodes.map((node) => ({ ...node })), edges = viz.data.edges;
  if (!nodes.length) { el("p", { class: "sub", text: "No connections to draw." }, root); return; }
  const enc = viz.encoding;
  const kinds = unique(nodes.map((node) => node[enc.nodes.color.field]));
  if (kinds.length > 1) drawLegend(kinds.map((kind, i) => ({ label: String(kind), color: color(i), shape: "dot" })));
  const W = chartWidth(), H = 620;
  layout(nodes, edges, W, H);
  const byId = new Map(nodes.map((node) => [node.id, node]));
  const sizeField = enc.nodes.size.field, weightField = enc.edges.weight.field;
  const maxSize = Math.max(1, ...nodes.map((node) => node[sizeField]));
  const maxWeight = Math.max(1, ...edges.map((edge) => edge[weightField]));
  const radius = (node) => 4 + 13 * Math.sqrt(node[sizeField] / maxSize);
  const neighbours = new Map(nodes.map((node) => [node.id, new Set([node.id])]));
  for (const edge of edges) {
    neighbours.get(edge[enc.edges.source]).add(edge[enc.edges.target]);
    neighbours.get(edge[enc.edges.target]).add(edge[enc.edges.source]);
  }
  const biggest = [...nodes].sort((a, b) => b[sizeField] - a[sizeField]).map((node) => node.id);
  const view = fullView();
  const X = (x) => ((x / W - view.u0) / (view.u1 - view.u0)) * W;
  const Y = (y) => ((y / H - view.v0) / (view.v1 - view.v0)) * H;
  const g = svg("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: "img", "aria-label": viz.title }, root);

  function draw() {
    g.replaceChildren();
    const zoom = 1 / (view.u1 - view.u0);
    // When the graph is zoomed, all names fit. Before that, only the largest nodes have names.
    const named = new Set(nodes.length <= 45 || zoom >= 1.6 ? biggest : biggest.slice(0, 30));
    const marks = [];
    for (const edge of edges) {
      const a = byId.get(edge[enc.edges.source]), b = byId.get(edge[enc.edges.target]);
      const group = svg("g", {}, g);
      const ends = { x1: X(a.x), y1: Y(a.y), x2: X(b.x), y2: Y(b.y) };
      svg("line", { ...ends, stroke: "transparent", "stroke-width": 12 }, group);  // The click area.
      svg("line", { ...ends, class: "edge", "stroke-width": 1 + 5 * (edge[weightField] / maxWeight), "stroke-opacity": 0.75 }, group);
      bind(group, edge.id, `${a.label} — ${b.label}`, [{ value: fmt(edge[weightField]), label: enc.edges.weight.title.toLowerCase() }]);
      if (edge.id === current.id) group.classList.add("selected");
      marks.push({ node: group, ends: [a.id, b.id] });
    }
    for (const node of nodes) {
      const group = svg("g", {}, g);
      svg("circle", { cx: X(node.x), cy: Y(node.y), r: radius(node), fill: color(kinds.indexOf(node[enc.nodes.color.field])), class: "ring" }, group);
      if (named.has(node.id)) {
        svg("text", { x: X(node.x) + radius(node) + 4, y: Y(node.y) + 4, "pointer-events": "none", text: truncate(node[enc.nodes.label.field], zoom >= 1.6 ? 40 : 26) }, group);
      }
      bind(group, node.id, node[enc.nodes.label.field], [{ value: fmt(node[sizeField]), label: enc.nodes.size.unit || "" }]);
      if (node.id === current.id) group.classList.add("selected");
      // When the pointer is on a node, the node, its neighbors, and its lines stay. All other marks fade.
      group.addEventListener("pointerenter", () => marks.forEach((mark) => {
        const related = mark.ends ? mark.ends.includes(node.id) : neighbours.get(node.id).has(mark.id);
        mark.node.classList.toggle("dim", !related);
      }));
      group.addEventListener("pointerleave", () => marks.forEach((mark) => mark.node.classList.remove("dim")));
      marks.push({ node: group, id: node.id });
    }
  }
  attachZoom(g, { left: 0, top: 0, width: W, height: H, W }, view, draw);
  draw();
}

const RENDERERS = {
  bar_chart: renderBars, grouped_bar_chart: renderBars, stacked_bar_chart: renderBars,
  time_series: renderTimeSeries, histogram: renderHistogram, heatmap: renderHeatmap,
  scatter_plot: renderScatter, gantt: renderGantt, network_graph: renderNetwork,
};

/* ---- the answer around the chart ------------------------------------------------------------ */

/* The position of a quoted value in the registry record of a trial, in plain words. */
const RECORD_FIELDS = [
  [/designModule\.phases/, "Phase"], [/overallStatus/, "Status"], [/locations\[\d+\]\.country/, "Country"],
  [/leadSponsor\.name/, "Lead sponsor"], [/leadSponsor\.class/, "Sponsor category"],
  [/interventions\[\d+\]\.name/, "Intervention"], [/interventions\[\d+\]\.type/, "Intervention type"],
  [/conditions\[\d+\]/, "Condition"], [/enrollmentInfo\.count/, "Enrollment"],
  [/startDateStruct\.date/, "Start date"], [/completionDateStruct\.date/, "Completion date"],
  [/lastUpdatePostDateStruct/, "Last updated"], [/hasResults/, "Results posted"],
];
const recordField = (path) => (RECORD_FIELDS.find(([pattern]) => pattern.test(path)) || [null, path])[1];
const recordValue = (value) => (Array.isArray(value) ? value.join(", ") : typeof value === "boolean" ? (value ? "yes" : "no") : String(value));
const prettyCode = (code) => String(code).replace(/PHASE(\d)/g, "PHASE $1").replace(/_/g, " ").toLowerCase().replace(/^./, (c) => c.toUpperCase());
/* Changes registry codes ("NOT_YET_RECRUITING", "PHASE2") into plain words. Names and short
   abbreviations ("NIH") do not change. The code itself is shown with the record path. */
const isCode = (value) => typeof value === "string" && /^[A-Z][A-Z0-9_]{3,}$/.test(value);
const plainValue = (value) => (Array.isArray(value) ? value.map(plainValue).join(", ") : isCode(value) ? prettyCode(value) : recordValue(value));
const FILTER_NAMES = { condition: "Condition", drug: "Drug", sponsor: "Sponsor", country: "Country", status: "Status", phase: "Phase", start_year_min: "Started from", start_year_max: "Started up to" };
const trials = (n) => `${fmt(n)} ${n === 1 ? "trial" : "trials"}`;

let showPaths = false;

function renderCitations(id, label, scroll = true) {
  current = { id, label };
  const body = $("citation-body");
  body.replaceChildren();
  $("citation-hint").hidden = true;
  const set = state.response.citations[id];
  if (!set) { el("p", { class: "sub", text: "No trials are recorded for this part of the chart." }, body); return; }

  el("div", { class: "cite-what", text: (set.items[0] && set.items[0].bucket_label) || label || "" }, body);
  const count = el("p", { class: "cite-count" }, body);
  el("b", { text: fmt(set.total) }, count);
  count.appendChild(document.createTextNode(set.total === 1 ? " trial" : " trials"));
  if (set.total === 0) { el("p", { class: "sub", text: "Nothing to list here." }, body); return; }

  const link = el("a", { class: "button-link", href: set.search_url, target: "_blank", rel: "noopener",
    text: set.total === 1 ? "Open on ClinicalTrials.gov ↗" : "See them all on ClinicalTrials.gov ↗" }, body);
  link.title = set.search_is_exact
    ? "This search lists exactly these trials."
    : "This is the closest search the registry offers. It matches names loosely, so it can list a few more trials.";
  const small = [];
  if (set.shown < set.total) small.push(`Showing ${set.shown} of ${fmt(set.total)}${set.is_sample ? " (a sample; the total is exact)" : ""}.`);
  if (small.length) el("p", { class: "cite-small", text: small.join(" ") }, body);

  const list = el("ul", { class: "cite-list" }, body);
  for (const item of set.items) {
    const entry = el("li", {}, list);
    el("a", { class: "cite-title", href: item.url, target: "_blank", rel: "noopener", text: item.title || item.nct_id }, entry);
    if (item.title) el("div", { class: "cite-id", text: item.nct_id }, entry);
    for (const evidence of item.evidence) {
      const line = el("div", { class: "evidence" }, entry);
      line.appendChild(document.createTextNode(`${recordField(evidence.field)}: `));
      if (evidence.value === null) line.appendChild(document.createTextNode("not recorded for this trial"));
      else el("code", { text: plainValue(evidence.value) }, line);
      if (showPaths) el("span", { class: "path", text: `${evidence.field} = ${JSON.stringify(evidence.value)}` }, line);
    }
  }
  const toggle = el("label", { class: "toggle-line" }, body);
  const box = el("input", { type: "checkbox" }, toggle);
  box.checked = showPaths;
  toggle.appendChild(document.createTextNode("Show where each value sits in the trial's record"));
  box.addEventListener("change", () => { showPaths = box.checked; renderCitations(id, label, false); });
  if (scroll && window.matchMedia("(max-width: 980px)").matches) $("citations").scrollIntoView({ behavior: "smooth", block: "start" });
}

/* Returns the row for the first sentence: the largest row that has a real value. "Unknown"
   (trials with no recorded value) is a warning and not a result. It is used only when there
   is no other row. */
function largest(rows) {
  const known = rows.filter((row) => !Object.values(row).includes("Unknown"));
  const pool = known.length ? known : rows;
  return pool.reduce((best, row) => ((row.trial_count || 0) > (best.trial_count || 0) ? row : best), pool[0]);
}

/* Writes one plain sentence about the chart. The page computes it from the data. A model does not write it. */
function headline(viz) {
  const say = (...parts) => {
    const node = $("headline");
    node.replaceChildren();
    for (const part of parts) {
      if (typeof part === "string") node.appendChild(document.createTextNode(part));
      else el("b", { text: part.b }, node);
    }
  };
  const enc = viz.encoding;
  if (viz.type === "network_graph") {
    const edge = viz.data.edges[0];
    if (!edge) return say("No connections were found among these trials.");
    const name = (id) => viz.data.nodes.find((node) => node.id === id).label;
    return say("The strongest link is between ", { b: name(edge.source) }, " and ", { b: name(edge.target) }, ", which appear together in ", { b: trials(edge.shared_trials) }, ".");
  }
  if (!viz.data.length) return say("No trials matched, so there is nothing to draw.");
  const one = viz.data.length === 1;
  if (viz.type === "scatter_plot") return say({ b: trials(viz.data.length) }, one ? " is plotted as one dot." : " are plotted, one dot each.");
  if (viz.type === "gantt") return say({ b: trials(viz.data.length) }, one ? " is shown as one bar, from start to completion." : " are shown, one bar each, from start to completion.");
  const top = largest(viz.data);
  if (top.trial_count === 0) return say("No trials matched, so every value is zero.");
  if (viz.type === "histogram") {
    const unit = enc.x.unit ? ` ${enc.x.unit}` : "";
    const range = top.is_overflow ? `${fmt(top.bin_start)}${unit} or more` : `${fmt(top.bin_start)} to ${fmt(top.bin_end)}${unit}`;
    return say("The most common range is ", { b: range }, ", with ", { b: trials(top.trial_count) }, ".");
  }
  const second = enc.series || enc.stack || (viz.type === "heatmap" ? enc.y : null);
  const where = second ? `${top[enc.x.field]} · ${top[second.field]}` : String(top[enc.x.field]);
  // A yes/no field: give the meaning of yes and no as a share.
  const answers = viz.data.map((row) => row[enc.x.field]);
  if (!second && answers.length === 2 && answers.includes("Yes") && answers.includes("No")) {
    const all = viz.data.reduce((sum, row) => sum + row.trial_count, 0);
    const yes = viz.data.find((row) => row[enc.x.field] === "Yes").trial_count;
    return say({ b: `${fmt(yes)} of ${trials(all)}` }, ` (${Math.round((100 * yes) / all)}%): ${(enc.x.title || enc.x.field).toLowerCase()}.`);
  }
  if (viz.type === "time_series") return say("The busiest year is ", { b: where }, ", with ", { b: trials(top.trial_count) }, " started.");
  return say({ b: where }, second ? " is the largest group, with " : " has the most, with ", { b: trials(top.trial_count) }, ".");
}

function tile(value, label) {
  const node = el("div", { class: "tile" }, $("tiles"));
  el("div", { class: "value", text: value }, node);
  el("div", { class: "label", text: label }, node);
}

function renderTable(viz) {
  const wrap = $("table");
  wrap.replaceChildren();
  const tables = Array.isArray(viz.data) ? [["", viz.data]] : [["What is connected", viz.data.nodes], ["Connections", viz.data.edges]];
  for (const [caption, rows] of tables) {
    if (caption) el("h4", { text: caption }, wrap);
    if (!rows.length) { el("p", { class: "sub", text: "Nothing to show." }, wrap); continue; }
    const columns = Object.keys(rows[0]).filter((column) => column !== "id");
    const table = el("table", {}, wrap);
    const head = el("tr", {}, el("thead", {}, table));
    columns.forEach((column) => el("th", { text: prettyCode(column) }, head));
    const tbody = el("tbody", {}, table);
    for (const row of rows) {
      const tr = el("tr", { title: "Click to see the trials" }, tbody);
      tr.addEventListener("click", () => select(row.id, ""));
      columns.forEach((column) => {
        const value = row[column];
        const kind = typeof value === "number" ? "num" : String(value).length > 60 ? "wrap" : "";
        el("td", { class: kind, text: typeof value === "number" ? fmt(value) : typeof value === "boolean" ? (value ? "yes" : "no") : String(value) }, tr);
      });
    }
  }
}

function renderTrace(meta) {
  const wrap = $("trace");
  wrap.replaceChildren();
  for (const step of meta.trace || []) {
    const { step: name, ms, ...details } = step;
    const row = el("div", { class: "trace-step" }, wrap);
    el("b", { text: name }, row);
    el("span", { text: `${fmt(ms)} ms ` }, row);
    if (Object.keys(details).length) el("code", { text: JSON.stringify(details) }, el("div", {}, row));
  }
}

/* Shows the terms that the service searched, as labels that the user cannot change. */
function renderSearched(meta) {
  const wrap = $("searched");
  wrap.replaceChildren();
  const groups = Object.entries(meta.filters_applied || {});
  for (const [series, filters] of groups) {
    for (const [key, value] of Object.entries(filters)) {
      const pill = el("span", { class: "pill plain" }, wrap);
      const prefix = groups.length > 1 ? `${series} — ` : "";
      pill.appendChild(document.createTextNode(`${prefix}${FILTER_NAMES[key] || key}: `));
      el("b", { text: Array.isArray(value) ? value.map(prettyCode).join(", ") : String(value) }, pill);
    }
  }
  if (!wrap.children.length) el("span", { class: "pill plain", text: "No filters: all trials in the registry" }, wrap);
}

function show(...visible) {
  for (const id of ["start", "loading", "message", "result"]) $(id).hidden = !visible.includes(id);
}

function setView(table) {
  $("chart").hidden = table; $("legend").hidden = table; $("table").hidden = !table;
  $("view-chart").classList.toggle("on", !table); $("view-table").classList.toggle("on", table);
  $("chart-tip").textContent = table ? "Click a row to see its trials" : "Hover for exact numbers · click any part to see its trials";
}

function renderProblem(response) {
  const message = $("message"), unsupported = response.status === "unsupported";
  message.replaceChildren();
  message.className = "card" + (unsupported ? "" : " error");
  el("h3", { text: unsupported ? "I can't turn that into a chart" : "Something outside this page isn't responding" }, message);
  el("p", { text: response.error.message }, message);
  const extra = [...response.error.details, ...response.meta.notes];
  if (extra.length) { const list = el("ul", {}, message); extra.forEach((text) => el("li", { text }, list)); }
  if (!unsupported) {
    el("button", { type: "button", class: "ghost", text: "Try again" }, message).addEventListener("click", () => ask());
  }
  const offers = response.error.suggestions || [];
  const narrowing = offers.some((offer) => !offer.query);
  renderOffers(message, offers, narrowing ? "One click gets you the same question for a smaller group:" : "One click gets you something close:");
  finishMessage(message, response);
}

/* Shows one-click offers. A different question contains all that it asks, so the page clears
   the filter controls for it. A narrower version keeps the question and sets one more filter. */
function renderOffers(message, offers, heading) {
  if (!offers.length) return;
  el("h4", { text: heading }, message);
  const wrap = el("div", { class: "offers" }, message);
  for (const offer of offers) {
    const text = offer.trials_matched == null ? offer.label : `${offer.label} — ${trials(offer.trials_matched)}`;
    const choice = el("button", { type: "button", class: "offer", text }, wrap);
    choice.addEventListener("click", () => {
      if (offer.query) { clearFilters(); $("query").value = offer.query; }
      applyRequestFields(offer.request || {});
      ask();
    });
  }
  el("p", { class: "hint", text: "Or change your question in the box below and ask again." }, message);
}

function finishMessage(message, response) {
  const more = el("details", { class: "dev" }, message);
  el("summary", { text: "Details for developers" }, more);
  el("pre", { text: JSON.stringify(response, null, 2) }, more);
  show("message", "start");
  window.scrollTo({ top: 0, behavior: "smooth" });
  $("query").focus({ preventScroll: true });
  $("query").select();  // New text replaces the question. The arrow keys let the user edit it.
}

/* For a clear question that found no trials: shows the cause at the top and offers the
   nearest questions that find trials. The page does not draw an empty chart. */
function renderEmpty(response) {
  const message = $("message");
  message.replaceChildren();
  message.className = "card";
  el("h3", { text: "No trials match that" }, message);
  el("p", { text: `Nothing on ClinicalTrials.gov matches “${response.visualization.title}”.` }, message);
  const extra = response.meta.notes.filter((note) => !/^No trials matched/.test(note));
  if (extra.length) { const list = el("ul", {}, message); extra.forEach((text) => el("li", { text }, list)); }
  const offers = response.meta.suggestions || [];
  const widened = offers.some((offer) => offer.trials_matched != null);
  renderOffers(message, offers, widened ? "One click drops one filter and finds trials:" : "Check the spelling, or try one of these:");
  finishMessage(message, response);
}

function renderResponse(response, httpStatus) {
  state.response = response;
  $("raw").textContent = JSON.stringify({ request: state.request, http_status: httpStatus, response }, null, 2);
  if (response.status !== "ok") return renderProblem(response);
  if (response.meta.trials_matched === 0) return renderEmpty(response);

  const viz = response.visualization, meta = response.meta;
  current = { id: null, label: null };
  $("legend").replaceChildren(); $("chart").replaceChildren(); $("tiles").replaceChildren();
  $("citation-body").replaceChildren(); $("citation-hint").hidden = false;
  $("title").textContent = viz.title;
  headline(viz);
  tile(fmt(meta.trials_matched), meta.trials_matched === 1 ? "trial matches your question" : "trials match your question");
  if (meta.trials_used !== meta.trials_matched) tile(fmt(meta.trials_used), "of them are in this chart");
  if (meta.tier === 2) tile("Exact", "totals come straight from the registry");
  if (meta.data_timestamp) tile(new Date(meta.data_timestamp).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" }), "registry last updated");

  show("result");  // Make it visible before the draw, so that the chart can measure its width.
  setView(false);
  (RENDERERS[viz.type] || ((_, root) => el("p", { text: `This page cannot draw a ${viz.type} yet.` }, root)))(viz, $("chart"));
  $("pruning").textContent = meta.pruning ? meta.pruning.message : "";

  const notes = $("notes");
  notes.replaceChildren();
  meta.notes.forEach((text) => el("li", { text }, notes));
  $("notes-card").hidden = meta.notes.length === 0;
  $("interpretation").textContent = meta.interpretation;
  renderSearched(meta);
  renderTable(viz);
  renderTrace(meta);

  // Open the panel on the largest value, to show the effect of a click.
  const rows = Array.isArray(viz.data) ? viz.data : viz.data.edges;
  if (rows.length) select(largest(rows).id, "", false);
  $("title").scrollIntoView({ behavior: "smooth", block: "start" });
}

/* ---- asking -------------------------------------------------------------------------------- */

const form = $("ask");
const pressed = (field) => [...form.querySelectorAll(`.toggles[data-field="${field}"] button[aria-pressed="true"]`)].map((b) => b.dataset.value);

function requestBody() {
  const body = { query: $("query").value.trim() };
  const names = (text) => text.split(",").map((part) => part.trim()).filter(Boolean);
  for (const field of ["drug_name", "condition"]) {
    const list = names(form.elements[field].value);
    if (list.length) body[field] = list.length === 1 ? list[0] : list;
  }
  for (const field of ["sponsor", "country", "chart_type"]) {
    const value = form.elements[field].value.trim();
    if (value) body[field] = value;
  }
  for (const field of ["start_year", "end_year"]) {
    if (form.elements[field].value !== "") body[field] = Number(form.elements[field].value);
  }
  for (const field of ["trial_phase", "status"]) {
    if (pressed(field).length) body[field] = pressed(field);
  }
  body.max_citations = Number(form.elements.max_citations.value);
  return body;
}

/* Puts request fields (with their API names) into the controls of the form. */
function applyRequestFields(fields) {
  for (const [field, value] of Object.entries(fields)) {
    const toggles = form.querySelectorAll(`.toggles[data-field="${field}"] button`);
    if (toggles.length) toggles.forEach((b) => b.setAttribute("aria-pressed", String([].concat(value).includes(b.dataset.value))));
    else if (form.elements[field]) form.elements[field].value = [].concat(value).join(", ");
  }
  if (Object.keys(fields).length) $("filters").open = true;
  renderActiveFilters();
}

/* Shows the active filters next to the question, so that the user always sees them. */
const FORM_NAMES = { drug_name: "Drug", condition: "Condition", sponsor: "Sponsor", country: "Country", start_year: "Started from", end_year: "Started up to", trial_phase: "Phase", status: "Status", chart_type: "Chart" };
function renderActiveFilters() {
  const wrap = $("active-filters"), body = requestBody();
  wrap.replaceChildren();
  for (const [field, label] of Object.entries(FORM_NAMES)) {
    if (body[field] === undefined) continue;
    const pill = el("span", { class: "pill" }, wrap);
    const value = Array.isArray(body[field]) ? body[field].map(prettyCode).join(", ") : field === "chart_type" ? prettyCode(body[field]) : String(body[field]);
    pill.appendChild(document.createTextNode(`${label}: `));
    el("b", { text: value }, pill);
    const remove = el("button", { type: "button", "aria-label": `Remove the ${label} filter`, text: "×" }, pill);
    remove.addEventListener("click", () => {
      if (form.elements[field]) form.elements[field].value = "";
      form.querySelectorAll(`.toggles[data-field="${field}"] button`).forEach((b) => b.setAttribute("aria-pressed", "false"));
      renderActiveFilters();
    });
  }
}

function remember(question) {
  try {
    const recent = [question, ...JSON.parse(localStorage.getItem("recentQuestions") || "[]").filter((q) => q !== question)].slice(0, 6);
    localStorage.setItem("recentQuestions", JSON.stringify(recent));
  } catch (error) { /* Browser storage can be not available. The list is an aid only. */ }
  renderRecent();
}
function renderRecent() {
  let recent = [];
  try { recent = JSON.parse(localStorage.getItem("recentQuestions") || "[]"); } catch (error) { recent = []; }
  const wrap = $("recent");
  wrap.replaceChildren();
  for (const question of recent) {
    el("button", { type: "button", text: question }, wrap).addEventListener("click", () => { $("query").value = question; ask(); });
  }
  $("recent-wrap").hidden = recent.length === 0;
}

let timer = null;
async function ask(event) {
  if (event) event.preventDefault();
  if (!form.reportValidity()) return;
  const button = $("submit");
  state.request = requestBody();
  button.disabled = true; button.textContent = "Working…";
  show("loading");
  const started = Date.now();
  const tick = () => {
    const seconds = Math.round((Date.now() - started) / 1000);
    $("loading-sub").textContent = seconds < 10
      ? `Most answers take 3 to 10 seconds. ${seconds} s so far.`
      : `${seconds} s so far. Questions that match a very large number of trials can take up to about 30 seconds, because the registry limits how fast it can be read.`;
  };
  tick();
  timer = setInterval(tick, 1000);
  try {
    const reply = await fetch("/query", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(state.request) });
    const data = await reply.json();
    if (reply.status === 422) {
      const message = $("message");
      message.replaceChildren();
      message.className = "card error";
      el("h3", { text: "Something in the question or the filters needs fixing" }, message);
      const list = el("ul", {}, message);
      (data.detail || []).forEach((problem) => el("li", { text: `${FORM_NAMES[(problem.loc || [])[1]] || (problem.loc || []).slice(1).join(".")}: ${problem.msg}` }, list));
      show("message", "start");
      window.scrollTo({ top: 0, behavior: "smooth" });
    } else {
      renderResponse(data, reply.status);
      if (data.status === "ok") remember(state.request.query);
    }
  } catch (error) {
    const message = $("message");
    message.replaceChildren();
    message.className = "card error";
    el("h3", { text: "This page could not reach the service" }, message);
    el("p", { text: "Check that the server is still running, then try again." }, message);
    show("message", "start");
    window.scrollTo({ top: 0, behavior: "smooth" });
  } finally {
    clearInterval(timer);
    button.disabled = false; button.textContent = "Ask";
  }
}

form.addEventListener("submit", ask);
form.addEventListener("input", renderActiveFilters);
form.querySelectorAll(".toggles button").forEach((button) => {
  button.setAttribute("aria-pressed", "false");
  button.addEventListener("click", () => {
    button.setAttribute("aria-pressed", button.getAttribute("aria-pressed") === "true" ? "false" : "true");
    renderActiveFilters();
  });
});
function clearFilters() {
  const question = $("query").value;
  form.reset();
  $("query").value = question;
  form.querySelectorAll(".toggles button").forEach((b) => b.setAttribute("aria-pressed", "false"));
  renderActiveFilters();
}
$("clear-filters").addEventListener("click", clearFilters);
$("view-chart").addEventListener("click", () => setView(false));
$("view-table").addEventListener("click", () => setView(true));
$("new-question").addEventListener("click", () => {
  show("start");
  $("query").value = "";
  window.scrollTo({ top: 0, behavior: "smooth" });
  $("query").focus();
});
$("theme").addEventListener("click", () => {
  const root = document.documentElement;
  const dark = root.dataset.theme ? root.dataset.theme === "dark" : window.matchMedia("(prefers-color-scheme: dark)").matches;
  root.dataset.theme = dark ? "light" : "dark";
  if (state.response && state.response.status === "ok" && !$("result").hidden) {
    // The colors are CSS variables. Thus the chart follows the theme and needs no new draw.
  }
});
for (const example of EXAMPLES) {
  const card = el("button", { type: "button", class: "example" }, $("examples"));
  el("span", { class: "what", text: example.what }, card);
  el("span", { class: "ask", text: example.ask }, card);
  el("span", { class: "kind", text: example.kind }, card);
  card.addEventListener("click", () => { $("query").value = example.ask; ask(); });
}
window.addEventListener("scroll", hideTip, { passive: true });
renderRecent();
renderActiveFilters();
