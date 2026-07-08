import os
import uuid
import json
from contextlib import contextmanager

from psycopg_pool import ConnectionPool
from psycopg.rows import dict_row


_DB_URL = os.getenv("SUPABASE_DB_URL")
_pool: ConnectionPool | None = None


def _configure_conn(conn):
    # Supabase's transaction pooler (port 6543, PgBouncer in transaction mode)
    # does not keep session-level prepared statements between transactions.
    # psycopg3 prepares statements after a few uses of the same query and
    # would crash with 'prepared statement "_pg3_0" already exists' on hot
    # paths. Disabling preparation client-side keeps every query plain.
    conn.prepare_threshold = None


def _get_pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        if not _DB_URL:
            raise RuntimeError("SUPABASE_DB_URL is not set")
        _pool = ConnectionPool(
            conninfo=_DB_URL,
            min_size=2,
            max_size=10,
            open=True,
            configure=_configure_conn,
        )
    return _pool


@contextmanager
def _cursor():
    with _get_pool().connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            yield cur


def init_db():
    with _cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY,
                email TEXT UNIQUE NOT NULL,
                name TEXT,
                zip_code TEXT,
                state TEXT,
                city TEXT,
                gmail_address TEXT,
                email_screened BOOLEAN DEFAULT FALSE,
                gmail_refresh_token TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                last_active TIMESTAMPTZ DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS rate_limits (
                user_id TEXT,
                action TEXT,
                window_date TEXT,
                count INTEGER DEFAULT 0,
                PRIMARY KEY (user_id, action, window_date)
            );

            CREATE TABLE IF NOT EXISTS correspondence (
                id TEXT PRIMARY KEY,
                user_id TEXT REFERENCES users(id),
                bill_id TEXT,
                bill_title TEXT,
                legislator_name TEXT,
                legislator_office TEXT,
                legislator_state TEXT,
                to_email TEXT,
                contact_form_url TEXT,
                subject TEXT,
                body TEXT,
                sent_at TIMESTAMPTZ,
                delivery_method TEXT,
                gmail_thread_id TEXT,
                gmail_message_id TEXT,
                status TEXT DEFAULT 'sent',
                created_at TIMESTAMPTZ DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS replies (
                id TEXT PRIMARY KEY,
                correspondence_id TEXT REFERENCES correspondence(id),
                gmail_message_id TEXT UNIQUE,
                received_at TIMESTAMPTZ,
                preview_text TEXT
            );

            CREATE TABLE IF NOT EXISTS elections_search_cache (
                state_code TEXT PRIMARY KEY,
                results TEXT NOT NULL,
                cached_at DOUBLE PRECISION NOT NULL
            );

            CREATE TABLE IF NOT EXISTS disk_cache (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                cached_at DOUBLE PRECISION NOT NULL
            );

            CREATE TABLE IF NOT EXISTS known_elections (
                id BIGSERIAL PRIMARY KEY,
                state_code TEXT NOT NULL,
                name TEXT NOT NULL,
                date TEXT NOT NULL,
                type TEXT,
                source_url TEXT,
                notes TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(state_code, date, name)
            );
            CREATE INDEX IF NOT EXISTS idx_known_elections_state ON known_elections(state_code);

            CREATE TABLE IF NOT EXISTS subscriptions (
                id TEXT PRIMARY KEY,
                user_id TEXT,
                email TEXT NOT NULL,
                bill_id TEXT NOT NULL,
                bill_title TEXT,
                congress INTEGER,
                bill_type TEXT,
                bill_number INTEGER,
                ocd_id TEXT,
                subscribed_at TIMESTAMPTZ DEFAULT NOW(),
                last_notified_state TEXT,
                source TEXT DEFAULT 'manual',
                active BOOLEAN DEFAULT TRUE,
                UNIQUE(email, bill_id)
            );

            CREATE TABLE IF NOT EXISTS lobbying_bill_mentions (
                congress     INTEGER NOT NULL,
                bill_type    TEXT NOT NULL,
                bill_number  INTEGER NOT NULL,
                entity_name  TEXT NOT NULL,
                entity_kind  TEXT NOT NULL,
                mentions     INTEGER NOT NULL DEFAULT 0,
                entity_spend DOUBLE PRECISION NOT NULL DEFAULT 0,
                updated_at   DOUBLE PRECISION NOT NULL,
                PRIMARY KEY (congress, bill_type, bill_number, entity_name, entity_kind)
            );
            CREATE INDEX IF NOT EXISTS idx_lbm_bill
                ON lobbying_bill_mentions (congress, bill_type, bill_number);
        """)
    _bootstrap_known_elections_from_file()


def _bootstrap_known_elections_from_file():
    """One-shot: if known_elections is empty, populate from the shipped
    data/known_elections.json. Idempotent — skips when any row already exists,
    so admin edits via /admin/elections never get overwritten."""
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "data", "known_elections.json",
    )
    try:
        with _cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM known_elections")
            existing = cur.fetchone()["n"]
        if existing > 0:
            return
        with open(path) as f:
            data = json.load(f)
        inserted = 0
        with _cursor() as cur:
            for state_code, entries in data.items():
                for e in entries:
                    name = (e.get("name") or "").strip()
                    date_str = (e.get("date") or "").strip()
                    type_str = (e.get("type") or None)
                    if not name or not date_str:
                        continue
                    cur.execute("""
                        INSERT INTO known_elections (state_code, name, date, type)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT(state_code, date, name) DO NOTHING
                    """, (state_code.upper(), name, date_str, type_str))
                    inserted += 1
        print(f"[DB] Bootstrapped known_elections with {inserted} entries from JSON")
    except FileNotFoundError:
        print(f"[DB] known_elections JSON not found at {path} — skipping bootstrap")
    except Exception as e:
        print(f"[DB] Bootstrap error: {e}")


def upsert_user(user_id, email, name):
    with _cursor() as cur:
        cur.execute("""
            INSERT INTO users (id, email, name) VALUES (%s, %s, %s)
            ON CONFLICT(id) DO UPDATE SET
                email=excluded.email,
                name=excluded.name,
                last_active=NOW()
        """, (user_id, email, name))


def get_user(user_id):
    with _cursor() as cur:
        cur.execute("SELECT * FROM users WHERE id=%s", (user_id,))
        return cur.fetchone()


def update_user_gmail(user_id, gmail_address, refresh_token, screened):
    with _cursor() as cur:
        cur.execute("""
            UPDATE users SET
                gmail_address=%s, gmail_refresh_token=%s, email_screened=%s
            WHERE id=%s
        """, (gmail_address, refresh_token, bool(screened), user_id))


def update_user_zip(user_id, zip_code, state, city):
    with _cursor() as cur:
        cur.execute(
            "UPDATE users SET zip_code=%s, state=%s, city=%s WHERE id=%s",
            (zip_code, state, city, user_id),
        )


def check_rate_limit(user_id, action, daily_max):
    """Returns True if the user is within their daily limit."""
    from datetime import date
    today = date.today().isoformat()
    with _cursor() as cur:
        cur.execute(
            "SELECT count FROM rate_limits WHERE user_id=%s AND action=%s AND window_date=%s",
            (user_id, action, today),
        )
        row = cur.fetchone()
    count = row["count"] if row else 0
    return count < daily_max


def increment_rate_limit(user_id, action):
    from datetime import date
    today = date.today().isoformat()
    with _cursor() as cur:
        cur.execute("""
            INSERT INTO rate_limits (user_id, action, window_date, count)
            VALUES (%s, %s, %s, 1)
            ON CONFLICT(user_id, action, window_date) DO UPDATE SET count=rate_limits.count+1
        """, (user_id, action, today))


def check_bill_rep_cooldown(user_id, bill_id, legislator_name):
    """Block repeat sends to the same rep for the same bill within 30 days."""
    with _cursor() as cur:
        cur.execute("""
            SELECT id FROM correspondence
            WHERE user_id=%s AND bill_id=%s AND legislator_name=%s
              AND sent_at > NOW() - INTERVAL '30 days'
        """, (user_id, bill_id, legislator_name))
        return cur.fetchone() is not None  # True = in cooldown


def save_correspondence(item):
    with _cursor() as cur:
        cur.execute("""
            INSERT INTO correspondence
            (id, user_id, bill_id, bill_title, legislator_name, legislator_office,
             legislator_state, to_email, contact_form_url, subject, body,
             sent_at, delivery_method, gmail_thread_id, gmail_message_id, status)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """, (
            item["id"], item["user_id"], item["bill_id"], item["bill_title"],
            item["legislator_name"], item["legislator_office"], item["legislator_state"],
            item.get("to_email"), item.get("contact_form_url"),
            item["subject"], item["body"], item["sent_at"],
            item["delivery_method"], item.get("gmail_thread_id"),
            item.get("gmail_message_id"), "sent",
        ))


def get_user_correspondence(user_id):
    with _cursor() as cur:
        cur.execute(
            "SELECT * FROM correspondence WHERE user_id=%s ORDER BY sent_at DESC",
            (user_id,),
        )
        return cur.fetchall()


def get_correspondence_by_id(corr_id, user_id):
    with _cursor() as cur:
        cur.execute(
            "SELECT * FROM correspondence WHERE id=%s AND user_id=%s",
            (corr_id, user_id),
        )
        return cur.fetchone()


def save_reply(corr_id, gmail_message_id, received_at, preview_text):
    with _cursor() as cur:
        cur.execute("""
            INSERT INTO replies
            (id, correspondence_id, gmail_message_id, received_at, preview_text)
            VALUES (%s,%s,%s,%s,%s)
            ON CONFLICT(gmail_message_id) DO NOTHING
        """, (str(uuid.uuid4()), corr_id, gmail_message_id, received_at, preview_text))
        cur.execute(
            "UPDATE correspondence SET status='replied' WHERE id=%s",
            (corr_id,),
        )


def upsert_subscription(email, bill_id, bill_title, source='manual',
                        user_id=None, congress=None, bill_type=None,
                        bill_number=None, ocd_id=None):
    with _cursor() as cur:
        cur.execute("""
            INSERT INTO subscriptions
                (id, user_id, email, bill_id, bill_title, congress, bill_type,
                 bill_number, ocd_id, source, active)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, TRUE)
            ON CONFLICT(email, bill_id) DO UPDATE SET
                active=TRUE,
                source=CASE WHEN excluded.source='letter' THEN 'letter' ELSE subscriptions.source END,
                bill_title=COALESCE(excluded.bill_title, subscriptions.bill_title)
        """, (str(uuid.uuid4()), user_id, email, bill_id, bill_title,
              congress, bill_type, bill_number, ocd_id, source))


def get_subscription(email, bill_id):
    with _cursor() as cur:
        cur.execute(
            "SELECT * FROM subscriptions WHERE email=%s AND bill_id=%s",
            (email, bill_id),
        )
        return cur.fetchone()


def deactivate_subscription(email, bill_id):
    with _cursor() as cur:
        cur.execute(
            "UPDATE subscriptions SET active=FALSE WHERE email=%s AND bill_id=%s",
            (email, bill_id),
        )


def get_active_subscribed_bills():
    """Returns distinct federal bills with at least one active subscription."""
    with _cursor() as cur:
        cur.execute("""
            SELECT DISTINCT congress, bill_type, bill_number, bill_id, bill_title
            FROM subscriptions
            WHERE active=TRUE AND congress IS NOT NULL
        """)
        return cur.fetchall()


def get_subscriptions_for_bill(bill_id):
    """All active subscribers for a given bill_id."""
    with _cursor() as cur:
        cur.execute(
            "SELECT * FROM subscriptions WHERE bill_id=%s AND active=TRUE",
            (bill_id,),
        )
        return cur.fetchall()


def update_subscription_state(email, bill_id, new_state):
    with _cursor() as cur:
        cur.execute(
            "UPDATE subscriptions SET last_notified_state=%s WHERE email=%s AND bill_id=%s",
            (new_state, email, bill_id),
        )


def get_elections_cache(state_code, max_age_seconds=None):
    """
    Return cached results for state_code, or None if missing/stale.
    TTL is automatic: 2hr if result is empty, 48hr if non-empty.
    Pass max_age_seconds to override.
    """
    import time
    with _cursor() as cur:
        cur.execute(
            "SELECT results, cached_at FROM elections_search_cache WHERE state_code=%s",
            (state_code,),
        )
        row = cur.fetchone()
    if not row:
        return None
    results = json.loads(row["results"])
    age = time.time() - row["cached_at"]
    ttl = max_age_seconds if max_age_seconds is not None else (7200 if not results else 172800)
    return results if age < ttl else None


def set_elections_cache(state_code, results):
    """Persist Claude election results for a state."""
    import time
    with _cursor() as cur:
        cur.execute("""
            INSERT INTO elections_search_cache (state_code, results, cached_at)
            VALUES (%s, %s, %s)
            ON CONFLICT(state_code) DO UPDATE SET results=excluded.results, cached_at=excluded.cached_at
        """, (state_code, json.dumps(results), time.time()))


def get_known_elections(state_code):
    """
    Return list of known elections for a state from the Postgres known_elections
    table, shape matching what Claude web search produces. Dates from 60 days
    ago onward.

    Source of truth is now Postgres. The table is bootstrapped from the shipped
    data/known_elections.json on first init when empty; admin edits via
    /admin/elections persist there and are never overwritten.
    """
    from datetime import date, timedelta
    cutoff = (date.today() - timedelta(days=60)).isoformat()
    try:
        with _cursor() as cur:
            cur.execute("""
                SELECT name, date, type FROM known_elections
                WHERE state_code = %s AND date >= %s
                ORDER BY date ASC
            """, (state_code.upper(), cutoff))
            rows = cur.fetchall()
            result = [
                {
                    "name": r["name"],
                    "date": r["date"].isoformat() if hasattr(r["date"], "isoformat") else r["date"],
                    "type": r["type"],
                }
                for r in rows
            ]
            print(f"[DB] get_known_elections({state_code!r}) cutoff={cutoff} → {len(result)} rows")
            return result
    except Exception as e:
        print(f"[DB] get_known_elections error for {state_code}: {e}")
        return []


def list_known_elections(state_code=None):
    """Admin: list all known elections, optionally filtered by state."""
    with _cursor() as cur:
        if state_code:
            cur.execute(
                "SELECT * FROM known_elections WHERE state_code = %s ORDER BY date ASC",
                (state_code.upper(),),
            )
        else:
            cur.execute(
                "SELECT * FROM known_elections ORDER BY state_code ASC, date ASC"
            )
        return cur.fetchall()


def add_known_election(state_code, name, date, election_type=None, source_url=None, notes=None):
    """Insert a known election. Returns the new row's id."""
    with _cursor() as cur:
        cur.execute("""
            INSERT INTO known_elections (state_code, name, date, type, source_url, notes)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT(state_code, date, name) DO UPDATE SET
                type = excluded.type,
                source_url = excluded.source_url,
                notes = excluded.notes
            RETURNING id
        """, (state_code.upper(), name, date, election_type, source_url, notes))
        return cur.fetchone()["id"]


