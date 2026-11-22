"use strict";
/* LogiShop back office: dashboard, orders, dispatch, inventory, returns, catalogue, ML insights, system.
   Every page function returns { title, sub, src, tools, body }; show() wraps it in the page header and renders it.
   `src` is the place in the codebase where the SQL behind the screen lives (shown as a chip in the header). */
const A = { user: null, timer: null, page: "dashboard", seq: 0, f: { orders: { q: "", status: "" } } };

/* ---------------- icons (inline SVG, no external assets) ---------------- */
const ICONS = {
  bag: '<path d="M6 2 3 6v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V6l-3-4z"/><path d="M3 6h18"/><path d="M16 10a4 4 0 0 1-8 0"/>',
  grid: '<rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/>',
  file: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6M16 13H8M16 17H8M10 9H8"/>',
  truck: '<path d="M1 3h15v13H1z"/><path d="M16 8h4l3 3v5h-7z"/><circle cx="5.5" cy="18.5" r="2.5"/><circle cx="18.5" cy="18.5" r="2.5"/>',
  box: '<path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><path d="M3.27 6.96 12 12.01l8.73-5.05M12 22.08V12"/>',
  undo: '<path d="M1 4v6h6"/><path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10"/>',
  tag: '<path d="M20.59 13.41l-7.17 7.17a2 2 0 0 1-2.83 0L2 12V2h10l8.59 8.59a2 2 0 0 1 0 2.82z"/><circle cx="7" cy="7" r="1.5"/>',
  pulse: '<path d="M22 12h-4l-3 9L9 3l-3 9H2"/>',
  server: '<rect x="2" y="2" width="20" height="8" rx="2"/><rect x="2" y="14" width="20" height="8" rx="2"/><path d="M6 6h.01M6 18h.01"/>',
  trend: '<path d="M23 6l-9.5 9.5-5-5L1 18"/><path d="M17 6h6v6"/>',
  users: '<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/>',
  clock: '<circle cx="12" cy="12" r="10"/><path d="M12 6v6l4 2"/>',
  alert: '<path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><path d="M12 9v4M12 17h.01"/>',
  code: '<path d="M16 18l6-6-6-6M8 6l-6 6 6 6"/>',
  info: '<circle cx="12" cy="12" r="10"/><path d="M12 16v-4M12 8h.01"/>',
  check: '<path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><path d="M22 4 12 14.01l-3-3"/>',
  db: '<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"/><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"/>',
  pin: '<path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 0 1 18 0z"/><circle cx="12" cy="10" r="3"/>',
  zap: '<path d="M13 2 3 14h9l-1 8 10-12h-9z"/>',
};
function ico(name) {
  const t = document.createElement("template");
  t.innerHTML = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${ICONS[name] || ""}</svg>`;
  return t.content.firstChild;
}

const NAV = [
  { id: "dashboard", label: "Dashboard", icon: "grid", group: "Overview" },
  { id: "orders", label: "Orders", icon: "file", group: "Operations" },
  { id: "shipments", label: "Dispatch", icon: "truck", group: "Operations" },
  { id: "inventory", label: "Inventory", icon: "box", group: "Operations" },
  { id: "requests", label: "Returns", icon: "undo", group: "Operations" },
  { id: "catalogue", label: "Catalogue", icon: "tag", group: "Manage" },
  { id: "insights", label: "Insights (ML)", icon: "pulse", group: "Manage" },
  { id: "system", label: "System", icon: "server", group: "Manage" },
];
const main = () => $("#main");

/* ---------------- small building blocks ---------------- */
const card = (title, o = {}, ...body) => h("section", { class: "card" },
  (title || o.right) ? h("div", { class: "hd" }, h("div", { class: "row", style: "gap:10px;flex-wrap:nowrap" }, title ? h("h2", {}, title) : null, o.hint ? h("span", { class: "hint" }, o.hint) : null), o.right || null) : null, ...body);
const bd = (...k) => h("div", { class: "bd" }, ...k);
const scroll = (...k) => h("div", { class: "scroll" }, ...k);
const empty = (title, text) => h("div", { class: "empty-state" }, h("b", {}, title), text || "");
const srcChip = text => h("span", { class: "src", title: "Where the SQL behind this screen lives in the codebase" }, ico("code"), text);
function tbl(heads, rows) {
  return h("table", { class: "t" }, h("thead", {}, h("tr", {}, heads.map(x => Array.isArray(x) ? h("th", { class: x[1] }, x[0]) : h("th", {}, x)))), h("tbody", {}, ...rows));
}
const risk = () => h("span", { class: "pill risk", style: "margin-left:6px" }, "At risk");
const seg = (...btns) => h("div", { class: "seg" }, ...btns);
const initials = n => (n || "?").split(/\s+/).map(w => w[0]).slice(0, 2).join("").toUpperCase();

async function act(label, fn) {
  try {
    const r = await fn();
    if (r && r.rejections && r.rejections.length) toast(`✗ ${label}: refused - ${r.rejections.map(x => x.reason).join("; ")}`, "warn", 7000);
    else if (r && r.duplicate) toast(`↺ ${label}: duplicate delivery ignored (no rows changed)`, "info", 6000);
    else toast(`✓ ${label}`, "ok");
    show(A.page, true); return r;
  } catch (e) { toast(`✗ ${label}: ${e.message}`, "warn", 7000); }
}

/* ---------------- sidebar ---------------- */
function renderSide() {
  document.body.classList.remove("signed-out");
  const els = [h("div", { class: "brand" }, h("div", { class: "mark" }, ico("bag")), h("div", { class: "name" }, "LogiShop", h("small", {}, "Back office")))];
  let last = null;
  for (const n of NAV) {
    if (n.group !== last) { els.push(h("div", { class: "grp" }, n.group)); last = n.group; }
    els.push(h("a", { href: `#/${n.id}`, class: "nav" + (A.page === n.id ? " on" : "") }, ico(n.icon), n.label));
  }
  els.push(h("div", { class: "foot" },
    h("div", { class: "me" }, h("div", { class: "av" }, initials(A.user && A.user.name)), h("div", {}, h("b", {}, A.user ? A.user.name : ""), h("span", {}, "Administrator"))),
    h("a", { href: "/" }, "Open storefront ↗"), h("a", { href: "/driver" }, "Open driver app ↗"),
    h("a", { href: "#", onclick: async e => { e.preventDefault(); await api("/api/auth/logout", {}); location.reload(); } }, "Sign out")));
  $("#side").replaceChildren(...els);
}

