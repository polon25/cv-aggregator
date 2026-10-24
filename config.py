"""Settings, read from the environment or from .env in the project directory
(see .env.example). Standard library only, so it also runs on Python 3.8.
"""

import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_env(path):
    """Sets KEY=VALUE lines from path as environment variables, unless
    they're already set."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _flag(name, default):
    return (os.getenv(name) or default).strip().lower() in (
        "1", "true", "yes", "on")


def _list(name):
    return [m.strip() for m in (os.getenv(name) or "").split(",")
            if m.strip()]


_load_env(os.path.join(BASE_DIR, ".env"))

DATA_DIR = os.getenv("DATA_DIR") or os.path.join(BASE_DIR, "data")
# The secret that users' API keys are encrypted with in the database (see
# vault.py); made on first use.
SECRET_FILE = os.getenv("SECRET_FILE") or os.path.join(BASE_DIR, "secret.key")
# Accounts, their CVs and candidate profiles.
DB_FILE = os.path.join(DATA_DIR, "cv.db")
# The uploaded files, one directory per user.
CV_DIR = os.path.join(DATA_DIR, "cvs")
WEB_DIR = os.path.join(BASE_DIR, "web")

WEB_HOST = os.getenv("WEB_HOST") or "0.0.0.0"
WEB_PORT = int(os.getenv("WEB_PORT") or "8075")
# 0: no new accounts from the page (`app.py --add-user` still makes them).
ALLOW_REGISTRATION = _flag("ALLOW_REGISTRATION", "1")

LOG_LEVEL = os.getenv("LOG_LEVEL") or "INFO"

# --- the models reading CVs and describing candidates -------------------

# Every user gives their own key in the page's settings; this one is only
# what `app.py --set-key USER` hands a user when given none.
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY") or ""
# Any OpenAI-compatible chat completions endpoint works.
OPENROUTER_URL = os.getenv("OPENROUTER_URL") or (
    "https://openrouter.ai/api/v1/chat/completions")
# "openrouter/free" routes each request to some free model.
OPENROUTER_MODEL = os.getenv("OPENROUTER_MODEL") or "openrouter/free"
# Preferred free models, comma-separated, tried first (one attempt each)
# before OPENROUTER_ATTEMPTS attempts with OPENROUTER_MODEL.
OPENROUTER_MODELS = _list("OPENROUTER_MODELS")
OPENROUTER_ATTEMPTS = int(os.getenv("OPENROUTER_ATTEMPTS") or "3")
# Paid models, comma-separated, tried before all of the above for users
# who want them (empty: there's no such choice). A name starting with "~"
# is OpenRouter's alias for the latest of a family.
OPENROUTER_PAID_MODELS = _list("OPENROUTER_PAID_MODELS")
# Whether a user who hasn't chosen yet gets the paid models.
OPENROUTER_PAID_DEFAULT = _flag("OPENROUTER_PAID_DEFAULT", "1")
# And the free ones (OPENROUTER_MODELS, OPENROUTER_MODEL), whose providers
# may keep what they're sent: the users' CVs. Without them nothing is sent
# to a free model, even when the paid ones fail.
OPENROUTER_FREE_DEFAULT = _flag("OPENROUTER_FREE_DEFAULT", "0")
# Dollars left on the key or the account below which the settings warn
# that the paid models' funds are running out.
OPENROUTER_LOW_FUNDS = float(os.getenv("OPENROUTER_LOW_FUNDS") or "0.5")
# Seconds one request (including reading the whole answer) may take.
OPENROUTER_TIMEOUT = int(os.getenv("OPENROUTER_TIMEOUT") or "180")
# Empty: the model's default.
OPENROUTER_TEMPERATURE = os.getenv("OPENROUTER_TEMPERATURE") or ""

# --- CVs ------------------------------------------------------------------

# Files a user may keep.
MAX_CVS = int(os.getenv("MAX_CVS") or "6")
# Largest file accepted, in megabytes.
MAX_CV_MB = float(os.getenv("MAX_CV_MB") or "5")
# A CV's text is cut to this many characters before a model reads it.
MAX_CV_CHARS = int(os.getenv("MAX_CV_CHARS") or "40000")
# Background threads reading CVs and writing analyses.
WORKERS = int(os.getenv("WORKERS") or "2")
