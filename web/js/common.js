"use strict";
/* Shared helpers for the shop, back-office and driver apps */
const $ = (s, el = document) => el.querySelector(s);

/* DOM's replaceChildren/append turn null into the text "null": filter falsy children instead */
for (const m of ["replaceChildren", "append"]) {
  const orig = Element.prototype[m];
  Element.prototype[m] = function (...kids) { return orig.apply(this, kids.flat(Infinity).filter(x => x != null && x !== false)); };
}

function h(tag, props, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v === false || v == null) continue;
    if (k === "class") e.className = v;
    else if (k === "html") e.innerHTML = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (["value", "checked", "selected", "disabled"].includes(k)) e[k] = v;
    else e.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid == null || kid === false) continue;
    e.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return e;
}

async function api(path, body, opts = {}) {
  const init = body !== undefined
    ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }
    : { headers: opts.poll ? { "X-Poll": "1" } : {} };
  const r = await fetch(path, init);
  let j; try { j = await r.json(); } catch { j = { error: "bad response from server" }; }
  if (!r.ok) { const e = new Error(j.error || r.statusText); e.status = r.status; throw e; }
  return j;
}

function toast(msg, kind = "info", ms = 4200) {
  let box = $("#toasts"); if (!box) { box = h("div", { id: "toasts" }); document.body.append(box); }
  const t = h("div", { class: "toast " + kind }, msg); box.append(t); setTimeout(() => t.remove(), ms);
}

