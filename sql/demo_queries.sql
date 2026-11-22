-- =====================================================================
--  DEMO QUERIES  -  for psql (scripts\psql.ps1) or pgAdmin's Query Tool
--
--  Read-only blocks run as  demo_viewer.   Blocks marked  [as app_user]  change data inside
--  BEGIN ... ROLLBACK, so you can try the violations without leaving traces
--  (open psql with:  scripts\psql.ps1 -User app_user ).
-- =====================================================================

-- ### 1. THE SCHEMA ----------------------------------------------------
-- psql shortcuts:  \dn+   \dt shop.*   \dt logistics.*   \dt infra.*   \dv *.*   \df shop.*   \d shop.customer_order

-- 1.1 every table with its column count
SELECT table_schema, table_name,
       (SELECT count(*) FROM information_schema.columns c WHERE c.table_schema = t.table_schema AND c.table_name = t.table_name) AS columns
  FROM information_schema.tables t
 WHERE table_schema IN ('shop', 'logistics', 'infra') AND table_type = 'BASE TABLE'
 ORDER BY 1, 2;

-- 1.2 the foreign keys: the ER diagram as data
SELECT conrelid::regclass AS child_table, confrelid::regclass AS parent_table, conname, pg_get_constraintdef(oid) AS definition
  FROM pg_constraint
 WHERE contype = 'f' AND connamespace::regnamespace::text IN ('shop', 'logistics')
 ORDER BY 1, 2;

-- 1.3 every constraint on one table (primary key, CHECKs, foreign keys, unique)
SELECT conname, contype, pg_get_constraintdef(oid) AS definition
  FROM pg_constraint WHERE conrelid = 'logistics.shipment'::regclass ORDER BY contype, conname;

-- 1.4 business rules that live inside the database (triggers)
SELECT c.relnamespace::regnamespace || '.' || c.relname AS on_table, t.tgname AS trigger_name,
       CASE WHEN t.tgtype & 2 = 2 THEN 'BEFORE' ELSE 'AFTER' END AS timing, p.proname AS function
  FROM pg_trigger t
  JOIN pg_class c ON c.oid = t.tgrelid
  JOIN pg_proc p ON p.oid = t.tgfoid
 WHERE NOT t.tgisinternal AND t.tgname NOT LIKE 'aud\_%'
   AND c.relnamespace::regnamespace::text IN ('shop', 'logistics', 'infra')
 ORDER BY 1, 2;

-- 1.5 indexes (btree, partial, GIN, GiST, BRIN)
SELECT schemaname, tablename, indexname, left(indexdef, 120) AS definition
  FROM pg_indexes WHERE schemaname IN ('shop', 'logistics') AND indexname NOT LIKE '%\_pkey' ORDER BY 1, 2, 3;

-- ### 2. CATALOGUE AND SEARCH -----------------------------------------
-- 2.1 best discounts
SELECT sku_id, description, brand, price, mrp, round((1 - price / mrp) * 100) AS pct_off
  FROM shop.sku ORDER BY pct_off DESC, sku_id LIMIT 8;

