"use strict";
/* LogiShop driver app (mobile first): the person who moves the parcel */
const D = { user: null };

async function act(label, fn) {
  try {
    const r = await fn();
    if (r && r.rejections && r.rejections.length) toast(`✗ ${label}: refused - ${r.rejections.map(x => x.reason).join("; ")}`, "warn", 6500);
    else toast(`✓ ${label}`, "ok", 2200);
    load(true);
  } catch (e) { toast(`✗ ${e.message}`, "warn", 6000); }
}

function loginView() {
  const email = h("input", { class: "in", value: "ramesh@logistics.local" }), pw = h("input", { class: "in", type: "password", value: "driver123" });
  $("#app").replaceChildren(h("div", { class: "dr-wrap", style: "padding-top:12vh" }, h("div", { class: "job" }, h("h2", {}, "🚚 Driver sign-in"), h("div", { style: "display:grid;gap:10px" }, email, pw,
    h("button", { class: "btn go drbtn", onclick: async () => { try { const r = await api("/api/auth/login", { email: email.value, password: pw.value }); if (r.role !== "DRIVER") { toast("This account is not a driver", "warn"); await api("/api/auth/logout", {}); return; } location.reload(); } catch (e) { toast(e.message, "warn"); } } }, "Sign in"),
    h("div", { class: "small muted" }, "Demo drivers: ramesh@ / suresh@ / mahesh@ / kiran@logistics.local · driver123")))));
}

function jobCard(j, detail) {
  const sid = j.shipment_id;
  const post = (p, b) => () => api(`/api/driver/jobs/${sid}${p}`, b);
  const b = (label, fn, cls = "") => h("button", { class: `btn drbtn ${cls}`, onclick: () => act(`${sid}: ${label}`, fn) }, label);
  const sim = (label, body, cls = "") => h("button", { class: `btn drbtn ${cls}`, onclick: async () => { try { await api(`/api/driver/jobs/${sid}/drive`, body); toast(`Auto-drive started for ${sid}`, "info"); } catch (e) { toast(e.message, "warn", 6000); } } }, label);
  const map = j.dest_lat ? drawMap({ warehouse: null, dest: { lat: j.dest_lat, lon: j.dest_lon }, trail: [], current: j.current_lat != null ? { lat: j.current_lat, lon: j.current_lon } : null, status: j.status, height: 170 }) : null;
  return h("div", { class: "job" },
    h("div", { class: "row sp" }, h("b", { style: "font-size:17px" }, sid), h("div", {}, pill(j.status), j.at_risk ? h("span", { class: "pill risk", style: "margin-left:6px" }, "at risk") : null)),
    h("div", { style: "margin:8px 0" }, h("div", {}, `👤 ${j.customer_name}  ·  📞 ${j.customer_phone || ""}`), h("div", { class: "muted" }, `📍 ${j.destination_address}`), h("div", { class: "muted small" }, `From ${j.warehouse_name} · window ${fmt(j.delivery_start)}–${fmt(j.delivery_end)} · ETA ${fmt(j.eta_current)} · ${j.stops_remaining} stop(s) before`)),
    map,
    h("div", { class: "row", style: "margin-top:12px" },
      j.status === "CREATED" ? b("📦 Picked up parcels", post("/status", { status: "PICKED_UP" }), "go") : null,
      j.status === "PICKED_UP" ? b("🚦 Start trip", post("/status", { status: "IN_TRANSIT" }), "go") : null,
      ["IN_TRANSIT", "PICKED_UP"].includes(j.status) ? [b("📍 Move 25%", post("/move", { fraction: 0.25 })), b("📍 Move 60%", post("/move", { fraction: 0.6 })), b("📍 Move 95%", post("/move", { fraction: 0.95 }))] : null,
      j.status === "IN_TRANSIT" ? b("🚪 Out for delivery", post("/status", { status: "OUT_FOR_DELIVERY" })) : null,
      j.status === "OUT_FOR_DELIVERY" ? b("✅ Delivered (proof of delivery)", post("/status", { status: "DELIVERED" }), "go") : null,
      j.status !== "DELIVERED" ? b("⚠️ Report a problem", post("/status", { status: "EXCEPTION", reason: "DRIVER_REPORTED_ISSUE" }), "warnb") : null,
      j.status === "EXCEPTION" ? b("▶ Resume trip", post("/status", { status: "IN_TRANSIT" }), "go") : null),
    j.status !== "DELIVERED" && j.status !== "CREATED" ? h("div", { class: "row", style: "margin-top:10px" }, sim("🛣️ Auto-drive (live)", { mode: "live", until: "end" }), sim("⚡ Fast", { mode: "fast", until: "end" }), sim("⚠ Breakdown", { mode: "live", until: "end", anomaly: "breakdown" }),
      h("button", { class: "btn drbtn", onclick: () => api("/api/admin/simulate/stop", { shipment_id: sid }) }, "■ Stop")) : null);
}

async function load(quiet) {
  let d; try { d = await api("/api/driver/jobs", undefined, { poll: quiet }); } catch (e) { if (e.status === 401 || e.status === 403) return loginView(); toast(e.message, "warn"); return; }
  $("#app").replaceChildren(h("div", { class: "dr-top" }, h("div", {}, h("b", {}, `🚚 ${D.user.name}`), h("div", { class: "small", style: "color:#93c5fd" }, `${d.vehicle.registration_no} · ${d.vehicle.vehicle_type.toLowerCase()} · ${d.delivered_today} delivered in 24 h`)),
    h("div", { class: "row" }, h("a", { href: "/", style: "color:#93c5fd" }, "shop"), h("button", { class: "btn sm", style: "background:#334155;border-color:#475569;color:#fff", onclick: async () => { await api("/api/auth/logout", {}); location.reload(); } }, "Sign out"))),
    h("div", { class: "dr-wrap" }, h("h2", {}, `My deliveries (${d.jobs.length})`), d.jobs.length ? h("div", {}, ...d.jobs.map(jobCard)) : h("div", { class: "job muted" }, "No deliveries assigned to your vehicle right now. A dispatcher assigns them in the back office.")));
}
(async () => {
  const me = (await api("/api/auth/me")).user;
  if (!me || me.role !== "DRIVER") return loginView();
  D.user = me; await load(false);
  setInterval(() => { if (!document.hidden) load(true); }, 4000);
})();
