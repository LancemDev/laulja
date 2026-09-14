from __future__ import annotations

import subprocess
from typing import List, Optional

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

"""
Decrypts Chrome-family (Chrome/Chromium/Brave/Edge) cookie values on Linux.
The AES key is derived from a password stored in the OS keyring via libsecret
(falling back to Chromium's well-known "peanuts" password when no keyring
entry exists, e.g. no desktop keyring is running). Mirrors ChromeCookieCrypto.cs.
"""

_SALT = b"saltysalt"
_IV = b" " * 16
_FALLBACK_PASSWORD = b"peanuts"


def _try_libsecret_lookup(application_name: str) -> Optional[str]:
    try:
        proc = subprocess.run(
            ["secret-tool", "lookup", "application", application_name],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if proc.returncode == 0 and proc.stdout:
            return proc.stdout.rstrip("\n")
    except Exception:
        pass
    return None


def derive_key(secret_tool_application_names: List[str]) -> bytes:
    password = _FALLBACK_PASSWORD
    for app in secret_tool_application_names:
        pw = _try_libsecret_lookup(app)
        if pw:
            password = pw.encode("utf-8")
            break

    kdf = PBKDF2HMAC(algorithm=hashes.SHA1(), length=16, salt=_SALT, iterations=1)
    return kdf.derive(password)


def _remove_pkcs7_padding(data: bytes) -> bytes:
    if not data:
        return data
    pad = data[-1]
    if pad <= 0 or pad > 16 or pad > len(data):
        return data
    return data[:-pad]


def decrypt(encrypted_value: bytes, key: bytes) -> Optional[str]:
    """A wrong AES key (e.g. a stale profile whose keyring secret has since rotated) still
    "decrypts" without raising — it just produces garbage bytes; callers should sanity-check
    the result (see browser_cookies._looks_decrypted)."""
    if len(encrypted_value) == 0:
        return ""
    if len(encrypted_value) <= 3:
        return None

    prefix = encrypted_value[:3]
    if prefix not in (b"v10", b"v11"):
        try:
            return encrypted_value.decode("utf-8")
        except UnicodeDecodeError:
            return None

    try:
        cipher_text = encrypted_value[3:]
        decryptor = Cipher(algorithms.AES(key), modes.CBC(_IV)).decryptor()
        padded = decryptor.update(cipher_text) + decryptor.finalize()
        unpadded = _remove_pkcs7_padding(padded)
        return unpadded.decode("utf-8")
    except Exception:
        return None
