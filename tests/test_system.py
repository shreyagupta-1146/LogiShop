"""End-to-end tests against a real PostgreSQL server:   python -m unittest discover -s tests -v

They use their own database ("logistics_test", created on demand in the same Docker container), the real
pipeline (outbox relay, broker, six consumers) and the real stored functions and triggers - never the demo data.
Requires:  docker compose -f deploy/docker-compose.yml up -d db
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = tempfile.mkdtemp(prefix="logi_test_")
os.environ["LOGISTICS_DATA_DIR"] = TMP
os.environ["LOGISTICS_BROKER"] = str(Path(TMP) / "broker.db")
os.environ["PGDATABASE"] = "logistics_test"
os.environ["LOG_SQL"] = "off"
if (ROOT / "data" / "models").exists():
    shutil.copytree(ROOT / "data" / "models", Path(TMP) / "models")

from logistics import auth, config, db, dbsetup, domain, replay, seed, termlog      # noqa: E402
from logistics.events import make_event                                              # noqa: E402
from logistics.pipeline import Pipeline                                              # noqa: E402
from logistics.producers import App                                                  # noqa: E402

auth.ITERATIONS = 1000                       # fast password hashing for the tests
termlog.set_quiet(True)
E = db.errors if hasattr(db, "errors") else None
import psycopg.errors as pgerr               # noqa: E402

STOCK = {"WH-1": {"SKU-1001": 10, "SKU-1042": 10, "SKU-1600": 10, "SKU-1100": 10, "SKU-1704": 10}, "WH-2": {"SKU-1420": 4, "SKU-1001": 5}}


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        dbsetup.setup("logistics_test", quiet=True)
        cls.p = Pipeline()
        cls.p.start()
        cls.app = App(cls.p)

    @classmethod
    def tearDownClass(cls):
        cls.p.stop()

    def setUp(self):
        p = self.p
        p.wait_idle(10)
        p.pause()
        dbsetup.setup("logistics_test", quiet=True)
        p.broker.reset()
        p.reset_state()
        p.resume()
        seed.load_reference(p, "2026-01-01T00:00:00Z")
        for wh, lines in STOCK.items():
            for sku, q in lines.items():
                self.app.stock_in(wh, sku, q, event_time="2026-01-01T00:00:00Z")
        self.idle()
        self.c = db.connect("test", log=False)                                                       # app_user
        self.su = db.connect("test-admin", user=config.PG_SUPER_USER, password=config.PG_SUPER_PASSWORD, log=False)
        self.addCleanup(self.c.close)
        self.addCleanup(self.su.close)

    # ---- helpers
    def idle(self):
        self.assertTrue(self.p.wait_idle(20), "pipeline did not become idle")

    def one(self, sql, *params, conn=None):
        r = (conn or self.c).execute(sql, params or None).fetchone()
        return None if r is None else r[0]

    def rows(self, sql, *params):
        return [list(r.values()) for r in self.c.execute(sql, params or None).fetchall()]

    def order(self, items=None, cust="CUS-7", method="UPI"):
        oid, sid = seed.place(self.app, self.p, cust, items or [("SKU-1001", 2), ("SKU-1042", 1)], domain.now_ts(), method)
        return oid, sid

    def status(self, sid):
        return self.one("SELECT status::text FROM logistics.shipment WHERE shipment_id = ?", sid)

    def order_status(self, oid):
        return self.one("SELECT status::text FROM shop.customer_order WHERE order_id = ?", oid)

    def stock(self, wh, sku):
        return self.one("SELECT available_qty FROM logistics.inventory_balance WHERE warehouse_id = ? AND sku_id = ?", wh, sku)

    def road(self, sid, veh="VEH-04"):
        self.app.assign_vehicle(sid, veh); self.app.set_status(sid, "PICKED_UP"); self.app.set_status(sid, "IN_TRANSIT")
        self.idle()

    def consumer(self, group):
        return next(c for c in self.p.consumers if c.group == group)


class DatabaseRules(Base):
    """Rules that live inside PostgreSQL and hold for every client."""

    def test_setup_command_never_erases_a_database_that_has_data(self):
        self.assertFalse(dbsetup.is_empty("logistics_test"))                      # setUp loaded customers
        before = self.one("SELECT count(*) FROM shop.customer")
        r = subprocess.run([sys.executable, "-m", "logistics.dbsetup", "--db", "logistics_test"], cwd=ROOT, capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("Refusing", r.stdout + r.stderr)
        self.assertEqual(self.one("SELECT count(*) FROM shop.customer"), before)  # nothing was dropped
        self.assertTrue(dbsetup.is_empty("logistics_does_not_exist_yet"))         # a missing database counts as empty (first start)

    def test_schema_objects(self):
        self.assertEqual(self.one("SELECT count(*) FROM information_schema.tables WHERE table_schema IN ('shop','logistics','infra') AND table_type='BASE TABLE'"), 27)
        self.assertGreaterEqual(self.one("SELECT count(*) FROM pg_trigger WHERE NOT tgisinternal AND tgname NOT LIKE 'aud\\_%'"), 18)

    def test_lifecycle_trigger_blocks_illegal_jump(self):
        oid, sid = self.order()
        with self.assertRaisesRegex(pgerr.CheckViolation, "lifecycle violation"):
            self.c.execute("UPDATE logistics.shipment SET status = 'DELIVERED', actual_delivery_at = now(), vehicle_id = 'VEH-01' WHERE shipment_id = ?", (sid,))
        self.assertEqual(self.status(sid), "CREATED")

    def test_delivered_is_final(self):
        oid, sid = self.order(); self.road(sid)
        self.app.set_status(sid, "OUT_FOR_DELIVERY"); self.app.set_status(sid, "DELIVERED"); self.idle()
        self.assertEqual(self.status(sid), "DELIVERED")
        with self.assertRaisesRegex(pgerr.CheckViolation, "final state"):
            self.c.execute("UPDATE logistics.shipment SET status = 'IN_TRANSIT', actual_delivery_at = NULL WHERE shipment_id = ?", (sid,))

    def test_stock_cannot_go_negative(self):
        with self.assertRaises(pgerr.CheckViolation):
            self.c.execute("UPDATE logistics.inventory_balance SET available_qty = -1 WHERE warehouse_id = 'WH-1' AND sku_id = 'SKU-1001'")

    def test_logs_are_append_only(self):
        oid, sid = self.order()
        with self.assertRaises(pgerr.InsufficientPrivilege):                 # privilege layer: the app role cannot even try
            self.c.execute("DELETE FROM logistics.shipment_event")
        for stmt in ("DELETE FROM logistics.shipment_event", "UPDATE logistics.shipment_event SET event_type = 'DELIVERED'",
                     "DELETE FROM logistics.inventory_ledger", "UPDATE logistics.inventory_ledger SET quantity_delta = 1",
                     "TRUNCATE logistics.shipment_event"):
            with self.assertRaisesRegex(pgerr.CheckViolation, "append-only"):   # trigger layer: not even the superuser
                self.su.execute(stmt)

    def test_foreign_key_and_domain_checks(self):
        oid, sid = self.order()
        with self.assertRaises(pgerr.ForeignKeyViolation):
            self.c.execute("INSERT INTO logistics.gps_ping (shipment_id, vehicle_id, ping_time, lat, lon, speed_kmh, heading_deg) VALUES ('NOPE','VEH-01',now(),12,77,1,1)")
        with self.assertRaises(pgerr.CheckViolation):
            self.c.execute("INSERT INTO logistics.gps_ping (shipment_id, vehicle_id, ping_time, lat, lon, speed_kmh, heading_deg) VALUES (?, 'VEH-01', now(), 123, 77, 1, 1)", (sid,))

    def test_ledger_must_match_shipment(self):
        oid, sid = self.order()
        with self.assertRaisesRegex(pgerr.CheckViolation, "must match a shipment line"):
            self.c.execute("INSERT INTO logistics.inventory_ledger (sku_id, warehouse_id, movement_type, quantity_delta, event_time, shipment_id) "
                           "VALUES ('SKU-1001', 'WH-1', 'SHIPMENT_OUT', -9, now(), ?)", (sid,))
        with self.assertRaises(pgerr.CheckViolation):                         # a movement points at a shipment XOR a request
            self.c.execute("INSERT INTO logistics.inventory_ledger (sku_id, warehouse_id, movement_type, quantity_delta, event_time, shipment_id, request_id) "
                           "VALUES ('SKU-1001', 'WH-1', 'STOCK_IN', 1, now(), ?, 'REQ-1')", (sid,))

    def test_orders_are_immutable(self):
        oid, sid = self.order()
        with self.assertRaises(pgerr.InsufficientPrivilege):
            self.c.execute("UPDATE shop.order_line SET unit_price = 1 WHERE order_id = ?", (oid,))
        with self.assertRaisesRegex(pgerr.CheckViolation, "never deleted"):
            self.c.execute("DELETE FROM shop.customer_order WHERE order_id = ?", (oid,))
        with self.assertRaisesRegex(pgerr.CheckViolation, "immutable"):
            self.c.execute("UPDATE shop.customer_order SET total = 1, subtotal = 1, delivery_fee = 0 WHERE order_id = ?", (oid,))
        with self.assertRaisesRegex(pgerr.CheckViolation, "cannot be changed"):
            self.su.execute("UPDATE shop.order_line SET quantity = 5 WHERE order_id = ?", (oid,))

    def test_price_history_trigger_and_price_check(self):
        old = self.one("SELECT price FROM shop.sku WHERE sku_id = 'SKU-1001'")
        self.c.execute("UPDATE shop.sku SET price = 250 WHERE sku_id = 'SKU-1001'")
        self.assertEqual(self.rows("SELECT old_price, new_price FROM shop.price_history WHERE sku_id = 'SKU-1001'"), [[old, 250.0]])
        with self.assertRaises(pgerr.CheckViolation):
            self.c.execute("UPDATE shop.sku SET price = mrp + 1 WHERE sku_id = 'SKU-1001'")

    def test_one_default_address_per_customer(self):
        with self.assertRaises(pgerr.UniqueViolation):
            self.c.execute("UPDATE shop.customer_address SET is_default = true WHERE customer_id = 'CUS-7'")

    def test_case_insensitive_unique_email(self):
        with self.assertRaises(pgerr.UniqueViolation):
            self.c.execute("INSERT INTO shop.customer (name, email, password_hash) VALUES ('Dup', 'ASHA.RAO@example.com', 'x')")

    def test_audit_log_is_trigger_only_and_attributed(self):
        with self.assertRaises(pgerr.InsufficientPrivilege):
            self.c.execute("INSERT INTO infra.audit_log (table_name, op, actor) VALUES ('x', 'INSERT', 'me')")
        self.c.execute("UPDATE shop.customer SET phone = '+91-9000000099' WHERE customer_id = 'CUS-7'")
        a = self.rows("SELECT actor, op FROM infra.audit_log WHERE table_name = 'shop.customer' AND op = 'UPDATE' ORDER BY change_id DESC LIMIT 1")
        self.assertEqual(a, [["app_user", "UPDATE"]])                          # no app.actor set -> the SQL user is recorded
        self.assertNotIn("password_hash", str(self.one("SELECT new_row FROM infra.audit_log WHERE table_name = 'shop.customer' ORDER BY change_id DESC LIMIT 1")))

    def test_viewer_is_read_only(self):
        v = db.connect("viewer", user="demo_viewer", password="demo", log=False)
        try:
            self.assertGreaterEqual(v.execute("SELECT count(*) FROM shop.customer").fetchone()[0], 8)
            with self.assertRaises(pgerr.InsufficientPrivilege):
                v.execute("DELETE FROM shop.customer")
        finally:
            v.close()

    def test_place_order_function(self):
        self.c.execute("INSERT INTO shop.cart_item (customer_id, sku_id, quantity) VALUES ('CUS-7', 'SKU-1600', 1)")     # Rs 199 -> delivery fee
        addr = self.one("SELECT address_id FROM shop.customer_address WHERE customer_id = 'CUS-7' AND is_default")
        oid = self.one("SELECT shop.place_order('CUS-7', ?, 'COD')", addr)
        o = self.c.execute("SELECT subtotal, delivery_fee, total, status::text AS status FROM shop.customer_order WHERE order_id = ?", (oid,)).fetchone()
        self.assertEqual((o["subtotal"], o["delivery_fee"], o["total"], o["status"]), (199.0, 40.0, 239.0, "PLACED"))
        self.assertEqual(self.rows("SELECT kind, status, amount FROM shop.payment WHERE order_id = ?", oid), [["CHARGE", "PENDING", 239.0]])
        self.assertEqual(self.one("SELECT count(*) FROM shop.cart_item WHERE customer_id = 'CUS-7'"), 0)
        self.assertEqual(self.one("SELECT count(*) FROM infra.outbox WHERE msg_key = ?", oid), 1)
        with self.assertRaisesRegex(pgerr.Error, "cart is empty"):
            self.c.execute("SELECT shop.place_order('CUS-7', ?, 'UPI')", (addr,))
        self.c.execute("INSERT INTO shop.cart_item (customer_id, sku_id, quantity) VALUES ('CUS-7', 'SKU-1420', 5)")
        with self.assertRaisesRegex(pgerr.Error, "Only 4 left"):
            self.c.execute("SELECT shop.place_order('CUS-7', ?, 'UPI')", (addr,))


class Pipeline_(Base):
    def test_order_to_delivery_and_replay(self):
        oid, sid = self.order(); wh = self.one("SELECT origin_warehouse_id FROM logistics.shipment WHERE shipment_id = ?", sid)
        self.assertEqual(wh, "WH-1")                                                   # nearest warehouse that has every item
        self.assertEqual(self.order_status(oid), "CONFIRMED")                          # set by a trigger on shipment
        self.assertEqual((self.stock("WH-1", "SKU-1001"), self.stock("WH-1", "SKU-1042")), (8, 9))
        self.assertEqual(self.rows("SELECT sku_id, quantity_delta FROM logistics.inventory_ledger WHERE shipment_id = ? ORDER BY sku_id", sid), [["SKU-1001", -2], ["SKU-1042", -1]])
        self.road(sid)
        self.assertEqual(self.order_status(oid), "SHIPPED")
        self.app.send_ping(sid, 12.99, 77.62, 24, 100); self.idle()
        self.assertEqual(self.one("SELECT count(*) FROM logistics.ml_prediction WHERE shipment_id = ? AND prediction_type = 'ETA'", sid), 3)
        self.app.send_ping(sid, 12.980, 77.643, 12, 100); self.idle()                   # inside the delivery zone -> geofence query
        self.assertEqual(self.status(sid), "OUT_FOR_DELIVERY")
        self.assertEqual(self.order_status(oid), "OUT_FOR_DELIVERY")
        self.app.set_status(sid, "DELIVERED"); self.idle()
        self.assertEqual(self.order_status(oid), "DELIVERED")
        self.assertEqual(self.one("SELECT count(*) FROM logistics.ml_prediction WHERE shipment_id = ? AND prediction_type = 'ETA' AND abs_error_min IS NULL", sid), 0)
        v = replay.verify(self.c)
        self.assertTrue(v["ok"], v)

    def test_cod_is_collected_on_delivery(self):
        oid, sid = self.order(method="COD")
        self.assertEqual(self.one("SELECT status FROM shop.payment WHERE order_id = ?", oid), "PENDING")
        self.road(sid); self.app.send_ping(sid, 12.980, 77.643, 12, 100); self.idle()
        self.app.set_status(sid, "DELIVERED"); self.idle()
        self.assertEqual(self.one("SELECT status FROM shop.payment WHERE order_id = ?", oid), "PAID")

    def test_invalid_transition_is_not_applied_but_logged(self):
        oid, sid = self.order()
        self.app.set_status(sid, "DELIVERED"); self.idle()
        self.assertEqual(self.status(sid), "CREATED")
        self.assertEqual(self.one("SELECT payload ->> 'rejected_type' FROM logistics.shipment_event WHERE shipment_id = ? AND event_type = 'EVENT_REJECTED'", sid), "DELIVERED")
        self.app.set_status(sid, "PICKED_UP"); self.idle()                              # no vehicle yet
        self.assertEqual(self.status(sid), "CREATED")
        self.assertTrue(replay.verify(self.c)["ok"])

    def test_no_warehouse_with_stock_cancels_and_refunds(self):
        self.consumer("order-handler").paused.set()
        self.p.wait_idle(5)
        self.c.execute("INSERT INTO shop.cart_item (customer_id, sku_id, quantity) VALUES ('CUS-7', 'SKU-1100', 2)")
        addr = self.one("SELECT address_id FROM shop.customer_address WHERE customer_id = 'CUS-7' AND is_default")
        with db.write_tx(self.c, "test", "T9999"):
            oid = self.one("SELECT shop.place_order('CUS-7', ?, 'UPI')", addr)
        self.su.execute("UPDATE logistics.inventory_balance SET available_qty = 0")      # stock vanishes before fulfilment (superuser bypasses privileges)
        self.p.flush_outbox(); self.consumer("order-handler").paused.clear(); self.p.broker.wait(0); self.idle()
        self.assertEqual(self.order_status(oid), "CANCELLED")
        self.assertEqual(self.rows("SELECT kind, status FROM shop.payment WHERE order_id = ? ORDER BY kind", oid), [["CHARGE", "REFUNDED"], ["REFUND", "PAID"]])
        self.assertEqual(self.one("SELECT count(*) FROM logistics.shipment WHERE order_id = ?", oid), 0)

    def test_stock_race_cancels_order_via_saga(self):
        inv = self.consumer("inventory-handler"); inv.paused.set(); self.p.wait_idle(5)
        oid, sid = None, None
        self.c.execute("INSERT INTO shop.cart_item (customer_id, sku_id, quantity) VALUES ('CUS-7', 'SKU-1704', 3)")
        addr = self.one("SELECT address_id FROM shop.customer_address WHERE customer_id = 'CUS-7' AND is_default")
        with db.write_tx(self.c, "test", "T9998"):
            oid = self.one("SELECT shop.place_order('CUS-7', ?, 'CARD')", addr)
        self.p.flush_outbox()
        for _ in range(100):                                                           # wait for the shipment projection
            sid = self.one("SELECT shipment_id FROM logistics.shipment WHERE order_id = ?", oid)
            if sid: break
            import time; time.sleep(0.05)
        self.assertIsNotNone(sid)
        self.su.execute("UPDATE logistics.inventory_balance SET available_qty = 1 WHERE sku_id = 'SKU-1704'")   # somebody else bought it first
        inv.paused.clear(); self.p.broker.wait(0); self.idle()
        self.assertEqual(self.status(sid), "EXCEPTION")
        self.assertEqual(self.order_status(oid), "CANCELLED")
        self.assertIn("Out of stock", self.one("SELECT cancel_reason FROM shop.customer_order WHERE order_id = ?", oid))
        self.assertEqual(self.rows("SELECT kind, status FROM shop.payment WHERE order_id = ? ORDER BY kind", oid), [["CHARGE", "REFUNDED"], ["REFUND", "PAID"]])
        self.assertEqual(self.one("SELECT count(*) FROM logistics.inventory_ledger WHERE shipment_id = ?", sid), 0)
        self.assertEqual(self.stock("WH-1", "SKU-1704"), 1)                              # untouched: the conditional UPDATE matched 0 rows

    def test_multi_line_order_rolls_back_atomically(self):
        """Direct shipment: line 1 can be served, line 2 cannot -> nothing is deducted."""
        before = (self.stock("WH-2", "SKU-1001"), self.stock("WH-2", "SKU-1420"))
        r = self.app.create_shipment("CUS-7", "WH-2", [{"sku_id": "SKU-1001", "quantity": 1}, {"sku_id": "SKU-1420", "quantity": 9}],
                                     "x", 12.98, 77.64); self.idle(); sid = r["shipment_id"]
        self.assertEqual((self.stock("WH-2", "SKU-1001"), self.stock("WH-2", "SKU-1420")), before)
        self.assertEqual(self.status(sid), "EXCEPTION")
        self.assertEqual(self.one("SELECT count(*) FROM logistics.inventory_ledger WHERE shipment_id = ?", sid), 0)

    def test_duplicate_delivery_is_ignored(self):
        oid, sid = self.order()
        counts = lambda: (self.one("SELECT count(*) FROM logistics.shipment_event"), self.one("SELECT count(*) FROM logistics.shipment"),
                          self.one("SELECT count(*) FROM logistics.inventory_ledger"), self.one("SELECT count(*) FROM logistics.notification"), self.stock("WH-1", "SKU-1001"))
        before = counts()
        for eid in (f"ORDER_PLACED:{oid}", f"SHIP-CREATED:{oid}", f"STOCK-OUT:{oid}"):
            self.app.redeliver(eid)
        self.idle()
        self.assertEqual(counts(), before)
        self.assertGreaterEqual(sum(c.stats["duplicates"] for c in self.p.consumers), 4)

    def test_outbox_is_published_exactly_once(self):
        oid, sid = self.order()
        self.assertEqual(self.rows("SELECT published_at IS NOT NULL FROM infra.outbox WHERE msg_key = ?", oid), [[True]])
        self.assertEqual(self.p.flush_outbox(), 0)

    def test_events_arriving_before_their_shipment_are_retried(self):
        sid, t = "SHP-777", domain.now_ts()
        lines = [{"sku_id": "SKU-1001", "quantity": 1}]
        self.p.publish("inventory-events", sid, make_event("STOCK_OUT", sid, {"warehouse_id": "WH-1", "items": lines}), None)
        self.p.publish("shipment-events", sid, make_event("SHIPMENT_CREATED", sid, {
            "customer_id": "CUS-1", "origin_warehouse_id": "WH-1", "delivery_start": domain.add_minutes(t, 60), "delivery_end": domain.add_minutes(t, 240),
            "destination_address": "x", "dest_lat": 12.98, "dest_lon": 77.64, "items": lines, "eta_original": domain.add_minutes(t, 90)}), None)
        self.idle()
        self.assertEqual(self.status(sid), "CREATED")
        self.assertEqual(self.stock("WH-1", "SKU-1001"), 9)
        self.assertEqual(self.p.broker.dead_letters(), [])

    def test_reschedule_and_return_rules_with_refund(self):
        oid, sid = self.order([("SKU-1001", 2), ("SKU-1600", 1)]); self.road(sid)
        tomorrow = domain.add_minutes(domain.now_ts(), 36 * 60)
        r = self.app.submit_request(sid, "RESCHEDULE", "away", tomorrow, domain.add_minutes(tomorrow, 120)); self.idle()
        self.assertEqual(self.one("SELECT status FROM logistics.customer_request WHERE request_id = ?", r["request_id"]), "APPROVED")
        self.app.send_ping(sid, 12.980, 77.643, 10, 90); self.idle()
        r2 = self.app.submit_request(sid, "RESCHEDULE", "again", tomorrow, domain.add_minutes(tomorrow, 120)); self.idle()
        self.assertEqual(self.one("SELECT status FROM logistics.customer_request WHERE request_id = ?", r2["request_id"]), "REJECTED")
        r3 = self.app.submit_request(sid, "RETURN", "early", items=[{"sku_id": "SKU-1001", "quantity": 1}]); self.idle()
        self.assertEqual(self.one("SELECT status FROM logistics.customer_request WHERE request_id = ?", r3["request_id"]), "REJECTED")
        self.app.set_status(sid, "DELIVERED"); self.idle()
        bad = self.app.submit_request(sid, "RETURN", "x", items=[{"sku_id": "SKU-1001", "quantity": 5}]); self.idle()
        self.assertEqual(self.one("SELECT status FROM logistics.customer_request WHERE request_id = ?", bad["request_id"]), "REJECTED")
        ok = self.app.submit_request(sid, "RETURN", "wrong", items=[{"sku_id": "SKU-1001", "quantity": 1, "item_condition": "GOOD"},
                                                                  {"sku_id": "SKU-1600", "quantity": 1, "item_condition": "DAMAGED"}]); self.idle()
        rid = ok["request_id"]
        self.assertEqual(self.one("SELECT status FROM logistics.customer_request WHERE request_id = ?", rid), "PICKUP_SCHEDULED")
        s0 = (self.stock("WH-1", "SKU-1001"), self.stock("WH-1", "SKU-1600"))
        self.app.advance_request(rid, "PICKED_UP"); self.app.advance_request(rid, "COMPLETED"); self.idle()
        self.assertEqual((self.stock("WH-1", "SKU-1001"), self.stock("WH-1", "SKU-1600")), (s0[0] + 1, s0[1]))     # only the GOOD item is restocked
        refund = self.one("SELECT amount FROM shop.payment WHERE order_id = ? AND kind = 'REFUND'", oid)
        self.assertEqual(refund, 299.0 + 199.0)                                                                  # both items are refunded at the price paid
        self.assertEqual(self.rows("SELECT * FROM logistics.v_inventory_audit WHERE audit <> 'OK'"), [])
        self.assertTrue(replay.verify(self.c)["ok"])

    def test_tampering_is_detected_and_rebuild_heals_it(self):
        oid, sid = self.order(); self.road(sid)
        self.su.execute(f"UPDATE logistics.shipment SET eta_current = '2031-01-01T00:00:00Z', at_risk = true WHERE shipment_id = '{sid}'")
        self.su.execute("UPDATE logistics.inventory_balance SET available_qty = available_qty + 3 WHERE warehouse_id = 'WH-1' AND sku_id = 'SKU-1001'")
        v = replay.verify(self.c)
        self.assertFalse(v["ok"])
        self.assertEqual({m["field"] for m in v["mismatches"]}, {"eta_current", "at_risk"})
        self.assertTrue(v["inventory_mismatches"])
        r = replay.rebuild(self.p)
        self.assertTrue(r["after"]["ok"], r)
        self.assertEqual(self.stock("WH-1", "SKU-1001"), 8)

    def test_audit_log_attributes_changes_to_components_and_actions(self):
        oid, sid = self.order()
        trace = self.one("SELECT trace_id FROM infra.audit_log WHERE table_name = 'shop.customer_order' AND op = 'INSERT' AND pk = ?", oid)
        got = {(r[0], r[1]) for r in self.rows("SELECT table_name, actor FROM infra.audit_log WHERE trace_id = ?", trace)}
        for expect in [("shop.customer_order", "seed"), ("shop.payment", "seed"), ("infra.outbox", "seed"), ("logistics.shipment", "state-updater"),
                       ("logistics.inventory_balance", "inventory-handler"), ("logistics.shipment_event", "event-writer"), ("logistics.notification", "event-writer"),
                       ("shop.customer_order", "state-updater")]:           # the status change PLACED -> CONFIRMED, made by a trigger inside the state updater's transaction
            self.assertIn(expect, got)

    def test_ml_models_and_fallback(self):
        oid, sid = self.order(); self.road(sid)
        self.app.send_ping(sid, 12.99, 77.62, 24, 100); self.idle()
        names = {r[0] for r in self.rows("SELECT model_name FROM logistics.ml_prediction WHERE shipment_id = ? AND prediction_type = 'ETA'", sid)}
        self.assertEqual(names, {"rule_based", "linear_regression", "gradient_boosting"})
        self.p.models.enabled = False
        try:
            self.app.send_ping(sid, 12.985, 77.63, 24, 100); self.idle()
            last = {r[0] for r in self.rows("SELECT model_name FROM logistics.ml_prediction WHERE shipment_id = ? AND prediction_type = 'ETA' ORDER BY prediction_id DESC LIMIT 1", sid)}
            self.assertEqual(last, {"rule_based"})
        finally:
            self.p.models.enabled = True

    def test_breakdown_raises_risk_alert_and_normal_trip_does_not(self):
        from logistics.simulator import drive
        oid, sid = self.order([("SKU-1001", 1)])
        drive(self.app, sid, until="breakdown", anomaly="breakdown", step_min=3.0, seed=5, sync_every=1, vehicle_id="VEH-04"); self.idle()
        self.assertTrue(self.one("SELECT at_risk FROM logistics.shipment WHERE shipment_id = ?", sid))
        self.assertGreaterEqual(self.one("SELECT count(*) FROM logistics.shipment_event WHERE shipment_id = ? AND event_type = 'EXCEPTION_RISK'", sid), 1)
        self.assertGreaterEqual(self.one("SELECT count(*) FROM logistics.notification WHERE shipment_id = ? AND notification_type = 'ALERT'", sid), 1)
        oid2, sid2 = self.order([("SKU-1100", 1)])
        drive(self.app, sid2, step_min=3.0, seed=11, sync_every=1, vehicle_id="VEH-01"); self.idle()
        self.assertEqual(self.status(sid2), "DELIVERED")
        self.assertEqual(self.one("SELECT count(*) FROM logistics.shipment_event WHERE shipment_id = ? AND event_type = 'EXCEPTION_RISK'", sid2), 0)
        acc = dict((r[0], r[1]) for r in self.rows("SELECT model_name, mae_minutes FROM logistics.v_ml_model_accuracy"))
        self.assertLess(acc["gradient_boosting"], acc["rule_based"])

    def test_gps_ping_retention_job(self):
        oid, sid = self.order(); self.road(sid)
        self.app.send_ping(sid, 12.99, 77.62, 24, 100, ping_time="2020-01-01T00:00:00Z"); self.idle()
        self.assertEqual(self.one("SELECT count(*) FROM logistics.gps_ping"), 1)
        self.assertEqual(self.app.purge_old_pings(90)["deleted"], 1)


class DomainUnit(unittest.TestCase):
    def test_state_machine(self):
        ev = lambda t, p=None, ts="2026-01-01T10:00:00Z": {"event_id": t, "shipment_id": "S", "event_type": t, "event_time": ts, "payload": p or {}}
        created = ev("SHIPMENT_CREATED", {"customer_id": "C", "origin_warehouse_id": "W", "delivery_start": "a", "delivery_end": "b", "destination_address": "x",
                                           "dest_lat": 1, "dest_lon": 1, "items": [{"sku_id": "K", "quantity": 1}]})
        s = domain.apply_event(None, created)
        with self.assertRaises(domain.Rejected):
            domain.apply_event(s, ev("DELIVERED"))
        with self.assertRaises(domain.Rejected):
            domain.apply_event(s, ev("PICKED_UP"))                       # no vehicle
        s = domain.apply_event(s, ev("VEHICLE_ASSIGNED", {"vehicle_id": "V"}))
        for t in ("PICKED_UP", "IN_TRANSIT", "OUT_FOR_DELIVERY", "DELIVERED"):
            s = domain.apply_event(s, ev(t))
        self.assertEqual(s["status"], "DELIVERED")
        with self.assertRaises(domain.Rejected):
            domain.apply_event(s, ev("EXCEPTION"))


if __name__ == "__main__":
    unittest.main()
