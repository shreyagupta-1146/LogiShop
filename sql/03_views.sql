-- =====================================================================
--  03  VIEWS  - read models, audits and reports
-- =====================================================================
SET ROLE logistics_owner;

-- Joined, display-ready shipment list ---------------------------------
CREATE VIEW logistics.v_shipment_overview AS
SELECT s.shipment_id, s.order_id, s.status, s.at_risk,
       c.customer_id, c.name AS customer_name, c.phone AS customer_phone,
       w.warehouse_id, w.name AS warehouse_name,
       v.vehicle_id, v.registration_no, v.vehicle_type,
       g.name AS zone_name, s.zone_id,
       s.eta_original, s.eta_current, s.delivery_start, s.delivery_end,
       CASE WHEN s.eta_current IS NOT NULL AND s.status <> 'DELIVERED'
            THEN round((EXTRACT(EPOCH FROM (s.eta_current - s.delivery_end)) / 60)::numeric, 1) END AS minutes_past_window,
       s.stops_remaining, s.destination_address, s.dest_lat, s.dest_lon,
       s.current_lat, s.current_lon, s.actual_delivery_at, s.created_at, s.updated_at,
       (SELECT count(*)      FROM logistics.shipment_item i WHERE i.shipment_id = s.shipment_id) AS line_items,
       (SELECT sum(quantity) FROM logistics.shipment_item i WHERE i.shipment_id = s.shipment_id) AS units
  FROM logistics.shipment s
  JOIN shop.customer c           ON c.customer_id  = s.customer_id
  JOIN logistics.warehouse w     ON w.warehouse_id = s.origin_warehouse_id
  LEFT JOIN logistics.vehicle v  ON v.vehicle_id   = s.vehicle_id
  LEFT JOIN logistics.geofence g ON g.geofence_id  = s.zone_id;

-- What the customer sees for an order (order + shipment + payment) -----
CREATE VIEW shop.v_order_summary AS
SELECT o.order_id, o.customer_id, o.placed_at, o.status AS order_status, o.total, o.payment_method,
       o.cancel_reason,
       (SELECT p.status FROM shop.payment p WHERE p.order_id = o.order_id AND p.kind = 'CHARGE') AS payment_status,
       (SELECT sum(quantity) FROM shop.order_line l WHERE l.order_id = o.order_id) AS units,
       s.shipment_id, s.status AS shipment_status, s.eta_current, s.delivery_start, s.delivery_end, s.at_risk
  FROM shop.customer_order o
  LEFT JOIN logistics.shipment s ON s.order_id = o.order_id;

-- The MongoDB-style document of the design (section 2.3 / 5.4): items embedded, customer
-- snapshot copied in, last 10 events copied in.  One read = the whole tracking page.
CREATE VIEW logistics.v_shipment_document AS
SELECT s.shipment_id AS _id,
       jsonb_build_object(
           '_id', s.shipment_id, 'order_id', s.order_id, 'status', s.status,
           'customer', jsonb_build_object('customer_id', c.customer_id, 'name', c.name, 'phone', c.phone),
           'origin_warehouse_id', s.origin_warehouse_id, 'vehicle_id', s.vehicle_id, 'zone_id', s.zone_id,
           'current_location', CASE WHEN s.current_lat IS NULL THEN NULL
                                    ELSE jsonb_build_object('type', 'Point', 'coordinates', jsonb_build_array(s.current_lon, s.current_lat)) END,
           'eta', jsonb_build_object('original', s.eta_original, 'current', s.eta_current),
           'at_risk', s.at_risk,
           'delivery_window', jsonb_build_object('start', s.delivery_start, 'end', s.delivery_end),
           'destination', jsonb_build_object('address', s.destination_address,
                              'location', jsonb_build_object('type', 'Point', 'coordinates', jsonb_build_array(s.dest_lon, s.dest_lat))),
           'stops_remaining', s.stops_remaining,
           'items', (SELECT COALESCE(jsonb_agg(jsonb_build_object('sku_id', i.sku_id, 'quantity', i.quantity)), '[]')
                       FROM logistics.shipment_item i WHERE i.shipment_id = s.shipment_id),
           'recent_events', (SELECT COALESCE(jsonb_agg(x.j ORDER BY x.seq DESC), '[]')
                               FROM (SELECT e.log_seq AS seq,
                                            jsonb_build_object('event_id', e.event_id, 'event_type', e.event_type, 'event_time', e.event_time) AS j
                                       FROM logistics.shipment_event e WHERE e.shipment_id = s.shipment_id
                                      ORDER BY e.log_seq DESC LIMIT 10) x),
           'updated_at', s.updated_at) AS document
  FROM logistics.shipment s
  JOIN shop.customer c ON c.customer_id = s.customer_id;

