-- =====================================================================
--  02  FUNCTIONS AND TRIGGERS  (PL/pgSQL)
--
--  Business rules that must hold no matter which client writes: the application,
--  psql, pgAdmin ... SQLSTATE 23514 (check_violation) is used for rule violations so
--  they surface exactly like CHECK constraint failures.
-- =====================================================================
SET ROLE logistics_owner;

-- ---------------------------------------------------------------------
--  helpers
-- ---------------------------------------------------------------------
CREATE FUNCTION logistics.haversine_km(lat1 DOUBLE PRECISION, lon1 DOUBLE PRECISION,
                                       lat2 DOUBLE PRECISION, lon2 DOUBLE PRECISION)
RETURNS DOUBLE PRECISION LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$
    SELECT 2 * 6371.0088 * asin(sqrt(
        power(sin(radians(lat2 - lat1) / 2), 2) +
        cos(radians(lat1)) * cos(radians(lat2)) * power(sin(radians(lon2 - lon1) / 2), 2)))
$$;

-- the geofence query: which delivery zone contains this point?  (GiST-indexed point-in-polygon)
CREATE FUNCTION logistics.zone_for_point(p_lat DOUBLE PRECISION, p_lon DOUBLE PRECISION)
RETURNS TEXT LANGUAGE sql STABLE AS $$
    SELECT geofence_id FROM logistics.geofence
     WHERE geofence_type = 'DELIVERY_ZONE' AND boundary @> point(p_lon, p_lat)
     LIMIT 1
$$;

CREATE FUNCTION logistics.in_zone(p_zone TEXT, p_lat DOUBLE PRECISION, p_lon DOUBLE PRECISION)
RETURNS BOOLEAN LANGUAGE sql STABLE AS $$
    SELECT EXISTS (SELECT 1 FROM logistics.geofence WHERE geofence_id = p_zone AND boundary @> point(p_lon, p_lat))
$$;

-- ---------------------------------------------------------------------
--  AUDIT: one generic trigger function for every business table
--  SECURITY DEFINER: the application can cause audit rows but cannot write or edit them.
--  The component name and the user action come from session settings the application
--  sets at the start of each transaction:  set_config('app.actor' / 'app.trace_id', ..., true)
-- ---------------------------------------------------------------------
CREATE FUNCTION infra.audit_log_change() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path = infra, pg_catalog AS $$
DECLARE
    v_old JSONB;
    v_new JSONB;
    v_pk  TEXT;
    v_id  BIGINT;
BEGIN
    IF TG_OP <> 'INSERT' THEN v_old := to_jsonb(OLD) - 'password_hash' - 'search_vec'; END IF;
    IF TG_OP <> 'DELETE' THEN v_new := to_jsonb(NEW) - 'password_hash' - 'search_vec'; END IF;
    SELECT string_agg(COALESCE(COALESCE(v_new, v_old) ->> k, ''), '/' ORDER BY ord)
      INTO v_pk FROM unnest(TG_ARGV) WITH ORDINALITY AS t(k, ord);
    INSERT INTO infra.audit_log (table_name, op, pk, old_row, new_row, actor, trace_id)
    VALUES (TG_TABLE_SCHEMA || '.' || TG_TABLE_NAME, TG_OP, v_pk, v_old, v_new,
            COALESCE(NULLIF(current_setting('app.actor', true), ''), session_user::text),
            NULLIF(current_setting('app.trace_id', true), ''))
    RETURNING change_id INTO v_id;
    PERFORM pg_notify('db_changes', v_id::text);          -- wakes up LISTENing terminals (db_watch.py)
    RETURN NULL;
END $$;

