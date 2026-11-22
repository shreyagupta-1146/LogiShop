"""Export the whole database to ordinary files you can open without any database tool.

    python scripts/export_db.py

Creates (in the exports/ folder):
    logistics_all_tables.xlsx     Excel workbook - ONE SHEET PER TABLE (open it and look at everything)
    csv/<schema>.<table>.csv      one CSV file per table
    logistics_full_dump.sql       complete pg_dump: schema + data, restorable with psql
    DATABASE_OVERVIEW.txt         every table with its columns, types and row count

Reads through the read-only role demo_viewer, so it cannot change anything.
Where does the live data physically live?  In the Docker volume "logistics_logistics_pgdata"
(inside Docker's own storage, not a normal folder) - these exports are the file-based copy.
"""
import csv
import subprocess
import sys
from pathlib import Path

import psycopg

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "exports"
SCHEMAS = ("shop", "logistics", "infra")
SKIP = {"infra.auth_session"}               # login tokens: not for export


def main():
    (OUT / "csv").mkdir(parents=True, exist_ok=True)
    conn = psycopg.connect(host="127.0.0.1", port=5433, dbname="logistics", user="demo_viewer", password="demo",
                           options="-c timezone=Asia/Kolkata")
    tables = [f"{r[0]}.{r[1]}" for r in conn.execute(
        "SELECT table_schema, table_name FROM information_schema.tables WHERE table_schema = ANY(%s) AND table_type = 'BASE TABLE' "
        "ORDER BY table_schema, table_name", (list(SCHEMAS),))]
    tables = [t for t in tables if t not in SKIP]

    overview = ["LOGISTICS DATABASE - every table (PostgreSQL 16, database 'logistics')", "=" * 78, ""]
    sheets = {}
    for t in tables:
        sch, name = t.split(".")
        cols = conn.execute("SELECT column_name, data_type FROM information_schema.columns WHERE table_schema = %s AND table_name = %s "
                            "ORDER BY ordinal_position", (sch, name)).fetchall()
        cur = conn.execute(f'SELECT * FROM "{sch}"."{name}"')
        header = [d.name for d in cur.description]
        rows = cur.fetchall()
        with open(OUT / "csv" / f"{t}.csv", "w", newline="", encoding="utf-8-sig") as f:      # utf-8-sig: Excel shows rupee signs / emoji correctly
            w = csv.writer(f)
            w.writerow(header)
            w.writerows(rows)
        sheets[t] = (header, rows)
        overview.append(f"{t}   ({len(rows)} rows)")
        overview.extend(f"    {c:<26} {dt}" for c, dt in cols)
        overview.append("")
        print(f"  {t:<34} {len(rows):>6} rows")
    (OUT / "DATABASE_OVERVIEW.txt").write_text("\n".join(overview), encoding="utf-8")

    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill
        wb = Workbook()
        wb.remove(wb.active)
        idx = wb.create_sheet("INDEX")
        idx.append(["table", "rows"])
        for t, (header, rows) in sheets.items():
            title = t.replace("logistics.", "lg.").replace("shop.", "sh.").replace("infra.", "in.")[:31]
            ws = wb.create_sheet(title)
            ws.append(header)
            for r in rows:
                ws.append([v if isinstance(v, (int, float, str, type(None), bool)) else str(v) for v in r])
            for c in ws[1]:
                c.font = Font(bold=True, color="FFFFFF")
                c.fill = PatternFill("solid", fgColor="3B25C7")
            ws.freeze_panes = "A2"
            for i, h in enumerate(header, 1):
                ws.column_dimensions[ws.cell(1, i).column_letter].width = min(40, max(12, len(h) + 4))
            idx.append([f"{t}  (sheet '{title}')", len(rows)])
        idx.column_dimensions["A"].width = 52
        wb.save(OUT / "logistics_all_tables.xlsx")
        print(f"\nExcel workbook: {OUT / 'logistics_all_tables.xlsx'}")
    except Exception as e:
        print("Excel export skipped:", e)

    r = subprocess.run(["docker", "exec", "logistics-db", "pg_dump", "-U", "postgres", "-d", "logistics", "--no-owner", "--no-privileges",
                        "-f", "/tmp/logistics_dump.sql"], capture_output=True, text=True)
    if r.returncode == 0:
        subprocess.run(["docker", "cp", "logistics-db:/tmp/logistics_dump.sql", str(OUT / "logistics_full_dump.sql")], check=True, capture_output=True)
        print(f"SQL dump:       {OUT / 'logistics_full_dump.sql'}  ({(OUT / 'logistics_full_dump.sql').stat().st_size // 1024} KB)")
    else:
        print("pg_dump skipped:", r.stderr.strip()[:200])
    print(f"CSV files:      {OUT / 'csv'}  ({len(tables)} files)")
    print(f"Overview:       {OUT / 'DATABASE_OVERVIEW.txt'}")


if __name__ == "__main__":
    main()
