"""Accounts, their CVs and candidate profiles, in one SQLite database
(config.DB_FILE); the CVs' files lie in config.CV_DIR.

    users       username and password hash (PBKDF2-SHA256); the user's
                OpenRouter key (sealed, see vault.py; NULL: none given),
                own lists of paid and free models, and whether
                each of the two may be used
    logins      login sessions, by the SHA-256 of their cookie token
    cvs         a user's uploaded CV: file name, kind, its text, and what a
                model read from it (status: pending, working, done,
                failed)
    profiles    a user's profile: their own description, what they look
                for (`wishes`, see matching.py, and `goals`, in their own
                words), their changes to what the CVs say (see
                candidate.py), the analysis a model wrote of the profile
                (status as for CVs; `analysis_of`: the fingerprint of the
                profile it was written from), when their offers were last
                looked for and whether they asked for it now, when they
                last changed what they look for (`wishes_at`: offers
                rated before are rated again, and the day's number of
                ratings starts anew)
    offers      job offers found on the portals (see portals.py), shared
                by all users: what the portal's list says (JSON), when it
                was first and last seen there, and its full text once it
                was needed
    searches    when a portal was last searched for a term, with what came
                of it
    places      where a place is (see geo.py), NULL when it wasn't found
    matches     an offer kept for a user: how promising it looked to the
                program (`prescore`), what a model made of it (`review`,
                with `score` 0..100; `wanted`: the user asked for it,
                `extra`: it was, so it doesn't count in the day's number),
                and the user's mark: new, interested, applied or rejected
"""

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time