-- AUDIT: inventory_balance must equal SUM(ledger.quantity_delta) --------
CREATE VIEW logistics.v_inventory_audit AS
SELECT k.warehouse_id, k.sku_id,
       COALESCE(b.available_qty, 0)                                              AS balance,
       COALESCE(l.ledger_sum, 0)                                                 AS ledger_sum,
       CASE WHEN COALESCE(b.available_qty, 0) = COALESCE(l.ledger_sum, 0) THEN 'OK' ELSE 'MISMATCH' END AS audit
  FROM (SELECT warehouse_id, sku_id FROM logistics.inventory_balance
        UNION
        SELECT warehouse_id, sku_id FROM logistics.inventory_ledger) k
  LEFT JOIN logistics.inventory_balance b USING (warehouse_id, sku_id)
  LEFT JOIN (SELECT warehouse_id, sku_id, sum(quantity_delta) AS ledger_sum
               FROM logistics.inventory_ledger GROUP BY warehouse_id, sku_id) l USING (warehouse_id, sku_id);

-- AUDIT: projected status must equal the last *applied* status event ------
-- (events refused by the State Updater are followed by an EVENT_REJECTED event naming them)
CREATE VIEW logistics.v_status_audit AS
SELECT t.shipment_id, t.projected_status, t.status_from_log,
       CASE WHEN t.projected_status = t.status_from_log THEN 'OK' ELSE 'MISMATCH' END AS audit
  FROM (SELECT s.shipment_id, s.status::text AS projected_status,
               (SELECT CASE e.event_type WHEN 'SHIPMENT_CREATED' THEN 'CREATED' ELSE e.event_type END
                  FROM logistics.shipment_event e
                 WHERE e.shipment_id = s.shipment_id
                   AND e.event_type IN ('SHIPMENT_CREATED', 'PICKED_UP', 'IN_TRANSIT', 'OUT_FOR_DELIVERY', 'DELIVERED', 'EXCEPTION')
                   AND NOT EXISTS (SELECT 1 FROM logistics.shipment_event r
                                    WHERE r.shipment_id = e.shipment_id AND r.event_type = 'EVENT_REJECTED'
                                      AND r.payload @> jsonb_build_object('rejected_event_id', e.event_id))
                 ORDER BY e.log_seq DESC LIMIT 1) AS status_from_log
          FROM logistics.shipment s) t;

CREATE VIEW logistics.v_stock_levels AS
SELECT b.warehouse_id, w.name AS warehouse, b.sku_id, k.description, b.available_qty,
       CASE WHEN b.available_qty = 0 THEN 'OUT' WHEN b.available_qty <= 5 THEN 'LOW' ELSE 'OK' END AS level,
       b.updated_at
  FROM logistics.inventory_balance b
  JOIN logistics.warehouse w ON w.warehouse_id = b.warehouse_id
  JOIN shop.sku k            ON k.sku_id       = b.sku_id;

CREATE VIEW logistics.v_delivery_performance AS
SELECT shipment_id, customer_id, created_at, delivery_end, actual_delivery_at,
       round((EXTRACT(EPOCH FROM (actual_delivery_at - delivery_end)) / 60)::numeric, 1) AS minutes_vs_window_end,
       CASE WHEN actual_delivery_at <= delivery_end THEN 'ON_TIME' ELSE 'LATE' END       AS outcome
  FROM logistics.shipment WHERE status = 'DELIVERED';

-- ML accuracy: mean absolute ETA error per model (needs delivered trips)
CREATE VIEW logistics.v_ml_model_accuracy AS
SELECT model_name, count(*) AS predictions_scored,
       round(avg(abs_error_min), 2) AS mae_minutes, round(min(abs_error_min), 2) AS best_minutes,
       round(max(abs_error_min), 2) AS worst_minutes
  FROM logistics.ml_prediction
 WHERE prediction_type = 'ETA' AND abs_error_min IS NOT NULL
 GROUP BY model_name;

CREATE VIEW logistics.v_at_risk_shipments AS
SELECT shipment_id, status, customer_name, registration_no, eta_current, delivery_end, minutes_past_window, zone_name
  FROM logistics.v_shipment_overview WHERE at_risk AND status <> 'DELIVERED';

-- analytics -------------------------------------------------------------
CREATE VIEW shop.v_sales_by_category AS
SELECT c.name AS category, count(DISTINCT o.order_id) AS orders, sum(l.quantity) AS units,
       sum(l.line_total) AS revenue
  FROM shop.order_line l
  JOIN shop.customer_order o ON o.order_id = l.order_id AND o.status <> 'CANCELLED'
  JOIN shop.sku k            ON k.sku_id = l.sku_id
  JOIN shop.category c       ON c.category_id = k.category_id
 GROUP BY c.name;

CREATE MATERIALIZED VIEW shop.mv_daily_sales AS
SELECT (placed_at AT TIME ZONE 'Asia/Kolkata')::date AS day, count(*) AS orders, sum(total) AS revenue
  FROM shop.customer_order WHERE status <> 'CANCELLED' GROUP BY 1;
CREATE UNIQUE INDEX ON shop.mv_daily_sales (day);

CREATE FUNCTION shop.refresh_daily_sales() RETURNS void LANGUAGE sql SECURITY DEFINER SET search_path = shop AS $$
    REFRESH MATERIALIZED VIEW shop.mv_daily_sales
$$;

RESET ROLE;
