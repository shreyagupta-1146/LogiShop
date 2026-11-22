# LogiShop — real-time logistics & delivery tracking
### a shopping application on PostgreSQL, with event sourcing and a Kafka-style event stream

Team: Shreya Gupta (1RV24CS264), Sahana V S (1RV24CS240) — DBMS (CD252IA), RV College of Engineering

A normal online shop (browse → cart → checkout → live order tracking → returns), a back office for dispatchers and a driver app.
Everything the application does is visible in four real places: **the browser**, **PostgreSQL** (pgAdmin / psql), **the terminal log**
and **the code**. See **[DEMO_GUIDE.md](DEMO_GUIDE.md)** for the step-by-step presentation.

## Run it

Requirements: Docker Desktop (running), Python 3.10+ with `pip install -r requirements.txt`.

```powershell
cd C:\Users\Shreya\OneDrive\Desktop\DBMS
.\scripts\demo_windows.ps1          # database + pgAdmin + app + 3 terminals + browser tabs
```
or step by step: `.\scripts\start_db.ps1`, then `python run.py`.

| | |
|---|---|
| Shop | http://127.0.0.1:8765/  (asha.rao@example.com / demo123) |
| Back office | http://127.0.0.1:8765/admin  (admin@logistics.local / admin123) |
| Driver app | http://127.0.0.1:8765/driver  (ramesh@logistics.local / driver123) |
| pgAdmin | http://127.0.0.1:5050  (pre-connected, no login) |
| PostgreSQL | `127.0.0.1:5433`, database `logistics`; roles `app_user` / `demo_viewer` (read-only, password `demo`) / `postgres` |
| Demo accounts | seed data in `logistics/seed.py` (local demo only; the list of logins is kept out of the repository) |
| **Where the database is in the code** | **[DATABASE_IN_THE_CODE.md](DATABASE_IN_THE_CODE.md)** - tables → definition line → the Python lines that use them; each button traced to its SQL (regenerate: `python scripts\db_map.py`) |
| Export all tables to files | `python scripts\export_db.py` → `exports\logistics_all_tables.xlsx`, `exports\csv\`, `exports\logistics_full_dump.sql` |
| Terminal tools | `python db_watch.py` (live row changes), `python db_log.py` (PostgreSQL's own statement log), `.\scripts\psql.ps1` |

## Architecture

```
 browser ──HTTP──► web-api ───────────────► PostgreSQL 16  (docker: logistics-db, schemas shop | logistics | infra)
 (shop/admin/driver)   │  checkout = SELECT shop.place_order()  → order + lines + payment + outbox row, ONE transaction
                       ▼
                 outbox relay (LISTEN/NOTIFY, FOR UPDATE SKIP LOCKED) ──► Kafka-style broker (data/broker.db)
                                                                             │ topics: orders · shipment-events · gps-pings ·
   driver / dispatcher / customer requests ──events──────────────────────────┤ customer-requests · inventory-events · notifications
                                                                             ▼
   order-handler → event-writer → state-updater → inventory-handler → request-handler → ml-service   (each: own thread + own DB connection)
                                  └────────────────────── write PostgreSQL ─────────────────────────┘
```

* **PostgreSQL** holds everything: 27 tables, 11 views, 21 functions, 42 triggers (lifecycle, append-only logs, immutable orders,
  ledger consistency, order status sync, price history, audit), least-privilege roles, GiST/GIN/BRIN/partial indexes, polygons for geofences.
* **Event sourcing**: `logistics.shipment_event` (append-only) is the source of truth; `shipment` and `inventory_balance` are projections that
  can be verified and rebuilt (`logistics/replay.py`).
* **Reliability**: transactional outbox, at-least-once delivery, idempotent consumers (`infra.processed_event`), dependency retry, dead letters,
  saga compensation (cancel + refund when stock disappears).
* **ML**: ETA by rule-based / linear regression / gradient boosting (fallback to rule-based) and an Isolation Forest for anomalies; accuracy is
  measured live in the database (`logistics.v_ml_model_accuracy`).

### Differences from the written documents (please read)
* **PostgreSQL instead of MongoDB** — you asked for a real SQL database; the design document's relational schema (BCNF) is implemented and
  extended with a shop layer (catalogue, customers, addresses, carts, orders, payments). The Mongo-style document is the view `logistics.v_shipment_document`.
* **Kafka is emulated** (`logistics/broker.py`, `data/broker.db`) with the same semantics (partitions by key, offsets, consumer groups, replay).
* Payments are simulated; no card data exists anywhere.

## Project layout

```
run.py  db_watch.py  db_log.py  DEMO_GUIDE.md  DATABASE_IN_THE_CODE.md  requirements.txt
deploy/   docker-compose.yml (PostgreSQL + pgAdmin)  pgadmin/  psqlrc
scripts/  start_db.ps1  demo_windows.ps1  psql.ps1  stop_all.ps1  export_db.py  db_map.py (generates DATABASE_IN_THE_CODE.md)
sql/      00_bootstrap  01_schema  02_functions_triggers  03_views  04_grants  demo_queries.sql
logistics/  api server db termlog broker outbox pipeline producers domain replay seed simulator auth dbsetup config events
            consumers/ (order_handler event_writer state_updater inventory_handler request_handler ml_service base)   ml/
web/      index.html (shop)  admin.html  driver.html  css/  js/
tests/    test_system.py   (31 tests against a real PostgreSQL: python -m unittest discover -s tests -v)
```