import candidate
import config
import matching
import vault

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    username TEXT NOT NULL,
    username_key TEXT NOT NULL UNIQUE,   -- casefolded, for lookups
    password_hash TEXT NOT NULL,
    created_at REAL NOT NULL,
    api_key TEXT,                        -- OpenRouter's, sealed (vault.py)
    paid_models TEXT,                    -- JSON list; NULL: the defaults
    free_models TEXT,
    use_paid INTEGER,                    -- NULL: the default
    use_free INTEGER,                    -- NULL: the default
    daily_reviews INTEGER                -- offers rated a day; NULL: default
);
CREATE TABLE IF NOT EXISTS logins (
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS cvs (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    filename TEXT NOT NULL,
    kind TEXT NOT NULL,                  -- documents.KINDS
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    uploaded_at REAL NOT NULL,
    text TEXT,                           -- NULL until read
    language TEXT,
    status TEXT NOT NULL,                -- pending, working, done, failed
    error TEXT,
    extraction TEXT,                     -- JSON, see candidate.py
    model TEXT,                          -- JSON: who read it
    processed_at REAL
);
CREATE INDEX IF NOT EXISTS cvs_by_user ON cvs (user_id, id);
CREATE TABLE IF NOT EXISTS profiles (
    user_id INTEGER PRIMARY KEY REFERENCES users(id),
    about TEXT NOT NULL DEFAULT '',
    goals TEXT NOT NULL DEFAULT '',
    overrides TEXT NOT NULL DEFAULT '{}',
    updated_at REAL NOT NULL,
    analysis TEXT,                       -- JSON, see agents.analyze
    analysis_status TEXT,                -- pending, working, done, failed
    analysis_error TEXT,
    analysis_of TEXT,                    -- the profile's fingerprint
    analysis_model TEXT,                 -- JSON: who wrote it
    analysis_at REAL,
    wishes TEXT,                         -- JSON, see matching.py
    offers_at REAL,                      -- offers last looked for
    offers_wanted INTEGER NOT NULL DEFAULT 0,
    wishes_at REAL                       -- what they look for last changed
);
CREATE TABLE IF NOT EXISTS places (
    key TEXT PRIMARY KEY,                -- candidate.normalize(name)
    name TEXT NOT NULL,
    lat REAL,                            -- NULL: not found
    lon REAL,
    looked_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS offers (
    id INTEGER PRIMARY KEY,
    portal TEXT NOT NULL,
    key TEXT NOT NULL,                   -- the portal's id
    url TEXT NOT NULL,
    data TEXT NOT NULL,                  -- JSON, see portals.py
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    details TEXT,                        -- the full text, once fetched
    details_at REAL,
    UNIQUE (portal, key)
);
CREATE INDEX IF NOT EXISTS offers_by_time ON offers (last_seen);
CREATE TABLE IF NOT EXISTS searches (
    portal TEXT NOT NULL,
    term TEXT NOT NULL,                  -- casefolded
    searched_at REAL NOT NULL,
    found INTEGER,
    error TEXT,
    PRIMARY KEY (portal, term)
);
CREATE TABLE IF NOT EXISTS matches (
    user_id INTEGER NOT NULL REFERENCES users(id),
    offer_id INTEGER NOT NULL REFERENCES offers(id),
    created_at REAL NOT NULL,
    prescore REAL NOT NULL,
    status TEXT NOT NULL DEFAULT 'new',  -- new, interested, applied, rejected
    wanted INTEGER NOT NULL DEFAULT 0,   -- the user asked for a review
    extra INTEGER NOT NULL DEFAULT 0,    -- reviewed as asked: not counted
    score INTEGER,                       -- 0..100 by the model
    review TEXT,                         -- JSON, see agents.review_offer
    review_error TEXT,
    model TEXT,                          -- JSON: who reviewed it
    reviewed_at REAL,
    PRIMARY KEY (user_id, offer_id)
);
CREATE INDEX IF NOT EXISTS matches_by_review ON matches (user_id, reviewed_at);
"""

ADDED_COLUMNS = (
    ("users", "use_free", "INTEGER"), ("users", "daily_reviews", "INTEGER"),
    ("profiles", "wishes", "TEXT"), ("profiles", "offers_at", "REAL"),
    ("profiles", "offers_wanted", "INTEGER NOT NULL DEFAULT 0"),
    ("profiles", "wishes_at", "REAL"),
    ("matches", "extra", "INTEGER NOT NULL DEFAULT 0"),
)

USERNAME_RE = re.compile(r"[\w.\-]{3,32}")
MIN_PASSWORD = 6
PBKDF2_ITERATIONS = 200000
LOGIN_DAYS = 30
MAX_MODELS = 8    # in each of a user's two lists
MAX_MODEL_NAME = 100
MAX_FILENAME = 120


class Error(ValueError):
    """A request that can't be done, with a message for the user."""


def connect():
    conn = sqlite3.connect(config.DB_FILE, timeout=10)
    conn.row_factory = lambda cursor, row: {
        d[0]: v for d, v in zip(cursor.description, row)}
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init():
    """Makes the tables; CVs and analyses left half done by a restart are
    started again."""
    os.makedirs(config.CV_DIR, exist_ok=True)
    conn = connect()
    with conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        # columns added since the tables were first made
        for table, column, kind in ADDED_COLUMNS:
            if column not in {r["name"] for r in conn.execute(
                    "PRAGMA table_info(%s)" % table)}:
                conn.execute("ALTER TABLE %s ADD COLUMN %s %s"
                             % (table, column, kind))
                if column == "wishes_at":
                    # wishes saved before this was kept: as of the
                    # profile's last change
                    conn.execute("UPDATE profiles SET wishes_at = updated_at "
                                 "WHERE wishes IS NOT NULL")
        conn.execute("UPDATE cvs SET status = 'pending' "
                     "WHERE status = 'working'")
        conn.execute("UPDATE profiles SET analysis_status = 'pending' "
                     "WHERE analysis_status = 'working'")
        # reviews asked for before `extra` was kept
        conn.execute("UPDATE matches SET extra = 1, wanted = 0 WHERE "
                     "wanted = 1 AND reviewed_at IS NOT NULL")
    conn.close()


# --- accounts ---------------------------------------------------------

def _hash_password(password, salt=None, iterations=PBKDF2_ITERATIONS):
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 salt.encode("ascii"), iterations)
    return "pbkdf2_sha256$%d$%s$%s" % (iterations, salt, digest.hex())


def _check_password(password, stored):
    _, iterations, salt, _ = stored.split("$")
    return hmac.compare_digest(
        _hash_password(password, salt, int(iterations)), stored)


def create_user(conn, username, password):
    username = (username or "").strip()
    if not USERNAME_RE.fullmatch(username):
        raise Error("Nazwa użytkownika: 3–32 znaki, litery, cyfry, "
                    "kropka, myślnik lub podkreślnik.")
    if len(password or "") < MIN_PASSWORD:
        raise Error("Hasło musi mieć co najmniej %d znaków." % MIN_PASSWORD)
    try:
        with conn:
            cursor = conn.execute(
                "INSERT INTO users (username, username_key, password_hash, "
                "created_at) VALUES (?, ?, ?, ?)",
                (username, username.casefold(), _hash_password(password),
                 time.time()))
    except sqlite3.IntegrityError:
        raise Error("Ta nazwa użytkownika jest już zajęta.")
    return {"id": cursor.lastrowid, "username": username}


def find_user(conn, username):
    return conn.execute("SELECT id, username FROM users WHERE "
                        "username_key = ?",
                        ((username or "").strip().casefold(),)).fetchone()


def set_password(conn, username, password):
    """Sets a user's password and logs them out everywhere; returns the
    username as stored, None if there's no such user."""
    if len(password or "") < MIN_PASSWORD:
        raise Error("Hasło musi mieć co najmniej %d znaków." % MIN_PASSWORD)
    user = find_user(conn, username)
    if not user:
        return None
    with conn:
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?",
                     (_hash_password(password), user["id"]))
        conn.execute("DELETE FROM logins WHERE user_id = ?", (user["id"],))
    return user["username"]


