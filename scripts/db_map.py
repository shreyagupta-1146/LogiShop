"""Generate DATABASE_IN_THE_CODE.md - a map of where the database lives in this codebase.

    python scripts/db_map.py

It reads the SQL files (the database *definition*) and every Python file (the code that *calls* the
database), connects the two - which table is defined on which line, which function writes it, which
screen reads it - and writes the result as a Markdown file with clickable file:line links.
Nothing here touches the running database; it only reads source files, so it is safe to run any time.
Re-run it after you change a .sql file or add a query, and the links stay correct.
"""
import re
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "DATABASE_IN_THE_CODE.md"
SCHEMAS = ("shop", "logistics", "infra")
SQL_DEF_FILES = ["sql/00_bootstrap.sql", "sql/01_schema.sql", "sql/02_functions_triggers.sql", "sql/03_views.sql", "sql/04_grants.sql"]


def lines_of(rel):
    return (ROOT / rel).read_text(encoding="utf-8").split("\n")


def link(rel, n, text=None):
    return f"[{text or f'{rel}:{n}'}]({rel}#L{n})"


# ----------------------------------------------------------------------------------------------
#  1. the definition: what the .sql files create, and on which line
# ----------------------------------------------------------------------------------------------
tables, views, functions, triggers, indexes, types, sequences = {}, {}, {}, {}, defaultdict(list), {}, {}
for rel in SQL_DEF_FILES:
    text = "\n".join(lines_of(rel))
    ls = lines_of(rel)
    for m in re.finditer(r"^CREATE TABLE (\w+\.\w+)", text, re.M):
        start = text.count("\n", 0, m.start()) + 1
        end = next((i + 1 for i in range(start, len(ls)) if ls[i].startswith(");")), start)
        cols = []
        for ln in ls[start:end - 1]:
            c = re.match(r"^\s{4}([a-z_][a-z0-9_]*)\s+[A-Za-z]", ln)
            if c and c.group(1) not in ("primary", "unique", "check", "foreign", "constraint", "exclude"):
                cols.append(c.group(1))
        tables[m.group(1)] = dict(file=rel, line=start, end=end, cols=cols)
    for m in re.finditer(r"^CREATE (MATERIALIZED )?VIEW (\w+\.\w+)", text, re.M):
        views[m.group(2)] = dict(file=rel, line=text.count("\n", 0, m.start()) + 1, mat=bool(m.group(1)))
    for m in re.finditer(r"^CREATE FUNCTION (\w+\.\w+)\(", text, re.M):
        functions[m.group(1)] = dict(file=rel, line=text.count("\n", 0, m.start()) + 1)
    for m in re.finditer(r"^CREATE TRIGGER (\w+)\s+(.*?)\s+ON\s+(\w+\.\w+)", text, re.M | re.S):
        when = " ".join(m.group(2).split())
        fn = re.search(r"EXECUTE FUNCTION (\w+\.\w+)", text[m.start():m.start() + 600])
        triggers[m.group(1)] = dict(file=rel, line=text.count("\n", 0, m.start()) + 1, table=m.group(3), when=when, fn=fn.group(1) if fn else "")
    for m in re.finditer(r"^CREATE (UNIQUE )?INDEX\s+(\w+)\s+ON\s+(\w+\.\w+)([^\n;]*)", text, re.M):
        indexes[m.group(3)].append(dict(name=m.group(2), file=rel, line=text.count("\n", 0, m.start()) + 1, unique=bool(m.group(1)), rest=m.group(4).split("--")[0].strip()))
    for m in re.finditer(r"^CREATE TYPE (\w+\.\w+) AS ENUM", text, re.M):
        types[m.group(1)] = dict(file=rel, line=text.count("\n", 0, m.start()) + 1)

objects = {**{k: "table" for k in tables}, **{k: "view" for k in views}, **{k: "function" for k in functions}}

# the audit trigger is attached to many tables by a loop (a DO block), not by 24 separate statements
audit_loop = next((i + 1 for i, l in enumerate(lines_of("sql/02_functions_triggers.sql")) if "FOR t IN SELECT * FROM (VALUES" in l), None)

# ----------------------------------------------------------------------------------------------
#  2. the callers: every line of Python that names a database object
# ----------------------------------------------------------------------------------------------
py_files = sorted([p for p in (ROOT / "logistics").rglob("*.py") if "__pycache__" not in p.parts] + [ROOT / "db_watch.py", ROOT / "db_log.py", ROOT / "run.py"]
                  + sorted((ROOT / "scripts").glob("*.py")))