const pad = n => String(n).padStart(2, "0");
const sleep = ms => new Promise(r => setTimeout(r, ms));
const rupee = n => "₹" + Number(n).toLocaleString("en-IN", { minimumFractionDigits: 0, maximumFractionDigits: 2 });
function fmt(iso, withDate = false) {
  if (!iso) return "-"; const d = new Date(iso); if (isNaN(d)) return String(iso);
  const t = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  return (new Date().toDateString() === d.toDateString() && !withDate) ? t : `${d.getDate()} ${d.toLocaleString([], { month: "short" })}, ${t}`;
}
function fmtDay(iso) { const d = new Date(iso); return `${d.getDate()} ${d.toLocaleString([], { month: "short" })} ${d.getFullYear()}`; }
function isoLocalInput(d) { return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`; }
const inputToIso = v => new Date(v).toISOString().replace(/\.\d+Z$/, "Z");
function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }

const STATUS_TEXT = {
  PLACED: "Order placed", CONFIRMED: "Order confirmed", SHIPPED: "Shipped", OUT_FOR_DELIVERY: "Out for delivery",
  DELIVERED: "Delivered", DELAYED: "Delayed", CANCELLED: "Cancelled",
  CREATED: "Created", PICKED_UP: "Picked up", IN_TRANSIT: "In transit", EXCEPTION: "Exception",
};
function pill(status, label) { return h("span", { class: "pill st-" + status }, label || STATUS_TEXT[status] || String(status).replace(/_/g, " ").toLowerCase()); }

/* ---------------- map (inline SVG, no external tiles) ---------------- */
function drawMap(opts) {
  const { warehouse, dest, trail = [], current, status, zone, height = 260 } = opts;
  const W = 640, H = height, pad = 26;
  const pts = [[dest.lat, dest.lon]]; if (warehouse) pts.push([warehouse.lat, warehouse.lon]); if (current) pts.push([current.lat, current.lon]); trail.forEach(t => pts.push([t.lat, t.lon]));
  if (zone) pts.push([zone.min_lat, zone.min_lon], [zone.max_lat, zone.max_lon]);
  const la = pts.map(p => p[0]), lo = pts.map(p => p[1]);
  const spanLa = Math.max(0.03, Math.max(...la) - Math.min(...la)), spanLo = Math.max(0.03, Math.max(...lo) - Math.min(...lo));
  const minLat = Math.min(...la) - spanLa * 0.15, maxLat = Math.max(...la) + spanLa * 0.15;
  const minLon = Math.min(...lo) - spanLo * 0.15, maxLon = Math.max(...lo) + spanLo * 0.15;
  const k = Math.cos(((minLat + maxLat) / 2) * Math.PI / 180);
  const sx = (maxLon - minLon) * k, sy = maxLat - minLat;
  const sc = Math.min((W - 2 * pad) / sx, (H - 2 * pad) / sy), ox = (W - sx * sc) / 2, oy = (H - sy * sc) / 2;
  const X = lon => ox + (lon - minLon) * k * sc, Y = lat => oy + (maxLat - lat) * sc;
  const NS = "http://www.w3.org/2000/svg";
  const el = (n, a, t) => { const e = document.createElementNS(NS, n); for (const [kk, v] of Object.entries(a || {})) e.setAttribute(kk, v); if (t) e.textContent = t; return e; };
  const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, class: "map" });
  // faint street grid
  for (let i = 1; i < 12; i++) { svg.append(el("line", { x1: (W / 12) * i, y1: 0, x2: (W / 12) * i + 40, y2: H, stroke: "#dbe3f0", "stroke-width": 1 })); }
  for (let i = 1; i < 7; i++) { svg.append(el("line", { x1: 0, y1: (H / 7) * i, x2: W, y2: (H / 7) * i - 14, stroke: "#dbe3f0", "stroke-width": 1 })); }
  if (zone) svg.append(el("rect", { x: X(zone.min_lon), y: Y(zone.max_lat), width: (zone.max_lon - zone.min_lon) * k * sc, height: (zone.max_lat - zone.min_lat) * sc, fill: "#bfdbfe", opacity: .45, stroke: "#60a5fa", "stroke-dasharray": "4 3" }));
  const col = { SHIPPED: "#2563eb", IN_TRANSIT: "#2563eb", PICKED_UP: "#6366f1", OUT_FOR_DELIVERY: "#0d9488", DELIVERED: "#16a34a", EXCEPTION: "#dc2626", DELAYED: "#dc2626" }[status] || "#2563eb";
  if (trail.length > 1) svg.append(el("polyline", { points: trail.map(t => `${X(t.lon)},${Y(t.lat)}`).join(" "), fill: "none", stroke: col, "stroke-width": 3.2, "stroke-linecap": "round", "stroke-linejoin": "round", opacity: .85 }));
  if (warehouse) {
    svg.append(el("rect", { x: X(warehouse.lon) - 8, y: Y(warehouse.lat) - 8, width: 16, height: 16, rx: 3, fill: "#334155" }));
    svg.append(el("text", { x: X(warehouse.lon) + 12, y: Y(warehouse.lat) + 4, "font-size": 11, fill: "#334155", "font-weight": 600 }, warehouse.name || "Warehouse"));
  }
  svg.append(el("path", { d: `M${X(dest.lon)},${Y(dest.lat)} l0,-22 l14,6 l-14,6`, fill: "#ef4444", stroke: "#b91c1c", "stroke-width": 2 }));
  svg.append(el("circle", { cx: X(dest.lon), cy: Y(dest.lat), r: 3.5, fill: "#7f1d1d" }));
  svg.append(el("text", { x: X(dest.lon) + 18, y: Y(dest.lat) - 14, "font-size": 11, fill: "#7f1d1d", "font-weight": 700 }, "You"));
  if (current) {
    svg.append(el("circle", { cx: X(current.lon), cy: Y(current.lat), r: 14, fill: col, opacity: .18 }));
    svg.append(el("circle", { cx: X(current.lon), cy: Y(current.lat), r: 7, fill: col, stroke: "#fff", "stroke-width": 2.5 }));
    svg.append(el("text", { x: X(current.lon), y: Y(current.lat) - 14, "font-size": 15, "text-anchor": "middle" }, "🚚"));
  }
  return svg;
}

/* modal */
function modal(title, content, opts = {}) {
  const ov = h("div", { class: "overlay", onclick: e => { if (e.target === ov) ov.remove(); } },
    h("div", { class: "dialog", style: opts.width ? `width:${opts.width}` : "" },
      h("div", { class: "row sp", style: "margin-bottom:10px" }, h("h2", { style: "margin:0" }, title), h("button", { class: "btn sm", onclick: () => ov.remove() }, "✕")), content));
  document.body.append(ov); return ov;
}
