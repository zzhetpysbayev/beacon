"""Password hashing with scrypt from the standard library."""
import hashlib
import hmac
import os

_N, _R, _P, _DKLEN = 2**14, 8, 1, 64


def _derive(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN)


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    return f"scrypt${salt.hex()}${_derive(password, salt).hex()}"


# Used when the username does not exist, so login timing does not reveal accounts.
DUMMY_HASH = hash_password("dummy-password-for-timing")


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, salt_hex, hash_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        candidate = _derive(password, bytes.fromhex(salt_hex))
        return hmac.compare_digest(candidate, bytes.fromhex(hash_hex))
    except ValueError:
        return False