/* ---------------- sign-in ---------------- */
function loginView(note) {
  clearInterval(A.timer); document.body.classList.add("signed-out");
  const email = h("input", { type: "email", value: "admin@logistics.local", autocomplete: "username", required: true });
  const pw = h("input", { type: "password", value: "admin123", autocomplete: "current-password", required: true });
  const err = h("div", { class: "small", style: "color:#b42318;min-height:18px;margin:-4px 0 10px" }, note || "");
  const btn = h("button", { class: "btn primary big block", type: "submit" }, "Sign in");
  const form = h("form", {
    onsubmit: async e => {
      e.preventDefault(); btn.disabled = true; err.textContent = "";
      try {
        const r = await api("/api/auth/login", { email: email.value, password: pw.value });
        if (r.role !== "ADMIN") { await api("/api/auth/logout", {}); err.textContent = "That account is not an administrator."; btn.disabled = false; return; }
        location.reload();
      } catch (x) { err.textContent = x.message; btn.disabled = false; }
    },
  }, h("label", {}, "Email", email), h("label", {}, "Password", pw), err, btn);
  main().replaceChildren(h("div", { class: "signin" },
    h("div", { class: "art" },
      h("div", { class: "brand" }, h("div", { class: "mark" }, ico("bag")), "LogiShop"),
      h("div", {}, h("h1", {}, "Run every order, warehouse to doorstep."),
        h("p", {}, "Dispatch vehicles, watch live shipments, control stock and prices - every action goes through the event stream and lands in PostgreSQL."),
        h("ul", {}, h("li", {}, ico("truck"), "Live dispatch and GPS tracking"), h("li", {}, ico("box"), "Stock ledger that always balances"), h("li", {}, ico("pulse"), "ML delay prediction and anomaly alerts"))),
      h("div", { class: "small", style: "color:#7d89b5" }, "RV College of Engineering · DBMS (CD252IA)")),
    h("div", { class: "form" }, h("div", { class: "box" }, h("h2", {}, "Back office sign-in"), h("p", { class: "lead" }, "Administrators only."), form,
      h("div", { class: "demo" }, "Demo administrator", h("br"), h("code", {}, "admin@logistics.local"), " / ", h("code", {}, "admin123"))))));
  setTimeout(() => pw.focus(), 50);
}

/* ---------------- pages ---------------- */
const PAGES = {};

