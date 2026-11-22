# Presentation guide — showing the application, the database, the terminals and the code

You show **four things at once**, each in its own window:

| Window | What it is | What it proves |
|---|---|---|
| **Browser** (shop, back office, driver app) | a normal shopping application | the application behaves like a real product |
| **pgAdmin** + **psql** | the real PostgreSQL server (Docker container `logistics-db`) | the data is in a real DBMS: tables, constraints, triggers, roles, queries |
| **Terminal 1 – the app log** | `python run.py` | every HTTP request → SQL statement → Kafka message → consumer decision, in order |
| **Terminal 2/3 – the database's own view** | `python db_log.py` and `python db_watch.py` | what *PostgreSQL itself* received, and which **rows** changed (old → new) |
| **VS Code** | this folder | the code behind each step |

Everything on screen is real: there is no database panel inside the shop, and nothing is faked for the demo.

---

## 0. One-time setup (do this before the audience arrives)

1. **Docker Desktop must be running** (the database lives in a container).
2. Open PowerShell in the project folder and start everything:

```powershell
cd C:\Users\Shreya\OneDrive\Desktop\DBMS
.\scripts\demo_windows.ps1
```

This starts the database and pgAdmin, then opens four PowerShell windows (app, DB server log, change feed, psql) and the two browser tabs. If you prefer to do it by hand: `.\scripts\start_db.ps1`, then `python run.py`, `python db_log.py`, `python db_watch.py --hide ml_prediction gps_ping`, `.\scripts\psql.ps1`.

3. The first start loads demo data through the real pipeline (~20 s). To get a clean slate at any time: **Back office → System → Reset demo database**.

**Suggested layout** (one 1080p screen): browser on the left half; on the right, the app terminal on top, the change feed in the middle, psql / DB log at the bottom. pgAdmin in a second browser tab (`http://127.0.0.1:5050`). With two screens: browser + pgAdmin on one, the four terminals on the other.

### Logins

| App | URL | Login |
|---|---|---|
| Shop (customer) | http://127.0.0.1:8765/ | `asha.rao@example.com` / `demo123` (also priya.nair@, neha.sharma@, arjun.mehta@, karthik.iyer@, divya.reddy@, ravi.kumar@, imran.khan@ example.com) |
| Back office | http://127.0.0.1:8765/admin | `admin@logistics.local` / `admin123` |
| Driver app | http://127.0.0.1:8765/driver | `ramesh@logistics.local` / `driver123` (van VEH-03); also suresh@, mahesh@, kiran@ |
| pgAdmin | http://127.0.0.1:5050 | no login; open the *read-only* server (password is filled in for you) |
| psql | `.\scripts\psql.ps1` | read-only `demo_viewer`; `.\scripts\psql.ps1 -User app_user` to write; `-User postgres` for superuser |

Database facts to quote: PostgreSQL 16 · host `127.0.0.1` port `5433` · database `logistics` · **3 schemas** (`shop`, `logistics`, `infra`) · **27 tables**, 11 views, 21 stored functions, 42 triggers (18 business rules + 24 audit) · roles `postgres` (superuser), `logistics_owner` (owns objects), `app_user` (what the application uses), `demo_viewer` (read-only).

---

## 1. Opening (2 min) — "this is a real database, not a file"

* **pgAdmin** → *Logistics DB (read-only)* → Schemas: `shop` (storefront), `logistics` (the 15 relations of the design document + staff), `infra` (audit log, outbox, inbox, sessions).
* **Tools → ERD Tool** (or right-click the database → *ERD For Database*): the foreign keys draw the ER diagram of the design document.
* Open a table (`logistics.shipment` → *Properties/Constraints/Triggers*): CHECK constraints, ENUM status, partial indexes, the triggers.
* **psql**: `\dn+`, `\dt shop.*`, `\d shop.customer_order`, then `SELECT * FROM shop.v_order_summary LIMIT 5;`
* Point at roles: `\du` and run query **12.5** in `sql/demo_queries.sql` — the application role has *no* UPDATE/DELETE on `shipment_event`, the audit log can only be written by a trigger.

*Say:* "Two sides: the shop writes straight to PostgreSQL in transactions; everything about delivery is event-sourced — events go through Kafka-style topics and small consumer programs write the logistics tables."

---

## 2. Customer journey and what each click does to the database

Keep **Terminal 1** (app log) and **Terminal 3** (change feed) visible. For every step: *click in the shop → look at the terminals → run one SQL query in psql/pgAdmin*.