DO $$
DECLARE t RECORD;
BEGIN
    FOR t IN SELECT * FROM (VALUES
        ('shop', 'customer',            ARRAY['customer_id']),
        ('shop', 'customer_address',    ARRAY['address_id']),
        ('shop', 'category',            ARRAY['category_id']),
        ('shop', 'sku',                 ARRAY['sku_id']),
        ('shop', 'price_history',       ARRAY['history_id']),
        ('shop', 'cart_item',           ARRAY['customer_id', 'sku_id']),
        ('shop', 'customer_order',      ARRAY['order_id']),
        ('shop', 'order_line',          ARRAY['order_id', 'sku_id']),
        ('shop', 'payment',             ARRAY['payment_id']),
        ('logistics', 'geofence',       ARRAY['geofence_id']),
        ('logistics', 'warehouse',      ARRAY['warehouse_id']),
        ('logistics', 'vehicle',        ARRAY['vehicle_id']),
        ('logistics', 'staff_user',     ARRAY['staff_id']),
        ('logistics', 'shipment',       ARRAY['shipment_id']),
        ('logistics', 'shipment_item',  ARRAY['shipment_id', 'sku_id']),
        ('logistics', 'shipment_event', ARRAY['event_id']),
        ('logistics', 'gps_ping',       ARRAY['ping_id']),
        ('logistics', 'inventory_ledger',  ARRAY['ledger_id']),
        ('logistics', 'inventory_balance', ARRAY['warehouse_id', 'sku_id']),
        ('logistics', 'customer_request',  ARRAY['request_id']),
        ('logistics', 'request_item',   ARRAY['request_id', 'sku_id']),
        ('logistics', 'ml_prediction',  ARRAY['prediction_id']),
        ('logistics', 'notification',   ARRAY['notification_id']),
        ('infra',     'outbox',         ARRAY['outbox_id'])
    ) AS x(sch, tbl, pk)
    LOOP
        EXECUTE format(
            'CREATE TRIGGER aud_%1$s AFTER INSERT OR UPDATE OR DELETE ON %2$I.%1$I '
            'FOR EACH ROW EXECUTE FUNCTION infra.audit_log_change(%3$s)',
            t.tbl, t.sch, (SELECT string_agg(quote_literal(k), ', ') FROM unnest(t.pk) AS k));
    END LOOP;
END $$;

-- outbox: wake the relay as soon as a row is committed
CREATE FUNCTION infra.notify_outbox() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_notify('outbox', NEW.outbox_id::text);
    RETURN NULL;
END $$;
CREATE TRIGGER trg_outbox_notify AFTER INSERT ON infra.outbox FOR EACH ROW EXECUTE FUNCTION infra.notify_outbox();

-- ---------------------------------------------------------------------
--  1. shipment lifecycle:  CREATED > PICKED_UP > IN_TRANSIT > OUT_FOR_DELIVERY > DELIVERED
--     EXCEPTION is a side state (reachable from every open state, can resume)
-- ---------------------------------------------------------------------
CREATE FUNCTION logistics.trg_shipment_lifecycle() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.status = 'DELIVERED' THEN
        RAISE EXCEPTION 'lifecycle violation: shipment % is DELIVERED, a final state', OLD.shipment_id
            USING ERRCODE = '23514';
    END IF;
    IF NOT (
        (OLD.status = 'CREATED'          AND NEW.status IN ('PICKED_UP', 'EXCEPTION')) OR
        (OLD.status = 'PICKED_UP'        AND NEW.status IN ('IN_TRANSIT', 'EXCEPTION')) OR
        (OLD.status = 'IN_TRANSIT'       AND NEW.status IN ('OUT_FOR_DELIVERY', 'EXCEPTION')) OR
        (OLD.status = 'OUT_FOR_DELIVERY' AND NEW.status IN ('DELIVERED', 'EXCEPTION')) OR
        (OLD.status = 'EXCEPTION'        AND NEW.status IN ('PICKED_UP', 'IN_TRANSIT', 'OUT_FOR_DELIVERY'))
    ) THEN
        RAISE EXCEPTION 'lifecycle violation: % -> % is not an allowed shipment status transition', OLD.status, NEW.status
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER trg_shipment_lifecycle BEFORE UPDATE OF status ON logistics.shipment
    FOR EACH ROW WHEN (NEW.status IS DISTINCT FROM OLD.status) EXECUTE FUNCTION logistics.trg_shipment_lifecycle();