PAGES.dashboard = async () => {
  const k = await api("/api/admin/dashboard", undefined, { poll: true });
  const max = Math.max(1, ...k.by_status.map(s => s.n)), maxRev = Math.max(1, ...k.by_category.map(c => c.revenue));
  const tiles = [["orders", "Orders", "file"], ["open_orders", "Open orders", "clock"], ["revenue", "Revenue", "trend", true], ["customers", "Customers", "users"],
    ["at_risk", "At-risk shipments", "alert", false, true], ["open_returns", "Open returns", "undo"], ["low_stock", "Low-stock lines", "box", false, true]];
  const days = k.daily.slice().reverse(), maxDay = Math.max(1, ...days.map(d => d.revenue));
  const dayLabel = v => { const d = new Date(v); return isNaN(d) ? String(v) : `${d.getDate()} ${d.toLocaleString([], { month: "short" })}`; };
  return {
    title: "Dashboard", sub: "Orders, revenue and fulfilment at a glance. Refreshes every few seconds.", src: "logistics/api.py › dashboard()",
    body: [
      h("div", { class: "kpis" }, tiles.map(([key, label, icon, money, warn]) => h("div", { class: "kpi" + (warn && k[key] > 0 ? " alert" : "") }, h("div", { class: "l" }, ico(icon), label), h("div", { class: "n" }, money ? rupee(k[key]) : k[key])))),
      h("div", { class: "grid2" },
        card("Orders by status", {}, bd(...k.by_status.map(s => h("div", { class: "barrow" }, h("div", { class: "lab" }, pill(s.status)), h("div", { class: "trk" }, h("div", { class: "bar", style: `width:${(s.n / max) * 100}%` })), h("div", { class: "val" }, s.n))))),
        card("Revenue by category", { hint: "view shop.v_sales_by_category" }, bd(...k.by_category.map(c => h("div", { class: "barrow" }, h("div", { class: "lab", title: c.category }, c.category), h("div", { class: "trk" }, h("div", { class: "bar g", style: `width:${(c.revenue / maxRev) * 100}%` })), h("div", { class: "val" }, rupee(c.revenue))))))),
      card("Daily sales", { hint: "materialized view shop.mv_daily_sales" }, bd(days.length
        ? h("div", { class: "cols" }, ...days.map(d => h("div", { class: "col", title: `${d.orders} orders` }, h("div", { class: "v" }, rupee(d.revenue)), h("div", { class: "b", style: `height:${Math.max(4, (d.revenue / maxDay) * 100)}px` }), h("div", { class: "d" }, dayLabel(d.day)))))
        : empty("No sales yet", "Place an order in the storefront and it shows up here."))),
    ],
  };
};

PAGES.orders = async () => {
  const os = await api("/api/admin/orders", undefined, { poll: true });
  const f = A.f.orders, statuses = [...new Set(os.map(o => o.status))];
  const tbody = h("tbody", {});
  const fill = () => {
    const q = f.q.trim().toLowerCase();
    const rows = os.filter(o => (!f.status || o.status === f.status) && (!q || `${o.order_id} ${o.customer} ${o.shipment_id || ""} ${o.vehicle_id || ""}`.toLowerCase().includes(q)));
    tbody.replaceChildren(...(rows.length ? rows.map(o => h("tr", {},
      h("td", {}, h("span", { class: "id" }, o.order_id)), h("td", {}, o.customer), h("td", { class: "dim" }, fmt(o.placed_at, true)),
      h("td", {}, pill(o.status), o.at_risk ? risk() : null), h("td", { class: "num" }, rupee(o.total)),
      h("td", {}, h("span", { class: "dim" }, o.payment_method + " "), pill(o.payment_status || "PENDING")),
      h("td", {}, o.shipment_id ? h("span", {}, h("span", { class: "id" }, o.shipment_id), h("span", { class: "dim" }, " · " + o.shipment_status.toLowerCase().replace(/_/g, " "))) : h("span", { class: "dim" }, "-")),
      h("td", {}, o.vehicle_id ? h("span", { class: "id" }, o.vehicle_id) : h("span", { class: "dim" }, "-")))) : [h("tr", {}, h("td", { colspan: 8 }, empty("No orders match", "Clear the search or status filter.")))]));
    count.textContent = `${rows.length} of ${os.length}`;
  };
  const count = h("span", { class: "hint" });
  const q = h("input", { class: "in", placeholder: "Search order, customer, shipment…", value: f.q, style: "width:260px", oninput: e => { f.q = e.target.value; fill(); } });
  const st = h("select", { class: "in", onchange: e => { f.status = e.target.value; fill(); } }, h("option", { value: "" }, "All statuses"), ...statuses.map(s => h("option", { value: s, selected: s === f.status }, s.replace(/_/g, " ").toLowerCase())));
  fill();
  return {
    title: "Orders", sub: "Every customer order with its payment and shipment.", src: "logistics/api.py › admin_orders()",
    body: [card(null, {}, h("div", { class: "hd" }, h("div", { class: "row" }, q, st), count),
      scroll(h("table", { class: "t" }, h("thead", {}, h("tr", {}, ["Order", "Customer", "Placed", "Status", ["Total", "num"], "Payment", "Shipment", "Vehicle"].map(x => Array.isArray(x) ? h("th", { class: x[1] }, x[0]) : h("th", {}, x)))), tbody)))],
  };
};

