"""PostgreSQL storage for the accident portal.

Why: Render's disk is wiped on every deploy/restart, so reports, evidence images and clips
must live in the database. If DATABASE_URL is not set (local development) every function
quietly does nothing / returns None and the app keeps using the local outputs/ folder.

Tables (created automatically on start):
  incidents  one row per confirmed accident  (full report JSON in a JSONB column)
  media      evidence images + accident clip (bytes) for each incident
  settings   values saved from the Settings page (email, weather key, webhook)
"""
import json
import mimetypes
import os

try:
    import psycopg2
    import psycopg2.extras
except ImportError:  # psycopg2 missing -> run without a database
    psycopg2 = None

DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()
if DATABASE_URL.startswith("postgres://"):          # older style URL, psycopg2 prefers postgresql://
    DATABASE_URL = "postgresql://" + DATABASE_URL[len("postgres://"):]

SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    id          TEXT PRIMARY KEY,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TEXT,
    severity    TEXT,
    source      TEXT,
    summary     TEXT,
    lat         DOUBLE PRECISION,
    lon         DOUBLE PRECISION,
    collisions  INTEGER,
    vehicles    JSONB,
    result      JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS incidents_created_idx ON incidents (created_at DESC);
CREATE TABLE IF NOT EXISTS media (
    incident_id  TEXT NOT NULL REFERENCES incidents(id) ON DELETE CASCADE,
    name         TEXT NOT NULL,
    content_type TEXT NOT NULL,
    data         BYTEA NOT NULL,
    PRIMARY KEY (incident_id, name)
);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def enabled():
    return bool(psycopg2 and DATABASE_URL)


def _connect():
    # Render's managed Postgres requires SSL for external URLs; internal URLs work with the default.
    return psycopg2.connect(DATABASE_URL, connect_timeout=10)


def init():
    """Create tables. Returns True when the database is ready."""
    if not enabled():
        return False
    with _connect() as con, con.cursor() as cur:
        cur.execute(SCHEMA)
    return True


# ---------------- incidents ----------------
def save_incident(result, out_dir=None):
    """Store the report JSON and every file in out_dir (evidence jpgs, clip mp4)."""
    if not enabled():
        return False
    loc = result.get("location") or {}
    veh = [v.get("type") for v in result.get("vehicles", [])]
    with _connect() as con, con.cursor() as cur:
        cur.execute(
            """INSERT INTO incidents (id, finished_at, severity, source, summary, lat, lon, collisions, vehicles, result)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
               ON CONFLICT (id) DO UPDATE SET finished_at=EXCLUDED.finished_at, severity=EXCLUDED.severity,
                 source=EXCLUDED.source, summary=EXCLUDED.summary, lat=EXCLUDED.lat, lon=EXCLUDED.lon,
                 collisions=EXCLUDED.collisions, vehicles=EXCLUDED.vehicles, result=EXCLUDED.result""",
            (result["id"], result.get("finished_at"), (result.get("severity") or {}).get("level"),
             result.get("filename"), result.get("summary", ""), loc.get("lat"), loc.get("lon"),
             (result.get("metrics") or {}).get("collision_count"),
             psycopg2.extras.Json(veh), psycopg2.extras.Json(result)))
        if out_dir and os.path.isdir(out_dir):
            for name in os.listdir(out_dir):
                path = os.path.join(out_dir, name)
                if not os.path.isfile(path) or name == "result.json" or name.endswith(".raw.mp4"):
                    continue
                ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
                with open(path, "rb") as f:
                    cur.execute(
                        """INSERT INTO media (incident_id, name, content_type, data) VALUES (%s,%s,%s,%s)
                           ON CONFLICT (incident_id, name) DO UPDATE SET data=EXCLUDED.data, content_type=EXCLUDED.content_type""",
                        (result["id"], name, ctype, psycopg2.Binary(f.read())))
    return True


def get_incident(rid):
    if not enabled():
        return None
    with _connect() as con, con.cursor() as cur:
        cur.execute("SELECT result FROM incidents WHERE id=%s", (rid,))
        row = cur.fetchone()
    return row[0] if row else None


def list_incidents(limit=200):
    """Rows shaped exactly like server.api_incidents expects."""
    if not enabled():
        return None
    with _connect() as con, con.cursor() as cur:
        cur.execute("SELECT id, finished_at, severity, source, summary, vehicles, collisions, lat, lon, "
                    "result->'evidence'->>'confirmed' FROM incidents ORDER BY created_at DESC LIMIT %s", (limit,))
        rows = cur.fetchall()
    return [{"id": r[0], "time": r[1], "severity": r[2], "source": r[3], "summary": r[4] or "",
             "vehicles": r[5] or [], "collisions": r[6],
             "location": {"lat": r[7], "lon": r[8]}, "ready": True, "thumb": r[9]} for r in rows]


def get_media(rid, name):
    """-> (bytes, content_type) or None."""
    if not enabled():
        return None
    with _connect() as con, con.cursor() as cur:
        cur.execute("SELECT data, content_type FROM media WHERE incident_id=%s AND name=%s", (rid, name))
        row = cur.fetchone()
    return (bytes(row[0]), row[1]) if row else None


# ---------------- settings ----------------
def load_settings_into_env():
    """Called at start-up: values saved on the Settings page override the environment."""
    if not enabled():
        return 0
    with _connect() as con, con.cursor() as cur:
        cur.execute("SELECT key, value FROM settings")
        rows = cur.fetchall()
    for k, v in rows:
        os.environ[k] = v
    return len(rows)


def save_settings(updates):
    if not enabled() or not updates:
        return False
    with _connect() as con, con.cursor() as cur:
        for k, v in updates.items():
            cur.execute("INSERT INTO settings (key, value) VALUES (%s,%s) "
                        "ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value", (k, v))
    return True