### 2.1 Search and browse (SELECT only)
* Shop → search **"wireless"**. The terminal shows `GET /api/shop/products?q=wireless` (SELECTs are hidden by default; restart with `$env:LOG_SQL="all"` to show them).
* psql: query **2.2** (full-text search, GIN index) and **2.3** `EXPLAIN` (index scan). *Say:* a generated `tsvector` column + GIN index; no `LIKE '%…%'` scan.

### 2.2 Sign in and add to cart
* Sign in as Asha, open *True Wireless Earbuds*, **Add to cart**.
* Terminal 1: `INSERT INTO shop.cart_item … ON CONFLICT … DO UPDATE` (an *upsert*). Terminal 3: `+ INSERT shop.cart_item CUS-7/SKU-1100`.
* Click **+** in the cart → `~ UPDATE … quantity 1 → 2`; **Remove** → `- DELETE`. Try 20 of an item → rejected by `CHECK (quantity BETWEEN 1 AND 10)`.
* psql: `SELECT * FROM shop.cart_item;`.

### 2.3 Checkout — one stored function, one transaction  ⭐ (the main demo)
* **Place order**. Walk through the terminals top to bottom:
  * **Terminal 1**: `POST /api/checkout` → `SELECT shop.place_order('CUS-7', 9, 'UPI')` (one statement!) → `COMMIT` → the **outbox relay** publishes `ORDER_PLACED` to Kafka (`orders[2]@5`) → **order-handler** consumes it, chooses the nearest warehouse with stock (SQL), publishes `SHIPMENT_CREATED` and `STOCK_OUT` → **state-updater** creates the shipment, **inventory-handler** runs the atomic conditional `UPDATE … WHERE available_qty >= qty`, **event-writer** appends to the event log, notifications are stored. You may also see one red line "waiting for dependency … retry 1": the event arrived before its shipment existed, the foreign key refused it, the consumer retried — real at-least-once handling.
  * **Terminal 3 (change feed)**: the *same click* as rows: `customer_order`, `order_line`, `payment`, `cart_item` deleted, `outbox`, then `shipment` (by state-updater), `customer_order` **PLACED → CONFIRMED** (by a **trigger**), `shipment_item`, `shipment_event`, `inventory_balance` **16 → 15**, `inventory_ledger`, `notification` — each tagged with the component that wrote it and the trace id `T00xx`.
  * **Terminal 2 (DB log)**: PostgreSQL's own record of those statements, tagged `[web-api]`, `[order-handler]`, `[inventory-handler]` … (the `application_name` of each connection).
* psql: **4.1** (all rows changed by that checkout, by trace id), **4.5** (the outbox), **3.2** (order + lines + payment), **6.3** (ledger with running balance).
* *Say:* "Order, lines, payment, emptied cart and the outbox event are written in ONE transaction. If anything fails, nothing is written — and the event can never be lost or published without the order."

### 2.4 A failed checkout leaves no trace
* Add **5 × Air Fryer** (only 2 left) → checkout shows **"Only 2 left of Air Fryer 4 L"**. The change feed prints **nothing**: the function raised an error inside the transaction, so everything rolled back. psql: `SELECT * FROM shop.cart_item;` – the cart is unchanged.

### 2.5 Order tracking
* **My orders → Track my order.** The page polls every 3 s. Keep it open for the next section.

---

## 3. Operations: dispatch, driver, live tracking, business rules

Open **Back office → Dispatch** in a second tab and the **driver app** (`/driver`) on a phone-sized window if you like.

