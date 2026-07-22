"""Encryption for the credentials the platform now stores.

Connections used to hold nothing but the *names* of environment variables, and
that was a stronger guarantee than any promise: there was no field to type a
password into, so there was no password to leak. Managing several ServiceNow
instances from the UI needs the opposite, so this is what makes the trade
survivable rather than reckless.

**What this gives you.** Secrets are unreadable in the database file, in a
backup of it, and in anything that dumps a table. A stolen `signalops.db` is
useless on its own. Values are never returned by any endpoint — the API can
report that a secret is *set*, never what it is.

**What this does not give you.** The key lives on the same machine as the
database, because there is nowhere else for it to live in a single-process app.
Anyone who can read both files can read the secrets. That is meaningfully
better than plaintext and meaningfully worse than a secret manager, and it is
worth being exact about which one you have: this is tamper-resistance and
blast-radius reduction, not custody.

Set `SIGNALOPS_SECRET_KEY` to control the key. Without it, one is generated and
written next to the database with owner-only permissions, and rotating it
invalidates every stored secret — deliberately, since a key that can be lost
silently is a key whose absence you discover during an incident.

**Where this is going.** `SIGNALOPS_SECRET_KEY` is the seam a real secret
manager plugs into: supply the key from Vault, a cloud KMS or an orchestrator's
secret injection and nothing is written to disk here at all. Moving custody
there is the intended destination — this module deliberately has one place the
key comes from, so that change is a resolver swap rather than a rewrite.
"""
from __future__ import annotations

import base64
import logging
import os
import stat
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger("crypto")

KEY_ENV_VAR = "SIGNALOPS_SECRET_KEY"
KEY_PATH = Path(__file__).resolve().parent / "data" / "secret.key"

_fernet: Fernet | None = None


class SecretUnreadable(Exception):
    """A stored secret could not be decrypted, almost always because the key
    changed. Distinguished from "no secret" so the UI can say which."""


def _load_key() -> bytes:
    from_env = os.getenv(KEY_ENV_VAR)
    if from_env:
        # Accept either a Fernet key or any passphrase, so nobody has to know
        # what a urlsafe-base64 32-byte key is to get started.
        raw = from_env.encode("utf-8")
        if len(raw) == 44 and raw.endswith(b"="):
            return raw
        import hashlib
        return base64.urlsafe_b64encode(hashlib.sha256(raw).digest())

    if KEY_PATH.exists():
        return KEY_PATH.read_bytes().strip()

    KEY_PATH.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    KEY_PATH.write_bytes(key)
    try:
        KEY_PATH.chmod(stat.S_IRUSR | stat.S_IWUSR)      # owner only
    except OSError:                                      # pragma: no cover
        pass                                             # best effort on Windows
    logger.warning(
        "generated a new encryption key at %s. Back it up: losing it makes every "
        "stored connection secret unreadable. Set %s to manage the key yourself.",
        KEY_PATH, KEY_ENV_VAR)
    return key


def cipher() -> Fernet:
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_load_key())
    return _fernet


def encrypt(value: str | None) -> str | None:
    if value is None or value == "":
        return None
    return cipher().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt(token: str | None) -> str | None:
    if not token:
        return None
    try:
        return cipher().decrypt(token.encode("ascii")).decode("utf-8")
    except InvalidToken as error:
        raise SecretUnreadable(
            "a stored secret could not be decrypted — the encryption key has changed "
            "since it was saved. Re-enter the credential to store it under the new key."
        ) from error


def encrypt_map(values: dict[str, str | None]) -> dict[str, str]:
    """Encrypt a bundle of secrets, dropping the empty ones."""
    return {name: encrypt(value) for name, value in values.items() if value}


def present(secrets: dict | None) -> dict[str, bool]:
    """Which secrets are set. The only shape of this that ever leaves the
    process — presence, never values."""
    return {name: bool(value) for name, value in (secrets or {}).items()}