def check_login(conn, username, password):
    """Returns the user, or None if the name or password is wrong."""
    user = conn.execute(
        "SELECT id, username, password_hash FROM users "
        "WHERE username_key = ?",
        ((username or "").strip().casefold(),)).fetchone()
    if user and _check_password(password or "", user["password_hash"]):
        return {"id": user["id"], "username": user["username"]}
    if not user:
        _hash_password(password or "")  # take as long as a wrong password
    return None


# --- a user's key and models ------------------------------------------

def set_api_key(conn, user_id, key):
    """Stores the user's OpenRouter key (None removes it), sealed."""
    with conn:
        conn.execute("UPDATE users SET api_key = ? WHERE id = ?",
                     (vault.seal(key) if key else None, user_id))


def set_models(conn, user_id, paid, free):
    """The user's own ladder of models: two lists of names. None for
    both goes back to the server's defaults."""
    with conn:
        conn.execute(
            "UPDATE users SET paid_models = ?, free_models = ? WHERE id = ?",
            (None if paid is None else json.dumps(paid),
             None if free is None else json.dumps(free), user_id))


def set_use(conn, user_id, which, on):
    """Whether the user's paid models (which: "paid") or free ones
    ("free") may be used."""
    column = {"paid": "use_paid", "free": "use_free"}[which]
    with conn:
        conn.execute("UPDATE users SET %s = ? WHERE id = ?" % column,
                     (int(bool(on)), user_id))


def account(conn, user_id):
    """What a user's requests go with: {key (None: none given), paid, free
    (lists of models), custom (the lists are the user's own, not the
    defaults), use_paid, use_free (whether the paid ones, the free ones
    may be used; see llm.ladder)}."""
    row = conn.execute("SELECT api_key, paid_models, free_models, use_paid, "
                       "use_free FROM users WHERE id = ?",
                       (user_id,)).fetchone() or {}
    custom = row.get("paid_models") is not None
    paid = json.loads(row["paid_models"]) if custom \
        else list(config.OPENROUTER_PAID_MODELS)
    wanted = config.OPENROUTER_PAID_DEFAULT if row.get("use_paid") is None \
        else bool(row["use_paid"])
    free = config.OPENROUTER_FREE_DEFAULT if row.get("use_free") is None \
        else bool(row["use_free"])
    return {
        "key": vault.unseal(row.get("api_key")),
        "paid": paid,
        "free": json.loads(row["free_models"] or "[]") if custom
        else list(config.OPENROUTER_MODELS),
        "custom": custom,
        "use_paid": wanted and bool(paid),
        "use_free": free,
    }


