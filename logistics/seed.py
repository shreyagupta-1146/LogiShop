"""Demo data.  Reference data (catalogue, customers, warehouses ...) is loaded directly; stock, orders,
payments, shipments and trips go through the real checkout function and the event pipeline,
exactly like live use.

Logins (demo only):  customers  <e-mail> / demo123        e.g. asha.rao@example.com
                     admin      admin@logistics.local / admin123
                     drivers    ramesh@logistics.local / driver123 (VEH-03 van) ...
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone

from . import auth, config, db, dbsetup, termlog
from .domain import add_minutes, fmt_ts
from .simulator import drive

ZONE_HALF = 0.012          # degrees, ~1.3 km
WH_HALF = 0.006

ZONES = [   # id, name, pincode, centre lat, lon
    ("GF-Z1", "Indiranagar", "560038", 12.9784, 77.6408), ("GF-Z2", "Koramangala", "560034", 12.9352, 77.6245),
    ("GF-Z3", "Jayanagar", "560041", 12.9308, 77.5838), ("GF-Z4", "HSR Layout", "560102", 12.9116, 77.6389),
    ("GF-Z5", "Malleshwaram", "560003", 13.0035, 77.5643), ("GF-Z6", "Hebbal", "560024", 13.0358, 77.5970),
]
WAREHOUSES = [  # id, name, address, geofence id, lat, lon
    ("WH-1", "Peenya Hub", "Peenya Industrial Area, Bengaluru", "GF-W1", 13.0285, 77.5197),
    ("WH-2", "Electronic City Hub", "Phase 1, Electronic City, Bengaluru", "GF-W2", 12.8452, 77.6602),
    ("WH-3", "Whitefield Hub", "ITPL Road, Whitefield, Bengaluru", "GF-W3", 12.9698, 77.7500),
]
VEHICLES = [
    ("VEH-01", "KA01-AB-1001", "BIKE"), ("VEH-02", "KA02-CD-2002", "BIKE"), ("VEH-03", "KA01-1234", "VAN"),
    ("VEH-04", "KA03-EF-4004", "VAN"), ("VEH-05", "KA05-GH-5005", "TRUCK"), ("VEH-06", "KA04-JK-6006", "BIKE"),
]
STAFF = [  # name, email, password, role, vehicle
    ("Store Admin", "admin@logistics.local", "admin123", "ADMIN", None),
    ("Ramesh Gowda", "ramesh@logistics.local", "driver123", "DRIVER", "VEH-03"),
    ("Suresh Naik", "suresh@logistics.local", "driver123", "DRIVER", "VEH-01"),
    ("Mahesh Rao", "mahesh@logistics.local", "driver123", "DRIVER", "VEH-04"),
    ("Kiran Shetty", "kiran@logistics.local", "driver123", "DRIVER", "VEH-05"),
]
CUSTOMERS = [  # customers get ids CUS-1 .. CUS-8 from the sequence, in this order (CUS-7 = the design document's example)
    ("Ravi Kumar", "ravi.kumar@example.com", "+91-9000000001"), ("Priya Nair", "priya.nair@example.com", "+91-9000000002"),
    ("Arjun Mehta", "arjun.mehta@example.com", "+91-9000000003"), ("Neha Sharma", "neha.sharma@example.com", "+91-9000000004"),
    ("Karthik Iyer", "karthik.iyer@example.com", "+91-9000000005"), ("Divya Reddy", "divya.reddy@example.com", "+91-9000000006"),
    ("Asha Rao", "asha.rao@example.com", "+91-9000000007"), ("Imran Khan", "imran.khan@example.com", "+91-9000000008"),
]
# serviceable addresses (locality, line1, lat, lon) - the address book picks from these
ADDRESSES = {
    "Indiranagar": [("12, 5th Cross, 100 Feet Road", 12.9810, 77.6440), ("88, CMH Road", 12.9750, 77.6370)],
    "Koramangala": [("21, 80 Feet Road, 4th Block", 12.9380, 77.6280), ("4, 5th Block, KHB Colony", 12.9320, 77.6210)],
    "Jayanagar": [("7, 11th Main, 4th Block", 12.9340, 77.5870), ("33, 4th T Block", 12.9280, 77.5800)],
    "HSR Layout": [("19, 27th Main, Sector 1", 12.9140, 77.6420), ("52, Sector 2", 12.9090, 77.6350)],
    "Malleshwaram": [("15, Sampige Road", 13.0060, 77.5670), ("9, 8th Cross", 13.0010, 77.5620)],
    "Hebbal": [("31, Bellary Road", 13.0390, 77.6000), ("6, Kempapura Main Road", 13.0330, 77.5940)],
}
CUSTOMER_ZONE = {"Ravi Kumar": "Koramangala", "Priya Nair": "HSR Layout", "Arjun Mehta": "Malleshwaram", "Neha Sharma": "Hebbal",
                 "Karthik Iyer": "Jayanagar", "Divya Reddy": "Indiranagar", "Asha Rao": "Indiranagar", "Imran Khan": "Koramangala"}

CATEGORIES = [  # slug, name, parent slug
    ("mobile", "Mobiles & Accessories", None), ("cases", "Cases & Screen Guards", "mobile"), ("charging", "Chargers & Power", "mobile"),
    ("audio", "Audio", None), ("earbuds", "Earbuds & Headphones", "audio"), ("speakers", "Speakers", "audio"),
    ("wearables", "Wearables", None), ("computing", "Computers & Accessories", None),
    ("home", "Home & Kitchen", None), ("fitness", "Sports & Fitness", None),
]
# sku_id, code, title, brand, category, weight kg, MRP, price, emoji, colour, description
PRODUCTS = [
    ("SKU-1001", "CASE-ARM-01", "Armour Shockproof Phone Case", "CaseGuard", "cases", 0.10, 499, 299, "📱", "#dbeafe", "Military-grade drop protection with raised edges for the screen and camera."),
    ("SKU-1002", "CASE-CLR-02", "Clear Silicone Phone Case", "CaseGuard", "cases", 0.06, 399, 199, "🫧", "#e0f2fe", "Slim, flexible and yellowing-resistant. Shows off your phone's colour."),
    ("SKU-1003", "GLS-TMP-03", "Tempered Glass Screen Guard (2 pack)", "CaseGuard", "cases", 0.05, 299, 149, "🛡️", "#ede9fe", "9H hardness, bubble-free installation kit included."),
    ("SKU-1042", "CHG-20W-01", "Fast Charger 20W USB-C", "VoltEdge", "charging", 0.25, 1299, 799, "🔌", "#fef3c7", "20W Power Delivery wall charger. Charges a phone to 50% in 30 minutes."),
    ("SKU-1205", "PWR-10K-01", "Power Bank 10000 mAh", "ChargeMax", "charging", 0.30, 1999, 1399, "🔋", "#fde68a", "Slim 10000 mAh power bank with dual output and LED indicator."),
    ("SKU-1600", "CBL-USBC-1M", "USB-C Braided Cable 1 m", "VoltEdge", "charging", 0.05, 399, 199, "🔗", "#fed7aa", "Tangle-free braided cable, 60W charging, 480 Mbps data."),
    ("SKU-1100", "EAR-TWS-02", "True Wireless Earbuds", "SoundPeak", "earbuds", 0.15, 2999, 1799, "🎧", "#fce7f3", "Bluetooth 5.3, 24 hours with the case, touch controls and a low-latency gaming mode."),
    ("SKU-1101", "HDP-BT-03", "Over-ear Bluetooth Headphones", "SoundPeak", "earbuds", 0.35, 3999, 2499, "🎧", "#fbcfe8", "40 mm drivers, active noise reduction and 30 hours of playback."),
    ("SKU-1102", "ERP-WRD-04", "Wired Earphones with Mic", "SoundPeak", "earbuds", 0.05, 599, 299, "🎵", "#f5d0fe", "3.5 mm earphones with in-line mic and deep bass."),
    ("SKU-1420", "SPK-BT-01", "Bluetooth Speaker Mini", "BoomBox", "speakers", 0.60, 2499, 1599, "🔊", "#d1fae5", "Waterproof (IPX7) portable speaker with 12 hours of playtime."),
    ("SKU-1421", "SBR-21-02", "Soundbar 2.1 with Subwoofer", "BoomBox", "speakers", 2.50, 6999, 4999, "📻", "#a7f3d0", "120 W soundbar with wireless subwoofer, HDMI ARC and Bluetooth."),
    ("SKU-1310", "WCH-SMT-03", "Smart Watch Active", "TimeFit", "wearables", 0.20, 4999, 2999, "⌚", "#ccfbf1", "1.8-inch AMOLED display, heart-rate and SpO2 tracking, 7-day battery."),
    ("SKU-1311", "BND-FIT-05", "Fitness Band 5", "TimeFit", "wearables", 0.05, 2499, 1499, "⌚", "#99f6e4", "Step, sleep and workout tracking with a bright colour screen."),
    ("SKU-1555", "SLV-LAP-14", "Laptop Sleeve 15.6 inch", "CarryPro", "computing", 0.40, 999, 599, "💼", "#e0e7ff", "Water-resistant padded sleeve with an accessory pocket."),
    ("SKU-1556", "MSE-WL-02", "Wireless Mouse", "ClickWorks", "computing", 0.10, 899, 549, "🖱️", "#c7d2fe", "Silent clicks, 2.4 GHz receiver, 12-month battery."),
    ("SKU-1557", "KBD-MEC-03", "Mechanical Keyboard", "ClickWorks", "computing", 0.90, 3499, 2299, "⌨️", "#bfdbfe", "Hot-swappable blue switches, per-key backlight, full-size layout."),
    ("SKU-1558", "HUB-USB-04", "USB 3.0 Hub 4-Port", "ClickWorks", "computing", 0.08, 799, 449, "🧰", "#a5b4fc", "Four high-speed USB 3.0 ports in an aluminium body."),
    ("SKU-1559", "SSD-1TB-05", "1 TB Portable SSD", "DataVault", "computing", 0.12, 8999, 6499, "💾", "#93c5fd", "Read speeds up to 1050 MB/s, shock-resistant, USB-C."),
    ("SKU-1560", "CAM-FHD-06", "Full HD Webcam", "DataVault", "computing", 0.20, 2499, 1599, "📷", "#c4b5fd", "1080p at 30 fps with a noise-cancelling microphone."),
    ("SKU-1700", "BLB-LED-09", "LED Bulb 9 W (pack of 4)", "BrightHome", "home", 0.20, 599, 349, "💡", "#fef9c3", "Cool daylight, 25,000 hours of life, B22 cap."),
    ("SKU-1701", "LMP-DSK-02", "Desk Lamp with USB Port", "BrightHome", "home", 0.70, 1499, 899, "🔦", "#fef08a", "Three colour modes, touch dimmer and a USB charging port."),
    ("SKU-1702", "KTL-ELC-15", "Electric Kettle 1.5 L", "HomeChef", "home", 1.10, 1799, 1099, "☕", "#fed7aa", "Stainless steel body, auto shut-off, boil-dry protection."),
    ("SKU-1703", "FRY-AIR-04", "Air Fryer 4 L", "HomeChef", "home", 4.50, 7999, 5499, "🍟", "#fecaca", "Digital controls, 8 presets and a non-stick basket."),
    ("SKU-1704", "BTL-STL-01", "Steel Water Bottle 1 L", "HydroPure", "home", 0.40, 799, 449, "🥤", "#bae6fd", "Double-wall vacuum insulation, keeps drinks cold for 24 hours."),
    ("SKU-1800", "YGA-MAT-06", "Yoga Mat 6 mm", "FlexFit", "fitness", 1.00, 1299, 699, "🧘", "#dcfce7", "Anti-skid, extra-thick mat with a carry strap."),
    ("SKU-1801", "RES-BND-05", "Resistance Bands Set (5)", "FlexFit", "fitness", 0.40, 899, 499, "🏋️", "#bbf7d0", "Five resistance levels with door anchor and handles."),
    ("SKU-1802", "DMB-ADJ-10", "Adjustable Dumbbell 10 kg", "FlexFit", "fitness", 10.0, 3999, 2799, "🏋️", "#86efac", "Quick-lock adjustable dumbbell, 2-10 kg in 1 kg steps."),
    ("SKU-1803", "ROP-SKP-02", "Speed Skipping Rope", "FlexFit", "fitness", 0.20, 399, 199, "🪢", "#d9f99d", "Ball-bearing handles and an adjustable steel cable."),
]
# opening stock per warehouse; a few deliberately scarce lines for the stock demos
LOW_STOCK = {("WH-2", "SKU-1420"): 4, ("WH-3", "SKU-1310"): 3, ("WH-1", "SKU-1703"): 2, ("WH-2", "SKU-1703"): 1, ("WH-3", "SKU-1703"): 0}
HEAVY = {"SKU-1421", "SKU-1703", "SKU-1802"}


def _poly(lat, lon, half):
    return f"(({lon - half},{lat - half}),({lon + half},{lat - half}),({lon + half},{lat + half}),({lon - half},{lat + half}))"


def load_reference(pipeline, when: str) -> None:
    trace = pipeline.new_trace("Load reference data (catalogue, customers, staff, warehouses, geofences)", "seed")
    with db.POOL.connection() as c:
        with db.write_tx(c, "seed", trace):
            ids = {}
            for slug, name, parent in CATEGORIES:
                ids[slug] = c.execute("INSERT INTO shop.category (slug, name, parent_id) VALUES (?, ?, ?) RETURNING category_id",
                                      (slug, name, ids.get(parent))).fetchone()["category_id"]
            for sid, code, title, brand, cat, kg, mrp, price, emoji, color, desc in PRODUCTS:
                c.execute("INSERT INTO shop.sku (sku_id, sku_code, description, long_description, brand, category_id, weight_kg, "
                          "mrp, price, image_emoji, image_color) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                          (sid, code, title, desc, brand, ids[cat], kg, mrp, price, emoji, color))
            for zid, name, pin, lat, lon in ZONES:
                c.execute("INSERT INTO logistics.geofence (geofence_id, name, geofence_type, boundary) VALUES (?,?,?,?::polygon)",
                          (zid, f"{name} delivery zone", "DELIVERY_ZONE", _poly(lat, lon, ZONE_HALF)))
            for wid, name, addr, gid, lat, lon in WAREHOUSES:
                c.execute("INSERT INTO logistics.geofence (geofence_id, name, geofence_type, boundary) VALUES (?,?,?,?::polygon)",
                          (gid, f"{name} yard", "WAREHOUSE", _poly(lat, lon, WH_HALF)))
                c.execute("INSERT INTO logistics.warehouse (warehouse_id, name, address, geofence_id) VALUES (?,?,?,?)",
                          (wid, name, addr, gid))
            for vid, reg, vt in VEHICLES:
                c.execute("INSERT INTO logistics.vehicle (vehicle_id, registration_no, vehicle_type) VALUES (?,?,?)", (vid, reg, vt))
            for name, email, pw, role, veh in STAFF:
                c.execute("INSERT INTO logistics.staff_user (name, email, password_hash, role, vehicle_id) VALUES (?,?,?,?,?)",
                          (name, email, auth.hash_password(pw), role, veh))
            pins = {z[1]: z[2] for z in ZONES}
            for name, email, phone in CUSTOMERS:
                cid = c.execute("INSERT INTO shop.customer (name, email, phone, password_hash, created_at) VALUES (?,?,?,?,?) "
                                "RETURNING customer_id", (name, email, phone, auth.hash_password("demo123"), when)).fetchone()["customer_id"]
                zone = CUSTOMER_ZONE[name]
                line1, lat, lon = ADDRESSES[zone][0]
                c.execute("INSERT INTO shop.customer_address (customer_id, label, recipient, phone, line1, locality, pincode, lat, lon, is_default) "
                          "VALUES (?, 'Home', ?, ?, ?, ?, ?, ?, ?, true)", (cid, name, phone, line1, zone, pins[zone], lat, lon))
                if zone in ("Indiranagar", "Koramangala"):          # a second address for some customers
                    line1, lat, lon = ADDRESSES[zone][1]
                    c.execute("INSERT INTO shop.customer_address (customer_id, label, recipient, phone, line1, locality, pincode, lat, lon) "
                              "VALUES (?, 'Work', ?, ?, ?, ?, ?, ?, ?)", (cid, name, phone, line1, zone, pins[zone], lat, lon))


def place(app, pipeline, customer_id: str, items: list[tuple[str, int]], placed_at: str, method: str = "UPI", label: str = ""):
    """Fill a cart and check out through the real stored function; returns (order_id, shipment_id)."""
    trace = pipeline.new_trace(label or f"{customer_id} checks out", "seed")
    with db.POOL.connection() as c:
        with db.write_tx(c, "seed", trace):
            c.execute("DELETE FROM shop.cart_item WHERE customer_id = ?", (customer_id,))
            for sku, qty in items:
                c.execute("INSERT INTO shop.cart_item (customer_id, sku_id, quantity) VALUES (?, ?, ?)", (customer_id, sku, qty))
            addr = c.execute("SELECT address_id FROM shop.customer_address WHERE customer_id = ? AND is_default", (customer_id,)).fetchone()
            oid = c.execute("SELECT shop.place_order(?, ?, ?, ?::timestamptz) AS id", (customer_id, addr["address_id"], method, placed_at)).fetchone()["id"]
    pipeline.flush_outbox()
    pipeline.wait_idle(30)
    row = app.q1("SELECT shipment_id FROM logistics.shipment WHERE order_id = ?", (oid,))
    return oid, row["shipment_id"] if row else None


def seed_demo(pipeline, app, history: int = 8, verbose: bool = False) -> dict:
    rng = random.Random(42)
    now = datetime.now(timezone.utc)
    iso = fmt_ts
    say = (lambda m: termlog.info(m, tag="SEED")) if verbose else (lambda m: None)
    termlog.set_quiet(True)
    try:
        load_reference(pipeline, iso(now - timedelta(days=30)))
        say("reference data loaded")

        # opening stock (events -> inventory handler -> ledger + balance)
        t_stock = iso(now - timedelta(days=3))
        trace = pipeline.new_trace("Opening stock (STOCK_IN events)", "seed")
        for wid, *_ in WAREHOUSES:
            for p in PRODUCTS:
                sid = p[0]
                qty = LOW_STOCK.get((wid, sid), rng.randint(8, 25) if sid in HEAVY else rng.randint(40, 120))
                if qty > 0:
                    app.stock_in(wid, sid, qty, event_time=t_stock, trace_id=trace)
        pipeline.wait_idle(60)
        say("opening stock received")

        # historical, fully delivered orders (they feed the analytics and the ML accuracy comparison)
        small = [p[0] for p in PRODUCTS if p[0] not in HEAVY]
        for i in range(1, history + 1):
            cust = f"CUS-{rng.randint(1, 8)}"
            lines = [(s, rng.randint(1, 2)) for s in rng.sample(small, rng.randint(1, 3))]
            start = now - timedelta(hours=rng.randint(5, 60), minutes=rng.randint(0, 59))
            oid, sid = place(app, pipeline, cust, lines, iso(start), rng.choice(["UPI", "CARD", "COD", "NETBANKING"]), f"History: order for {cust}")
            veh = rng.choice(VEHICLES)[0]
            anomaly = {4: "traffic_jam", 7: "breakdown"}.get(i)
            drive(app, sid, start_time=iso(start + timedelta(minutes=40)), anomaly=anomaly, step_min=3.0, seed=100 + i,
                  sync_every=3, label=f"History: simulated trip {sid}", vehicle_id=veh)
            say(f"history order {oid} / {sid} delivered")

        # current, narrated orders (shipments SHP-101 ..; the design document's SHP-101 / SHP-102 are Asha Rao's)
        s = db.connect("seed-admin", user=config.PG_SUPER_USER, password=config.PG_SUPER_PASSWORD, log=False)
        s.execute("SELECT setval('logistics.shipment_seq', 100)")
        s.close()

        def order(cust, lines, minutes_ago, method="UPI"):
            t = iso(now - timedelta(minutes=minutes_ago))
            oid, sid = place(app, pipeline, cust, lines, t, method, f"{cust} places an order")
            return oid, sid, t

        oid, sid, t = order("CUS-7", [("SKU-1001", 2), ("SKU-1042", 1)], 40)
        app.assign_vehicle(sid, "VEH-03", event_time=add_minutes(t, 3)); pipeline.wait_idle(30)
        drive(app, sid, until=0.55, start_time=add_minutes(t, 5), step_min=2.0, seed=1, sync_every=2,
              label=f"{sid} leaves the warehouse and drives (partial)")
        order("CUS-7", [("SKU-1001", 1)], 20, "COD")
        oid, sid, t = order("CUS-2", [("SKU-1100", 1), ("SKU-1600", 2)], 200)
        drive(app, sid, start_time=add_minutes(t, 5), step_min=3.0, seed=3, sync_every=3, vehicle_id="VEH-01",
              label=f"{sid} completes its delivery")
        oid, sid, t = order("CUS-4", [("SKU-1205", 1)], 75, "CARD")
        drive(app, sid, until="ofd", start_time=add_minutes(t, 5), step_min=2.0, seed=4, sync_every=2, vehicle_id="VEH-04",
              label=f"{sid} drives until it reaches the delivery zone")
        oid, sid, t = order("CUS-5", [("SKU-1310", 1)], 110)
        drive(app, sid, until="breakdown", anomaly="breakdown", start_time=add_minutes(t, 5), step_min=3.0, seed=5,
              sync_every=2, final_exception="VEHICLE_BREAKDOWN", vehicle_id="VEH-05", label=f"{sid} breaks down on the way")
        oid, sid, t = order("CUS-3", [("SKU-1555", 1)], 12)
        app.assign_vehicle(sid, "VEH-02", event_time=add_minutes(t, 3)); pipeline.wait_idle(30)
        app.set_status(sid, "PICKED_UP", event_time=add_minutes(t, 6), trace_id=pipeline.new_trace("picked up", "seed"))
        pipeline.wait_idle(30)
        with db.POOL.connection() as c:
            c.execute("SELECT shop.refresh_daily_sales()")
        say("demo orders created")
    finally:
        termlog.set_quiet(False)
    return {"history_orders": history}


def full_reset(pipeline, app, history: int = 8, verbose: bool = True) -> None:
    """Drop and recreate every schema, then load the demo data (the app and consumers keep running)."""
    pipeline.wait_idle(10)
    pipeline.pause()
    try:
        dbsetup.setup(quiet=True)
        pipeline.broker.reset()
        pipeline.reset_state()
    finally:
        pipeline.resume()
    seed_demo(pipeline, app, history=history, verbose=verbose)
    pipeline.wait_idle(60)