CREATE FUNCTION logistics.trg_shipment_identity() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'shipment identity columns (id, order, customer, origin, created_at) are immutable'
        USING ERRCODE = '23514';
END $$;
CREATE TRIGGER trg_shipment_identity BEFORE UPDATE OF shipment_id, order_id, customer_id, origin_warehouse_id, created_at
    ON logistics.shipment FOR EACH ROW
    WHEN (NEW.shipment_id IS DISTINCT FROM OLD.shipment_id OR NEW.order_id IS DISTINCT FROM OLD.order_id
       OR NEW.customer_id IS DISTINCT FROM OLD.customer_id
       OR NEW.origin_warehouse_id IS DISTINCT FROM OLD.origin_warehouse_id OR NEW.created_at IS DISTINCT FROM OLD.created_at)
    EXECUTE FUNCTION logistics.trg_shipment_identity();

-- keep the order's status in step with its shipment; collect cash-on-delivery on delivery
CREATE FUNCTION logistics.trg_shipment_sync_order() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    v_new shop.order_status;
BEGIN
    v_new := (CASE NEW.status
                WHEN 'CREATED'          THEN 'CONFIRMED'
                WHEN 'PICKED_UP'        THEN 'SHIPPED'
                WHEN 'IN_TRANSIT'       THEN 'SHIPPED'
                WHEN 'OUT_FOR_DELIVERY' THEN 'OUT_FOR_DELIVERY'
                WHEN 'DELIVERED'        THEN 'DELIVERED'
                ELSE 'DELAYED' END)::shop.order_status;
    UPDATE shop.customer_order
       SET status = v_new, updated_at = now()
     WHERE order_id = NEW.order_id AND status <> 'CANCELLED' AND status <> v_new;
    IF NEW.status = 'DELIVERED' THEN
        UPDATE shop.payment SET status = 'PAID', updated_at = now()
         WHERE order_id = NEW.order_id AND kind = 'CHARGE' AND status = 'PENDING';
    END IF;
    RETURN NULL;
END $$;
CREATE TRIGGER trg_shipment_sync_order AFTER INSERT OR UPDATE OF status ON logistics.shipment
    FOR EACH ROW WHEN (NEW.order_id IS NOT NULL) EXECUTE FUNCTION logistics.trg_shipment_sync_order();

-- ---------------------------------------------------------------------
--  2. append-only logs (event sourcing) and immutable telemetry
-- ---------------------------------------------------------------------
CREATE FUNCTION logistics.trg_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is append-only: % is not allowed', TG_TABLE_NAME, TG_OP USING ERRCODE = '23514';
END $$;
CREATE TRIGGER trg_event_append_only  BEFORE UPDATE OR DELETE ON logistics.shipment_event
    FOR EACH ROW EXECUTE FUNCTION logistics.trg_append_only();
CREATE TRIGGER trg_event_no_truncate  BEFORE TRUNCATE ON logistics.shipment_event
    FOR EACH STATEMENT EXECUTE FUNCTION logistics.trg_append_only();
CREATE TRIGGER trg_ledger_append_only BEFORE UPDATE OR DELETE ON logistics.inventory_ledger
    FOR EACH ROW EXECUTE FUNCTION logistics.trg_append_only();
CREATE TRIGGER trg_ledger_no_truncate BEFORE TRUNCATE ON logistics.inventory_ledger
    FOR EACH STATEMENT EXECUTE FUNCTION logistics.trg_append_only();
CREATE TRIGGER trg_ping_immutable     BEFORE UPDATE ON logistics.gps_ping           -- may only expire (retention job)
    FOR EACH ROW EXECUTE FUNCTION logistics.trg_append_only();

