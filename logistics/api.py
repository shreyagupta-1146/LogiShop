"""REST API behind the three web apps (shop, back office, driver).

Each handler is short and the SQL is written out in the open, so it is easy to explain what a
click does to the database:

    customer actions  ->  SQL in this file  (cart, checkout = ONE stored function call)
    operations        ->  events published to Kafka; consumers write the logistics tables
"""
from __future__ import annotations

import random
import re
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from . import auth, config, db, replay, seed, simulator, termlog
from . import server as S
from .domain import RESCHEDULABLE, add_minutes, now_ts
from .ml import models as ml_models
from .server import ApiError, route


# ----------------------------------------------------------------------
#  helpers
# ----------------------------------------------------------------------
def Q(sql, params=None):
    with db.POOL.connection() as c:
        return [dict(r) for r in c.execute(sql, params).fetchall()]


def Q1(sql, params=None):
    rows = Q(sql, params)
    return rows[0] if rows else None


@contextmanager
def tx(req, label: str):
    """A write transaction attributed to the web API and to this user action (trace)."""
    trace = req.trace(label)
    with db.POOL.connection() as c:
        with db.write_tx(c, "web-api", trace):
            yield c, trace


def outcome(res: dict, wait: bool = True) -> dict:
    """Wait for the consumers, then report whether the command was applied or refused."""
    if wait:
        res["idle"] = S.CTX.pipeline.wait_idle(10)
    ids = [p["event_id"] for p in res.get("published", [])]
    rej = []
    if ids:
        keys = []
        for i in ids:
            keys += [f"REJ:{i}", f"STK-EXC:{i}", f"REQ-REJ:{i}"]
        for r in Q("SELECT event_type, payload FROM logistics.shipment_event WHERE event_id = ANY(?)", (keys,)):
            p = r["payload"] or {}
            reason = p.get("reason", "")
            if r["event_type"] == "EXCEPTION" and p.get("sku_id"):
                reason = f"insufficient stock for {p['sku_id']} (requested {p.get('requested')}, available {p.get('available')})"
            rej.append({"type": r["event_type"], "reason": reason})
    res["rejections"] = rej
    res["ok"] = not rej
    return res


def iso(v: str | None) -> str | None:
    if not v:
        return None
    d = datetime.fromisoformat(v.replace("Z", "+00:00"))
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


STOCK = "COALESCE((SELECT max(b.available_qty) FROM logistics.inventory_balance b WHERE b.sku_id = s.sku_id), 0)"


# ----------------------------------------------------------------------
#  public: catalogue
# ----------------------------------------------------------------------
@route("GET", "/api/health")
def health(req):
    return {"ok": True, "db": config.PG_DB, "state": S.CTX.state}


@route("GET", "/api/shop/categories")
def categories(req):
    return Q("""SELECT c.category_id, c.slug, c.name, c.parent_id,
                       (SELECT count(*) FROM shop.sku s
                         WHERE s.is_active AND (s.category_id = c.category_id
                               OR s.category_id IN (SELECT ch.category_id FROM shop.category ch WHERE ch.parent_id = c.category_id))) AS products
                  FROM shop.category c ORDER BY c.parent_id NULLS FIRST, c.category_id""")


ORDER_BY = {
    "popular": "(SELECT COALESCE(sum(l.quantity), 0) FROM shop.order_line l WHERE l.sku_id = s.sku_id) DESC, s.sku_id",
    "price_asc": "s.price ASC, s.sku_id", "price_desc": "s.price DESC, s.sku_id",
    "discount": "(1 - s.price / s.mrp) DESC, s.sku_id", "new": "s.created_at DESC, s.sku_id",
}


@route("GET", "/api/shop/products")
def products(req):
    q = (req.query.get("q") or "").strip()
    tokens = re.findall(r"[A-Za-z0-9]+", q.lower())
    tsq = " & ".join(t + ":*" for t in tokens)
    if q and not tokens:
        return []
    return Q(f"""SELECT s.sku_id, s.description AS name, s.brand, s.price, s.mrp,
                        round((1 - s.price / s.mrp) * 100)::int AS discount_pct,
                        s.image_emoji AS emoji, s.image_color AS color, c.name AS category, c.slug AS category_slug,
                        {STOCK} AS in_stock
                   FROM shop.sku s JOIN shop.category c USING (category_id)
                  WHERE s.is_active
                    AND (:q = '' OR s.search_vec @@ to_tsquery('simple', :tsq) OR s.description ILIKE '%' || :q || '%')
                    AND (:cat = '' OR c.slug = :cat OR c.parent_id = (SELECT category_id FROM shop.category WHERE slug = :cat))
                  ORDER BY {ORDER_BY.get(req.query.get('sort', 'popular'), ORDER_BY['popular'])}
                  LIMIT 60""", {"q": q, "tsq": tsq, "cat": req.query.get("category", "")})


