"use strict";
/* LogiShop - the customer storefront */
const ST = { user: null, cart: { items: [], count: 0, subtotal: 0, delivery_fee: 0, total: 0 }, cats: [], unread: 0, notifs: [], timer: null, zones: [] };
const page = () => $("#page");

/* ------------------------------ data ------------------------------ */
async function refreshUser() {
  try { ST.user = (await api("/api/auth/me")).user; } catch { ST.user = null; }
  if (ST.user && ST.user.type === "STAFF") ST.user = null;
  if (ST.user) { try { ST.cart = await api("/api/cart"); } catch { } try { const n = await api("/api/notifications"); ST.unread = n.unread; ST.notifs = n.items; } catch { } }
  else { ST.cart = { items: [], count: 0, subtotal: 0, delivery_fee: 0, total: 0 }; ST.unread = 0; }
  renderHeader();
}
function parseHash() {
  const raw = location.hash.replace(/^#/, "") || "/";
  const [path, qs] = raw.split("?");
  return { path, q: Object.fromEntries(new URLSearchParams(qs || "")) };
}
const go = p => { location.hash = p; };

/* ------------------------------ header ------------------------------ */
function renderHeader() {
  const search = h("input", { placeholder: "Search for earbuds, chargers, yoga mat ...", value: parseHash().q.q || "", onkeydown: e => { if (e.key === "Enter") doSearch(); } });
  const doSearch = () => go(`/?q=${encodeURIComponent(search.value.trim())}`);
  let menu = null;
  const acct = ST.user
    ? h("div", { style: "position:relative" }, h("button", { class: "nav-btn", onclick: e => { e.stopPropagation(); toggleMenu(); } }, `👤 ${ST.user.name.split(" ")[0]} ▾`))
    : h("button", { class: "nav-btn", onclick: () => go("/login") }, "Sign in");
  function toggleMenu() {
    if (menu) { menu.remove(); menu = null; return; }
    menu = h("div", { class: "dropdown" }, h("div", { class: "small muted", style: "padding:6px 12px" }, `Signed in as ${ST.user.name}`),
      h("button", { onclick: () => go("/orders") }, "📦 My orders"), h("button", { onclick: () => go("/account") }, "📍 Addresses"),
      h("button", { onclick: async () => { await api("/api/auth/logout", {}); await refreshUser(); go("/"); toast("Signed out", "info"); } }, "Sign out"));
    acct.append(menu); setTimeout(() => document.addEventListener("click", () => { menu && menu.remove(); menu = null; }, { once: true }), 0);
  }
  $("#topbar").replaceChildren(
    h("a", { class: "logo", href: "#/" }, "🛍️ Logi", h("b", {}, "Shop")),
    h("div", { class: "search" }, search, h("button", { onclick: doSearch }, "Search")),
    h("div", { class: "grow" }),
    ST.user ? h("button", { class: "nav-btn", title: "Notifications", onclick: showNotifs }, "🔔", ST.unread ? h("span", { class: "badge" }, ST.unread) : null) : null,
    h("button", { class: "nav-btn", onclick: () => go("/orders") }, "Orders"),
    h("button", { class: "nav-btn", onclick: () => go("/cart") }, "🛒 Cart", ST.cart.count ? h("span", { class: "badge" }, ST.cart.count) : null),
    acct);
  const cur = parseHash().q.cat || "";
  const tops = ST.cats.filter(c => c.parent_id == null);
  $("#cats").replaceChildren(h("button", { class: "cat" + (cur === "" && parseHash().path === "/" ? " on" : ""), onclick: () => go("/") }, "All"),
    ...tops.map(c => h("button", { class: "cat" + (cur === c.slug ? " on" : ""), onclick: () => go(`/?cat=${c.slug}`) }, c.name)));
}
async function showNotifs() {
  const n = await api("/api/notifications");
  const list = n.items.length ? n.items.map(x => h("div", { class: "notif" + (x.status === "SENT" ? " unread" : "") },
    h("div", {}, x.message), h("div", { class: "small muted" }, fmt(x.created_at, true), x.order_id ? ` · ${x.order_id}` : ""))) : [h("div", { class: "muted" }, "No notifications yet.")];
  modal("Notifications", h("div", {}, ...list), { width: "560px" });
  if (n.unread) { await api("/api/notifications/read", {}); ST.unread = 0; renderHeader(); }
}

/* ------------------------------ product cards ------------------------------ */
function stockLabel(n) { return n <= 0 ? h("span", { class: "stock out" }, "Out of stock") : n <= 5 ? h("span", { class: "stock low" }, `Only ${n} left`) : h("span", { class: "stock ok" }, "In stock"); }
async function addToCart(sku, qty = 1, go_ = null) {
  if (!ST.user) { toast("Please sign in to add items to your cart", "info"); return go(`/login?next=${encodeURIComponent(location.hash.slice(1) || "/")}`); }
  try { ST.cart = await api("/api/cart", { sku_id: sku, quantity: qty }); renderHeader(); toast("Added to cart", "ok", 1800); if (go_) go(go_); }
  catch (e) { toast(e.message, "warn"); }
}
function productCard(p) {
  return h("div", { class: "pcard", onclick: () => go(`/p/${p.sku_id}`) },
    h("div", { class: "pimg", style: `background:${p.color}` }, p.emoji, p.discount_pct > 0 ? h("span", { class: "off" }, `${p.discount_pct}% off`) : null),
    h("div", { class: "pbody" }, h("div", { class: "pbrand" }, p.brand), h("div", { class: "pname" }, p.name),
      h("div", {}, h("span", { class: "price" }, rupee(p.price)), h("span", { class: "mrp" }, rupee(p.mrp))), stockLabel(p.in_stock),
      h("div", { style: "margin-top:auto;padding-top:8px" }, h("button", { class: "btn primary block", disabled: p.in_stock <= 0, onclick: e => { e.stopPropagation(); addToCart(p.sku_id); } }, p.in_stock <= 0 ? "Unavailable" : "Add to cart"))));
}

/* ------------------------------ pages ------------------------------ */
async function pageHome(q) {
  const params = new URLSearchParams({ q: q.q || "", category: q.cat || "", sort: q.sort || "popular" });
  const prods = await api("/api/shop/products?" + params);
  const catName = (ST.cats.find(c => c.slug === q.cat) || {}).name;
  const sort = h("select", { class: "in", onchange: () => go(`/?${new URLSearchParams({ ...(q.q ? { q: q.q } : {}), ...(q.cat ? { cat: q.cat } : {}), sort: sort.value })}`) },
    [["popular", "Popularity"], ["price_asc", "Price: low to high"], ["price_desc", "Price: high to low"], ["discount", "Biggest discount"], ["new", "Newest"]].map(([v, l]) => h("option", { value: v, selected: v === (q.sort || "popular") }, l)));
  page().replaceChildren(
    !q.q && !q.cat ? h("div", { class: "hero" }, h("div", {}, h("h1", {}, "Big savings, delivered live 🚚"), h("div", {}, "Free delivery on orders above ₹500. Track your parcel on the map from warehouse to doorstep."),
      h("div", { class: "row", style: "margin-top:12px" }, h("button", { class: "btn accent big", onclick: () => go("/?sort=discount") }, "See top deals"))), h("div", { class: "hero-art" }, "🎧📱⌚")) : null,
    h("div", { class: "row sp", style: "margin-bottom:12px" }, h("h2", { style: "margin:0" }, q.q ? `Results for “${q.q}”` : (catName || "Popular products"), h("span", { class: "muted small" }, `  ${prods.length} item${prods.length === 1 ? "" : "s"}`)), h("div", { class: "row" }, "Sort by", sort)),
    prods.length ? h("div", { class: "pgrid" }, ...prods.map(productCard)) : h("div", { class: "empty" }, h("div", { class: "empty-art" }, "🔍"), h("div", {}, "No products matched your search.")));
}

async function pageProduct(sku) {
  const p = await api("/api/shop/products/" + sku);
  let qty = 1;
  const qv = h("span", {}, "1");
  const set = d => { qty = Math.max(1, Math.min(Math.min(10, p.in_stock), qty + d)); qv.textContent = qty; };
  page().replaceChildren(
    h("div", { class: "muted small", style: "margin-bottom:10px" }, h("a", { href: "#/" }, "Home"), " › ", h("a", { href: `#/?cat=${p.category_slug}` }, p.category), " › ", p.name),
    h("div", { class: "card pdp" },
      h("div", { class: "bigimg", style: `background:${p.color}` }, p.emoji),
      h("div", {}, h("div", { class: "pbrand" }, p.brand), h("h1", {}, p.name),
        h("div", { style: "margin:6px 0 10px" }, h("span", { class: "price", style: "font-size:28px" }, rupee(p.price)), h("span", { class: "mrp", style: "font-size:16px" }, rupee(p.mrp)), p.discount_pct > 0 ? h("span", { class: "pill st-DELIVERED", style: "margin-left:10px" }, `${p.discount_pct}% off`) : null),
        h("div", { style: "margin-bottom:10px" }, stockLabel(p.in_stock), h("span", { class: "muted small" }, "   ·   Free delivery above ₹500   ·   7-day returns")),
        h("p", {}, p.long_description), h("div", { class: "kv small", style: "margin:12px 0" }, h("span", { class: "k" }, "SKU"), h("span", {}, `${p.sku_id} (${p.sku_code})`), h("span", { class: "k" }, "Weight"), h("span", {}, `${p.weight_kg} kg`), h("span", { class: "k" }, "Category"), h("span", {}, p.category)),
        p.in_stock > 0 ? h("div", { class: "row" }, h("div", { class: "qty" }, h("button", { onclick: () => set(-1) }, "−"), qv, h("button", { onclick: () => set(1) }, "+")),
          h("button", { class: "btn primary big", onclick: () => addToCart(p.sku_id, qty) }, "Add to cart"), h("button", { class: "btn accent big", onclick: () => addToCart(p.sku_id, qty, "/cart") }, "Buy now")) : h("div", { class: "pill st-CANCELLED" }, "Currently unavailable"))),
    p.related.length ? h("div", {}, h("h2", {}, "You may also like"), h("div", { class: "pgrid" }, ...p.related.map(r => productCard({ ...r, brand: "", discount_pct: Math.round((1 - r.price / r.mrp) * 100), in_stock: 10 })))) : null);
}

async function pageCart() {
  if (!ST.user) return go("/login?next=/cart");
  ST.cart = await api("/api/cart"); renderHeader();
  const c = ST.cart;
  if (!c.items.length) return page().replaceChildren(h("div", { class: "card empty" }, h("div", { class: "empty-art" }, "🛒"), h("h2", {}, "Your cart is empty"), h("button", { class: "btn primary", onclick: () => go("/") }, "Start shopping")));
  const setQty = async (sku, q) => { try { ST.cart = await api("/api/cart", { sku_id: sku, quantity: q, mode: "set" }); pageCart(); } catch (e) { toast(e.message, "warn"); } };
  page().replaceChildren(h("h1", {}, `Shopping cart (${c.count})`), h("div", { class: "grid2", style: "grid-template-columns:1.6fr 1fr;align-items:start" },
    h("div", { class: "card" }, ...c.items.map(i => h("div", { class: "cart-line" },
      h("div", { class: "th", style: `background:${i.color}` }, i.emoji),
      h("div", {}, h("a", { href: `#/p/${i.sku_id}`, style: "font-weight:600" }, i.name), h("div", { class: "small muted" }, i.brand, " · ", rupee(i.price), " each"), i.in_stock < i.quantity ? h("div", { class: "stock out" }, `Only ${i.in_stock} in stock`) : null,
        h("button", { class: "btn sm", style: "margin-top:6px", onclick: async () => { ST.cart = await api("/api/cart/remove", { sku_id: i.sku_id }); pageCart(); } }, "Remove")),
      h("div", { class: "qty" }, h("button", { onclick: () => setQty(i.sku_id, i.quantity - 1) }, "−"), h("span", {}, i.quantity), h("button", { onclick: () => setQty(i.sku_id, i.quantity + 1) }, "+")),
      h("div", { style: "font-weight:800;min-width:90px;text-align:right" }, rupee(i.line_total))))),
    h("div", { class: "card" }, h("h2", {}, "Price details"),
      h("div", { class: "sumrow" }, h("span", {}, `Subtotal (${c.count} items)`), h("span", {}, rupee(c.subtotal))),
      h("div", { class: "sumrow" }, h("span", {}, "Delivery fee"), h("span", {}, c.delivery_fee ? rupee(c.delivery_fee) : h("b", { style: "color:var(--ok)" }, "FREE"))),
      c.delivery_fee ? h("div", { class: "small muted" }, `Add ${rupee(500 - c.subtotal)} more for free delivery`) : null,
      h("div", { class: "sumrow total" }, h("span", {}, "Total"), h("span", {}, rupee(c.total))),
      h("button", { class: "btn accent big block", style: "margin-top:12px", onclick: () => go("/checkout") }, "Proceed to checkout"))));
}

async function pageCheckout() {
  if (!ST.user) return go("/login?next=/checkout");
  ST.cart = await api("/api/cart"); const c = ST.cart;
  if (!c.items.length) return go("/cart");
  let addrs = await api("/api/addresses"); ST.zones = ST.zones.length ? ST.zones : await api("/api/zones");
  let addrId = (addrs.find(a => a.is_default) || addrs[0] || {}).address_id, method = "UPI";
  const body = h("div", {});
  const render = () => {
    const addrBox = h("div", {}, ...addrs.map(a => h("label", { class: "choice" + (a.address_id === addrId ? " on" : "") }, h("input", { type: "radio", name: "addr", checked: a.address_id === addrId, onchange: () => { addrId = a.address_id; render(); } }),
      h("div", {}, h("b", {}, `${a.recipient} `), h("span", { class: "pill" }, a.label), a.is_default ? h("span", { class: "pill st-DELIVERED", style: "margin-left:6px" }, "default") : null,
        h("div", {}, `${a.line1}, ${a.locality}, ${a.city} ${a.pincode}`), h("div", { class: "small muted" }, `📞 ${a.phone}`)))),
      h("button", { class: "btn sm", onclick: addAddress }, "+ Add a new address"));
    const pay = h("div", {}, ...[["UPI", "UPI", "Pay instantly with any UPI app (simulated)"], ["CARD", "Credit / Debit card", "Simulated - no card details are collected or stored"], ["NETBANKING", "Net banking", "Simulated"], ["COD", "Cash on delivery", "Pay when your order arrives"]]
      .map(([v, t, d]) => h("label", { class: "choice" + (method === v ? " on" : "") }, h("input", { type: "radio", name: "pay", checked: method === v, onchange: () => { method = v; render(); } }), h("div", {}, h("b", {}, t), h("div", { class: "small muted" }, d)))));
    body.replaceChildren(h("div", { class: "grid2", style: "grid-template-columns:1.5fr 1fr;align-items:start" },
      h("div", {}, h("div", { class: "card" }, h("h2", {}, "1 · Delivery address"), addrs.length ? addrBox : h("div", {}, h("p", { class: "muted" }, "Add a delivery address to continue."), h("button", { class: "btn primary", onclick: addAddress }, "+ Add address"))),
        h("div", { class: "card" }, h("h2", {}, "2 · Payment method"), pay)),
      h("div", { class: "card" }, h("h2", {}, "Order summary"), ...c.items.map(i => h("div", { class: "sumrow small" }, h("span", {}, `${i.emoji} ${i.name} × ${i.quantity}`), h("span", {}, rupee(i.line_total)))),
        h("hr", { style: "border:0;border-top:1px solid var(--line)" }),
        h("div", { class: "sumrow" }, h("span", {}, "Subtotal"), h("span", {}, rupee(c.subtotal))), h("div", { class: "sumrow" }, h("span", {}, "Delivery"), h("span", {}, c.delivery_fee ? rupee(c.delivery_fee) : "FREE")),
        h("div", { class: "sumrow total" }, h("span", {}, "Total"), h("span", {}, rupee(c.total))),
        h("button", { class: "btn accent big block", id: "placeBtn", style: "margin-top:12px", disabled: !addrId, onclick: place }, `Place order · ${rupee(c.total)}`))));
  };
  async function place() {
    const b = $("#placeBtn"); b.disabled = true; b.innerHTML = '<span class="spinner"></span> Placing order ...';
    try { const r = await api("/api/checkout", { address_id: addrId, payment_method: method }); await refreshUser(); go(`/success/${r.order_id}`); }
    catch (e) { toast(e.message, "warn", 6000); b.disabled = false; b.textContent = `Place order · ${rupee(c.total)}`; }
  }
  function addAddress() {
    const zone = h("select", { onchange: () => { pin.value = (ST.zones.find(z => z.geofence_id === zone.value) || {}).pincode || ""; } }, ST.zones.map(z => h("option", { value: z.geofence_id }, z.name.replace(" delivery zone", ""))));
    const pin = h("input", { value: ST.zones[0].pincode, maxlength: 6 });
    const f = { recipient: h("input", { value: ST.user.name }), phone: h("input", { value: "+91-9" + String(Math.floor(Math.random() * 1e9)).padStart(9, "0") }), line1: h("input", { placeholder: "House no., street" }), label: h("select", {}, ["Home", "Work", "Other"].map(x => h("option", {}, x))) };
    const m = modal("Add delivery address", h("div", { class: "grid2" }, h("label", { class: "f" }, "Recipient", f.recipient), h("label", { class: "f" }, "Phone", f.phone), h("label", { class: "f", style: "grid-column:1/-1" }, "Address", f.line1),
      h("label", { class: "f" }, "Area (we deliver to)", zone), h("label", { class: "f" }, "Pincode", pin), h("label", { class: "f" }, "Save as", f.label),
      h("div", { style: "grid-column:1/-1" }, h("button", { class: "btn primary block", onclick: async () => {
        try { addrs = await api("/api/addresses", { zone_id: zone.value, pincode: pin.value, recipient: f.recipient.value, phone: f.phone.value, line1: f.line1.value, label: f.label.value }); addrId = addrs[addrs.length - 1].address_id; m.remove(); render(); }
        catch (e) { toast(e.message, "warn"); } } }, "Save address"))));
  }
  page().replaceChildren(h("h1", {}, "Checkout"), body); render();
}

async function pageSuccess(oid) {
  const d = await api("/api/orders/" + oid);
  page().replaceChildren(h("div", { class: "card", style: "text-align:center;padding:34px" }, h("div", { style: "font-size:64px" }, "🎉"), h("h1", {}, "Order placed!"),
    h("p", {}, `Thank you ${ST.user.name.split(" ")[0]}. Your order `, h("b", {}, oid), " is ", pill(d.order.status), d.shipment ? ` and will be shipped from ${d.shipment.warehouse_name}.` : "."),
    h("div", { class: "muted" }, `Total ${rupee(d.order.total)} · ${d.order.payment_method} · payment ${(d.payments[0] || {}).status || ""}`),
    h("div", { class: "row", style: "justify-content:center;margin-top:16px" }, h("button", { class: "btn primary big", onclick: () => go(`/order/${oid}`) }, "Track my order"), h("button", { class: "btn big", onclick: () => go("/") }, "Continue shopping"))));
}

async function pageOrders() {
  if (!ST.user) return go("/login?next=/orders");
  const os = await api("/api/orders");
  page().replaceChildren(h("h1", {}, "My orders"), os.length ? h("div", {}, ...os.map(o => h("div", { class: "card ordercard", onclick: () => go(`/order/${o.order_id}`) },
    h("div", { class: "em" }, o.emojis || "📦"), h("div", { class: "grow" }, h("div", {}, h("b", {}, o.order_id), "  ", pill(o.order_status), o.at_risk && o.order_status !== "DELIVERED" ? h("span", { class: "pill risk", style: "margin-left:6px" }, "may be delayed") : null),
      h("div", { class: "small muted" }, (o.items_text || "").slice(0, 90)), h("div", { class: "small muted" }, `Placed ${fmt(o.placed_at, true)} · ${o.units} item(s)`)),
    h("div", { class: "right" }, h("div", { style: "font-weight:800" }, rupee(o.total)), h("div", { class: "small muted" }, o.order_status === "DELIVERED" ? "Delivered" : o.order_status === "CANCELLED" ? "Refunded" : o.eta_current ? `ETA ${fmt(o.eta_current)}` : ""))))) :
    h("div", { class: "card empty" }, h("div", { class: "empty-art" }, "📦"), h("div", {}, "You have not placed any orders yet.")));
}

/* ---------------- order tracking ---------------- */
const STEPS = [["PLACED", "Placed", "🧾"], ["CONFIRMED", "Confirmed", "✅"], ["SHIPPED", "Shipped", "📦"], ["OUT_FOR_DELIVERY", "Out for delivery", "🚚"], ["DELIVERED", "Delivered", "🏠"]];
function eventText(e, d) {
  const p = e.payload || {};
  return ({
    SHIPMENT_CREATED: `Order confirmed - being prepared at ${d.shipment ? d.shipment.warehouse_name : "the warehouse"}`, VEHICLE_ASSIGNED: `Delivery partner assigned (${p.vehicle_id})`,
    PICKED_UP: "Picked up from the warehouse", IN_TRANSIT: "On the way to you", ARRIVED_AT_ZONE: "Arrived in your area", OUT_FOR_DELIVERY: "Out for delivery - arriving soon",
    DELIVERED: "Delivered. Enjoy!", EXCEPTION: "Delivery delayed" + (p.reason ? `: ${String(p.reason).replace(/_/g, " ").toLowerCase()}` : ""), EXCEPTION_RISK: `Possible delay detected: ${p.reason || "unusual trip behaviour"}`,
    DELIVERY_RESCHEDULED: `Delivery rescheduled to ${fmt(p.new_start, true)} – ${fmt(p.new_end)}`, REQUEST_ACCEPTED: "Your request was accepted", REQUEST_REJECTED: `Request declined: ${p.reason || ""}`, REQUEST_UPDATED: `Return ${String(p.status || "").replace(/_/g, " ").toLowerCase()}`
  })[e.event_type] || e.event_type;
}
async function pageOrder(oid) {
  if (!ST.user) return go(`/login?next=/order/${oid}`);
  const root = h("div", {}); page().replaceChildren(root);
  const draw = async (poll) => {
    let d; try { d = await api("/api/orders/" + oid, undefined, { poll }); } catch (e) { root.replaceChildren(h("div", { class: "card empty" }, e.message)); return; }
    const o = d.order, s = d.shipment;
    const idx = Math.max(0, STEPS.findIndex(x => x[0] === (o.status === "DELAYED" ? "SHIPPED" : o.status)));
    const cancelled = o.status === "CANCELLED";
    const stepper = cancelled ? h("div", { class: "pill st-CANCELLED", style: "margin:10px 0" }, `Order cancelled - ${o.cancel_reason}. Your payment was refunded.`) :
      h("div", { class: "stepper" }, ...STEPS.map((st, i) => h("div", { class: "step " + (i < idx || o.status === "DELIVERED" ? "done" : i === idx ? (o.status === "DELAYED" ? "bad" : "now") : "") }, h("div", { class: "dot" }, i < idx || o.status === "DELIVERED" ? "✓" : st[2]), st[1])));
    let eta = null;
    if (s && !cancelled) {
      eta = h("div", { class: "eta-box" }, o.status === "DELIVERED" ? h("div", {}, h("div", { class: "muted small" }, "Delivered"), h("div", { class: "eta-time" }, fmt(s.actual_delivery_at, true))) :
        h("div", {}, h("div", { class: "muted small" }, s.at_risk ? "⚠️ Estimated arrival (delay expected)" : "Estimated arrival"), h("div", { class: "eta-time" }, fmt(s.eta_current)),
          h("div", { class: "small muted" }, `Delivery window ${fmt(s.delivery_start, true)} – ${fmt(s.delivery_end)}`, s.vehicle_id ? ` · 🚚 ${s.registration_no}` : "")));
    }
    const map = s && d.warehouse ? drawMap({ warehouse: d.warehouse, dest: { lat: s.dest_lat, lon: s.dest_lon }, trail: d.trail, current: s.current_lat != null ? { lat: s.current_lat, lon: s.current_lon } : null, status: o.status, zone: d.zone }) : null;
    const tl = h("ul", { class: "timeline" }, ...d.events.map((e, i, a) => h("li", { class: /EXCEPTION|REJECTED/.test(e.event_type) ? "warn" : i === a.length - 1 && o.status !== "DELIVERED" ? "now" : "done" }, h("div", { style: "font-weight:600" }, eventText(e, d)), h("div", { class: "small muted" }, fmt(e.event_time, true)))).reverse());
    const actions = [];
    if (d.can_reschedule) actions.push(h("button", { class: "btn", onclick: () => rescheduleDialog(oid, root, draw) }, "📅 Reschedule delivery"));
    if (d.can_return) actions.push(h("button", { class: "btn", onclick: () => returnDialog(oid, d, draw) }, "↩️ Return items"));
    root.replaceChildren(
      h("div", { class: "row sp" }, h("h1", { style: "margin:0" }, `Order ${o.order_id}`), h("div", {}, pill(o.status))),
      h("div", { class: "muted small", style: "margin-bottom:6px" }, `Placed ${fmt(o.placed_at, true)} · ${o.payment_method} · ${s ? "Shipment " + s.shipment_id : "no shipment yet"}`), stepper,
      h("div", { class: "tracking-grid" },
        h("div", {}, eta, map ? h("div", { class: "card", style: "padding:10px" }, h("div", { class: "row sp", style: "margin:0 4px 8px" }, h("b", {}, "Live tracking"), s.current_lat != null ? h("span", { class: "small muted" }, "vehicle position updates as the driver app reports GPS") : h("span", { class: "small muted" }, "the vehicle has not left the warehouse yet")), map) : null,
          h("div", { class: "card" }, h("h2", {}, "Delivery updates"), tl.children.length ? tl : h("div", { class: "muted" }, "Waiting for the first update ..."))),
        h("div", {}, h("div", { class: "card" }, h("h2", {}, "Items"), ...d.lines.map(l => h("div", { class: "row", style: "margin-bottom:8px" }, h("div", { style: `background:${l.color};width:42px;height:42px;border-radius:10px;display:grid;place-items:center;font-size:22px` }, l.emoji), h("div", { class: "grow" }, h("div", { style: "font-weight:600" }, l.name), h("div", { class: "small muted" }, `${l.quantity} × ${rupee(l.unit_price)}`)), h("b", {}, rupee(l.line_total)))),
          h("div", { class: "sumrow total" }, h("span", {}, "Order total"), h("span", {}, rupee(o.total)))),
          h("div", { class: "card" }, h("h2", {}, "Payment"), ...d.payments.map(p => h("div", { class: "sumrow" }, h("span", {}, `${p.kind === "REFUND" ? "Refund" : "Payment"} · ${p.method}`, h("div", { class: "small muted mono" }, p.reference || "to be collected on delivery")), h("span", {}, h("b", {}, (p.kind === "REFUND" ? "−" : "") + rupee(p.amount)), " ", pill(p.status))))),
          h("div", { class: "card" }, h("h2", {}, "Delivering to"), h("div", {}, o.ship_recipient), h("div", { class: "muted small" }, o.ship_address), h("div", { class: "muted small" }, `📞 ${o.ship_phone}`)),
          actions.length ? h("div", { class: "card" }, h("h2", {}, "Need changes?"), h("div", { class: "row" }, ...actions), h("div", { class: "small muted", style: "margin-top:6px" }, "Reschedule until the order is out for delivery · returns within 7 days of delivery")) : null,
          d.requests.length ? h("div", { class: "card" }, h("h2", {}, "Your requests"), ...d.requests.map(r => h("div", { class: "sumrow" }, h("span", {}, `${r.request_id} · ${r.request_type === "RETURN" ? "Return" : "Reschedule"}`, r.items.length ? h("div", { class: "small muted" }, r.items.map(i => `${i.quantity}× ${i.sku_id}`).join(", ")) : null, r.pickup_scheduled_at ? h("div", { class: "small muted" }, `Pickup ${fmt(r.pickup_scheduled_at, true)}`) : null), pill(r.status)))) : null,
          d.notifications.length ? h("div", { class: "card" }, h("h2", {}, "Messages"), ...d.notifications.slice(0, 5).map(n => h("div", { class: "notif" }, n.message, h("div", { class: "small muted" }, fmt(n.created_at, true))))) : null)));
    return !["DELIVERED", "CANCELLED"].includes(o.status);
  };
  await draw(false);
  clearInterval(ST.timer);
  ST.timer = setInterval(async () => { if (!location.hash.startsWith(`#/order/${oid}`) || document.querySelector(".overlay")) return; await draw(true); }, 3000);
}
function rescheduleDialog(oid, root, redraw) {
  const t0 = new Date(Date.now() + 26 * 3600e3); t0.setHours(10, 0, 0, 0); const t1 = new Date(t0.getTime() + 3 * 3600e3);
  const a = h("input", { type: "datetime-local", value: isoLocalInput(t0) }), b = h("input", { type: "datetime-local", value: isoLocalInput(t1) }), why = h("input", { value: "I won't be home" });
  const m = modal("Reschedule delivery", h("div", {}, h("div", { class: "grid2" }, h("label", { class: "f" }, "New window starts", a), h("label", { class: "f" }, "…and ends", b), h("label", { class: "f", style: "grid-column:1/-1" }, "Reason", why)),
    h("button", { class: "btn primary block", style: "margin-top:14px", onclick: async () => {
      try { const r = await api(`/api/orders/${oid}/reschedule`, { start: inputToIso(a.value), end: inputToIso(b.value), reason: why.value }); m.remove();
        r.ok ? toast("Delivery rescheduled ✓", "ok") : toast("Could not reschedule: " + r.rejections.map(x => x.reason).join("; "), "warn", 7000); redraw(false); } catch (e) { toast(e.message, "warn"); } } }, "Confirm new time")));
}
function returnDialog(oid, d, redraw) {
  const rows = d.lines.map(l => ({ l, cb: h("input", { type: "checkbox" }), q: h("select", {}, Array.from({ length: l.quantity }, (_, i) => h("option", { value: i + 1 }, i + 1))), c: h("select", {}, ["GOOD", "OPENED", "DAMAGED"].map(x => h("option", {}, x))) }));
  const why = h("input", { placeholder: "Reason for return", value: "Not as expected" });
  const m = modal("Return items", h("div", {}, ...rows.map(r => h("div", { class: "row", style: "margin-bottom:8px" }, r.cb, h("span", { class: "grow" }, `${r.l.emoji} ${r.l.name}`), "qty", r.q, r.c)), h("label", { class: "f" }, "Reason", why),
    h("div", { class: "small muted", style: "margin:8px 0" }, "Items returned in good condition are refunded in full; damaged or opened items are inspected first."),
    h("button", { class: "btn primary block", onclick: async () => {
      const items = rows.filter(r => r.cb.checked).map(r => ({ sku_id: r.l.sku_id, quantity: +r.q.value, item_condition: r.c.value }));
      if (!items.length) return toast("Select at least one item", "warn");
      try { const r = await api(`/api/orders/${oid}/return`, { items, reason: why.value }); m.remove(); r.ok ? toast("Return requested - pickup scheduled ✓", "ok") : toast("Return declined: " + r.rejections.map(x => x.reason).join("; "), "warn", 7000); redraw(false); } catch (e) { toast(e.message, "warn"); } } }, "Request return")), { width: "560px" });
}

async function pageAccount() {
  if (!ST.user) return go("/login?next=/account");
  const addrs = await api("/api/addresses");
  page().replaceChildren(h("h1", {}, "My account"), h("div", { class: "card" }, h("h2", {}, "Saved addresses"), ...addrs.map(a => h("div", { class: "row sp", style: "padding:8px 0;border-bottom:1px solid var(--line)" },
    h("div", {}, h("b", {}, a.recipient), " ", h("span", { class: "pill" }, a.label), a.is_default ? h("span", { class: "pill st-DELIVERED", style: "margin-left:6px" }, "default") : null, h("div", { class: "small muted" }, `${a.line1}, ${a.locality}, ${a.city} ${a.pincode}`)),
    a.is_default ? null : h("button", { class: "btn sm", onclick: async () => { await api("/api/addresses/default", { address_id: a.address_id }); pageAccount(); } }, "Make default")))));
}

function pageLogin(q) {
  let mode = "login";
  const box = h("div", { class: "card login-box" });
  const draw = () => {
    const email = h("input", { class: "in", placeholder: "E-mail", value: mode === "login" ? "asha.rao@example.com" : "" }), pw = h("input", { class: "in", type: "password", placeholder: "Password", value: mode === "login" ? "demo123" : "" });
    const name = h("input", { class: "in", placeholder: "Full name" }), phone = h("input", { class: "in", placeholder: "Phone (+91-9XXXXXXXXX)" });
    const submit = async () => {
      try { await api(mode === "login" ? "/api/auth/login" : "/api/auth/register", mode === "login" ? { email: email.value, password: pw.value } : { name: name.value, email: email.value, password: pw.value, phone: phone.value });
        await refreshUser(); if (!ST.user) { toast("This is a customer sign-in. Staff use the back office or driver app.", "warn", 6000); return; } toast(`Welcome ${ST.user.name.split(" ")[0]}!`, "ok"); go(q.next || "/"); }
      catch (e) { toast(e.message, "warn"); } };
    box.replaceChildren(h("h2", {}, mode === "login" ? "Sign in" : "Create your account"), h("div", { style: "display:grid;gap:10px" }, mode === "register" ? [name, phone] : null, email, pw,
      h("button", { class: "btn primary big block", onclick: submit }, mode === "login" ? "Sign in" : "Create account"),
      h("div", { class: "small muted" }, mode === "login" ? "Demo customer: asha.rao@example.com / demo123" : "Passwords are stored only as salted PBKDF2 hashes."),
      h("button", { class: "btn block", onclick: () => { mode = mode === "login" ? "register" : "login"; draw(); } }, mode === "login" ? "New here? Create an account" : "I already have an account")));
  };
  draw(); page().replaceChildren(box);
}

/* ------------------------------ router ------------------------------ */
async function route() {
  clearInterval(ST.timer);
  const { path, q } = parseHash();
  renderHeader();
  window.scrollTo(0, 0);
  try {
    let m;
    if (path === "/") await pageHome(q);
    else if ((m = path.match(/^\/p\/(.+)$/))) await pageProduct(m[1]);
    else if (path === "/cart") await pageCart();
    else if (path === "/checkout") await pageCheckout();
    else if ((m = path.match(/^\/success\/(.+)$/))) await pageSuccess(m[1]);
    else if (path === "/orders") await pageOrders();
    else if ((m = path.match(/^\/order\/(.+)$/))) await pageOrder(m[1]);
    else if (path === "/account") await pageAccount();
    else if (path === "/login") pageLogin(q);
    else page().replaceChildren(h("div", { class: "card empty" }, "Page not found"));
  } catch (e) { if (e.status === 401) go("/login"); else { page().replaceChildren(h("div", { class: "card empty" }, "Something went wrong: " + e.message)); console.error(e); } }
}
window.addEventListener("hashchange", route);
(async () => {
  try { ST.cats = await api("/api/shop/categories"); } catch { }
  await refreshUser(); await route();
  setInterval(async () => { if (ST.user && !document.hidden) { try { const n = await api("/api/notifications", undefined, { poll: true }); if (n.unread !== ST.unread) { ST.unread = n.unread; renderHeader(); } } catch { } } }, 6000);
})();