PAGES.shipments = async () => {
  const d = await api("/api/admin/shipments", undefined, { poll: true });
  const run = (sid, label, fn) => h("button", { onclick: () => act(`${sid}: ${label}`, fn) }, label);
  const simBtn = (sid, label, body, cls = "") => h("button", { class: "btn sm " + cls, onclick: async () => { try { await api("/api/admin/simulate", { shipment_id: sid, ...body }); toast(`Simulation started for ${sid} - watch the terminal`, "info"); } catch (e) { toast(e.message, "warn", 6000); } } }, label);
  const rows = d.shipments.map(s => {
    const live = s.status !== "DELIVERED";
    const sel = h("select", { class: "in", style: "max-width:200px" }, h("option", { value: "" }, "Assign vehicle…"), ...d.vehicles.map(v => h("option", { value: v.vehicle_id, selected: v.vehicle_id === s.vehicle_id }, `${v.vehicle_id} · ${v.registration_no} · ${v.vehicle_type.toLowerCase()}${v.driver ? " · " + v.driver : ""}`)));
    return h("tr", {},
      h("td", {}, h("div", { class: "id" }, s.shipment_id), h("div", { class: "sub-line" }, s.order_id || "no order")),
      h("td", {}, s.customer_name), h("td", { class: "dim" }, `${s.warehouse_name} → ${s.zone_name || "?"}`),
      h("td", {}, pill(s.status), s.at_risk && live ? risk() : null),
      h("td", {}, s.status === "DELIVERED" ? h("span", { style: "color:var(--ok);font-weight:600" }, "✓ " + fmt(s.actual_delivery_at)) : fmt(s.eta_current)),
      h("td", {}, h("div", { class: "actions", style: "flex-wrap:nowrap" }, sel, h("button", { class: "btn sm", onclick: () => sel.value ? act(`assign ${sel.value} to ${s.shipment_id}`, () => api(`/api/admin/shipments/${s.shipment_id}/assign`, { vehicle_id: sel.value })) : toast("Choose a vehicle first", "info") }, "Assign"))),
      h("td", {}, live ? h("div", {},
        h("div", { class: "actions" },
          seg(run(s.shipment_id, "Picked up", () => api(`/api/admin/shipments/${s.shipment_id}/status`, { status: "PICKED_UP" })), run(s.shipment_id, "In transit", () => api(`/api/admin/shipments/${s.shipment_id}/status`, { status: "IN_TRANSIT" })), run(s.shipment_id, "Delivered", () => api(`/api/admin/shipments/${s.shipment_id}/status`, { status: "DELIVERED" }))),
          simBtn(s.shipment_id, "▶ Drive", { mode: "live", until: "end" }, "primary"), simBtn(s.shipment_id, "Breakdown", { mode: "live", until: "end", anomaly: "breakdown" }),
          h("button", { class: "btn sm danger", title: "Stop simulation", onclick: () => api("/api/admin/simulate/stop", { shipment_id: s.shipment_id }) }, "Stop")),
        h("div", { class: "actions", style: "margin-top:7px" }, h("span", { class: "sub-line" }, "Move vehicle"), seg(...[0.25, 0.6, 0.95].map(fr => run(s.shipment_id, `${Math.round(fr * 100)}%`, () => api(`/api/admin/shipments/${s.shipment_id}/move`, { fraction: fr })))))) : h("span", { class: "dim" }, "Completed")));
  });
  return {
    title: "Dispatch", sub: "Each button publishes an event to the stream; the consumers apply it - or refuse it. Try an invalid one, e.g. mark a CREATED shipment Delivered.", src: "logistics/api.py › admin_shipments()",
    body: [card(null, {}, scroll(tbl(["Shipment", "Customer", "Route", "Status", "ETA", "Vehicle", "Actions"], rows)))],
  };
};