@route("GET", r"/api/shop/products/(?P<sku>[A-Za-z0-9_-]+)")
def product(req):
    p = Q1(f"""SELECT s.sku_id, s.sku_code, s.description AS name, s.long_description, s.brand, s.price, s.mrp, s.weight_kg,
                      round((1 - s.price / s.mrp) * 100)::int AS discount_pct, s.image_emoji AS emoji, s.image_color AS color,
                      c.name AS category, c.slug AS category_slug, {STOCK} AS in_stock
                 FROM shop.sku s JOIN shop.category c USING (category_id)
                WHERE s.sku_id = ? AND s.is_active""", (req.params["sku"],))
    if not p:
        raise ApiError(404, "product not found")
    p["related"] = Q(f"""SELECT s.sku_id, s.description AS name, s.price, s.mrp, s.image_emoji AS emoji, s.image_color AS color
                           FROM shop.sku s WHERE s.is_active AND s.category_id = (SELECT category_id FROM shop.sku WHERE sku_id = ?)
                            AND s.sku_id <> ? ORDER BY s.sku_id LIMIT 4""", (p["sku_id"], p["sku_id"]))
    return p


@route("GET", "/api/zones")
def zones(req):
    pins = {z[1]: z[2] for z in seed.ZONES}
    return [dict(r, pincode=pins.get(r["name"].replace(" delivery zone", ""), "560001")) for r in
            Q("SELECT geofence_id, name FROM logistics.geofence WHERE geofence_type = 'DELIVERY_ZONE' ORDER BY name")]


# ----------------------------------------------------------------------
#  auth
# ----------------------------------------------------------------------
def _login_session(req, c, subject_type: str, subject_id: str):
    token, th = auth.new_token()
    c.execute("INSERT INTO infra.auth_session (token_hash, subject_type, subject_id, expires_at) "
              "VALUES (?, ?, ?, now() + make_interval(hours => ?))", (th, subject_type, subject_id, auth.SESSION_HOURS))
    c.execute("DELETE FROM infra.auth_session WHERE expires_at < now()")
    req.set_cookie = token


@route("POST", "/api/auth/register")
def register(req):
    b = req.body
    name, email, pw = (b.get("name") or "").strip(), (b.get("email") or "").strip(), b.get("password") or ""
    if len(pw) < 6:
        raise ApiError(400, "password must be at least 6 characters")
    try:
        with tx(req, f"register {email}") as (c, _):
            cid = c.execute("INSERT INTO shop.customer (name, email, phone, password_hash) VALUES (?, ?, ?, ?) RETURNING customer_id",
                            (name, email, b.get("phone") or None, auth.hash_password(pw))).fetchone()["customer_id"]
            _login_session(req, c, "CUSTOMER", cid)
    except db.UniqueViolation:
        raise ApiError(400, "an account with this e-mail already exists")
    except db.IntegrityError:
        raise ApiError(400, "please check the name, e-mail and phone number")
    return {"role": "CUSTOMER", "name": name, "id": cid}


@route("POST", "/api/auth/login")
def login(req):
    email, pw = (req.body.get("email") or "").strip(), req.body.get("password") or ""
    with db.POOL.connection() as c:
        cu = c.execute("SELECT customer_id, name, password_hash FROM shop.customer WHERE email = ? AND is_active", (email,)).fetchone()
        st = None if cu else c.execute("SELECT staff_id, name, role, vehicle_id, password_hash FROM logistics.staff_user "
                                       "WHERE email = ? AND is_active", (email,)).fetchone()
    row = cu or st
    if not row or not auth.verify_password(pw, row["password_hash"]):
        raise ApiError(401, "incorrect e-mail or password")
    with tx(req, f"login {email}") as (c, _):
        if cu:
            _login_session(req, c, "CUSTOMER", cu["customer_id"])
            return {"role": "CUSTOMER", "name": cu["name"], "id": cu["customer_id"]}
        _login_session(req, c, "STAFF", st["staff_id"])
        return {"role": st["role"], "name": st["name"], "id": st["staff_id"], "vehicle_id": st["vehicle_id"]}


@route("POST", "/api/auth/logout")
def logout(req):
    raw = req.headers.get("Cookie") or ""
    m = re.search(rf"{S.COOKIE}=([^;]+)", raw)
    if m:
        with db.POOL.connection() as c:
            c.execute("DELETE FROM infra.auth_session WHERE token_hash = ?", (auth.token_hash(m.group(1)),))
    req.clear_cookie = True
    return {"ok": True}


