"""Create a fresh database with demo users and the three synthetic cases.

    python seed.py            # refuses to overwrite an existing database
    python seed.py --reset    # delete and recreate
    python seed.py --reset --no-cases   # users only (live Tazama POC: cases come from the CMS)
"""
from __future__ import annotations

import sys
import uuid

from app.auth import hash_password
from app.config import settings
from app.db import init_db, session
from app.intake import ingest_case
from app.synth import all_cases

USERS = [
    ("ayesha@lumous.demo", "Ayesha Khan", "analyst"),
    ("omar@lumous.demo", "Omar Siddiqui", "analyst"),
    ("bilal@lumous.demo", "Bilal Ahmed", "mlro"),
    ("sara@lumous.demo", "Sara Malik", "auditor"),
]
PASSWORD = "lumous-demo"


def main() -> None:
    path = settings.db_path
    if path.exists():
        if "--reset" not in sys.argv:
            print(f"{path} exists; pass --reset to recreate it.")
            return
        for suffix in ("", "-wal", "-shm"):
            p = path.with_name(path.name + suffix)
            if p.exists():
                p.unlink()
    init_db()
    pw = hash_password(PASSWORD)
    with session() as conn:
        for email, name, role in USERS:
            conn.execute("INSERT INTO users (id, email, name, role, pw_hash) VALUES (?,?,?,?,?)",
                         (str(uuid.uuid4()), email, name, role, pw))
        bundles = [] if "--no-cases" in sys.argv else all_cases()
        for bundle in bundles:
            ingest_case(conn, bundle, settings.tenant_id)
    print(f"Seeded {path} with {len(USERS)} users and {len(bundles)} cases. Password for all users: {PASSWORD}")


if __name__ == "__main__":
    main()
