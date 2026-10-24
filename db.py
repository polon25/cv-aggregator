"""Accounts, their CVs and candidate profiles, in one SQLite database
(config.DB_FILE); the CVs' files lie in config.CV_DIR.

    users       username and password hash (PBKDF2-SHA256); the user's
                OpenRouter key (sealed, see vault.py; NULL: the server's
                key), own lists of paid and free models, and whether
                each of the two may be used
    logins      login sessions, by the SHA-256 of their cookie token
    cvs         a user's uploaded CV: file name, kind, its text, and what a
                model read from it (status: pending, working, done,
                failed)
    profiles    a user's profile: their own description and what they look
                for, their changes to what the CVs say (see candidate.py),
                and the analysis a model wrote of the profile (status as
                for CVs; `analysis_of`: the fingerprint of the profile it
                was written from)
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
    use_free INTEGER                     -- NULL: the default
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
    analysis_at REAL
);
"""

ADDED_COLUMNS = (("users", "use_free", "INTEGER"),)

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
        conn.execute("UPDATE cvs SET status = 'pending' "
                     "WHERE status = 'working'")
        conn.execute("UPDATE profiles SET analysis_status = 'pending' "
                     "WHERE analysis_status = 'working'")
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
    """What a user's requests go with: {key (their own, else the
    server's; None: neither), own_key (whether it's their own), paid, free
    (lists of models), custom (the lists are the user's own, not the
    defaults), use_paid, use_free (whether the paid ones, the free ones
    may be used; see llm.ladder)}."""
    row = conn.execute("SELECT api_key, paid_models, free_models, use_paid, "
                       "use_free FROM users WHERE id = ?",
                       (user_id,)).fetchone() or {}
    custom = row.get("paid_models") is not None
    own = vault.unseal(row.get("api_key"))
    paid = json.loads(row["paid_models"]) if custom \
        else list(config.OPENROUTER_PAID_MODELS)
    wanted = config.OPENROUTER_PAID_DEFAULT if row.get("use_paid") is None \
        else bool(row["use_paid"])
    free = config.OPENROUTER_FREE_DEFAULT if row.get("use_free") is None \
        else bool(row["use_free"])
    return {
        "key": own or config.OPENROUTER_API_KEY or None,
        "own_key": bool(own),
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
                "analysis_at": None, "updated_at": None}
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


def save_about(conn, user_id, about, goals):
    with conn:
        _ensure_profile(conn, user_id)
        conn.execute("UPDATE profiles SET about = ?, goals = ?, "
                     "updated_at = ? WHERE user_id = ?",
                     (str(about or "").strip()[:candidate.MAX_ABOUT],
                      str(goals or "").strip()[:candidate.MAX_ABOUT],
                      time.time(), user_id))


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