@route("GET", "/api/auth/me")
def me(req):
    return {"user": req.user}


# ----------------------------------------------------------------------
#  customer: cart, addresses, checkout
# ----------------------------------------------------------------------
def cart_payload(uid: str) -> dict:
    items = Q(f"""SELECT c.sku_id, c.quantity, s.description AS name, s.brand, s.price, s.image_emoji AS emoji, s.image_color AS color,
                         c.quantity * s.price AS line_total, {STOCK} AS in_stock
                    FROM shop.cart_item c JOIN shop.sku s USING (sku_id) WHERE c.customer_id = ? ORDER BY c.added_at""", (uid,))
    sub = round(sum(i["line_total"] for i in items), 2)
    fee = Q1("SELECT shop.delivery_fee(?::numeric) AS fee", (sub,))["fee"] if items else 0
    return {"items": items, "subtotal": sub, "delivery_fee": fee, "total": sub + fee, "count": sum(i["quantity"] for i in items)}


@route("GET", "/api/cart", auth="customer")
def get_cart(req):
    return cart_payload(req.user["id"])


@route("POST", "/api/cart", auth="customer")
def update_cart(req):
    sku, qty, mode = req.body["sku_id"], int(req.body.get("quantity", 1)), req.body.get("mode", "add")
    uid = req.user["id"]
    if not Q1("SELECT 1 AS x FROM shop.sku WHERE sku_id = ? AND is_active", (sku,)):
        raise ApiError(404, "product not available")
    try:
        with tx(req, f"cart: {mode} {qty} x {sku}") as (c, _):
            if qty <= 0 and mode == "set":
                c.execute("DELETE FROM shop.cart_item WHERE customer_id = ? AND sku_id = ?", (uid, sku))
            elif mode == "set":
                c.execute("INSERT INTO shop.cart_item (customer_id, sku_id, quantity) VALUES (?, ?, ?) "
                          "ON CONFLICT (customer_id, sku_id) DO UPDATE SET quantity = EXCLUDED.quantity", (uid, sku, qty))
            else:
                c.execute("INSERT INTO shop.cart_item (customer_id, sku_id, quantity) VALUES (?, ?, ?) "
                          "ON CONFLICT (customer_id, sku_id) DO UPDATE SET quantity = LEAST(10, shop.cart_item.quantity + EXCLUDED.quantity)",
                          (uid, sku, max(1, qty)))
    except db.IntegrityError:
        raise ApiError(400, "you can order between 1 and 10 of an item")
    return cart_payload(uid)


@route("POST", "/api/cart/remove", auth="customer")
def remove_from_cart(req):
    with tx(req, f"cart: remove {req.body['sku_id']}") as (c, _):
        c.execute("DELETE FROM shop.cart_item WHERE customer_id = ? AND sku_id = ?", (req.user["id"], req.body["sku_id"]))
    return cart_payload(req.user["id"])


@route("GET", "/api/addresses", auth="customer")
def addresses(req):
    return Q("SELECT * FROM shop.customer_address WHERE customer_id = ? ORDER BY is_default DESC, address_id", (req.user["id"],))


@route("POST", "/api/addresses", auth="customer")
def add_address(req):
    b, uid = req.body, req.user["id"]
    z = Q1("SELECT geofence_id, name, center_lat, center_lon FROM logistics.geofence WHERE geofence_id = ? AND geofence_type = 'DELIVERY_ZONE'",
           (b.get("zone_id"),))
    if not z:
        raise ApiError(400, "we do not deliver to this area yet - please pick a serviceable area")
    lat = z["center_lat"] + random.uniform(-0.007, 0.007)           # stands in for a geocoding service
    lon = z["center_lon"] + random.uniform(-0.007, 0.007)
    try:
        with tx(req, "address book: add address") as (c, _):
            first = c.execute("SELECT count(*) AS n FROM shop.customer_address WHERE customer_id = ?", (uid,)).fetchone()["n"] == 0
            c.execute("INSERT INTO shop.customer_address (customer_id, label, recipient, phone, line1, line2, locality, pincode, lat, lon, is_default) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                      (uid, b.get("label", "Home"), b["recipient"], b["phone"], b["line1"], b.get("line2") or None,
                       z["name"].replace(" delivery zone", ""), b["pincode"], round(lat, 6), round(lon, 6), first))
    except db.IntegrityError:
        raise ApiError(400, "please check the phone number and the 6-digit pincode")
    return addresses(req)