def _token_hash(token):
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def create_login(conn, user_id):
    """Returns a new login's token, for the cookie."""
    token = secrets.token_urlsafe(32)
    with conn:
        conn.execute("DELETE FROM logins WHERE expires_at < ?",
                     (time.time(),))
        conn.execute("INSERT INTO logins VALUES (?, ?, ?)",
                     (_token_hash(token), user_id,
                      time.time() + LOGIN_DAYS * 86400))
    return token


def login_user(conn, token):
    """Returns the login's user, or None; extends the login."""
    if not token:
        return None
    try:
        key = _token_hash(token)
    except UnicodeEncodeError:
        return None
    user = conn.execute(
        "SELECT u.id, u.username FROM logins l JOIN users u "
        "ON u.id = l.user_id WHERE l.token_hash = ? AND l.expires_at > ?",
        (key, time.time())).fetchone()
    if user:
        with conn:
            conn.execute("UPDATE logins SET expires_at = ? "
                         "WHERE token_hash = ?",
                         (time.time() + LOGIN_DAYS * 86400, key))
    return user


def delete_login(conn, token):
    if token:
        with conn:
            conn.execute("DELETE FROM logins WHERE token_hash = ?",
                         (_token_hash(token),))


# --- CVs ------------------------------------------------------------------

def _cv_row(row):
    if row:
        row["extraction"] = json.loads(row["extraction"]) \
            if row["extraction"] else None
        row["model"] = json.loads(row["model"]) if row["model"] else None
    return row


def cv_path(cv):
    return os.path.join(config.CV_DIR, str(cv["user_id"]),
                        "%d.%s" % (cv["id"], cv["kind"]))


def clean_filename(name):
    name = os.path.basename(str(name or "").replace("\\", "/"))
    name = "".join(c for c in name if c.isprintable()).strip()
    return name[-MAX_FILENAME:] or "cv"


def add_cv(conn, user_id, filename, kind, data):
    """Stores an uploaded file as a CV waiting to be read; returns its
    id."""
    digest = hashlib.sha256(data).hexdigest()
    count = conn.execute("SELECT COUNT(*) AS n FROM cvs WHERE user_id = ?",
                         (user_id,)).fetchone()["n"]
    if count >= config.MAX_CVS:
        raise Error("Możesz mieć najwyżej %d CV: usuń któreś, żeby dodać "
                    "nowe." % config.MAX_CVS)
    same = conn.execute("SELECT filename FROM cvs WHERE user_id = ? AND "
                        "sha256 = ?", (user_id, digest)).fetchone()
    if same:
        raise Error("To CV już jest wgrane (%s)." % same["filename"])
    with conn:
        cursor = conn.execute(
            "INSERT INTO cvs (user_id, filename, kind, size, sha256, "
            "uploaded_at, status) VALUES (?, ?, ?, ?, ?, ?, 'pending')",
            (user_id, clean_filename(filename), kind, len(data), digest,
             time.time()))
        cv = {"id": cursor.lastrowid, "user_id": user_id, "kind": kind}
        os.makedirs(os.path.dirname(cv_path(cv)), exist_ok=True)
        with open(cv_path(cv), "wb") as f:
            f.write(data)
    return cv["id"]


def get_cv(conn, user_id, cv_id):
    return _cv_row(conn.execute("SELECT * FROM cvs WHERE id = ? AND "
                                "user_id = ?", (cv_id, user_id)).fetchone())


def user_cvs(conn, user_id):
    """The user's CVs, oldest first, without their text (but its length,
    `chars`)."""
    return [_cv_row(r) for r in conn.execute(
        "SELECT id, user_id, filename, kind, size, uploaded_at, language, "
        "status, error, extraction, model, processed_at, length(text) AS "
        "chars FROM cvs WHERE user_id = ? ORDER BY id", (user_id,))]


def delete_cv(conn, cv):
    with conn:
        conn.execute("DELETE FROM cvs WHERE id = ?", (cv["id"],))
        overrides = get_profile(conn, cv["user_id"])["overrides"]
        candidate.forget_cv(overrides, cv["id"])
        save_overrides(conn, cv["user_id"], overrides)
    try:
        os.remove(cv_path(cv))
    except OSError:
        pass