py_files = [p for p in py_files if p.name != "__init__.py" and p.name != "db_map.py"]
refs = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))      # object -> kind -> file -> [lines]
name_re = re.compile(r"\b(" + "|".join(SCHEMAS) + r")\.([a-z_][a-z0-9_]*)\b")
# the roles have search_path = shop, logistics, infra, so SQL may also say just `FROM customer_request`
bare_map = {o.split(".")[1]: o for o, k in objects.items() if k != "function"}
assert len(bare_map) == len([o for o, k in objects.items() if k != "function"]), "two objects share a bare name"
sql_ctx_re = re.compile(r"\b(INSERT\s+INTO|UPDATE|DELETE\s+FROM|TRUNCATE(?:\s+TABLE)?|FROM|JOIN)\s+(?:ONLY\s+)?(?:(shop|logistics|infra)\.)?([a-z_][a-z0-9_]*)\b", re.I)


def classify(line):
    """{object: 'reads' | 'writes' | 'calls'} for one line of Python that contains SQL text."""
    found = {}
    for m in name_re.finditer(line):
        obj = f"{m.group(1)}.{m.group(2)}"
        if obj in objects:
            found[obj] = "calls" if objects[obj] == "function" else "reads"
    if '"' in line or "'" in line:                                   # bare names only count inside SQL strings, not prose
        for m in sql_ctx_re.finditer(line):
            obj = f"{m.group(2)}.{m.group(3)}" if m.group(2) else bare_map.get(m.group(3).lower())
            if obj not in objects or objects[obj] == "function":
                continue
            kw = m.group(1).upper()
            if kw not in ("FROM", "JOIN") or re.search(r"DELETE\s+$", line[:m.start()], re.I):    # INSERT INTO / UPDATE / DELETE FROM / TRUNCATE
                found[obj] = "writes"
            elif found.get(obj) != "writes":
                found[obj] = "reads"
    return found


for p in py_files:
    rel = p.relative_to(ROOT).as_posix()
    for n, line in enumerate(p.read_text(encoding="utf-8").split("\n"), 1):
        if line.lstrip().startswith("#"):
            continue
        for obj, kind in classify(line).items():
            if n not in refs[obj][kind][rel]:
                refs[obj][kind][rel].append(n)


def short(rel):
    return rel.rsplit("/", 1)[-1]


def where(obj, kind, limit=3):
    parts = []
    for rel, ns in refs[obj][kind].items():
        shown = ", ".join(link(rel, n, str(n)) for n in ns[:limit]) + (f" +{len(ns) - limit}" if len(ns) > limit else "")
        parts.append(f"`{short(rel)}` {shown}")
    return "<br>".join(parts) or "-"


# ----------------------------------------------------------------------------------------------
#  3. routes: which HTTP endpoint runs which SQL
# ----------------------------------------------------------------------------------------------
api_lines = lines_of("logistics/api.py")
SKIP_HELPERS = {"Q", "Q1", "tx", "outcome", "_g", "iso"}
helper_bodies = {}
for i, l in enumerate(api_lines):
    m = re.match(r"^def (\w+)\(", l)
    if m and not api_lines[i - 1].startswith("@route"):          # plain helpers only, not the route handlers themselves
        k2 = i + 1
        while k2 < len(api_lines) and not re.match(r"^(@|def |class |[A-Za-z_]+ = )", api_lines[k2]):
            k2 += 1
        helper_bodies[m.group(1)] = "\n".join(api_lines[i:k2])
