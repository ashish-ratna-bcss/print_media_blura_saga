"""
One place that decides where the database is.

Until now the connection was spelled out twice -- `Store` and `Corpus` each
carried their own `"mongodb://localhost:27017"` / `"epaper"` default, and
web/app.py read `EPAPER_MONGO` but nothing read a database name at all. That
works exactly as long as there is one MongoDB with one database on it. Pointing
the corpus at Atlas breaks all three assumptions at once: the cluster is
shared, the database name matters, and the URI carries credentials that must
never reach git.

So: settings come from the environment, the environment can be primed from a
gitignored `.env`, and every module asks this file rather than hardcoding.

Precedence, highest first:

    1. an argument passed explicitly in code
    2. a real environment variable
    3. .env at the repo root
    4. the defaults below

Real environment beats .env deliberately. A `.env` is a convenience for a
developer machine; when this runs under systemd or a container the process
environment is the operating truth and a stale file must not override it.

THE DEFAULT DATABASE IN THE URI IS IGNORED, ALWAYS
--------------------------------------------------
Atlas hands you a connection string ending in `/some_database`, and that name
is whatever database happened to be selected in the UI when you clicked Connect
-- on a shared cluster it is usually someone else's. PyMongo's
`get_default_database()` would follow it. Nothing here calls that. The
database is named by `EPAPER_MONGO_DB` and by nothing else, so a copy-pasted
URI cannot silently steer writes into a neighbouring tenant's data.
`RESERVED_DBS` is the second half of that guard.
"""

from __future__ import annotations

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_FILE = os.path.join(ROOT, ".env")

DEFAULT_URI = "mongodb://localhost:27017"
DEFAULT_DB = "epaper_corpus"

# Mongo's own bookkeeping databases. Writing the corpus into any of these is
# never intentional -- it is a typo or an unedited connection string -- so it
# fails loudly instead of succeeding quietly in the wrong place.
RESERVED_DBS = {"admin", "local", "config"}

_CRED = re.compile(r"://([^:/@]+):([^@]+)@")
_loaded = False
# Keys that `.env` put into os.environ, as opposed to keys that were already
# there. Once loaded the two are indistinguishable by inspection, which is
# enough to make a diagnostic lie: "EPAPER_MONGO is set in your environment"
# is unhelpful when the thing that set it was the file being diagnosed.
_from_file: set = set()


# -- .env ------------------------------------------------------------------

def load_env(path: str | None = None, override: bool = False) -> bool:
    """
    Prime os.environ from `.env`. Idempotent; safe to call from anywhere.

    Uses python-dotenv when present and falls back to a small parser when it
    is not, because the alternative -- an ImportError at startup on a box where
    someone skipped a pip install -- is a worse failure than a slightly less
    capable parser. The fallback handles the two forms that actually appear in
    a `.env`: `KEY=value` and `KEY="value"`.
    """
    global _loaded
    path = path or ENV_FILE
    if _loaded and path == ENV_FILE and not override:
        return True
    if not os.path.exists(path):
        _loaded = True
        return False
    before = set(os.environ)
    try:
        from dotenv import load_dotenv
        load_dotenv(path, override=override)
    except ImportError:
        _load_env_basic(path, override)
    _from_file.update(set(os.environ) - before)
    if path == ENV_FILE:
        _loaded = True
    return True


def _load_env_basic(path: str, override: bool):
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            if key.startswith("export "):
                key = key[7:].strip()
            val = val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                val = val[1:-1]
            if override or key not in os.environ:
                os.environ[key] = val


# -- settings ---------------------------------------------------------------

def mongo_uri(uri: str | None = None) -> str:
    load_env()
    return uri or os.environ.get("EPAPER_MONGO") or DEFAULT_URI


def mongo_db(db: str | None = None) -> str:
    load_env()
    name = db or os.environ.get("EPAPER_MONGO_DB") or DEFAULT_DB
    if name in RESERVED_DBS:
        raise ValueError(
            "EPAPER_MONGO_DB=%r is one of MongoDB's own databases. Set it to a "
            "database of your own, e.g. %r." % (name, DEFAULT_DB))
    return name


def mongo_kwargs(**over) -> dict:
    """
    Client options, tuned by where the server actually is.

    A local mongod answers in single-digit milliseconds and a 4 s timeout is
    generous. Atlas is a TLS handshake plus replica-set discovery across the
    public internet, and 4 s is enough to fail on a slow morning while the
    cluster is perfectly healthy -- so anything not on loopback gets 15 s.

    `appName` is not cosmetic on a shared cluster: it is the field the Atlas
    ops view groups connections by, which is how you tell this app's traffic
    apart from everything else pointed at the same hosts.
    """
    load_env()
    uri = mongo_uri()
    local = "localhost" in uri or "127.0.0.1" in uri
    kw = {
        "serverSelectionTimeoutMS": _int("EPAPER_MONGO_TIMEOUT_MS",
                                         4000 if local else 15000),
        "connectTimeoutMS": _int("EPAPER_MONGO_CONNECT_MS",
                                 4000 if local else 15000),
        "appName": os.environ.get("EPAPER_MONGO_APPNAME", "epaper-corpus"),
        "retryWrites": True,
    }
    kw.update(over)
    return kw


def _int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key) or default)
    except ValueError:
        return default


# -- reporting ---------------------------------------------------------------

def source(key: str) -> str:
    """
    Where a setting came from: "environment", ".env", or "default".

    Worth reporting, because the precedence surprises people. Editing `.env`
    has no effect at all on a key that is also a real environment variable,
    and the only visible symptom is a change that does not take.
    """
    load_env()
    if key in _from_file:
        return ".env"
    return "environment" if key in os.environ else "default"


def redact(uri: str) -> str:
    """A URI safe to print, log, or paste into a bug report."""
    return _CRED.sub(lambda m: "://%s:***@" % m.group(1), uri or "")


def hosts(uri: str) -> str:
    """Just the host list, for a one-line 'connected to' banner."""
    tail = (uri or "").split("://", 1)[-1]
    tail = tail.split("@", 1)[-1]
    return tail.split("/", 1)[0].split("?", 1)[0]


def is_atlas(uri: str | None = None) -> bool:
    return "mongodb.net" in (uri or mongo_uri())


def describe() -> dict:
    """Where we are pointed, with nothing secret in it."""
    uri = mongo_uri()
    return {
        "uri": redact(uri),
        "hosts": hosts(uri),
        "database": mongo_db(),
        "atlas": is_atlas(uri),
        "env_file": ENV_FILE if os.path.exists(ENV_FILE) else None,
    }