@route("POST", "/api/addresses/default", auth="customer")
def default_address(req):
    with tx(req, "address book: set default") as (c, _):
        c.execute("UPDATE shop.customer_address SET is_default = false WHERE customer_id = ? AND is_default", (req.user["id"],))
        c.execute("UPDATE shop.customer_address SET is_default = true WHERE customer_id = ? AND address_id = ?",
                  (req.user["id"], int(req.body["address_id"])))
    return addresses(req)


@route("POST", "/api/checkout", auth="customer")
def checkout(req):
    """Checkout = ONE stored-function call = ONE PostgreSQL transaction (order, lines, payment, cart, outbox)."""
    pipe = S.CTX.pipeline
    with tx(req, "checkout") as (c, trace):
        oid = c.execute("SELECT shop.place_order(?, ?, ?) AS id",
                        (req.user["id"], int(req.body["address_id"]), req.body["payment_method"])).fetchone()["id"]
    pipe.flush_outbox()                    # outbox -> Kafka now (the relay would also do it within milliseconds)
    pipe.wait_idle(10)                     # let the consumers pick a warehouse, create the shipment and deduct stock
    return {"order_id": oid, "trace_id": trace, "order": Q1("SELECT * FROM shop.v_order_summary WHERE order_id = ?", (oid,))}


# ----------------------------------------------------------------------
#  customer: orders and tracking
# ----------------------------------------------------------------------
@route("GET", "/api/orders", auth="customer")
def orders(req):
    return Q("""SELECT v.*,
                       (SELECT string_agg(s.image_emoji, ' ' ORDER BY l.sku_id) FROM shop.order_line l JOIN shop.sku s USING (sku_id) WHERE l.order_id = v.order_id) AS emojis,
                       (SELECT string_agg(s.description, ', ' ORDER BY l.sku_id) FROM shop.order_line l JOIN shop.sku s USING (sku_id) WHERE l.order_id = v.order_id) AS items_text
                  FROM shop.v_order_summary v WHERE v.customer_id = ? ORDER BY v.placed_at DESC""", (req.user["id"],))


CUSTOMER_EVENTS = ("SHIPMENT_CREATED", "VEHICLE_ASSIGNED", "PICKED_UP", "IN_TRANSIT", "OUT_FOR_DELIVERY", "DELIVERED", "EXCEPTION",
                   "ARRIVED_AT_ZONE", "DELIVERY_RESCHEDULED", "EXCEPTION_RISK", "REQUEST_ACCEPTED", "REQUEST_REJECTED", "REQUEST_UPDATED")


def _order_for(req, oid: str):
    o = Q1("SELECT * FROM shop.customer_order WHERE order_id = ? AND customer_id = ?", (oid, req.user["id"]))
    if not o:
        raise ApiError(404, "order not found")
    return o


@route("GET", r"/api/orders/(?P<oid>[A-Za-z0-9_-]+)", auth="customer")
def order_detail(req):
    o = _order_for(req, req.params["oid"])
    ship = Q1("SELECT * FROM logistics.v_shipment_overview WHERE order_id = ?", (o["order_id"],))
    out = {
        "order": o,
        "lines": Q("SELECT l.sku_id, s.description AS name, s.image_emoji AS emoji, s.image_color AS color, l.quantity, l.unit_price, l.line_total "
                   "FROM shop.order_line l JOIN shop.sku s USING (sku_id) WHERE l.order_id = ? ORDER BY l.sku_id", (o["order_id"],)),
        "payments": Q("SELECT payment_id, kind, method, amount, status, reference, created_at FROM shop.payment WHERE order_id = ? ORDER BY created_at, payment_id", (o["order_id"],)),
        "shipment": ship, "events": [], "trail": [], "requests": [], "warehouse": None, "can_reschedule": False, "can_return": False,
        "notifications": Q("SELECT notification_type, message, created_at FROM logistics.notification WHERE order_id = ? OR shipment_id = ? "
                           "ORDER BY created_at DESC, notification_id DESC LIMIT 8", (o["order_id"], ship["shipment_id"] if ship else "-")),
    }
    if ship:
        sid = ship["shipment_id"]
        out["events"] = Q("SELECT event_type, event_time, payload FROM logistics.shipment_event WHERE shipment_id = ? AND event_type = ANY(?) ORDER BY log_seq",
                          (sid, list(CUSTOMER_EVENTS)))
        out["trail"] = Q("SELECT lat, lon, speed_kmh, ping_time FROM logistics.gps_ping WHERE shipment_id = ? ORDER BY ping_time DESC LIMIT 120", (sid,))[::-1]
        out["warehouse"] = Q1("SELECT w.name, g.center_lat AS lat, g.center_lon AS lon FROM logistics.warehouse w JOIN logistics.geofence g USING (geofence_id) "
                              "WHERE w.warehouse_id = ?", (ship["warehouse_id"],))
        out["zone"] = Q1("SELECT name, (bbox[0])[0] AS max_lon, (bbox[0])[1] AS max_lat, (bbox[1])[0] AS min_lon, (bbox[1])[1] AS min_lat "
                         "FROM logistics.geofence WHERE geofence_id = ?", (ship["zone_id"],)) if ship["zone_id"] else None
        reqs = Q("SELECT * FROM logistics.customer_request WHERE shipment_id = ? ORDER BY created_at, request_id", (sid,))
        for r in reqs:
            r["items"] = Q("SELECT sku_id, quantity, item_condition FROM logistics.request_item WHERE request_id = ?", (r["request_id"],))
        out["requests"] = reqs
        out["can_reschedule"] = ship["status"] in RESCHEDULABLE and o["status"] != "CANCELLED"
        out["can_return"] = ship["status"] == "DELIVERED"
    return out


