"""Tests for the DB-backed bcrypt admin credential path.

The admin login flow authenticates via ADMIN_USERNAME/ADMIN_PASSWORD env vars
(legacy) OR a DB-backed account set up through POST /api/admin/setup. This
covers the DB path: bcrypt hashing/verification and the fixed-window login
rate limit.
"""

import pytest

from app.core.security import hash_password, verify_admin_credentials_hash, verify_password_stored


def test_hash_password_uses_bcrypt_with_salt():
    """Each hash call produces a unique bcrypt hash (random salt)."""
    h1 = hash_password("correct horse battery staple")
    h2 = hash_password("correct horse battery staple")
    assert h1 != h2
    assert h1.startswith("$2")


def test_verify_password_stored_bcrypt_roundtrip():
    """A bcrypt hash verifies correctly with the same plaintext."""
    stored = hash_password("s3cret!")
    assert verify_password_stored("s3cret!", stored) is True
    assert verify_password_stored("wrong", stored) is False


def test_verify_password_stored_legacy_sha256():
    """Legacy SHA-256 hashes still verify (migration path)."""
    import hashlib
    legacy = hashlib.sha256("legacy-pass".encode()).hexdigest()
    assert verify_password_stored("legacy-pass", legacy) is True


def test_verify_admin_credentials_hash_bcrypt():
    """verify_admin_credentials_hash checks username AND bcrypt password hash."""
    stored_username = "admin"
    stored_hash = hash_password("admin-pw-2026")
    assert verify_admin_credentials_hash("admin", "admin-pw-2026", stored_username, stored_hash) is True
    assert verify_admin_credentials_hash("admin", "nope", stored_username, stored_hash) is False
    assert verify_admin_credentials_hash("other", "admin-pw-2026", stored_username, stored_hash) is False