-- ---------------------------------------------------------------------
--  3. inventory ledger must agree with the shipment / return it points to
-- ---------------------------------------------------------------------
CREATE FUNCTION logistics.trg_ledger_matches_source() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.movement_type = 'SHIPMENT_OUT' THEN
        IF NEW.warehouse_id IS DISTINCT FROM
           (SELECT origin_warehouse_id FROM logistics.shipment WHERE shipment_id = NEW.shipment_id) THEN
            RAISE EXCEPTION 'ledger warehouse % is not the origin warehouse of shipment %', NEW.warehouse_id, NEW.shipment_id
                USING ERRCODE = '23514';
        END IF;
        IF NOT EXISTS (SELECT 1 FROM logistics.shipment_item si
                        WHERE si.shipment_id = NEW.shipment_id AND si.sku_id = NEW.sku_id
                          AND si.quantity = -NEW.quantity_delta) THEN
            RAISE EXCEPTION 'ledger line must match a shipment line (same SKU, exact quantity): % x % on %',
                NEW.sku_id, -NEW.quantity_delta, NEW.shipment_id USING ERRCODE = '23514';
        END IF;
    ELSIF NEW.movement_type = 'RETURN_IN' THEN
        IF NEW.warehouse_id IS DISTINCT FROM
           (SELECT s.origin_warehouse_id FROM logistics.customer_request r
              JOIN logistics.shipment s ON s.shipment_id = r.shipment_id WHERE r.request_id = NEW.request_id) THEN
            RAISE EXCEPTION 'a return can only be restocked at the shipment''s origin warehouse' USING ERRCODE = '23514';
        END IF;
        IF NOT EXISTS (SELECT 1 FROM logistics.request_item ri
                        WHERE ri.request_id = NEW.request_id AND ri.sku_id = NEW.sku_id
                          AND ri.quantity >= NEW.quantity_delta) THEN
            RAISE EXCEPTION 'restock line does not match a returned line of request %', NEW.request_id USING ERRCODE = '23514';
        END IF;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER trg_ledger_matches_source BEFORE INSERT ON logistics.inventory_ledger
    FOR EACH ROW EXECUTE FUNCTION logistics.trg_ledger_matches_source();

-- ---------------------------------------------------------------------
--  4. customer requests
-- ---------------------------------------------------------------------
CREATE FUNCTION logistics.trg_request_insert_rules() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    s RECORD;
BEGIN
    SELECT customer_id, status INTO s FROM logistics.shipment WHERE shipment_id = NEW.shipment_id;
    IF NEW.customer_id IS DISTINCT FROM s.customer_id THEN
        RAISE EXCEPTION 'a request must be raised by the shipment''s own customer' USING ERRCODE = '23514';
    END IF;
    IF NEW.status <> 'REJECTED' THEN
        IF NEW.request_type = 'RESCHEDULE' AND s.status IN ('OUT_FOR_DELIVERY', 'DELIVERED') THEN
            RAISE EXCEPTION 'cannot reschedule: shipment is already % ', s.status USING ERRCODE = '23514';
        END IF;
        IF NEW.request_type = 'RETURN' AND s.status <> 'DELIVERED' THEN
            RAISE EXCEPTION 'cannot return: shipment has not been delivered yet (%)', s.status USING ERRCODE = '23514';
        END IF;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER trg_request_insert_rules BEFORE INSERT ON logistics.customer_request
    FOR EACH ROW EXECUTE FUNCTION logistics.trg_request_insert_rules();

CREATE FUNCTION logistics.trg_request_status_machine() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT (
        (OLD.request_type = 'RESCHEDULE' AND OLD.status = 'PENDING'          AND NEW.status IN ('APPROVED', 'REJECTED')) OR
        (OLD.request_type = 'RETURN'     AND OLD.status = 'PENDING'          AND NEW.status IN ('PICKUP_SCHEDULED', 'REJECTED')) OR
        (OLD.request_type = 'RETURN'     AND OLD.status = 'PICKUP_SCHEDULED' AND NEW.status = 'PICKED_UP') OR
        (OLD.request_type = 'RETURN'     AND OLD.status = 'PICKED_UP'        AND NEW.status = 'COMPLETED')
    ) THEN
        RAISE EXCEPTION 'invalid request status transition % -> %', OLD.status, NEW.status USING ERRCODE = '23514';
    END IF;
    IF OLD.request_type = 'RETURN' AND NEW.status = 'PICKUP_SCHEDULED'
       AND NOT EXISTS (SELECT 1 FROM logistics.request_item WHERE request_id = NEW.request_id) THEN
        RAISE EXCEPTION 'a RETURN request needs at least one item before it can be accepted' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER trg_request_status_machine BEFORE UPDATE OF status ON logistics.customer_request
    FOR EACH ROW WHEN (NEW.status IS DISTINCT FROM OLD.status) EXECUTE FUNCTION logistics.trg_request_status_machine();

