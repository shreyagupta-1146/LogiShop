"""Start the whole system:   python run.py      ->  http://127.0.0.1:8765

Needs the PostgreSQL container:  docker compose -f deploy/docker-compose.yml up -d
Everything the system does is printed in this terminal, in colour.
"""
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from logistics.server import main

if __name__ == "__main__":
    main()
