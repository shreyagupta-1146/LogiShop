-- =====================================================================
--  04  PRIVILEGES  (least privilege)
-- =====================================================================
SET ROLE logistics_owner;

GRANT USAGE ON SCHEMA shop, logistics, infra TO app_user, demo_viewer;

-- the read-only presentation user sees every table (passwords are stored only as salted PBKDF2 hashes)
GRANT SELECT ON ALL TABLES IN SCHEMA shop, logistics, infra TO demo_viewer;
GRANT SELECT ON ALL SEQUENCES IN SCHEMA shop, logistics, infra TO demo_viewer;
REVOKE ALL ON infra.auth_session FROM demo_viewer;                         -- session tokens are not for display

-- the application
GRANT SELECT, INSERT, UPDATE, DELETE ON
    shop.customer, shop.customer_address, shop.category, shop.sku, shop.cart_item, shop.customer_order, shop.payment,
    logistics.warehouse, logistics.geofence, logistics.vehicle, logistics.staff_user,
    logistics.shipment, logistics.shipment_item, logistics.inventory_balance, logistics.customer_request,
    logistics.request_item, logistics.ml_prediction, logistics.notification,
    infra.processed_event, infra.outbox, infra.auth_session
  TO app_user;
GRANT SELECT, INSERT ON shop.order_line, shop.price_history TO app_user;                 -- never updated / deleted
GRANT SELECT, INSERT ON logistics.shipment_event, logistics.inventory_ledger TO app_user; -- append-only logs
GRANT SELECT, INSERT, DELETE ON logistics.gps_ping TO app_user;                           -- DELETE only for the 90-day retention job
GRANT SELECT ON infra.audit_log TO app_user;                                              -- written only by the audit trigger
GRANT SELECT ON ALL TABLES IN SCHEMA shop, logistics TO app_user;                         -- the views
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA shop, logistics, infra TO app_user;
GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA shop, logistics TO app_user;
GRANT EXECUTE ON FUNCTION infra.audit_log_change() TO app_user;

ANALYZE;
RESET ROLE;