routes = []
for i, l in enumerate(api_lines):
    m = re.match(r'^@route\("(GET|POST)",\s*r?"([^"]+)"(?:,\s*auth="(\w+)")?\)', l)
    if not m:
        continue
    j = i + 1
    while not api_lines[j].startswith("def "):
        j += 1
    fn = re.match(r"def (\w+)", api_lines[j]).group(1)
    k = j + 1
    while k < len(api_lines) and not re.match(r"^(@|def |class |[A-Za-z_]+ = )", api_lines[k]):
        k += 1
    body = "\n".join(api_lines[j:k])
    seen, todo = {fn}, [body]
    while todo:                                   # include the SQL of plain helper functions this route calls (e.g. cart_payload)
        for callee in re.findall(r"\b([A-Za-z_]\w*)\(", todo.pop()):
            if callee in helper_bodies and callee not in seen and callee not in SKIP_HELPERS and len(seen) < 12:
                seen.add(callee)
                todo.append(helper_bodies[callee])
                body += "\n" + helper_bodies[callee]
    rd, wr, fnc = set(), set(), set()
    for bl in body.split("\n"):
        for obj, kind in classify(bl).items():
            {"reads": rd, "writes": wr, "calls": fnc}[kind].add(obj)
    if re.search(r"\{STOCK\}", body):
        rd.add("logistics.inventory_balance")
    via = sorted(set(re.findall(r"S\.CTX\.app\.(\w+)", body)))
    routes.append(dict(method=m.group(1), path=m.group(2), auth=m.group(3) or "public", fn=fn, line=j + 1, rd=sorted(rd - wr), wr=sorted(wr), call=sorted(fnc), via=via))

prod_lines = lines_of("logistics/producers.py")
prod_def = {m.group(1): i + 1 for i, l in enumerate(prod_lines) if (m := re.match(r"    def (\w+)\(", l))}