PAGES.inventory = async () => {
  const d = await api("/api/admin/inventory", undefined, { poll: true });
  const by = {}; d.stock.forEach(r => { (by[r.sku_id] ||= { name: r.description })[r.warehouse_id] = r; });
  const wh = h("select", { class: "in" }, ...d.warehouses.map(w => h("option", { value: w.warehouse_id }, `${w.warehouse_id} · ${w.name}`)));
  const sku = h("select", { class: "in", style: "max-width:280px" }, ...d.skus.map(k => h("option", { value: k.sku_id }, `${k.sku_id} · ${k.name}`)));
  const qty = h("input", { class: "in", type: "number", value: 20, min: 1, style: "width:90px" });
  return {
    title: "Inventory", sub: "Stock per warehouse. Each balance is derived from an append-only ledger and can be audited against it.", src: "logistics/api.py › admin_inventory()",
    body: [
      card("Stock by warehouse", { hint: "available quantity", right: d.audit_bad.length ? pill("MISMATCH", `${d.audit_bad.length} ledger mismatches`) : pill("OK", "Balance = ledger on every line") },
        scroll(tbl(["SKU", "Product", ...d.warehouses.map(w => [w.warehouse_id, "num"])], Object.entries(by).map(([id, r]) => h("tr", {}, h("td", {}, h("span", { class: "id" }, id)), h("td", {}, r.name), ...d.warehouses.map(w => { const c = r[w.warehouse_id]; return h("td", { class: "num" }, c ? h("span", { class: "pill st-" + c.level }, c.available_qty) : h("span", { class: "dim" }, "-")); })))))),
      card("Receive stock", { hint: "STOCK_IN event → balance upsert + ledger row, one transaction" }, bd(h("div", { class: "actions" }, wh, sku, h("span", { class: "dim" }, "×"), qty,
        h("button", { class: "btn primary", onclick: () => act(`receive ${qty.value} × ${sku.value} at ${wh.value}`, () => api("/api/admin/inventory/stock-in", { warehouse_id: wh.value, sku_id: sku.value, quantity: +qty.value })) }, "Receive stock")))),
      card("Latest stock movements", { hint: "append-only ledger" }, scroll(tbl(["#", "Warehouse", "SKU", "Movement", ["Change", "num"], "Reference", "Time"],
        d.ledger.map(l => h("tr", {}, h("td", { class: "dim" }, l.ledger_id), h("td", {}, l.warehouse_id), h("td", {}, h("span", { class: "id" }, l.sku_id)), h("td", {}, l.movement_type.replace(/_/g, " ").toLowerCase()),
          h("td", { class: "num", style: `font-weight:700;color:${l.quantity_delta < 0 ? "var(--bad)" : "var(--ok)"}` }, (l.quantity_delta > 0 ? "+" : "") + l.quantity_delta), h("td", { class: "dim" }, l.shipment_id || l.request_id || "-"), h("td", { class: "dim" }, fmt(l.event_time, true))))))),
    ],
  };
};

PAGES.requests = async () => {
  const rs = await api("/api/admin/requests", undefined, { poll: true });
  const rows = rs.map(r => h("tr", {},
    h("td", {}, h("span", { class: "id" }, r.request_id)), h("td", {}, h("span", { class: "id" }, r.order_id || r.shipment_id)), h("td", {}, r.customer_name),
    h("td", {}, r.request_type.toLowerCase()), h("td", {}, pill(r.status)),
    h("td", { class: "dim" }, r.request_type === "RESCHEDULE" ? `${fmt(r.requested_start, true)} – ${fmt(r.requested_end)}` : r.items.map(i => `${i.quantity}× ${i.sku_id} (${i.item_condition})`).join(", ")),
    h("td", { class: "num" }, r.status === "PICKUP_SCHEDULED" ? h("button", { class: "btn sm", onclick: () => act(`${r.request_id}: pickup done`, () => api(`/api/admin/requests/${r.request_id}/advance`, { to_status: "PICKED_UP" })) }, "Mark picked up")
      : r.status === "PICKED_UP" ? h("button", { class: "btn sm primary", onclick: () => act(`${r.request_id}: received at warehouse`, () => api(`/api/admin/requests/${r.request_id}/advance`, { to_status: "COMPLETED" })) }, "Receive, restock & refund") : "")));
  return {
    title: "Returns & reschedules", sub: "Customer requests move through a fixed lifecycle. Completing a return restocks the items and refunds the payment.", src: "logistics/api.py › admin_requests()",
    body: [card(null, {}, rs.length ? scroll(tbl(["Request", "Order", "Customer", "Type", "Status", "Details", ""], rows)) : empty("No requests yet", "As a customer, reschedule an order that is not out for delivery yet, or return a delivered one."))],
  };
};