1. **Assign vehicle** to the new shipment → event `VEHICLE_ASSIGNED` → `~ UPDATE logistics.shipment vehicle_id NULL → VEH-04`.
2. **Pick up** → `PICKED_UP`. In the change feed two rows move together: `shipment.status CREATED → PICKED_UP` and, via the trigger, `customer_order.status → SHIPPED`. The customer's page changes by itself.
3. **Invalid action on purpose**: click **Delivered** on a *CREATED* shipment. The toast says *refused*; the shipment does not change; a row `EVENT_REJECTED` appears in `shipment_event`. psql **5.6** lists it. *Say:* "the lifecycle is enforced in the State Updater **and** in the database".
4. **Same cheat in SQL** (`.\scripts\psql.ps1 -User app_user`): run **7.1**. PostgreSQL answers `lifecycle violation: CREATED -> DELIVERED is not an allowed shipment status transition` — a trigger, no matter who writes. Also try **7.3** (stock below zero → CHECK), **7.5** (delete from the append-only log → permission denied; as superuser the trigger stops it), **7.6** (orders are never deleted), **7.10** (the app cannot forge audit rows).
5. **Move the vehicle by hand**: Dispatch → *move vehicle: 25% 60% 95%* (or the same buttons in the driver app, `/driver`, signed in as ramesh@logistics.local). Each press is one GPS ping. Or press **▶ Drive** (back office) / **Auto-drive** (driver app) to run the whole trip. Watch the customer's map: the vehicle moves, the stepper advances, the ETA changes. In Terminal 3 (re-run with `python db_watch.py --only gps_ping shipment ml_prediction`): every ping = `gps_ping` row + `shipment` position update + 3 ETA predictions + an anomaly score.
6. When the vehicle enters the delivery zone, the State Updater runs the **geofence query** (point-in-polygon on a GiST-indexed `polygon`) and emits `ARRIVED_AT_ZONE` + `OUT_FOR_DELIVERY` itself. psql **8.1 / 8.2**.
7. **Delivered** → `shipment.actual_delivery_at` set, order → DELIVERED, a cash-on-delivery payment flips PENDING → PAID (trigger). The ML service then fills `abs_error_min`: psql **9.1** compares the three ETA models against the real arrival (gradient boosting wins).
8. **Breakdown** (Dispatch → ⚠ Breakdown on a moving shipment): the Isolation Forest flags the stopped vehicle, `EXCEPTION_RISK` is raised, the customer gets an alert. psql **9.3**.

## 4. Returns, refunds and rules

* Customer: on a **delivered** order → *Return items* (one good, one damaged). On an order **not yet out for delivery** → *Reschedule delivery*; try rescheduling an order that is already out for delivery (declined with a reason).
* Back office → **Returns & reschedules** → *Mark picked up* → *Receive & restock + refund*. Change feed: `inventory_ledger RETURN_IN` for the **good** item only, `inventory_balance +1`, a `payment` row of kind `REFUND` for both items.

## 5. Integrity and recovery (the database-theory part)

| Show | How | Concept |
|---|---|---|
| Atomicity | **11.x/7.11** in psql: two updates, the second violates CHECK → both undone | ACID – A |
| Consistency | `SELECT * FROM logistics.v_inventory_audit WHERE audit <> 'OK';` (empty) — balance always equals the sum of the ledger | integrity, derived data |
| Isolation / locking | two psql windows, recipe **13** at the end of `sql/demo_queries.sql` (second `UPDATE … WHERE available_qty >= 1` waits, then re-checks) | row locks, MVCC |
| Durability | `docker restart logistics-db`, refresh the shop – everything is still there | WAL, volume |
| Idempotency | Back office → System → **Redeliver last event** → counters "duplicates ignored" rise, no row changes | at-least-once + idempotent consumer |
| Event sourcing | psql superuser: `UPDATE logistics.shipment SET eta_current = '2031-01-01', at_risk = true WHERE shipment_id = 'SHP-101';` → Back office → System → **Verify** (shows the mismatch) → **Rebuild** (drops and rebuilds `shipment` from the event log in one transaction using deferred constraints) | event sourcing, recoverability |
| Security | `\du`, query **12.5**, try `DELETE` as `demo_viewer` | least privilege, SECURITY DEFINER |
| Normalisation | query **11.1** rebuilds the un-normalised `SHIPMENT_FLAT` by lossless joins; **11.2** shows no update anomaly | BCNF from the design document |

Server internals (query **12.x**): connections by component (`pg_stat_activity.application_name`), most-called statements (`pg_stat_statements`), table activity, sizes.

---

## 6. "Show me the code" — which file explains which step

> **Start with [DATABASE_IN_THE_CODE.md](DATABASE_IN_THE_CODE.md)** (open it in VS Code, Ctrl+Shift+V). It lists every table with the line where it is defined
> and the exact lines of Python that read or write it, traces each button from the browser to the table, and maps every HTTP endpoint to its SQL.
> It is generated from the code by `python scripts/db_map.py` - re-run that after editing any `.sql` file or query.
> In the back office, every page header also shows a chip such as `logistics/api.py › admin_orders()` - the function that holds that screen's SQL.