# ----------------------------------------------------------------------------------------------
#  4. a guided path for each user action
# ----------------------------------------------------------------------------------------------
FLOWS = [
    ("A customer clicks **Place order** (checkout)", [
        ("Browser", "web/js/shop.js", r'api\("/api/checkout"', "the click sends POST /api/checkout"),
        ("HTTP route", "logistics/api.py", r'@route\("POST", "/api/checkout"', "validates, then makes ONE database call"),
        ("SQL call", "logistics/api.py", r"shop\.place_order", "`SELECT shop.place_order(...)`"),
        ("Stored function", "sql/02_functions_triggers.sql", r"CREATE FUNCTION shop\.place_order", "order + lines + payment + stock check + outbox row, all in one transaction"),
        ("Outbox row", "sql/02_functions_triggers.sql", r"INSERT INTO infra\.outbox", "the event is written in the same transaction as the order"),
        ("Trigger", "sql/02_functions_triggers.sql", r"CREATE TRIGGER trg_outbox_notify", "tells the relay (NOTIFY) that a row is waiting"),
        ("Relay to Kafka", "logistics/outbox.py", r"FROM infra\.outbox", "claims pending rows (FOR UPDATE SKIP LOCKED) and publishes them to the broker"),
        ("Consumer", "logistics/consumers/order_handler.py", r"class OrderHandler", "creates the shipment through events"),
        ("Audit trigger", "sql/02_functions_triggers.sql", r"CREATE FUNCTION infra\.audit_log_change", "records every row change - this is what `db_watch.py` prints"),
    ]),
    ("A customer clicks **Add to cart**", [
        ("Browser", "web/js/shop.js", r'api\("/api/cart", \{ sku_id: sku, quantity: qty \}', "POST /api/cart"),
        ("HTTP route", "logistics/api.py", r'@route\("POST", "/api/cart"', ""),
        ("SQL", "logistics/api.py", r"INSERT INTO shop\.cart_item", "an upsert (INSERT ... ON CONFLICT)"),
        ("Table", "sql/01_schema.sql", r"CREATE TABLE shop\.cart_item", "composite primary key, quantity CHECK"),
    ]),
    ("The admin changes a **price** (Catalogue page)", [
        ("Browser", "web/js/admin.js", r"/price`", "POST /api/admin/products/{sku}/price"),
        ("HTTP route", "logistics/api.py", r'@route\("POST", r"/api/admin/products/\(\?P<sku>\[A-Za-z0-9_-\]\+\)/price"', ""),
        ("SQL", "logistics/api.py", r"UPDATE shop\.sku SET price", "inside a transaction tagged with the user action"),
        ("Rule 1", "sql/01_schema.sql", r"CHECK \(price <= mrp\)", "a price above the MRP is refused by the database, not by the app"),
        ("Rule 2", "sql/02_functions_triggers.sql", r"CREATE TRIGGER trg_price_history", "old and new price are copied to shop.price_history"),
    ]),
    ("A dispatcher clicks **Picked up** / **In transit** / **Delivered** (Dispatch page)", [
        ("Browser", "web/js/admin.js", r'"Picked up"', "POST /api/admin/shipments/{id}/status"),
        ("HTTP route", "logistics/api.py", r'@route\("POST", r"/api/admin/shipments/\(\?P<sid>\[A-Za-z0-9_-\]\+\)/status"', ""),
        ("Command to event", "logistics/producers.py", r"def set_status", "builds a shipment event and publishes it - no SQL yet"),
        ("Event stream", "logistics/broker.py", r"def publish", "the Kafka-style topic `shipment-events`"),
        ("Consumer writes", "logistics/consumers/event_writer.py", r"INSERT INTO logistics\.shipment_event", "appends to the immutable event log"),
        ("Consumer writes", "logistics/consumers/state_updater.py", r"UPDATE logistics\.shipment SET", "updates the shipment projection"),
        ("Rule 1", "sql/02_functions_triggers.sql", r"CREATE TRIGGER trg_shipment_lifecycle", "illegal jumps (CREATED -> DELIVERED) are blocked inside PostgreSQL"),
        ("Rule 2", "sql/02_functions_triggers.sql", r"CREATE TRIGGER trg_shipment_sync_order", "the customer's order status follows the shipment"),
    ]),
    ("The admin clicks **Receive stock** (Inventory page)", [
        ("Browser", "web/js/admin.js", r"/api/admin/inventory/stock-in", ""),
        ("HTTP route", "logistics/api.py", r'@route\("POST", "/api/admin/inventory/stock-in"', ""),
        ("Command to event", "logistics/producers.py", r"def stock_in", ""),
        ("Consumer writes", "logistics/consumers/inventory_handler.py", r"INSERT INTO logistics\.inventory_ledger", "an append-only ledger row, then the balance is updated"),
        ("Rule", "sql/02_functions_triggers.sql", r"CREATE TRIGGER trg_ledger_matches_source", "the ledger must agree with the movement that caused it"),
        ("Table", "sql/01_schema.sql", r"CREATE TABLE logistics\.inventory_balance", "CHECK (available_qty >= 0): stock can never go negative"),
    ]),
    ("A customer **returns** an item, the admin completes it (refund)", [
        ("Browser", "web/js/shop.js", r"/return`", "POST /api/orders/{id}/return"),
        ("HTTP route", "logistics/api.py", r'@route\("POST", r"/api/orders/\(\?P<oid>\[A-Za-z0-9_-\]\+\)/return"', ""),
        ("Consumer", "logistics/consumers/request_handler.py", r"def _advance", "moves the request through its lifecycle, restocks, then refunds"),
        ("Stored function", "sql/02_functions_triggers.sql", r"CREATE FUNCTION shop\.refund_return", "writes the REFUND payment row"),
    ]),
    ("A GPS ping arrives (Drive / Move vehicle buttons, driver app)", [
        ("HTTP route", "logistics/api.py", r'@route\("POST", r"/api/admin/shipments/\(\?P<sid>\[A-Za-z0-9_-\]\+\)/move"', ""),
        ("Command to event", "logistics/producers.py", r"def send_ping", "publishes to topic `gps-pings`"),
        ("Consumer writes", "logistics/consumers/event_writer.py", r"INSERT INTO logistics\.gps_ping", "raw ping stored"),
        ("Consumer writes", "logistics/consumers/state_updater.py", r"UPDATE logistics\.shipment SET current", "latest position on the shipment"),
        ("ML consumer", "logistics/consumers/ml_service.py", r"INSERT INTO logistics\.ml_prediction", "ETA prediction + anomaly flag stored per ping"),
    ]),
]
cache = {}


def find(rel, rx):
    if rel not in cache:
        cache[rel] = lines_of(rel)
    for i, l in enumerate(cache[rel], 1):
        if re.search(rx, l):
            return i
    return None


missing = []


def flow_md():
    out = []
    for title, steps in FLOWS:
        out.append(f"#### {title.replace('**', chr(34))}\n")
        out.append("| step | where in the code | what happens |\n|---|---|---|")
        for what, rel, rx, note in steps:
            n = find(rel, rx)
            if n is None:
                missing.append((title, rel, rx))
            out.append(f"| {what} | {link(rel, n) if n else rel + ' (?)'} | {note} |")
        out.append("")
    return "\n".join(out)