PAGES.catalogue = async () => {
  const ps = await api("/api/admin/products", undefined, { poll: true });
  return {
    title: "Catalogue", sub: "Changing a price runs UPDATE shop.sku; a trigger records the old and new price in shop.price_history. Past orders keep the price they were bought at.", src: "logistics/api.py › admin_price()",
    body: [card(null, {}, scroll(tbl(["", "SKU", "Product", "Category", ["MRP", "num"], "Selling price", ["In stock", "num"], ["Sold", "num"], "Active"], ps.map(p => {
      const pr = h("input", { class: "in", type: "number", value: p.price, style: "width:92px" });
      return h("tr", {}, h("td", { style: "font-size:20px;width:44px" }, p.emoji), h("td", {}, h("span", { class: "id" }, p.sku_id)), h("td", {}, p.name, h("div", { class: "sub-line" }, p.brand)), h("td", { class: "dim" }, p.category),
        h("td", { class: "num dim" }, rupee(p.mrp)),
        h("td", {}, h("div", { class: "actions", style: "flex-wrap:nowrap" }, pr, h("button", { class: "btn sm", onclick: () => act(`price of ${p.sku_id} → ${pr.value}`, () => api(`/api/admin/products/${p.sku_id}/price`, { price: +pr.value })) }, "Set"))),
        h("td", { class: "num" }, p.in_stock), h("td", { class: "num" }, p.units_sold),
        h("td", {}, h("input", { type: "checkbox", checked: p.is_active, onchange: e => act(`${p.sku_id} ${e.target.checked ? "enabled" : "disabled"}`, () => api(`/api/admin/products/${p.sku_id}/active`, { active: e.target.checked })) })));
    })))) ],
  };
};

PAGES.insights = async () => {
  const m = await api("/api/admin/ml", undefined, { poll: true });
  const best = Math.max(1, ...m.accuracy.map(a => a.mae_minutes));
  const tog = h("input", { type: "checkbox", checked: m.models.enabled, onchange: async e => { await api("/api/admin/ml", { enabled: e.target.checked }); toast(e.target.checked ? "ML models enabled" : "ML disabled: ETA falls back to the rule-based estimate", "info"); } });
  return {
    title: "Insights (ML)", sub: "Delivery-time prediction and delay alerts, with accuracy measured against real delivery times.", src: "logistics/ml/ · views.sql › v_ml_model_accuracy",
    body: [
      card("Model control", {}, bd(h("label", { class: "actions", style: "cursor:pointer" }, tog, h("b", {}, "ML models enabled"), h("span", { class: "dim" }, "Turn off to fall back to the rule-based ETA automatically.")))),
      card("Live ETA accuracy", { hint: "mean absolute error in minutes (lower is better) · view logistics.v_ml_model_accuracy" },
        m.accuracy.length ? scroll(tbl(["Model", ["Predictions scored", "num"], ["MAE (min)", "num"], "", ["Best", "num"], ["Worst", "num"]], m.accuracy.map(a => h("tr", {},
          h("td", {}, h("b", {}, a.model_name.replace(/_/g, " "))), h("td", { class: "num" }, a.predictions_scored), h("td", { class: "num" }, h("b", {}, a.mae_minutes)),
          h("td", { style: "width:32%" }, h("div", { class: "trk", style: "height:8px;border-radius:4px;background:#eef0f6;overflow:hidden" }, h("div", { class: "bar " + (a.model_name === "gradient_boosting" ? "g" : a.model_name === "rule_based" ? "r" : ""), style: `width:${(a.mae_minutes / best) * 100}%` }))),
          h("td", { class: "num dim" }, a.best_minutes), h("td", { class: "num dim" }, a.worst_minutes)))))
          : bd(empty("No delivered trips scored yet", "Run a delivery with the ▶ Drive button on the Dispatch page.")),
        m.report ? bd(h("div", { class: "sub-line" }, `Offline test (${m.report.eta_holdout_samples} simulated trips): rule-based ${m.report.eta_mae_minutes.rule_based} · linear regression ${m.report.eta_mae_minutes.linear_regression} · gradient boosting ${m.report.eta_mae_minutes.gradient_boosting} min MAE`)) : null),
      card("Shipments at risk of delay", { right: m.at_risk.length ? pill("EXCEPTION", `${m.at_risk.length} at risk`) : pill("OK", "None at risk") },
        m.at_risk.length ? scroll(tbl(["Shipment", "Status", "Customer", "ETA", "Window end"], m.at_risk.map(r => h("tr", {}, h("td", {}, h("span", { class: "id" }, r.shipment_id)), h("td", {}, pill(r.status)), h("td", {}, r.customer_name), h("td", {}, fmt(r.eta_current)), h("td", {}, fmt(r.delivery_end))))))
          : bd(empty("Nothing at risk", "Run a Breakdown simulation on the Dispatch page to see an alert raised."))),
    ],
  };
};

