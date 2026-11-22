"""Central configuration (database connection, ports, topics)."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("LOGISTICS_DATA_DIR", ROOT / "data"))
SQL_DIR = ROOT / "sql"
WEB_DIR = ROOT / "web"
MODEL_DIR = DATA_DIR / "models"

# ---- PostgreSQL (docker container "logistics-db", see deploy/docker-compose.yml) ----
PG_HOST = os.environ.get("PGHOST", "127.0.0.1")
PG_PORT = int(os.environ.get("PGPORT", "5433"))
PG_DB = os.environ.get("PGDATABASE", os.environ.get("LOGISTICS_DB_NAME", "logistics"))
PG_APP_USER, PG_APP_PASSWORD = "app_user", os.environ.get("LOGISTICS_APP_PASSWORD", "app")
PG_SUPER_USER, PG_SUPER_PASSWORD = "postgres", os.environ.get("LOGISTICS_SUPER_PASSWORD", "postgres")

# The simulated Kafka broker's log (topics / partitions / offsets / consumer groups)
BROKER_PATH = Path(os.environ.get("LOGISTICS_BROKER", DATA_DIR / "broker.db"))

HOST = os.environ.get("HOST", "127.0.0.1")        # localhost only (spec: Security)
PORT = int(os.environ.get("PORT", "8765"))

PARTITIONS = 3

# topic -> purpose.  Every message is keyed by shipment_id (orders: order_id) so one shipment's
# events always land in the same partition and keep their order.
TOPICS = {
    "orders":            "ORDER_PLACED events relayed from the transactional outbox",
    "shipment-events":   "lifecycle, ETA and alert events (state-changing, totally ordered per shipment)",
    "gps-pings":         "raw GPS telemetry from vehicles",
    "customer-requests": "reschedule / return requests and their outcomes",
    "inventory-events":  "stock-in, stock-out and return-restock commands",
    "notifications":     "customer notifications to be stored",
}

# Consumer group -> topics (the consumer programs of the DFD, plus the order handler)
CONSUMER_GROUPS = {
    "order-handler":     ["orders"],
    "event-writer":      ["shipment-events", "gps-pings", "customer-requests", "notifications"],
    "state-updater":     ["shipment-events", "gps-pings"],
    "inventory-handler": ["inventory-events"],
    "request-handler":   ["customer-requests"],
    "ml-service":        ["shipment-events", "gps-pings"],
}

# domain constants
TZ_OFFSET_MIN = 330          # IST, used for the hour-of-day traffic feature
LATE_GRACE_MIN = 5           # ETA this many minutes after the window end => at risk
RETURN_WINDOW_DAYS = 7