@route("POST", r"/api/orders/(?P<oid>[A-Za-z0-9_-]+)/reschedule", auth="customer")
def reschedule(req):
    o = _order_for(req, req.params["oid"])
    ship = Q1("SELECT shipment_id FROM logistics.shipment WHERE order_id = ?", (o["order_id"],))
    if not ship:
        raise ApiError(400, "this order has no shipment yet")
    res = S.CTX.app.submit_request(ship["shipment_id"], "RESCHEDULE", req.body.get("reason"), iso(req.body["start"]), iso(req.body["end"]))
    res = outcome(res)
    r = Q1("SELECT status FROM logistics.customer_request WHERE request_id = ?", (res["request_id"],))
    res["request_status"] = r["status"] if r else None
    return res


@route("POST", r"/api/orders/(?P<oid>[A-Za-z0-9_-]+)/return", auth="customer")
def return_items(req):
    o = _order_for(req, req.params["oid"])
    ship = Q1("SELECT shipment_id FROM logistics.shipment WHERE order_id = ?", (o["order_id"],))
    if not ship:
        raise ApiError(400, "this order has no shipment yet")
    res = S.CTX.app.submit_request(ship["shipment_id"], "RETURN", req.body.get("reason"), items=req.body.get("items"))
    res = outcome(res)
    r = Q1("SELECT status FROM logistics.customer_request WHERE request_id = ?", (res["request_id"],))
    res["request_status"] = r["status"] if r else None
    return res


@route("GET", "/api/notifications", auth="customer")
def notifications(req):
    rows = Q("SELECT notification_id, order_id, shipment_id, notification_type, message, status, created_at FROM logistics.notification "
             "WHERE customer_id = ? ORDER BY created_at DESC, notification_id DESC LIMIT 30", (req.user["id"],))
    return {"items": rows, "unread": sum(1 for r in rows if r["status"] == "SENT")}


@route("POST", "/api/notifications/read", auth="customer")
def notifications_read(req):
    with tx(req, "notifications: mark all read") as (c, _):
        c.execute("UPDATE logistics.notification SET status = 'READ' WHERE customer_id = ? AND status = 'SENT'", (req.user["id"],))
    return {"ok": True}


# ----------------------------------------------------------------------
#  back office (admin)
# ----------------------------------------------------------------------
@route("GET", "/api/admin/dashboard", auth="admin")
def dashboard(req):
    k = Q1("""SELECT (SELECT count(*) FROM shop.customer_order) AS orders,
                     (SELECT count(*) FROM shop.customer_order WHERE status NOT IN ('DELIVERED', 'CANCELLED')) AS open_orders,
                     (SELECT COALESCE(sum(total), 0) FROM shop.customer_order WHERE status <> 'CANCELLED') AS revenue,
                     (SELECT count(*) FROM shop.customer) AS customers,
                     (SELECT count(*) FROM logistics.shipment WHERE at_risk AND status <> 'DELIVERED') AS at_risk,
                     (SELECT count(*) FROM logistics.customer_request WHERE status IN ('PICKUP_SCHEDULED', 'PICKED_UP')) AS open_returns,
                     (SELECT count(*) FROM logistics.v_stock_levels WHERE level <> 'OK') AS low_stock""")
    k["by_status"] = Q("SELECT status::text AS status, count(*) AS n FROM shop.customer_order GROUP BY status ORDER BY n DESC")
    k["by_category"] = Q("SELECT * FROM shop.v_sales_by_category ORDER BY revenue DESC")
    k["daily"] = Q("SELECT day, orders, revenue FROM shop.mv_daily_sales ORDER BY day DESC LIMIT 7")
    return k