def retry_cv(conn, cv):
    with conn:
        conn.execute("UPDATE cvs SET status = 'pending', error = NULL "
                     "WHERE id = ?", (cv["id"],))


def claim_cv(conn):
    """The next CV waiting to be read, marked as being read; None when
    there's none. A user's CVs are read one at a time, each knowing what
    the ones before held (see worker.read_cv). (Only one process uses the
    database: the worker's lock keeps two threads from claiming the same
    one.)"""
    row = conn.execute(
        "SELECT id FROM cvs c WHERE status = 'pending' AND NOT EXISTS "
        "(SELECT 1 FROM cvs w WHERE w.user_id = c.user_id AND "
        "w.status = 'working') ORDER BY id LIMIT 1").fetchone()
    if not row:
        return None
    with conn:
        conn.execute("UPDATE cvs SET status = 'working' WHERE id = ?",
                     (row["id"],))
    return _cv_row(conn.execute("SELECT * FROM cvs WHERE id = ?",
                                (row["id"],)).fetchone())


def save_cv_text(conn, cv_id, text, language):
    with conn:
        conn.execute("UPDATE cvs SET text = ?, language = ? WHERE id = ?",
                     (text, language, cv_id))


def finish_cv(conn, cv_id, extraction=None, model=None, error=None):
    with conn:
        conn.execute(
            "UPDATE cvs SET status = ?, extraction = ?, model = ?, "
            "error = ?, processed_at = ? WHERE id = ?",
            ("failed" if error else "done",
             json.dumps(extraction, ensure_ascii=False) if extraction
             else None, json.dumps(model) if model else None, error,
             time.time(), cv_id))


def cvs_busy(conn, user_id):
    return conn.execute("SELECT COUNT(*) AS n FROM cvs WHERE user_id = ? "
                        "AND status IN ('pending', 'working')",
                        (user_id,)).fetchone()["n"]


# --- profiles ---------------------------------------------------------

def _profile_row(row, user_id):
    if not row:
        return {"user_id": user_id, "about": "", "goals": "",
                "overrides": candidate.empty_overrides(), "analysis": None,
                "analysis_status": None, "analysis_error": None,
                "analysis_of": None, "analysis_model": None,
                "analysis_at": None, "updated_at": None,
                "wishes": matching.empty(), "offers_at": None,
                "offers_wanted": 0, "wishes_at": None}
    wishes = matching.empty()
    wishes.update(json.loads(row["wishes"] or "{}"))
    row["wishes"] = wishes
    overrides = candidate.empty_overrides()
    overrides.update(json.loads(row["overrides"] or "{}"))
    row["overrides"] = overrides
    row["analysis"] = json.loads(row["analysis"]) if row["analysis"] else None
    row["analysis_model"] = json.loads(row["analysis_model"]) \
        if row["analysis_model"] else None
    return row


def get_profile(conn, user_id):
    return _profile_row(conn.execute("SELECT * FROM profiles WHERE "
                                     "user_id = ?", (user_id,)).fetchone(),
                        user_id)


def built_profile(conn, user_id):
    """(the profile as candidate.build() makes it, the profiles row, the
    user's CVs)."""
    row = get_profile(conn, user_id)
    cvs = user_cvs(conn, user_id)
    return candidate.build(cvs, row["overrides"]), row, cvs


def profile_text(profile, row):
    """The profile as text for a model (candidate.text), with what the
    user wrote of themselves and what they look for."""
    return candidate.text(profile, row["about"], matching.describe(
        row["wishes"], row["goals"]))


def _ensure_profile(conn, user_id):
    conn.execute("INSERT OR IGNORE INTO profiles (user_id, updated_at) "
                 "VALUES (?, ?)", (user_id, time.time()))


def save_overrides(conn, user_id, overrides):
    with conn:
        _ensure_profile(conn, user_id)
        conn.execute("UPDATE profiles SET overrides = ?, updated_at = ? "
                     "WHERE user_id = ?",
                     (json.dumps(overrides, ensure_ascii=False), time.time(),
                      user_id))