# ----------------------------------------------------------------------------------------------
#  5. consumers: what each background worker writes
# ----------------------------------------------------------------------------------------------
def consumer_md():
    rows = []
    for p in sorted((ROOT / "logistics" / "consumers").glob("*.py")) + [ROOT / "logistics" / "outbox.py", ROOT / "logistics" / "seed.py"]:
        rel = p.relative_to(ROOT).as_posix()
        if p.name in ("__init__.py", "base.py"):
            continue
        w = sorted({o for o, d in refs.items() if rel in d["writes"]})
        r = sorted({o for o, d in refs.items() if rel in d["reads"]})
        rows.append(f"| `{rel}` | {', '.join(f'`{x}`' for x in w) or '-'} | {', '.join(f'`{x}`' for x in r) or '-'} |")
    return "| file | tables it writes | tables / views it reads |\n|---|---|---|\n" + "\n".join(rows)


# ----------------------------------------------------------------------------------------------
#  6. demo_queries.sql table of contents
# ----------------------------------------------------------------------------------------------
dq = lines_of("sql/demo_queries.sql")
dq_toc = []
for i, l in enumerate(dq, 1):
    m = re.match(r"^-- ### (\d+)\. (.*?)\s*-*$", l)
    if m:
        dq_toc.append(f"- {link('sql/demo_queries.sql', i, 'Section ' + m.group(1))} - {m.group(2).title() if m.group(2).isupper() else m.group(2)}")

# ----------------------------------------------------------------------------------------------
#  7. write the document
# ----------------------------------------------------------------------------------------------
sql_counts = {f: len(lines_of(f)) for f in SQL_DEF_FILES + ["sql/demo_queries.sql"]}
doc = []
A = doc.append
A(f"""# The database, in the code

> Generated by `python scripts/db_map.py` on {date.today().isoformat()} - every link below goes to the exact line.
> Open this file in VS Code (**Ctrl+Shift+V** for the preview) and click a link to jump to the code.

LogiShop uses **PostgreSQL 16** (running in Docker). The database is not hidden inside the app: it is *defined* by plain `.sql`
files, and the Python code *calls* it with plain SQL text. So there are two halves to find in the codebase.

## 1. The two halves

| | where | what it is |
|---|---|---|
| **Definition** (what the database *is*) | the `sql/` folder | `CREATE TABLE`, constraints, indexes, functions, triggers, views, roles. Run automatically on the first start (or by the *Reset demo database* button). **Never run it by hand on the live database - it erases all data.** |
| **Queries** (what the app *asks*) | `logistics/api.py` and `logistics/consumers/` | `SELECT / INSERT / UPDATE` written as ordinary strings, executed through `logistics/db.py`. |

```
 sql/00_bootstrap.sql   ->  roles, extensions, the three schemas (shop, logistics, infra)
 sql/01_schema.sql      ->  {len(tables)} tables, keys, CHECK constraints, {sum(len(v) for v in indexes.values())} CREATE INDEX statements
 sql/02_functions_triggers.sql -> {len(functions)} functions, {len(triggers)} named triggers (+ the audit trigger on many tables)
 sql/03_views.sql       ->  {len(views)} views (one materialized)
 sql/04_grants.sql      ->  who may do what (least privilege)
 sql/demo_queries.sql   ->  ~90 ready-made queries to run in pgAdmin / psql while presenting
 ---------------------------------------------------------------------------------
 logistics/db.py        ->  the connection pool + transaction helpers (sets the "who did it" audit tag)
 logistics/api.py       ->  the SQL behind every screen (customer, back office, driver)
 logistics/consumers/   ->  the SQL each background worker runs when it handles an event
 logistics/seed.py      ->  the demo data (catalogue, customers, orders)
```

## 2. How to navigate it (5 steps)

1. **Start with the definition.** Open {link('sql/01_schema.sql', 1, 'sql/01_schema.sql')}. Every table starts with `CREATE TABLE`; press **Ctrl+F** and type `CREATE TABLE` and press Enter to hop from table to table. The comment block at the top explains the design decisions.
2. **Use the outline.** In VS Code press **Ctrl+Shift+O** inside any `.py` file to list its functions; in a `.sql` file use **Ctrl+F** on `CREATE FUNCTION` / `CREATE TRIGGER` / `CREATE VIEW`.
3. **Find every place a table is used.** Press **Ctrl+Shift+F** (search in all files) and type the full name, e.g. `shop.customer_order`. Results show the `.sql` definition *and* every Python line that reads or writes it. (Section 6 below already lists these.)
4. **Find every write.** Search `INSERT INTO`, `UPDATE shop.` or `DELETE FROM` to see only the statements that *change* data.
5. **Follow one click end to end.** Section 3 traces each button from the browser to the table.

> Tip for the presentation: keep `sql/01_schema.sql` in one editor tab and `logistics/api.py` in another. Click a button in the app, show the
> function in `api.py` that handled it, then show the `CREATE TABLE` it writes to - and `python db_watch.py` shows the rows that changed.

## 3. What happens when a user clicks something

{flow_md()}
## 4. Every HTTP endpoint and the SQL behind it

Each row is one `@route` in {link('logistics/api.py', 1, 'logistics/api.py')}. *Reads* and *writes* are the tables the handler's SQL touches; *via* means it does
**not** write SQL itself but publishes an event (the producer is in {link('logistics/producers.py', 1, 'producers.py')}) and a consumer writes the tables.

| method | URL | who | handler | reads | writes | stored function | via event |
|---|---|---|---|---|---|---|---|""")
for r in routes:
    f = lambda xs: "<br>".join(f"`{x}`" for x in xs) or "-"
    via = ", ".join(link("logistics/producers.py", prod_def[v], f"`{v}`") if v in prod_def else f"`{v}`" for v in r["via"]) or "-"
    A(f"| {r['method']} | `{r['path'].replace('(?P<', '{').replace('>[A-Za-z0-9_-]+)', '}')}` | {r['auth']} | {link('logistics/api.py', r['line'], r['fn'] + '()')} | {f(r['rd'])} | {f(r['wr'])} | {f(r['call'])} | {via} |")