@route("GET", "/api/admin/orders", auth="admin")
def admin_orders(req):
    return Q("""SELECT o.order_id, o.placed_at, o.status::text AS status, o.total, o.payment_method, c.name AS customer, s.shipment_id,
                       s.status::text AS shipment_status, s.vehicle_id, s.at_risk,
                       (SELECT status FROM shop.payment p WHERE p.order_id = o.order_id AND p.kind = 'CHARGE') AS payment_status
                  FROM shop.customer_order o JOIN shop.customer c USING (customer_id)
                  LEFT JOIN logistics.shipment s ON s.order_id = o.order_id ORDER BY o.placed_at DESC""")


@route("GET", "/api/admin/shipments", auth="admin")
def admin_shipments(req):
    return {"shipments": Q("SELECT * FROM logistics.v_shipment_overview ORDER BY created_at DESC, shipment_id DESC"),
            "vehicles": Q("SELECT v.*, (SELECT name FROM logistics.staff_user u WHERE u.vehicle_id = v.vehicle_id) AS driver FROM logistics.vehicle v ORDER BY vehicle_id")}


@route("POST", r"/api/admin/shipments/(?P<sid>[A-Za-z0-9_-]+)/assign", auth="admin")
def admin_assign(req):
    return outcome(_g(S.CTX.app.assign_vehicle, req.params["sid"], req.body["vehicle_id"]))


@route("POST", r"/api/admin/shipments/(?P<sid>[A-Za-z0-9_-]+)/status", auth="admin")
def admin_status(req):
    return outcome(_g(S.CTX.app.set_status, req.params["sid"], req.body["status"], req.body.get("reason")))