PAGES.system = async () => {
  const s = await api("/api/admin/system", undefined, { poll: true });
  const lastEv = await api("/api/admin/last-event");
  const dbi = s.db || {};
  const files = [
    ["sql/01_schema.sql", "Tables, constraints, indexes"], ["sql/02_functions_triggers.sql", "Functions and triggers"], ["sql/03_views.sql", "Views"], ["sql/04_grants.sql", "Roles and privileges"],
    ["sql/demo_queries.sql", "Queries to run in pgAdmin / psql"], ["logistics/api.py", "The SQL behind every screen"], ["logistics/consumers/", "SQL written by each event consumer"], ["logistics/db.py", "Connections, transactions, audit actor"],
  ];
  return {
    title: "System", sub: "What runs behind the shop: the event stream, its consumers and the PostgreSQL server.", src: "logistics/api.py › admin_system()",
    body: [
      card("Maintenance & demonstrations", {}, bd(h("div", { class: "actions" },
        h("button", { class: "btn", onclick: async () => { const r = await api("/api/admin/verify"); modal("Verification against the event log", verifyView(r)); } }, "Verify tables vs event log"),
        h("button", { class: "btn", onclick: async () => { const r = await api("/api/admin/rebuild", {}); modal("Rebuild from the event log", rebuildView(r)); show("system", true); } }, "Rebuild derived tables"),
        h("button", { class: "btn", onclick: () => lastEv.event_id ? act(`redeliver ${lastEv.event_type} (${lastEv.event_id})`, () => api("/api/admin/redeliver", { event_id: lastEv.event_id })) : null }, "Redeliver last event (duplicate)"),
        h("button", { class: "btn", onclick: () => act("retention job", () => api("/api/admin/purge-pings", { days: 90 })) }, "Purge GPS pings > 90 days"),
        h("button", { class: "btn danger", onclick: async () => { if (confirm("Drop and recreate the database and reload the demo data (~20 s)?")) { await api("/api/admin/reset", {}); toast("Resetting - watch the terminal", "info"); } } }, "Reset demo database")))),
      card("Event consumers", { hint: "one thread + one PostgreSQL connection each" }, scroll(tbl(["Component", "Reads", ["Lag", "num"], ["Applied", "num"], ["Dups", "num"], ["Retries", "num"], ["Refused", "num"], ["Dead", "num"]],
          s.consumers.map(c => h("tr", {}, h("td", {}, h("span", { class: "dot", style: `background:${c.busy ? "#f59e0b" : c.alive ? "#22c55e" : "#ef4444"}` }), c.group), h("td", { class: "dim" }, c.topics.join(", ")), h("td", { class: "num" }, c.lag), h("td", { class: "num" }, c.stats.handled), h("td", { class: "num" }, c.stats.duplicates), h("td", { class: "num" }, c.stats.retries), h("td", { class: "num" }, c.stats.soft_fails), h("td", { class: "num" }, c.stats.dead)))))),
      h("div", { class: "grid2" },
        card("Event stream topics", { hint: `outbox waiting: ${s.outbox_pending} · dead letters: ${s.dead_letters.length}` }, scroll(tbl(["Topic", ["Messages", "num"], "Partition end offsets"], s.topics.map(t => h("tr", {}, h("td", {}, h("span", { class: "id" }, t.topic)), h("td", { class: "num" }, t.messages), h("td", { class: "dim", style: "font-family:Consolas,monospace;font-size:12px" }, t.partitions.map((e, i) => `p${i}:${e}`).join("  "))))))),
        card("PostgreSQL server", { hint: "live from the database" }, bd(h("div", { class: "kv", style: "grid-template-columns:150px 1fr;gap:8px 12px" },
          h("span", { class: "k" }, "Engine"), h("span", {}, `PostgreSQL ${dbi.version || "?"}`), h("span", { class: "k" }, "Database"), h("span", {}, `logistics · ${dbi.size || "?"}`),
          h("span", { class: "k" }, "Schema objects"), h("span", {}, `${dbi.tables ?? "?"} tables · ${dbi.views ?? "?"} views · ${dbi.functions ?? "?"} functions · ${dbi.triggers ?? "?"} triggers`),
          h("span", { class: "k" }, "Open connections"), h("span", {}, s.connections.length ? s.connections.map(c => `${c.application_name || "app"} ×${c.n}`).join(" · ") : "-"),
          h("span", { class: "k" }, "Watch it live"), h("span", {}, "pgAdmin at ", h("a", { href: "http://127.0.0.1:5050", target: "_blank" }, "127.0.0.1:5050"), " · or ", h("code", {}, "python db_watch.py"))))),
      ),
      card("Where the database lives in the code", { hint: "details: DATABASE_IN_THE_CODE.md" }, bd(h("div", { style: "display:grid;gap:7px" }, ...files.map(([f, d]) => h("div", { class: "row", style: "flex-wrap:nowrap;gap:12px" }, h("span", { class: "src", style: "min-width:230px" }, f), h("span", { class: "dim" }, d)))))),
    ],
  };
};