A("""
## 5. What each background worker writes

The consumers are small programs that each read one or more event topics and write PostgreSQL. This table is computed from the code.

""" + consumer_md())

A("""

## 6. Every table: where it is defined and who uses it

*Defined* jumps to the `CREATE TABLE`. *Written by* / *read by* list the Python lines (numbers are links; `+n` means more lines in that file).
Tables with nothing in *written by* are written only by SQL (stored functions/triggers) - e.g. `shop.order_line` is written inside `shop.place_order`.
""")
for sch in SCHEMAS:
    A(f"\n### schema `{sch}`\n")
    A("| table | defined | columns | written by | read by |\n|---|---|---|---|---|")
    for t, d in sorted(tables.items()):
        if not t.startswith(sch + "."):
            continue
        cols = ", ".join(d["cols"][:8]) + (f", ... (+{len(d['cols']) - 8})" if len(d["cols"]) > 8 else "")
        A(f"| `{t}` | {link(d['file'], d['line'])} | {cols} | {where(t, 'writes')} | {where(t, 'reads')} |")

A("\n## 7. Views (saved queries)\n\n| view | defined | used by |\n|---|---|---|")
for v, d in sorted(views.items()):
    A(f"| `{v}`{' (materialized)' if d['mat'] else ''} | {link(d['file'], d['line'])} | {where(v, 'reads')} |")

A("\n## 8. Stored functions and triggers (business rules inside the database)\n")
A("| function | defined | called from Python |\n|---|---|---|")
for fn, d in sorted(functions.items()):
    A(f"| `{fn}()` | {link(d['file'], d['line'])} | {where(fn, 'calls')} |")
A("\n| trigger | on table | fires | runs | defined |\n|---|---|---|---|---|")
for tr, d in sorted(triggers.items(), key=lambda kv: (kv[1]["table"], kv[0])):
    A(f"| `{tr}` | `{d['table']}` | {d['when']} | `{d['fn']}` | {link(d['file'], d['line'])} |")
if audit_loop:
    A(f"\nAnother {link('sql/02_functions_triggers.sql', audit_loop, 'loop in 02_functions_triggers.sql')} attaches the generic audit trigger "
      f"`infra.audit_log_change` to every business table (an `AFTER INSERT/UPDATE/DELETE` trigger each); that is why `infra.audit_log` records every change.")

A("\n## 9. Indexes\n\n| table | index | kind | defined |\n|---|---|---|---|")
for t in sorted(indexes):
    for ix in indexes[t]:
        kind = ("UNIQUE " if ix["unique"] else "") + (re.search(r"USING (\w+)", ix["rest"]).group(1).upper() if re.search(r"USING (\w+)", ix["rest"]) else "BTREE") + (" PARTIAL" if " WHERE " in ix["rest"] else "")
        A(f"| `{t}` | `{ix['name']}` | {kind} | {link(ix['file'], ix['line'])} |")

