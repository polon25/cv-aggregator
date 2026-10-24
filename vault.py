"""Keeps users' API keys unreadable in the database: seal() encrypts a
text with the server's secret, unseal() gives it back.

The secret is 32 random bytes in secret.key beside the app (SECRET_FILE;
made on first use, readable by its owner only, not in the repository),
apart from data/,
so that a copy of the database alone gives no keys away. Without it sealed
texts can't be read: the users enter their keys again.

Only the standard library is at hand, which has no cipher: the text is
XORed with a keystream of HMAC-SHA256 blocks over a random nonce and a
counter, and authenticated with a second HMAC (encrypt-then-MAC).
"""

import base64
import hashlib
import hmac
import logging
import os

import config

log = logging.getLogger("vault")

NONCE = 16
TAG = 16
_keys = None


def _load():
    """The encryption and authentication keys, derived from the secret."""
    global _keys
    if _keys is None:
        try:
            with open(config.SECRET_FILE, "rb") as f:
                secret = bytes.fromhex(f.read().decode("ascii").strip())
        except (OSError, ValueError):
            secret = b""
        if len(secret) < 32:
            secret = os.urandom(32)
            handle = os.open(config.SECRET_FILE,
                             os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(handle, "w") as f:
                f.write(secret.hex() + "\n")
            log.info("Made a new secret in %s", config.SECRET_FILE)
        _keys = (hmac.new(secret, b"encrypt", hashlib.sha256).digest(),
                 hmac.new(secret, b"authenticate", hashlib.sha256).digest())
    return _keys


def _stream(key, nonce, length):
    blocks = (hmac.new(key, nonce + n.to_bytes(4, "big"),
                       hashlib.sha256).digest()
              for n in range(length // 32 + 1))
    return b"".join(blocks)[:length]


def seal(text):
    """The text, encrypted, as a string to store."""
    encrypt, authenticate = _load()
    data = text.encode("utf-8")
    nonce = os.urandom(NONCE)
    sealed = bytes(a ^ b for a, b in zip(data, _stream(encrypt, nonce,
                                                       len(data))))
    tag = hmac.new(authenticate, nonce + sealed, hashlib.sha256).digest()[:TAG]
    return base64.b64encode(nonce + tag + sealed).decode("ascii")


def unseal(stored):
    """The text sealed in `stored`, or None if it isn't one of ours (the
    secret has changed, or the string was tampered with)."""
    if not stored:
        return None
    encrypt, authenticate = _load()
    try:
        raw = base64.b64decode(stored.encode("ascii"), validate=True)
    except (ValueError, UnicodeError):
        return None
    nonce, tag, sealed = raw[:NONCE], raw[NONCE:NONCE + TAG], raw[NONCE + TAG:]
    expected = hmac.new(authenticate, nonce + sealed,
                        hashlib.sha256).digest()[:TAG]
    if len(nonce) < NONCE or not hmac.compare_digest(tag, expected):
        return None
    data = bytes(a ^ b for a, b in zip(sealed, _stream(encrypt, nonce,
                                                       len(sealed))))
    try:
        return data.decode("utf-8")
    except UnicodeError:
        return None