CREATE FUNCTION logistics.trg_request_item_rules() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM logistics.customer_request r
                     JOIN logistics.shipment_item si ON si.shipment_id = r.shipment_id
                    WHERE r.request_id = NEW.request_id AND si.sku_id = NEW.sku_id AND si.quantity >= NEW.quantity) THEN
        RAISE EXCEPTION 'returned item % is not part of the shipment or exceeds the shipped quantity', NEW.sku_id
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER trg_request_item_rules BEFORE INSERT ON logistics.request_item
    FOR EACH ROW EXECUTE FUNCTION logistics.trg_request_item_rules();

-- notifications go to the shipment's / order's own customer
CREATE FUNCTION logistics.trg_notification_customer() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    v_owner TEXT;
BEGIN
    IF NEW.shipment_id IS NOT NULL THEN
        SELECT customer_id INTO v_owner FROM logistics.shipment WHERE shipment_id = NEW.shipment_id;
    ELSE
        SELECT customer_id INTO v_owner FROM shop.customer_order WHERE order_id = NEW.order_id;
    END IF;
    IF NEW.customer_id IS DISTINCT FROM v_owner THEN
        RAISE EXCEPTION 'notification customer must be the owner of the shipment / order' USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER trg_notification_customer BEFORE INSERT ON logistics.notification
    FOR EACH ROW EXECUTE FUNCTION logistics.trg_notification_customer();

-- ---------------------------------------------------------------------
--  5. shop rules: price history, immutable orders
-- ---------------------------------------------------------------------
CREATE FUNCTION shop.trg_price_history() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO shop.price_history (sku_id, old_price, new_price, changed_by)
    VALUES (NEW.sku_id, OLD.price, NEW.price,
            COALESCE(NULLIF(current_setting('app.actor', true), ''), session_user::text));
    RETURN NEW;
END $$;
CREATE TRIGGER trg_price_history AFTER UPDATE OF price ON shop.sku
    FOR EACH ROW WHEN (NEW.price IS DISTINCT FROM OLD.price) EXECUTE FUNCTION shop.trg_price_history();

CREATE FUNCTION shop.trg_order_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF TG_TABLE_NAME = 'order_line' THEN
        RAISE EXCEPTION 'order lines cannot be changed after the order is placed (order %)', OLD.order_id
            USING ERRCODE = '23514';
    ELSIF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'orders are never deleted - cancel them instead' USING ERRCODE = '23514';
    ELSE
        RAISE EXCEPTION 'order amounts and customer are immutable' USING ERRCODE = '23514';
    END IF;
END $$;
CREATE TRIGGER trg_order_line_frozen BEFORE UPDATE OR DELETE ON shop.order_line
    FOR EACH ROW EXECUTE FUNCTION shop.trg_order_immutable();
CREATE TRIGGER trg_order_no_delete BEFORE DELETE ON shop.customer_order
    FOR EACH ROW EXECUTE FUNCTION shop.trg_order_immutable();
CREATE TRIGGER trg_order_amounts_frozen BEFORE UPDATE OF customer_id, subtotal, delivery_fee, total, payment_method, placed_at
    ON shop.customer_order FOR EACH ROW
    WHEN (NEW.customer_id <> OLD.customer_id OR NEW.subtotal <> OLD.subtotal OR NEW.delivery_fee <> OLD.delivery_fee
       OR NEW.total <> OLD.total OR NEW.payment_method <> OLD.payment_method OR NEW.placed_at <> OLD.placed_at)
    EXECUTE FUNCTION shop.trg_order_immutable();