def delete_known_election(election_id):
    with _cursor() as cur:
        cur.execute("DELETE FROM known_elections WHERE id = %s", (election_id,))


def get_disk_cache(key, max_age_seconds):
    """Return cached value for key if it exists and is within max_age_seconds, else None."""
    import time
    with _cursor() as cur:
        cur.execute(
            "SELECT value, cached_at FROM disk_cache WHERE key=%s", (key,)
        )
        row = cur.fetchone()
    if not row:
        return None
    if time.time() - row["cached_at"] > max_age_seconds:
        return None
    return json.loads(row["value"])


def set_disk_cache(key, value):
    """Persist value under key with the current timestamp."""
    import time
    with _cursor() as cur:
        cur.execute("""
            INSERT INTO disk_cache (key, value, cached_at) VALUES (%s, %s, %s)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, cached_at=excluded.cached_at
        """, (key, json.dumps(value), time.time()))


def clear_disk_cache(prefix=None):
    """Delete cache entries. With prefix, only entries whose key starts with it.
    Returns number of rows deleted."""
    with _cursor() as cur:
        if prefix:
            cur.execute("DELETE FROM disk_cache WHERE key LIKE %s", (prefix + "%",))
        else:
            cur.execute("DELETE FROM disk_cache")
        return cur.rowcount