| You are explaining… | Open |
|---|---|
| tables, keys, CHECKs, indexes, ENUMs, polygons | `sql/01_schema.sql` |
| `place_order()`, lifecycle / append-only / ledger triggers, audit trigger, `cancel_order()` | `sql/02_functions_triggers.sql` |
| views (`v_order_summary`, audits, ML accuracy) | `sql/03_views.sql` |
| roles and privileges | `sql/00_bootstrap.sql`, `sql/04_grants.sql` |
| the SQL behind every screen | `logistics/api.py` |
| what the app turns into events | `logistics/producers.py` |
| outbox → Kafka | `logistics/outbox.py` |
| the Kafka-style broker (topics, partitions, offsets, consumer groups) | `logistics/broker.py` |
| consumer loop: batch, savepoint, idempotency inbox, retry, dead letter | `logistics/consumers/base.py` |
| the 6 consumer programs | `logistics/consumers/*.py` (order_handler, event_writer, state_updater, inventory_handler, request_handler, ml_service) |
| the lifecycle state machine (pure function, shared with replay) | `logistics/domain.py` |
| verify / rebuild | `logistics/replay.py` |
| ETA models and anomaly detection | `logistics/ml/` |
| how the terminal and audit trail are produced | `logistics/db.py` (statement logging, `app.actor`), `logistics/termlog.py`, `db_watch.py`, `db_log.py` |
| automated tests | `tests/test_system.py` (31 tests against a real PostgreSQL) |

## 7. Cheat sheet: UI action → database effect

| Click | Tables touched (and by whom) |
|---|---|
| Add to cart | `shop.cart_item` INSERT/UPDATE (web-api) |
| Place order | `customer_order`, `order_line`, `payment`, `cart_item`↓, `outbox` (web-api) → `shipment`, `shipment_item`, order status (state-updater + trigger) → `shipment_event`, `notification` (event-writer) → `inventory_balance`, `inventory_ledger` (inventory-handler) |
| Assign vehicle / status buttons | `shipment` (+ `customer_order` via trigger), `shipment_event`, `notification` |
| GPS ping / drive | `gps_ping`, `shipment` position, `ml_prediction` ×4, maybe `shipment_event` (ETA_UPDATE, ARRIVED_AT_ZONE, EXCEPTION_RISK) |
| Reschedule | `customer_request`, `shipment` window, events, notification |
| Return → receive | `customer_request`, `request_item`, `inventory_ledger`/`balance`, `payment` (REFUND) |
| Change a price (Back office → Catalogue) | `shop.sku` UPDATE + `price_history` INSERT (trigger); past orders keep their price |
| Receive stock | `inventory_ledger` INSERT + `inventory_balance` upsert |

## 8. Likely questions

* **Why PostgreSQL and not MongoDB?** The design document contains a full relational schema (BCNF); a relational server gives constraints, triggers, transactions and SQL. The Mongo-style document is still available as the view `logistics.v_shipment_document`.
* **Is Kafka real?** The broker is a small durable stand-in (`data/broker.db`) with the same semantics — topics, partitions by key, offsets, consumer groups, at-least-once, replay. Everything downstream (outbox, idempotent consumers, ordering) is real; swapping in `confluent-kafka` only touches `broker.py`.
* **Where is the event log?** `logistics.shipment_event` (append-only); `shipment` and `inventory_balance` are projections that can be rebuilt from it.
* **How do you prevent overselling?** `UPDATE inventory_balance SET available_qty = available_qty - :q WHERE … AND available_qty >= :q` (atomic), the CHECK `available_qty >= 0`, and a compensation (cancel + refund) if the warehouse cannot serve a paid order.
* **What if the app crashes after paying?** The order and its outbox row were committed together; the relay publishes them after restart; consumers are idempotent.
* **Are passwords/cards stored?** Passwords only as salted PBKDF2 hashes; payments are simulated and no card data exists anywhere.

## 9. Troubleshooting

| Problem | Fix |
|---|---|
| `cannot connect` / app won't start | Start Docker Desktop, then `.\scripts\start_db.ps1` |
| Port 8765 busy | `.\scripts\stop_all.ps1`, or `$env:PORT=8800; python run.py` |
| Terminal too noisy / too quiet | `$env:LOG_SQL="tx"` (adds BEGIN/COMMIT), `"all"` (adds SELECT), `"off"`; set before `python run.py` |
| Want a clean database | Back office → System → **Reset demo database** (reloads the demo data). Do **not** run `python -m logistics.dbsetup` on the live database: it erases every table and loads nothing back (it now refuses unless you add `--force`) |
| Cannot log in / "incorrect e-mail or password" for everyone | the database was emptied - stop the app (`.\scripts\stop_all.ps1`) and start it again (`.\scripts\demo_windows.ps1`); an empty database is recreated and the demo data is reloaded (~20 s) |
| pgAdmin shows no servers | wait 20 s after `docker compose up`, refresh; or connect manually: host `db`, port `5432`, user `demo_viewer`, password `demo` |
| Stop everything | `.\scripts\stop_all.ps1` (data stays in the Docker volume) |
