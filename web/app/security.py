"""Password hashing, session tokens, and constant-time comparison.

Nothing in here talks to the database; it is all pure functions over strings, so
it is unit-testable without a server.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.config import settings

_hasher = PasswordHasher()

# Verifying a real hash costs ~50 ms. If an unknown email skipped that work, the
# response time alone would tell an attacker which addresses exist, so the login
# route verifies against this instead. It is the hash of a value nobody can send.
_DUMMY_HASH = _hasher.hash("storemanager-dummy-password-for-constant-time-login")

MIN_PASSWORD_LENGTH = 10


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(stored_hash: str | None, password: str) -> bool:
    """True when ``password`` matches. Always does the full work, even when there
    is no hash to check against."""
    try:
        _hasher.verify(stored_hash or _DUMMY_HASH, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
    return stored_hash is not None


def needs_rehash(stored_hash: str) -> bool:
    """True when the hash was made with weaker parameters than we now use."""
    try:
        return _hasher.check_needs_rehash(stored_hash)
    except InvalidHashError:
        return False


def password_problem(password: str) -> str | None:
    """An Armenian complaint about the password, or None when it is acceptable."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return f"Գաղտնաբառը պետք է լինի առնվազն {MIN_PASSWORD_LENGTH} նիշ։"
    if password.strip() != password:
        return "Գաղտնաբառը չպետք է սկսվի կամ ավարտվի բացատով։"
    return None


def generate_token() -> str:
    """A session or CSRF token. 32 bytes of entropy, URL-safe."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """Keyed hash of a session token.

    Only the hash is stored, so a database dump does not hand over live sessions.
    HMAC rather than a bare SHA-256 so the secret is required to build a matching
    lookup key.
    """
    return hmac.new(
        settings.session_secret.encode("utf-8"),
        token.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def normalise_email(raw: str) -> str:
    """Emails are stored lowercase and trimmed; the CHECK constraint enforces it."""
    return raw.strip().lower()