def record_bill_mentions(rows):
    """Upsert (bill → entity) lobbying mentions. Each row is a dict with
    congress, bill_type, bill_number, entity_name, entity_kind, mentions,
    entity_spend. Powers the per-bill 'Who's pushing this' panel; populated
    lazily as entity profiles are viewed."""
    if not rows:
        return
    import time
    now = time.time()
    with _cursor() as cur:
        for r in rows:
            cur.execute(
                """
                INSERT INTO lobbying_bill_mentions
                    (congress, bill_type, bill_number, entity_name, entity_kind,
                     mentions, entity_spend, updated_at)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (congress, bill_type, bill_number, entity_name, entity_kind)
                DO UPDATE SET mentions=excluded.mentions,
                              entity_spend=excluded.entity_spend,
                              updated_at=excluded.updated_at
                """,
                (
                    int(r["congress"]), str(r["bill_type"]).lower(), int(r["bill_number"]),
                    r["entity_name"], r["entity_kind"],
                    int(r.get("mentions") or 0), float(r.get("entity_spend") or 0), now,
                ),
            )


def get_bill_lobbying(congress, bill_type, bill_number, limit=12):
    """Return entities recorded lobbying a bill, ranked by mention intensity then
    entity size. Rows for the same entity under both filing roles (client +
    registrant) are merged into one, keeping the role with the most mentions for
    the click-through. Each row: entity_name, entity_kind, mentions, entity_spend."""
    with _cursor() as cur:
        cur.execute(
            """
            SELECT entity_name,
                   (array_agg(entity_kind ORDER BY mentions DESC))[1] AS entity_kind,
                   SUM(mentions) AS mentions,
                   MAX(entity_spend) AS entity_spend
            FROM lobbying_bill_mentions
            WHERE congress=%s AND bill_type=%s AND bill_number=%s
            GROUP BY entity_name
            ORDER BY mentions DESC, entity_spend DESC
            LIMIT %s
            """,
            (int(congress), str(bill_type).lower(), int(bill_number), int(limit)),
        )
        return cur.fetchall()


def get_replies(corr_id):
    with _cursor() as cur:
        cur.execute(
            "SELECT * FROM replies WHERE correspondence_id=%s ORDER BY received_at DESC",
            (corr_id,),
        )
        return cur.fetchall()