A(f"""
## 10. Roles and privileges

{link('sql/00_bootstrap.sql', 1, 'sql/00_bootstrap.sql')} creates the four roles; {link('sql/04_grants.sql', 1, 'sql/04_grants.sql')} decides what each may do:
`logistics_owner` owns the objects, `app_user` is what the application connects as (cannot edit the append-only logs or write the audit log),
`demo_viewer` is read-only (used by pgAdmin, `export_db.py`), `postgres` is the superuser used only by setup.

## 11. Ready-made queries for the demonstration

{link('sql/demo_queries.sql', 1, 'sql/demo_queries.sql')} ({sql_counts['sql/demo_queries.sql']} lines) - paste blocks into pgAdmin's Query Tool or run with `scripts\\psql.ps1`:

""" + "\n".join(dq_toc) + """

## 12. See the same thing live (no code needed)

```text
\\dt shop.*                 -- the tables in a schema          (psql; pgAdmin: Schemas > shop > Tables)
\\d+ shop.customer_order    -- one table: columns, constraints, indexes, triggers
\\sf shop.place_order       -- the source of a stored function
\\dv logistics.*            -- views
SELECT * FROM infra.audit_log ORDER BY audit_id DESC LIMIT 20;    -- what the last clicks changed
```

Terminal tools: `python db_watch.py` (rows changing live), `python db_log.py` (PostgreSQL's own log: every statement and which component sent it).
""")

files_n = find("logistics/dbsetup.py", r"^FILES = ")
setup_n = find("logistics/dbsetup.py", r"^def setup\(")
conn_n = find("logistics/db.py", r"^def connect\(")
tx_n = find("logistics/db.py", r"def write_tx")
pool_n = find("logistics/db.py", r"POOL")
msg_n = find("logistics/broker.py", r"CREATE TABLE IF NOT EXISTS message")
A(f"""
## 13. How the SQL files reach the database, and the one other database

* **Loading the definition.** {link('logistics/dbsetup.py', setup_n, 'dbsetup.setup()')} reads the list in {link('logistics/dbsetup.py', files_n, 'FILES')}
  (`00` to `04`) and runs each file against PostgreSQL as the superuser. The app runs it by itself when it finds an empty database, and the *Reset demo database* button runs it and then reloads the demo data from {link('logistics/seed.py', 1, 'seed.py')}.
  **Warning:** it drops every table. Do not run `python -m logistics.dbsetup` yourself on the live database - the command now refuses unless you add `--force`.
* **How Python talks to PostgreSQL.** {link('logistics/db.py', conn_n, 'db.connect()')} opens a connection (and tags it with `application_name`, which is what the PostgreSQL log shows),
  {link('logistics/db.py', pool_n, 'db.POOL')} is the connection pool the web handlers borrow from, and {link('logistics/db.py', tx_n, 'db.write_tx()')} wraps a write in a transaction that also sets
  `app.actor` / `app.trace_id`, which the audit trigger reads - that is how `infra.audit_log` knows *who* changed a row and *which click* caused it.
* **Parameters.** Queries use `?` placeholders (`Q("SELECT ... WHERE sku_id = ?", (sku,))`); `db.py` turns them into PostgreSQL parameters, so values are never pasted into the SQL text (no SQL injection).
* **The second database - the Kafka stand-in.** The event stream is a small **SQLite** file, `data/broker.db`, defined in {link('logistics/broker.py', msg_n, 'logistics/broker.py')}
  (tables `message`, `consumer_offset`, `subscription`, `dead_letter`). It is deliberately separate from PostgreSQL, like a real Kafka cluster would be.
  Everything about orders, stock, shipments and customers is in PostgreSQL only.
""")

OUT.write_text("\n".join(doc), encoding="utf-8")
print(f"wrote {OUT.relative_to(ROOT)}: {len(tables)} tables, {len(views)} views, {len(functions)} functions, {len(triggers)} named triggers, "
      f"{sum(len(v) for v in indexes.values())} indexes, {len(routes)} routes, {len(py_files)} python files scanned")
for t, rel, rx in missing:
    print("  NOT FOUND:", t, "|", rel, "|", rx)