def save_about(conn, user_id, about):
    with conn:
        _ensure_profile(conn, user_id)
        conn.execute("UPDATE profiles SET about = ?, updated_at = ? "
                     "WHERE user_id = ?",
                     (str(about or "").strip()[:candidate.MAX_ABOUT],
                      time.time(), user_id))


def save_wishes(conn, user_id, wishes, goals):
    """What the user looks for (matching.clean()'s) and, in their own
    words, `goals`; their offers are looked for again soon. If that
    changed, offers rated before are rated again, and the day's number of
    ratings starts anew."""
    goals = str(goals or "").strip()[:candidate.MAX_ABOUT]
    old = get_profile(conn, user_id)
    changed = old["wishes"] != wishes or old["goals"] != goals
    now = time.time()
    with conn:
        _ensure_profile(conn, user_id)
        conn.execute("UPDATE profiles SET wishes = ?, goals = ?, "
                     "offers_wanted = 1, updated_at = ?, wishes_at = ? "
                     "WHERE user_id = ?",
                     (json.dumps(wishes, ensure_ascii=False), goals, now,
                      now if changed else old["wishes_at"], user_id))
        if changed:
            conn.execute("UPDATE matches SET review_error = NULL WHERE "
                         "user_id = ?", (user_id,))


def request_analysis(conn, user_id):
    with conn:
        _ensure_profile(conn, user_id)
        conn.execute("UPDATE profiles SET analysis_status = 'pending', "
                     "analysis_error = NULL WHERE user_id = ?", (user_id,))


def claim_analysis(conn):
    """The next user whose analysis waits to be written (and none of whose
    CVs are still being read), marked as being written; None if none."""
    row = conn.execute(
        "SELECT user_id FROM profiles p WHERE analysis_status = 'pending' "
        "AND NOT EXISTS (SELECT 1 FROM cvs c WHERE c.user_id = p.user_id "
        "AND c.status IN ('pending', 'working')) ORDER BY updated_at "
        "LIMIT 1").fetchone()
    if not row:
        return None
    with conn:
        conn.execute("UPDATE profiles SET analysis_status = 'working' "
                     "WHERE user_id = ?", (row["user_id"],))
    return row["user_id"]


def finish_analysis(conn, user_id, analysis=None, fingerprint=None,
                    model=None, error=None):
    with conn:
        if error:
            conn.execute("UPDATE profiles SET analysis_status = 'failed', "
                         "analysis_error = ? WHERE user_id = ? AND "
                         "analysis_status = 'working'",
                         (error, user_id))
            return
        # asked for again meanwhile: it stays pending
        conn.execute(
            "UPDATE profiles SET analysis = ?, analysis_status = CASE "
            "analysis_status WHEN 'working' THEN 'done' ELSE "
            "analysis_status END, analysis_error = NULL, analysis_of = ?, "
            "analysis_model = ?, analysis_at = ? WHERE user_id = ?",
            (json.dumps(analysis, ensure_ascii=False), fingerprint,
             json.dumps(model), time.time(), user_id))


# --- offers ---------------------------------------------------------------

def set_daily_reviews(conn, user_id, count):
    with conn:
        conn.execute("UPDATE users SET daily_reviews = ? WHERE id = ?",
                     (count, user_id))


def daily_reviews(conn, user_id):
    row = conn.execute("SELECT daily_reviews FROM users WHERE id = ?",
                       (user_id,)).fetchone()
    value = row and row["daily_reviews"]
    return config.OFFERS_DAILY_REVIEWS if value is None else value


def want_offers(conn, user_id):
    """The user asked for their offers to be looked for now."""
    with conn:
        _ensure_profile(conn, user_id)
        conn.execute("UPDATE profiles SET offers_wanted = 1 WHERE "
                     "user_id = ?", (user_id,))


def users_due(conn, before):
    """Users whose offers were last looked for before `before`, or who
    asked for it, and who have a profile to look with."""
    return [r["user_id"] for r in conn.execute(
        "SELECT user_id FROM profiles WHERE offers_wanted = 1 OR "
        "offers_at IS NULL OR offers_at < ? ORDER BY offers_wanted DESC, "
        "offers_at", (before,))]