-- 2.2 full-text search (what the shop's search box runs; GIN index on search_vec)
SELECT sku_id, description FROM shop.sku WHERE search_vec @@ to_tsquery('simple', 'wireless:*');

-- 2.3 does it use the index?  (the table is tiny, so we force the planner for the demonstration)
SET enable_seqscan = off;
EXPLAIN SELECT sku_id FROM shop.sku WHERE search_vec @@ to_tsquery('simple', 'wireless:*');
RESET enable_seqscan;

-- 2.4 the category tree (recursive CTE over a self-referencing foreign key)
WITH RECURSIVE tree AS (
    SELECT category_id, name, parent_id, name::text AS path FROM shop.category WHERE parent_id IS NULL
    UNION ALL
    SELECT c.category_id, c.name, c.parent_id, t.path || ' > ' || c.name FROM shop.category c JOIN tree t ON c.parent_id = t.category_id)
SELECT path FROM tree ORDER BY path;

-- ### 3. CUSTOMERS, ORDERS, PAYMENTS ----------------------------------
-- 3.1 orders as the customer sees them (view joining order + payment + shipment)
SELECT order_id, customer_id, order_status, total, payment_method, payment_status, shipment_id, shipment_status, eta_current
  FROM shop.v_order_summary ORDER BY placed_at DESC LIMIT 10;

-- 3.2 one order: header, lines (price at purchase time) and payment
SELECT o.order_id, c.name AS customer, o.status, l.sku_id, l.quantity, l.unit_price, l.line_total,
       o.subtotal, o.delivery_fee, o.total, p.kind, p.method, p.status AS payment_status
  FROM shop.customer_order o
  JOIN shop.customer c USING (customer_id)
  JOIN shop.order_line l USING (order_id)
  JOIN shop.payment p USING (order_id)
 WHERE o.order_id = (SELECT max(order_id) FROM shop.customer_order);

-- 3.3 passwords are never stored: only salted PBKDF2 hashes
SELECT customer_id, name, email, left(password_hash, 34) || '...' AS password_hash FROM shop.customer ORDER BY customer_id;

-- ### 4. WHAT DID THAT CLICK CHANGE?  (audit log written by triggers) ----
-- 4.1 every row changed by the most recent checkout, in order, with the component that wrote it
SELECT change_id, ts::time(3) AS at, actor, op, table_name, pk
  FROM infra.audit_log
 WHERE trace_id = (SELECT trace_id FROM infra.audit_log WHERE table_name = 'shop.customer_order' AND op = 'INSERT' ORDER BY change_id DESC LIMIT 1)
 ORDER BY change_id;

-- 4.2 column-level diff of every UPDATE (jsonb_each turns the stored row images into key/value pairs)
SELECT a.change_id, a.actor, a.table_name, a.pk, o.key AS column_name, o.value AS old_value, n.value AS new_value
  FROM infra.audit_log a, jsonb_each(a.old_row) o, jsonb_each(a.new_row) n
 WHERE a.op = 'UPDATE' AND o.key = n.key AND o.value IS DISTINCT FROM n.value
   AND a.table_name IN ('shop.customer_order', 'logistics.inventory_balance', 'logistics.shipment')
 ORDER BY a.change_id DESC LIMIT 30;

-- 4.4 who writes where
SELECT actor, table_name, op, count(*) AS changes FROM infra.audit_log GROUP BY 1, 2, 3 ORDER BY changes DESC LIMIT 20;

-- 4.5 the transactional outbox: written with the order in ONE transaction, published to Kafka afterwards
SELECT outbox_id, topic, msg_key, payload ->> 'event_type' AS type, created_at::time(3) AS created, published_at::time(3) AS published
  FROM infra.outbox ORDER BY outbox_id DESC LIMIT 6;

-- ### 5. SHIPMENTS AND EVENT SOURCING ---------------------------------
-- 5.1 current state of every shipment (a projection)
SELECT shipment_id, order_id, status, customer_name, registration_no, zone_name, eta_current, at_risk FROM logistics.v_shipment_overview ORDER BY created_at DESC LIMIT 8;

-- 5.2 the event log of one shipment (append-only; log_seq is the order of application)
SELECT log_seq, event_time, event_type, payload FROM logistics.shipment_event WHERE shipment_id = 'SHP-101' ORDER BY log_seq;

-- 5.3 query INSIDE the JSON payload (GIN index): all events about a zone, then all events with a reason
SELECT shipment_id, event_type, event_time FROM logistics.shipment_event WHERE payload @> '{"zone_id": "GF-Z1"}' ORDER BY log_seq;
SELECT shipment_id, event_type, payload ->> 'reason' AS reason FROM logistics.shipment_event WHERE payload ? 'reason' ORDER BY log_seq DESC LIMIT 10;

-- 5.4 the projection must equal the event log (empty = consistent)
SELECT * FROM logistics.v_status_audit WHERE audit <> 'OK';

-- 5.5 how long did each lifecycle step take?  (window function LAG)
SELECT shipment_id, event_type, event_time,
       round((EXTRACT(EPOCH FROM event_time - LAG(event_time) OVER (PARTITION BY shipment_id ORDER BY log_seq)) / 60)::numeric, 1) AS minutes_since_previous
  FROM logistics.shipment_event
 WHERE event_type IN ('SHIPMENT_CREATED', 'PICKED_UP', 'IN_TRANSIT', 'OUT_FOR_DELIVERY', 'DELIVERED', 'EXCEPTION')
 ORDER BY shipment_id, log_seq LIMIT 24;

-- 5.6 events the State Updater refused (invalid transitions are logged, never applied)
SELECT shipment_id, payload ->> 'rejected_type' AS attempted, payload ->> 'current_status' AS was, payload ->> 'reason' AS reason
  FROM logistics.shipment_event WHERE event_type = 'EVENT_REJECTED' ORDER BY log_seq DESC;

-- 5.7 the MongoDB-style document of the original design, assembled from the relational tables
SELECT jsonb_pretty(document) FROM logistics.v_shipment_document WHERE _id = 'SHP-101';

-- 5.8 idempotent consumers: which events did each consumer apply?
SELECT consumer, count(*) AS events_applied FROM infra.processed_event GROUP BY 1 ORDER BY 2 DESC;

-- ### 6. INVENTORY -----------------------------------------------------
-- 6.1 low or empty stock lines
SELECT warehouse, sku_id, description, available_qty, level FROM logistics.v_stock_levels WHERE level <> 'OK' ORDER BY level DESC, warehouse;

-- 6.2 consistency: balance must equal the sum of the ledger (empty = consistent)
SELECT * FROM logistics.v_inventory_audit WHERE audit <> 'OK';

-- 6.3 the ledger of one stock line with a running balance
SELECT ledger_id, event_time, movement_type, quantity_delta,
       sum(quantity_delta) OVER (ORDER BY ledger_id) AS running_balance, shipment_id, request_id
  FROM logistics.inventory_ledger
 WHERE (warehouse_id, sku_id) = (SELECT warehouse_id, sku_id FROM logistics.inventory_ledger GROUP BY 1, 2 ORDER BY count(*) DESC LIMIT 1)   -- the busiest stock line
 ORDER BY ledger_id;

-- ### 7. CONSTRAINTS AND TRIGGERS  [as app_user]  (each block rolls back) ------
-- 7.1 an illegal lifecycle jump is blocked by a trigger
BEGIN;
UPDATE logistics.shipment SET status = 'DELIVERED', actual_delivery_at = now() WHERE shipment_id = (SELECT shipment_id FROM logistics.shipment WHERE status = 'CREATED' LIMIT 1);
ROLLBACK;

-- 7.2 a delivered shipment is final
BEGIN;
UPDATE logistics.shipment SET status = 'IN_TRANSIT', actual_delivery_at = NULL WHERE shipment_id = (SELECT shipment_id FROM logistics.shipment WHERE status = 'DELIVERED' LIMIT 1);
ROLLBACK;

-- 7.3 stock can never go below zero (CHECK constraint)
BEGIN;
UPDATE logistics.inventory_balance SET available_qty = -1 WHERE warehouse_id = 'WH-1' AND sku_id = 'SKU-1001';
ROLLBACK;

-- 7.4 the selling price may not exceed the MRP (CHECK)
BEGIN;
UPDATE shop.sku SET price = mrp + 1 WHERE sku_id = 'SKU-1001';
ROLLBACK;

-- 7.5 the append-only event log cannot be edited (privileges AND trigger: even the owner is stopped by the trigger)
BEGIN;
DELETE FROM logistics.shipment_event WHERE shipment_id = 'SHP-101';
ROLLBACK;

-- 7.6 orders are never deleted and order lines never change (the price a customer paid is history)
BEGIN;
DELETE FROM shop.customer_order WHERE order_id = (SELECT min(order_id) FROM shop.customer_order);
ROLLBACK;
BEGIN;
UPDATE shop.order_line SET unit_price = 1 WHERE order_id = (SELECT min(order_id) FROM shop.customer_order);
ROLLBACK;

-- 7.7 unique key: a second account with the same e-mail (CITEXT: case does not matter)
BEGIN;
INSERT INTO shop.customer (name, email, password_hash) VALUES ('Duplicate', 'ASHA.RAO@example.com', 'x');
ROLLBACK;

-- 7.8 foreign key: a GPS ping for a shipment that does not exist
BEGIN;
INSERT INTO logistics.gps_ping (shipment_id, vehicle_id, ping_time, lat, lon, speed_kmh, heading_deg) VALUES ('SHP-9999', 'VEH-01', now(), 12.97, 77.59, 10, 90);
ROLLBACK;

-- 7.9 only one default address per customer (partial unique index)
BEGIN;
UPDATE shop.customer_address SET is_default = true WHERE customer_id = 'CUS-7';
ROLLBACK;

-- 7.10 the audit log is written only by the trigger (SECURITY DEFINER): the application itself cannot write it
BEGIN;
INSERT INTO infra.audit_log (table_name, op, actor) VALUES ('x', 'INSERT', 'me');
ROLLBACK;

-- 7.11 atomicity: two updates, the second fails -> BOTH are undone
BEGIN;
UPDATE logistics.inventory_balance SET available_qty = available_qty - 1 WHERE warehouse_id = 'WH-1' AND sku_id = 'SKU-1001';
UPDATE logistics.inventory_balance SET available_qty = available_qty - 99999 WHERE warehouse_id = 'WH-1' AND sku_id = 'SKU-1042';
ROLLBACK;

-- ### 8. GEOFENCING  (native polygons + GiST index) --------------------
-- 8.1 which delivery zone contains this point?  (lon, lat)
SELECT geofence_id, name FROM logistics.geofence WHERE boundary @> point(77.644, 12.981);

-- 8.2 active shipments currently inside a delivery zone
SELECT s.shipment_id, s.status, g.name AS inside_zone
  FROM logistics.shipment s
  JOIN logistics.geofence g ON g.geofence_type = 'DELIVERY_ZONE' AND g.boundary @> point(s.current_lon, s.current_lat)
 WHERE s.status <> 'DELIVERED';

-- 8.3 distance left to drive (stored SQL function, haversine)
SELECT shipment_id, status, round(logistics.haversine_km(current_lat, current_lon, dest_lat, dest_lon)::numeric, 2) AS km_to_go
  FROM logistics.shipment WHERE current_lat IS NOT NULL AND status <> 'DELIVERED' ORDER BY km_to_go;

-- ### 9. MACHINE LEARNING IN THE DATABASE -----------------------------
-- 9.1 which ETA model is most accurate?  (mean absolute error against the real delivery time)
SELECT * FROM logistics.v_ml_model_accuracy ORDER BY mae_minutes;

-- 9.2 the three models side by side for one shipment
SELECT created_at,
       max(predicted_eta) FILTER (WHERE model_name = 'rule_based')        AS rule_based,
       max(predicted_eta) FILTER (WHERE model_name = 'linear_regression') AS linear_regression,
       max(predicted_eta) FILTER (WHERE model_name = 'gradient_boosting') AS gradient_boosting
  FROM logistics.ml_prediction
 WHERE prediction_type = 'ETA' AND shipment_id = (SELECT shipment_id FROM logistics.shipment WHERE status = 'DELIVERED' ORDER BY created_at DESC LIMIT 1)
 GROUP BY created_at ORDER BY created_at LIMIT 12;

-- 9.3 anomalous driving flagged by the Isolation Forest, and the shipments at risk
SELECT shipment_id, created_at, anomaly_score, features ->> 'stationary_min' AS stationary_min FROM logistics.ml_prediction WHERE prediction_type = 'ANOMALY' AND flagged ORDER BY prediction_id DESC LIMIT 8;
SELECT * FROM logistics.v_at_risk_shipments;

-- ### 10. ANALYTICS ----------------------------------------------------
-- 10.1 sales by category and by day (view + materialized view)
SELECT * FROM shop.v_sales_by_category ORDER BY revenue DESC;
SELECT * FROM shop.mv_daily_sales ORDER BY day DESC;

-- 10.2 customers ranked by spend (window function)
SELECT customer_id, name, total_spent, rank() OVER (ORDER BY total_spent DESC) AS spend_rank
  FROM (SELECT c.customer_id, c.name, sum(o.total) AS total_spent FROM shop.customer c JOIN shop.customer_order o USING (customer_id)
         WHERE o.status <> 'CANCELLED' GROUP BY 1, 2) x;

-- 10.3 delivery performance
SELECT outcome, count(*) AS deliveries, round(avg(minutes_vs_window_end), 1) AS avg_minutes_vs_window_end FROM logistics.v_delivery_performance GROUP BY 1;

-- ### 11. NORMALISATION (from the design document) --------------------
-- 11.1 the un-normalised SHIPMENT_FLAT, rebuilt by lossless joins of the BCNF tables
SELECT s.shipment_id, s.customer_id, c.name AS customer_name, c.phone AS customer_phone, i.sku_id, k.description AS sku_description, i.quantity, s.status, s.vehicle_id, v.registration_no
  FROM logistics.shipment s
  JOIN shop.customer c USING (customer_id)
  JOIN logistics.shipment_item i USING (shipment_id)
  JOIN shop.sku k USING (sku_id)
  LEFT JOIN logistics.vehicle v USING (vehicle_id)
 ORDER BY 1, 5 LIMIT 15;

-- 11.2 no update anomaly: change a phone number in ONE place  [as app_user]
BEGIN;
UPDATE shop.customer SET phone = '+91-9111111111' WHERE customer_id = 'CUS-7';
SELECT s.shipment_id, c.name, c.phone FROM logistics.shipment s JOIN shop.customer c USING (customer_id) WHERE c.customer_id = 'CUS-7';
ROLLBACK;

-- 11.3 functional dependency check: customer_id -> (name, phone) holds (empty = no violations)
SELECT customer_id, count(DISTINCT (name, phone)) AS variants FROM shop.customer GROUP BY customer_id HAVING count(DISTINCT (name, phone)) > 1;

-- ### 12. THE DATABASE SERVER ITSELF ----------------------------------
-- 12.1 who is connected?  one connection per application component (application_name)
SELECT application_name, state, count(*) AS connections FROM pg_stat_activity WHERE datname = 'logistics' GROUP BY 1, 2 ORDER BY 1;

-- 12.2 the most frequently executed statements (pg_stat_statements)
SELECT calls, round(total_exec_time::numeric, 1) AS total_ms, round(mean_exec_time::numeric, 3) AS mean_ms, rows, left(regexp_replace(query, '\s+', ' ', 'g'), 90) AS query
  FROM pg_stat_statements WHERE dbid = (SELECT oid FROM pg_database WHERE datname = 'logistics') ORDER BY calls DESC LIMIT 10;

-- 12.3 table activity: sequential vs index scans, inserts / updates / deletes
SELECT schemaname || '.' || relname AS table_name, seq_scan, idx_scan, n_tup_ins, n_tup_upd, n_tup_del, n_live_tup
  FROM pg_stat_user_tables ORDER BY n_tup_ins DESC LIMIT 10;

-- 12.4 sizes
SELECT pg_size_pretty(pg_database_size('logistics')) AS database_size;
SELECT schemaname || '.' || relname AS table_name, pg_size_pretty(pg_total_relation_size(relid)) AS total_size
  FROM pg_stat_user_tables ORDER BY pg_total_relation_size(relid) DESC LIMIT 8;

-- 12.5 roles and privileges: the application cannot touch the append-only log
SELECT grantee, table_name, string_agg(privilege_type, ', ' ORDER BY privilege_type) AS privileges
  FROM information_schema.role_table_grants
 WHERE table_schema = 'logistics' AND table_name IN ('shipment_event', 'shipment') AND grantee IN ('app_user', 'demo_viewer')
 GROUP BY 1, 2 ORDER BY 2, 1;

-- 12.6 the stored function behind checkout - read it, it is the whole transaction
SELECT pg_get_functiondef('shop.place_order(text, bigint, text, timestamptz)'::regprocedure);

-- ### 13. TWO SESSIONS: LOCKING AND ATOMIC STOCK UPDATES  (run in two psql windows, as app_user) --
-- terminal A:  BEGIN;
--              UPDATE logistics.inventory_balance SET available_qty = available_qty - 1
--               WHERE warehouse_id = 'WH-2' AND sku_id = 'SKU-1420' AND available_qty >= 1;     -- do NOT commit yet
-- terminal B:  the same UPDATE ... it WAITS (row lock).   terminal C:  SELECT pid, relation::regclass, mode, granted FROM pg_locks WHERE NOT granted;
-- terminal A:  COMMIT;    -> B continues and re-checks  available_qty >= 1  on the NEW row version, so stock can never go below zero.
