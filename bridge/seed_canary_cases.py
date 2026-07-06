#!/usr/bin/env python3
"""
Seed canary_cases table from canary/fixtures/*.json

Usage:
    python bridge/seed_canary_cases.py

Reads fixtures from canary/fixtures/ (relative to repo root).
Connects to monitoring DB via LLM_OLLY_DB_URL env var (matches common.py).
"""

import json
import os
from pathlib import Path

import psycopg2
from psycopg2.extras import Json

FIXTURES_DIR = Path(__file__).parent.parent / "canary" / "fixtures"
DB_URL = os.environ["LLM_OLLY_DB_URL"]


def load_fixtures() -> list[dict]:
    fixtures = []
    for path in sorted(FIXTURES_DIR.glob("*.json")):
        with path.open() as f:
            data = json.load(f)
        fixtures.append(data)
    return fixtures


def seed(fixtures: list[dict]) -> None:
    conn = psycopg2.connect(DB_URL)
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM canary_cases")
            existing = cur.fetchone()[0]
            if existing > 0:
                print(f"canary_cases already has {existing} rows — skipping insert")
                return

            for fixture in fixtures:
                cur.execute(
                    """
                    INSERT INTO canary_cases (
                        case_id, persona_module, prospect_input, assertions,
                        reference_exemplar, is_active
                    ) VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (
                        fixture["case_id"],
                        fixture["persona_module"],
                        Json(fixture["prospect_input"]),
                        Json(fixture["assertions"]),
                        fixture.get("reference_exemplar"),
                        True,
                    ),
                )
            conn.commit()
            print(f"Inserted {len(fixtures)} fixtures into canary_cases")
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()


if __name__ == "__main__":
    fixtures = load_fixtures()
    print(f"Loaded {len(fixtures)} fixtures from {FIXTURES_DIR}")
    seed(fixtures)