def offers_done(conn, user_id):
    with conn:
        conn.execute("UPDATE profiles SET offers_at = ?, offers_wanted = 0 "
                     "WHERE user_id = ?", (time.time(), user_id))


def searched_at(conn, portal, term):
    row = conn.execute("SELECT searched_at FROM searches WHERE portal = ? "
                       "AND term = ?", (portal, term.casefold())).fetchone()
    return row["searched_at"] if row else None


def record_search(conn, portal, term, found=None, error=None):
    with conn:
        conn.execute("INSERT OR REPLACE INTO searches VALUES (?, ?, ?, ?, ?)",
                     (portal, term.casefold(), time.time(), found, error))


def save_offers(conn, offers):
    """Keeps offers a portal listed (new ones added, known ones updated,
    their text kept)."""
    now = time.time()
    with conn:
        for o in offers:
            data = json.dumps({k: v for k, v in o.items()
                               if k not in ("portal", "key", "url")},
                              ensure_ascii=False)
            conn.execute(
                "INSERT INTO offers (portal, key, url, data, first_seen, "
                "last_seen) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (portal, "
                "key) DO UPDATE SET url = excluded.url, data = excluded.data, "
                "last_seen = excluded.last_seen",
                (o["portal"], str(o["key"]), o["url"], data, now, now))


def _offer_row(row):
    if row:
        data = json.loads(row.pop("data"))
        row.update(data)
    return row


def recent_offers(conn, since):
    """Offers seen on their portals since `since`."""
    return [_offer_row(r) for r in conn.execute(
        "SELECT id, portal, key, url, data, first_seen, last_seen FROM "
        "offers WHERE last_seen >= ?", (since,))]


def offer_details(conn, offer_id):
    row = conn.execute("SELECT details FROM offers WHERE id = ?",
                       (offer_id,)).fetchone()
    return row and row["details"]


def save_offer_details(conn, offer_id, text):
    with conn:
        conn.execute("UPDATE offers SET details = ?, details_at = ? WHERE "
                     "id = ?", (text, time.time(), offer_id))


def matched_ids(conn, user_id):
    return {r["offer_id"] for r in conn.execute(
        "SELECT offer_id FROM matches WHERE user_id = ?", (user_id,))}


def add_matches(conn, user_id, scored):
    """[(offer id, prescore)] kept for the user."""
    now = time.time()
    with conn:
        conn.executemany(
            "INSERT OR IGNORE INTO matches (user_id, offer_id, created_at, "
            "prescore) VALUES (?, ?, ?, ?)",
            [(user_id, offer_id, now, score) for offer_id, score in scored])


def _match_row(row):
    row = _offer_row(row)
    for key in ("review", "model"):
        row[key] = json.loads(row[key]) if row.get(key) else None
    return row


MATCH_COLUMNS = (
    "m.offer_id, m.created_at, m.prescore, m.status, m.wanted, m.extra, "
    "m.score, "
    "m.review, m.review_error, m.model, m.reviewed_at, o.portal, o.url, "
    "o.data, o.first_seen, o.last_seen")


def user_matches(conn, user_id):
    return [_match_row(r) for r in conn.execute(
        "SELECT %s FROM matches m JOIN offers o ON o.id = m.offer_id WHERE "
        "m.user_id = ? ORDER BY m.score DESC, m.prescore DESC"
        % MATCH_COLUMNS, (user_id,))]


def get_match(conn, user_id, offer_id):
    row = conn.execute(
        "SELECT %s FROM matches m JOIN offers o ON o.id = m.offer_id WHERE "
        "m.user_id = ? AND m.offer_id = ?" % MATCH_COLUMNS,
        (user_id, offer_id)).fetchone()
    return _match_row(row) if row else None


def reviews_since(conn, user_id, since):
    """How many offers a model rated for the user since `since` (the
    ones the user asked for aside)."""
    return conn.execute(
        "SELECT COUNT(*) AS n FROM matches WHERE user_id = ? AND "
        "reviewed_at >= ? AND extra = 0", (user_id, since)).fetchone()["n"]