function verifyView(r) {
  return h("div", {}, r.ok ? pill("OK", "✔ derived tables match the event log") : pill("MISMATCH", "✗ derived tables differ from the event log"), h("div", { class: "small muted", style: "margin:8px 0" }, `${r.events_replayed} events replayed · ${r.shipments_in_log} shipments · ${r.ms} ms`),
    r.mismatches.length ? h("table", { class: "t" }, h("thead", {}, h("tr", {}, ["shipment", "field", "in table", "from event log"].map(x => h("th", {}, x)))), h("tbody", {}, ...r.mismatches.map(m => h("tr", {}, h("td", {}, m.shipment_id), h("td", {}, m.field), h("td", { style: "color:var(--bad)" }, String(m.live)), h("td", { style: "color:var(--ok)" }, String(m.replayed)))))) : null,
    r.inventory_mismatches.length ? h("p", { style: "color:var(--bad)" }, `${r.inventory_mismatches.length} inventory lines where balance ≠ ledger sum`) : null);
}
function rebuildView(r) {
  return h("div", {}, r.after.ok ? pill("OK", "✔ rebuilt - matches the event log") : pill("MISMATCH", "✗ still different"), h("div", { class: "kv", style: "margin-top:10px" },
    h("span", { class: "k" }, "before"), h("span", {}, r.before.ok ? "consistent" : `${r.before.mismatches} field mismatches`), h("span", { class: "k" }, "rebuilt"), h("span", {}, `${r.rebuilt_shipments} shipments, ${r.rebuilt_balances} stock balances in ${r.ms} ms`), h("span", { class: "k" }, "after"), h("span", {}, `${r.after.events_replayed} events replayed, ${r.after.mismatch_count} mismatches`)));
}

/* ---------------- shell ---------------- */
const shell = p => [
  h("header", { class: "top" }, h("div", {}, h("h1", {}, p.title), p.sub ? h("div", { class: "sub" }, p.sub) : null),
    h("div", { class: "tools" }, p.src ? srcChip(p.src) : null, h("span", { class: "live", title: "Refreshes every 4 seconds" }, h("i"), "Live"))),
  h("div", { class: "content" }, ...p.body),
];

async function show(page, quiet) {
  A.page = page; renderSide();
  const active = document.activeElement;
  if (quiet && active && /INPUT|SELECT|TEXTAREA/.test(active.tagName) && main().contains(active)) return;       // don't clobber typing
  const mine = ++A.seq;
  try {
    const p = await PAGES[page]();
    if (mine !== A.seq) return;
    main().replaceChildren(...shell(p));
    document.title = `${p.title} · LogiShop Back Office`;
  } catch (e) {
    if (mine !== A.seq) return;
    if (e.status === 401 || e.status === 403) return loginView("Your session has ended - please sign in again.");
    main().replaceChildren(...shell({ title: "Something went wrong", body: [card(null, {}, empty("Could not load this page", e.message), bd(h("button", { class: "btn", onclick: () => show(page) }, "Try again")))] }));
  }
}
function route() { const p = (location.hash.replace(/^#\//, "") || "dashboard"); show(PAGES[p] ? p : "dashboard"); }
window.addEventListener("hashchange", route);
(async () => {
  let me = null;
  try { me = (await api("/api/auth/me")).user; } catch (e) { return loginView("Cannot reach the server: " + e.message); }
  if (!me || me.role !== "ADMIN") return loginView(me ? `You are signed in as ${me.name} (${String(me.role).toLowerCase()}). Sign in with an administrator account.` : "");
  A.user = me; route();
  A.timer = setInterval(() => { if (!document.hidden && !document.querySelector(".overlay")) show(A.page, true); }, 4000);
})();