def _g(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except ValueError as e:
        raise ApiError(400, str(e))


@route("GET", "/api/admin/inventory", auth="admin")
def admin_inventory(req):
    return {"stock": Q("SELECT * FROM logistics.v_stock_levels ORDER BY warehouse_id, sku_id"),
            "audit_bad": Q("SELECT * FROM logistics.v_inventory_audit WHERE audit <> 'OK'"),
            "ledger": Q("SELECT ledger_id, sku_id, warehouse_id, movement_type, quantity_delta, event_time, shipment_id, request_id "
                        "FROM logistics.inventory_ledger ORDER BY ledger_id DESC LIMIT 30"),
            "warehouses": Q("SELECT warehouse_id, name FROM logistics.warehouse ORDER BY warehouse_id"),
            "skus": Q("SELECT sku_id, description AS name FROM shop.sku ORDER BY sku_id")}


@route("POST", "/api/admin/inventory/stock-in", auth="admin")
def admin_stock_in(req):
    return outcome(_g(S.CTX.app.stock_in, req.body["warehouse_id"], req.body["sku_id"], req.body["quantity"]))


@route("GET", "/api/admin/requests", auth="admin")
def admin_requests(req):
    rows = Q("""SELECT r.*, c.name AS customer_name, s.order_id FROM logistics.customer_request r
                  JOIN shop.customer c USING (customer_id) JOIN logistics.shipment s USING (shipment_id) ORDER BY r.created_at DESC, r.request_id DESC""")
    for r in rows:
        r["items"] = Q("SELECT sku_id, quantity, item_condition FROM logistics.request_item WHERE request_id = ?", (r["request_id"],))
    return rows


@route("POST", r"/api/admin/requests/(?P<rid>[A-Za-z0-9_-]+)/advance", auth="admin")
def admin_advance(req):
    return outcome(_g(S.CTX.app.advance_request, req.params["rid"], req.body["to_status"]))


@route("GET", "/api/admin/products", auth="admin")
def admin_products(req):
    return Q(f"""SELECT s.sku_id, s.description AS name, s.brand, s.price, s.mrp, s.is_active, s.image_emoji AS emoji, c.name AS category,
                        {STOCK} AS in_stock,
                        (SELECT COALESCE(sum(l.quantity), 0) FROM shop.order_line l WHERE l.sku_id = s.sku_id) AS units_sold
                   FROM shop.sku s JOIN shop.category c USING (category_id) ORDER BY s.sku_id""")


@route("POST", r"/api/admin/products/(?P<sku>[A-Za-z0-9_-]+)/price", auth="admin")
def admin_price(req):
    try:
        with tx(req, f"price change for {req.params['sku']}") as (c, _):
            c.execute("UPDATE shop.sku SET price = ? WHERE sku_id = ?", (req.body["price"], req.params["sku"]))
    except db.IntegrityError:
        raise ApiError(400, "the selling price must be positive and not above the MRP")
    return {"ok": True, "history": Q("SELECT old_price, new_price, changed_at, changed_by FROM shop.price_history WHERE sku_id = ? ORDER BY history_id DESC LIMIT 5", (req.params["sku"],))}


@route("POST", r"/api/admin/products/(?P<sku>[A-Za-z0-9_-]+)/active", auth="admin")
def admin_active(req):
    with tx(req, f"{'enable' if req.body['active'] else 'disable'} product {req.params['sku']}") as (c, _):
        c.execute("UPDATE shop.sku SET is_active = ? WHERE sku_id = ?", (bool(req.body["active"]), req.params["sku"]))
    return {"ok": True}


@route("GET", "/api/admin/ml", auth="admin")
def admin_ml(req):
    m = S.CTX.pipeline.models
    return {"report": ml_models.load_report(), "models": {"enabled": m.enabled, "sklearn": ml_models.SKLEARN, "eta_ready": m.eta_ready},
            "accuracy": Q("SELECT * FROM logistics.v_ml_model_accuracy ORDER BY mae_minutes"),
            "at_risk": Q("SELECT * FROM logistics.v_at_risk_shipments"),
            "performance": Q("SELECT outcome, count(*) AS n FROM logistics.v_delivery_performance GROUP BY outcome")}


@route("POST", "/api/admin/ml", auth="admin")
def admin_ml_toggle(req):
    S.CTX.pipeline.models.enabled = bool(req.body.get("enabled"))
    return {"enabled": S.CTX.pipeline.models.enabled}


@route("GET", "/api/admin/system", auth="admin")
def admin_system(req):
    pipe = S.CTX.pipeline
    b = pipe.broker
    return {
        "consumers": pipe.status(), "topics": b.topic_summary(), "lag": b.lag_table(), "dead_letters": b.dead_letters(10),
        "connections": Q("SELECT application_name, state, count(*) AS n FROM pg_stat_activity WHERE datname = current_database() "
                         "AND usename = current_user GROUP BY 1, 2 ORDER BY 1, 2"),
        "db": Q1("""SELECT current_setting('server_version') AS version, pg_size_pretty(pg_database_size(current_database())) AS size,
                           (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                             WHERE c.relkind = 'r' AND n.nspname IN ('shop', 'logistics', 'infra')) AS tables,
                           (SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                             WHERE c.relkind IN ('v', 'm') AND n.nspname IN ('shop', 'logistics', 'infra')) AS views,
                           (SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
                             WHERE n.nspname IN ('shop', 'logistics', 'infra')) AS functions,
                           (SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace
                             WHERE NOT t.tgisinternal AND n.nspname IN ('shop', 'logistics', 'infra')) AS triggers"""),
        "outbox_pending": Q1("SELECT count(*) AS n FROM infra.outbox WHERE published_at IS NULL")["n"],
        "state": S.CTX.state,
    }


@route("GET", "/api/admin/verify", auth="admin")
def admin_verify(req):
    S.CTX.pipeline.wait_idle(10)
    with db.POOL.connection() as c:
        return replay.verify(c)


@route("POST", "/api/admin/rebuild", auth="admin")
def admin_rebuild(req):
    return replay.rebuild(S.CTX.pipeline)


@route("POST", "/api/admin/redeliver", auth="admin")
def admin_redeliver(req):
    res = _g(S.CTX.app.redeliver, req.body["event_id"])
    res["idle"] = S.CTX.pipeline.wait_idle(10)
    res["duplicate"], res["rejections"], res["ok"] = True, [], True
    return res


@route("GET", "/api/admin/last-event", auth="admin")
def admin_last_event(req):
    r = Q1("SELECT event_id, event_type, shipment_id FROM logistics.shipment_event ORDER BY log_seq DESC LIMIT 1")
    return r or {}


@route("POST", "/api/admin/purge-pings", auth="admin")
def admin_purge(req):
    return S.CTX.app.purge_old_pings(int(req.body.get("days", 90)))


@route("POST", "/api/admin/reset", auth="admin")
def admin_reset(req):
    if S.CTX.state["seeding"]:
        raise ApiError(409, "already resetting")
    threading.Thread(target=S.reset_and_seed, daemon=True, name="reset").start()
    return {"started": True}


@route("GET", "/api/state")
def public_state(req):
    return S.CTX.state


# ----------------------------------------------------------------------
#  trip simulation (used by the back office and the driver app)
# ----------------------------------------------------------------------
def start_sim(sid: str, body: dict) -> dict:
    ctx = S.CTX
    _g(ctx.app.shipment_row, sid)
    if sid in ctx.sims and not ctx.sims[sid].is_set():
        raise ApiError(409, f"a simulation for {sid} is already running")
    live = body.get("mode", "live") == "live"
    until = body.get("until", "end")
    if until not in ("end", "ofd", "breakdown"):
        until = float(until)
    ctx.sims[sid] = simulator.run_in_background(
        ctx.app, sid, until=until, anomaly=body.get("anomaly") or None, per_ping_traces=live,
        delay=float(body.get("delay", 0.6)) if live else 0.0, step_min=2.0 if live else 3.0, sync_every=1 if live else 3,
        label=f"Simulated trip {sid} ({body.get('anomaly') or 'normal'})",
        final_exception="VEHICLE_BREAKDOWN" if body.get("anomaly") == "breakdown" and until == "breakdown" else None,
        vehicle_id=body.get("vehicle_id"))
    return {"started": True, "shipment_id": sid}


@route("POST", "/api/admin/simulate", auth="admin")
def admin_simulate(req):
    return start_sim(req.body["shipment_id"], req.body)


@route("POST", "/api/admin/simulate/stop", auth="staff")
def sim_stop(req):
    ev = S.CTX.sims.get(req.body.get("shipment_id"))
    if ev:
        ev.set()
    return {"stopped": bool(ev)}


# ----------------------------------------------------------------------
#  driver app
# ----------------------------------------------------------------------
def _job(req, sid: str):
    row = Q1("SELECT * FROM logistics.v_shipment_overview WHERE shipment_id = ? AND vehicle_id = ?", (sid, req.user["vehicle_id"]))
    if not row:
        raise ApiError(404, "this delivery is not assigned to your vehicle")
    return row


@route("GET", "/api/driver/jobs", auth="driver")
def driver_jobs(req):
    v = req.user["vehicle_id"]
    return {"vehicle": Q1("SELECT * FROM logistics.vehicle WHERE vehicle_id = ?", (v,)),
            "jobs": Q("SELECT * FROM logistics.v_shipment_overview WHERE vehicle_id = ? AND status <> 'DELIVERED' ORDER BY delivery_end", (v,)),
            "delivered_today": Q1("SELECT count(*) AS n FROM logistics.shipment WHERE vehicle_id = ? AND status = 'DELIVERED' "
                                  "AND actual_delivery_at > now() - interval '24 hours'", (v,))["n"]}


@route("POST", r"/api/driver/jobs/(?P<sid>[A-Za-z0-9_-]+)/status", auth="driver")
def driver_status(req):
    _job(req, req.params["sid"])
    return outcome(_g(S.CTX.app.set_status, req.params["sid"], req.body["status"], req.body.get("reason")))


def _move_toward(sid: str, fraction: float, speed: float):
    """Send one GPS ping that places the vehicle `fraction` of the remaining way toward the customer."""
    from .domain import bearing_deg
    full = S.CTX.app.shipment_row(sid)
    wh = Q1("SELECT g.center_lat AS lat, g.center_lon AS lon FROM logistics.warehouse w JOIN logistics.geofence g USING (geofence_id) WHERE w.warehouse_id = ?",
            (full["origin_warehouse_id"],))
    cur = (full["current_lat"], full["current_lon"]) if full["current_lat"] is not None else (wh["lat"], wh["lon"])
    lat, lon = cur[0] + (full["dest_lat"] - cur[0]) * fraction, cur[1] + (full["dest_lon"] - cur[1]) * fraction
    return outcome(_g(S.CTX.app.send_ping, sid, lat, lon, speed, bearing_deg(lat, lon, full["dest_lat"], full["dest_lon"])))


@route("POST", r"/api/driver/jobs/(?P<sid>[A-Za-z0-9_-]+)/move", auth="driver")
def driver_move(req):
    _job(req, req.params["sid"])
    return _move_toward(req.params["sid"], float(req.body.get("fraction", 0.25)), float(req.body.get("speed_kmh", 24)))


@route("POST", r"/api/admin/shipments/(?P<sid>[A-Za-z0-9_-]+)/move", auth="admin")
def admin_move(req):
    return _move_toward(req.params["sid"], float(req.body.get("fraction", 0.25)), float(req.body.get("speed_kmh", 24)))


@route("POST", r"/api/driver/jobs/(?P<sid>[A-Za-z0-9_-]+)/drive", auth="driver")
def driver_drive(req):
    _job(req, req.params["sid"])
    return start_sim(req.params["sid"], req.body)