TO_RATE = ("m.wanted = 0 AND (m.reviewed_at IS NULL OR m.reviewed_at < ?) "
           "AND m.review_error IS NULL AND m.status = 'new' AND "
           "o.last_seen >= ?")


def to_review(conn, user_id, since, limit, wishes_at):
    """The user's offers to be rated next: the ones they asked for, then
    the most promising not rated since the wishes last changed
    (`wishes_at`), seen on their portal since `since`, at most `limit` of
    the latter."""
    wanted = [_match_row(r) for r in conn.execute(
        "SELECT %s FROM matches m JOIN offers o ON o.id = m.offer_id WHERE "
        "m.user_id = ? AND m.wanted = 1" % MATCH_COLUMNS, (user_id,))]
    best = [_match_row(r) for r in conn.execute(
        "SELECT %s FROM matches m JOIN offers o ON o.id = m.offer_id WHERE "
        "m.user_id = ? AND %s ORDER BY m.prescore DESC LIMIT ?"
        % (MATCH_COLUMNS, TO_RATE),
        (user_id, wishes_at or 0, since, max(0, limit)))]
    return wanted + best


def want_more(conn, user_id, count, since, wishes_at):
    """Asks for the next `count` offers to be rated (as to_review() would
    take them); returns how many."""
    ids = [r["offer_id"] for r in conn.execute(
        "SELECT m.offer_id FROM matches m JOIN offers o ON o.id = m.offer_id "
        "WHERE m.user_id = ? AND %s ORDER BY m.prescore DESC LIMIT ?"
        % TO_RATE, (user_id, wishes_at or 0, since, count))]
    with conn:
        conn.executemany("UPDATE matches SET wanted = 1 WHERE user_id = ? "
                         "AND offer_id = ?", [(user_id, i) for i in ids])
    return len(ids)


def save_review(conn, user_id, offer_id, review=None, model=None,
                error=None):
    with conn:
        if error:
            conn.execute("UPDATE matches SET review_error = ?, wanted = 0 "
                         "WHERE user_id = ? AND offer_id = ?",
                         (error, user_id, offer_id))
            return
        conn.execute(
            "UPDATE matches SET review = ?, score = ?, model = ?, "
            "reviewed_at = ?, review_error = NULL, extra = wanted, "
            "wanted = 0 WHERE user_id = ? AND offer_id = ?",
            (json.dumps(review, ensure_ascii=False), review["score"],
             json.dumps(model), time.time(), user_id, offer_id))


def want_review(conn, user_id, offer_id):
    with conn:
        conn.execute("UPDATE matches SET wanted = 1, review_error = NULL "
                     "WHERE user_id = ? AND offer_id = ?",
                     (user_id, offer_id))


def set_match_status(conn, user_id, offer_id, status):
    with conn:
        conn.execute("UPDATE matches SET status = ? WHERE user_id = ? AND "
                     "offer_id = ?", (status, user_id, offer_id))


def forget_offers(conn, before):
    """Offers not seen since `before` that no user marked (as interesting
    or applied to) go, with their matches."""
    with conn:
        conn.execute(
            "DELETE FROM matches WHERE status IN ('new', 'rejected') AND "
            "offer_id IN (SELECT id FROM offers WHERE last_seen < ?)",
            (before,))
        conn.execute(
            "DELETE FROM offers WHERE last_seen < ? AND id NOT IN (SELECT "
            "offer_id FROM matches)", (before,))


def drop_matches(conn, user_id, offer_ids):
    with conn:
        conn.executemany("DELETE FROM matches WHERE user_id = ? AND "
                         "offer_id = ?", [(user_id, i) for i in offer_ids])


def rescore_matches(conn, user_id, scored):
    with conn:
        conn.executemany("UPDATE matches SET prescore = ? WHERE user_id = ? "
                         "AND offer_id = ?",
                         [(score, user_id, i) for i, score in scored])


def users_wanting_reviews(conn):
    return [r["user_id"] for r in conn.execute(
        "SELECT DISTINCT user_id FROM matches WHERE wanted = 1")]