-- ---------------------------------------------------------------------
--  6. STORED FUNCTIONS used by the application
-- ---------------------------------------------------------------------
-- Delivery fee rule, in one place: free delivery from Rs 500, otherwise Rs 40
CREATE FUNCTION shop.delivery_fee(p_subtotal NUMERIC) RETURNS NUMERIC LANGUAGE sql IMMUTABLE AS $$
    SELECT CASE WHEN p_subtotal >= 500 THEN 0::numeric ELSE 40::numeric END
$$;

-- Checkout: ONE transaction turns the cart into an order.
--   order + order lines (price snapshot) + payment + emptied cart + outbox event.
--   The outbox row is committed together with the order and published to Kafka afterwards
--   (transactional outbox pattern), so the order and its event can never disagree.
CREATE FUNCTION shop.place_order(p_customer TEXT, p_address BIGINT, p_method TEXT,
                                 p_placed_at TIMESTAMPTZ DEFAULT now())
RETURNS TEXT LANGUAGE plpgsql AS $$
DECLARE
    a       shop.customer_address%ROWTYPE;
    r       RECORD;
    v_order TEXT;
    v_sub   NUMERIC(12,2);
    v_fee   NUMERIC(8,2);
    v_stock BIGINT;
BEGIN
    SELECT * INTO a FROM shop.customer_address WHERE address_id = p_address AND customer_id = p_customer;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'Please choose a delivery address' USING ERRCODE = 'SH001';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM shop.cart_item WHERE customer_id = p_customer) THEN
        RAISE EXCEPTION 'Your cart is empty' USING ERRCODE = 'SH002';
    END IF;

    -- optimistic stock check for a friendly message; the Inventory Handler's atomic conditional
    -- UPDATE (WHERE available_qty >= :qty) remains the final authority
    FOR r IN SELECT c.sku_id, c.quantity, s.description, s.is_active
               FROM shop.cart_item c JOIN shop.sku s USING (sku_id) WHERE c.customer_id = p_customer LOOP
        IF NOT r.is_active THEN
            RAISE EXCEPTION '"%" is no longer available', r.description USING ERRCODE = 'SH003';
        END IF;
        SELECT COALESCE(max(available_qty), 0) INTO v_stock FROM logistics.inventory_balance WHERE sku_id = r.sku_id;
        IF v_stock < r.quantity THEN
            RAISE EXCEPTION 'Only % left of "%"', v_stock, r.description USING ERRCODE = 'SH003';
        END IF;
    END LOOP;

    SELECT sum(s.price * c.quantity) INTO v_sub
      FROM shop.cart_item c JOIN shop.sku s USING (sku_id) WHERE c.customer_id = p_customer;
    v_fee := shop.delivery_fee(v_sub);

    INSERT INTO shop.customer_order (customer_id, address_id, ship_recipient, ship_phone, ship_address,
                                     ship_lat, ship_lon, subtotal, delivery_fee, total, payment_method, placed_at, updated_at)
    VALUES (p_customer, a.address_id, a.recipient, a.phone,
            concat_ws(', ', a.line1, a.line2, a.locality, a.city || ' ' || a.pincode),
            a.lat, a.lon, v_sub, v_fee, v_sub + v_fee, p_method, p_placed_at, p_placed_at)
    RETURNING order_id INTO v_order;

    INSERT INTO shop.order_line (order_id, sku_id, quantity, unit_price)
    SELECT v_order, c.sku_id, c.quantity, s.price
      FROM shop.cart_item c JOIN shop.sku s USING (sku_id) WHERE c.customer_id = p_customer;

    INSERT INTO shop.payment (order_id, kind, method, amount, status, reference)
    VALUES (v_order, 'CHARGE', p_method, v_sub + v_fee,
            CASE WHEN p_method = 'COD' THEN 'PENDING' ELSE 'PAID' END,
            CASE WHEN p_method = 'COD' THEN NULL
                 ELSE 'TXN' || upper(substr(md5(random()::text || clock_timestamp()::text), 1, 12)) END);

    DELETE FROM shop.cart_item WHERE customer_id = p_customer;

    INSERT INTO infra.outbox (topic, msg_key, payload, trace_id)
    SELECT 'orders', v_order,
           jsonb_build_object(
               'event_id',   'ORDER_PLACED:' || v_order,
               'event_type', 'ORDER_PLACED',
               'order_id',   v_order,
               'customer_id', p_customer,
               'placed_at',  to_char(o.placed_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
               'ship_address', o.ship_address, 'ship_lat', o.ship_lat, 'ship_lon', o.ship_lon,
               'total', o.total,
               'lines', (SELECT jsonb_agg(jsonb_build_object('sku_id', l.sku_id, 'quantity', l.quantity) ORDER BY l.sku_id)
                           FROM shop.order_line l WHERE l.order_id = v_order)),
           NULLIF(current_setting('app.trace_id', true), '')
      FROM shop.customer_order o WHERE o.order_id = v_order;
    RETURN v_order;
END $$;

-- Cancel an order and refund the payment (used when the warehouse cannot fulfil it)
CREATE FUNCTION shop.cancel_order(p_order TEXT, p_reason TEXT) RETURNS BOOLEAN LANGUAGE plpgsql AS $$
DECLARE
    p RECORD;
BEGIN
    UPDATE shop.customer_order SET status = 'CANCELLED', cancel_reason = p_reason, updated_at = now()
     WHERE order_id = p_order AND status NOT IN ('CANCELLED', 'DELIVERED');
    IF NOT FOUND THEN
        RETURN false;
    END IF;
    FOR p IN SELECT * FROM shop.payment WHERE order_id = p_order AND kind = 'CHARGE' FOR UPDATE LOOP
        IF p.status = 'PAID' THEN
            UPDATE shop.payment SET status = 'REFUNDED', updated_at = now() WHERE payment_id = p.payment_id;
            INSERT INTO shop.payment (order_id, kind, method, amount, status, reference)
            VALUES (p_order, 'REFUND', p.method, p.amount, 'PAID',
                    'RFD' || upper(substr(md5(random()::text || clock_timestamp()::text), 1, 12)));
        ELSIF p.status = 'PENDING' THEN
            UPDATE shop.payment SET status = 'FAILED', updated_at = now() WHERE payment_id = p.payment_id;
        END IF;
    END LOOP;
    RETURN true;
END $$;

-- Refund for a completed return (the value of the returned lines at the price paid)
CREATE FUNCTION shop.refund_return(p_request TEXT) RETURNS NUMERIC LANGUAGE plpgsql AS $$
DECLARE
    v_order  TEXT;
    v_amount NUMERIC(12,2);
    v_method TEXT;
BEGIN
    SELECT s.order_id INTO v_order FROM logistics.customer_request r
      JOIN logistics.shipment s ON s.shipment_id = r.shipment_id WHERE r.request_id = p_request;
    IF v_order IS NULL THEN RETURN 0; END IF;
    SELECT COALESCE(sum(ri.quantity * ol.unit_price), 0) INTO v_amount
      FROM logistics.request_item ri JOIN shop.order_line ol ON ol.order_id = v_order AND ol.sku_id = ri.sku_id
     WHERE ri.request_id = p_request;
    SELECT method INTO v_method FROM shop.payment WHERE order_id = v_order AND kind = 'CHARGE';
    IF v_amount > 0 THEN
        INSERT INTO shop.payment (order_id, kind, method, amount, status, reference)
        VALUES (v_order, 'REFUND', v_method, v_amount, 'PAID',
                'RFD' || upper(substr(md5(random()::text || clock_timestamp()::text), 1, 12)));
    END IF;
    RETURN v_amount;
END $$;

RESET ROLE;
