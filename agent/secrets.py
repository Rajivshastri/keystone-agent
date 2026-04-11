"""DPAPI-backed secret store for the agent.

On Windows, secrets (control-plane bearer token, M365 credentials, WS
portal login, custodian zip passwords) are encrypted with the machine
account's DPAPI key so that only processes running on the same machine
as that account can decrypt them.

Non-Windows platforms use a fallback scheme (Fernet with a locally
derived key) — NOT suitable for production, only for dev on Mac/Linux.

Phase 0 goals:
  - API is stable (get/set/delete/list)
  - Windows path is a thin wrapper over win32crypt.CryptProtectData
  - Tests pass on the dev machine
  - No secret ever ends up in plaintext on disk

Phase 1 additions:
  - Per-secret rotation policies
  - Machine-binding check (refuse to decrypt on a different machine)
  - Audit log of secret access (who / when / purpose)
"""
from __future__ import annotations

import base64
import json
import logging
import os
import platform
from pathlib import Path
from typing import Any

from .config import data_dir

logger = logging.getLogger(__name__)


SECRET_STORE_NAME = "secrets.dat"


def _store_path() -> Path:
    return data_dir() / SECRET_STORE_NAME


# ── Windows DPAPI backend ────────────────────────────────────────────── #


def _dpapi_available() -> bool:
    if platform.system() != "Windows":
        return False
    try:
        import win32crypt  # noqa: F401
        return True
    except ImportError:
        return False


def _dpapi_encrypt(blob: bytes) -> bytes:
    import win32crypt

    # LOCAL_MACHINE scope means any process on the same machine with the
    # right DACLs can decrypt. CURRENT_USER scope would tie to the service
    # account and refuse access from a support session — Phase 1 decision.
    result = win32crypt.CryptProtectData(
        blob,
        "Keystone agent secret store",
        None,
        None,
        None,
        0x4,  # CRYPTPROTECT_LOCAL_MACHINE
    )
    return bytes(result)


def _dpapi_decrypt(blob: bytes) -> bytes:
    import win32crypt

    _, plain = win32crypt.CryptUnprotectData(blob, None, None, None, 0x4)
    return bytes(plain)


# ── Fallback (dev-only) backend ──────────────────────────────────────── #


def _fallback_key() -> bytes:
    """Derive a stable key on non-Windows dev machines.

    This is NOT secure against a local attacker. It exists only so the
    agent can boot and run on Mac/Linux during development. Any production
    install will run on Windows with the DPAPI backend.
    """
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    seed = (platform.node() or "keystone") + "-dev-fallback"
    salt = b"keystone-secrets-salt-v1"
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=100_000)
    return base64.urlsafe_b64encode(kdf.derive(seed.encode("utf-8")))


def _fallback_encrypt(blob: bytes) -> bytes:
    from cryptography.fernet import Fernet

    return Fernet(_fallback_key()).encrypt(blob)


def _fallback_decrypt(blob: bytes) -> bytes:
    from cryptography.fernet import Fernet

    return Fernet(_fallback_key()).decrypt(blob)


# ── Public API ───────────────────────────────────────────────────────── #


class SecretStore:
    """A tiny key/value store whose values are encrypted at rest.

    The store itself is a single JSON file mapping key -> base64(ciphertext).
    Every write rewrites the whole file atomically. Fine for ~dozens of
    secrets which is all the agent needs.
    """

    def __init__(self) -> None:
        self._path = _store_path()
        self._dpapi = _dpapi_available()
        if not self._dpapi and platform.system() == "Windows":
            # pywin32 missing on a Windows box is a hard error — the agent
            # must not fall back to the dev key on production Windows.
            raise RuntimeError(
                "pywin32 is not installed but this is a Windows machine. "
                "Reinstall the agent to pick up DPAPI support."
            )

    # ---- file I/O ---- #

    def _load_raw(self) -> dict[str, str]:
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("Secret store corrupt — starting fresh")
            return {}

    def _save_raw(self, data: dict[str, str]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".dat.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(self._path)
        # Tighten permissions on POSIX so curious local users can't read it
        if platform.system() != "Windows":
            try:
                os.chmod(self._path, 0o600)
            except OSError:
                pass

    # ---- encrypt / decrypt ---- #

    def _encrypt(self, value: str) -> str:
        blob = value.encode("utf-8")
        if self._dpapi:
            ct = _dpapi_encrypt(blob)
        else:
            ct = _fallback_encrypt(blob)
        return base64.b64encode(ct).decode("ascii")

    def _decrypt(self, encoded: str) -> str:
        ct = base64.b64decode(encoded.encode("ascii"))
        if self._dpapi:
            blob = _dpapi_decrypt(ct)
        else:
            blob = _fallback_decrypt(ct)
        return blob.decode("utf-8")

    # ---- public methods ---- #

    def set(self, key: str, value: str) -> None:
        data = self._load_raw()
        data[key] = self._encrypt(value)
        self._save_raw(data)

    def get(self, key: str) -> str | None:
        data = self._load_raw()
        encoded = data.get(key)
        if encoded is None:
            return None
        try:
            return self._decrypt(encoded)
        except Exception as e:  # pragma: no cover — surface decrypt failures
            logger.error(f"Secret {key!r} failed to decrypt: {e}")
            return None

    def delete(self, key: str) -> bool:
        data = self._load_raw()
        if key in data:
            del data[key]
            self._save_raw(data)
            return True
        return False

    def keys(self) -> list[str]:
        return sorted(self._load_raw().keys())


# ── Convenience top-level getters for the known secrets ─────────────── #

# Well-known secret keys — centralised so we don't scatter string literals
KEY_AGENT_TOKEN = "agent_token"
KEY_M365_CLIENT_SECRET = "m365_client_secret"
KEY_WS_PORTAL_PASSWORD = "ws_portal_password"
KEY_CUSTODIAN_ZIP_PASSWORD_PREFIX = "custodian_zip_password_"  # + source name


_store: SecretStore | None = None


def get_store() -> SecretStore:
    global _store
    if _store is None:
        _store = SecretStore()
    return _store


def smoke_test() -> dict[str, Any]:
    """Write a throwaway secret, read it back, delete it.

    Called during agent startup so we fail loud if DPAPI isn't working
    rather than only discovering the problem when the first real secret
    needs to be saved.
    """
    store = get_store()
    test_key = "__smoke__"
    test_value = "hello-dpapi-" + base64.b64encode(os.urandom(6)).decode("ascii")
    store.set(test_key, test_value)
    roundtrip = store.get(test_key)
    store.delete(test_key)
    return {
        "backend": "dpapi" if store._dpapi else "fallback",
        "roundtrip_ok": roundtrip == test_value,
    }
