"""Password, token, and TOTP primitives for the account API."""
import base64
import hashlib
import hmac
import os
import re
import secrets
import struct
import time

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken

PASSWORDS = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=2, hash_len=32, salt_len=16)
JWT_ISSUER = "accounting-intelligence"
JWT_AUDIENCE = "accounting-intelligence-web"
ACCESS_TOKEN_SECONDS = 15 * 60
SESSION_SECONDS = 8 * 60 * 60
TOTP_PERIOD = 30


def strong_password(password: str) -> bool:
    return (8 <= len(password) <= 256 and all(re.search(p, password) for p in
            (r"[A-Z]", r"[a-z]", r"[0-9]", r"[^A-Za-z0-9]")))


def hash_password(password: str) -> str:
    return PASSWORDS.hash(password)


def verify_password(password: str, encoded: str) -> bool:
    if encoded.startswith("$argon2id$"):
        try:
            return PASSWORDS.verify(encoded, password)
        except (InvalidHashError, VerificationError, VerifyMismatchError):
            return False
    try:
        algorithm, salt, expected = encoded.split("$")
        if algorithm != "scrypt":
            return False
        actual = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1).hex()
        return hmac.compare_digest(expected, actual)
    except (ValueError, TypeError):
        return False


def needs_rehash(encoded: str) -> bool:
    if encoded.startswith("$argon2id$"):
        try:
            return PASSWORDS.check_needs_rehash(encoded)
        except (InvalidHashError, VerificationError):
            return True
    return encoded.startswith("scrypt$")


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def otp_hash(secret: str, email: str, code: str) -> str:
    return hmac.new(secret.encode(), f"{email}:{code}".encode(), hashlib.sha256).hexdigest()


def jwt_secret() -> str:
    value = os.getenv("AUTH_SECRET", "")
    if len(value) < 32 or value.startswith("replace-"):
        raise RuntimeError("Configure a random AUTH_SECRET of at least 32 characters.")
    return value


def create_access_token(user_id: str, session_id: str) -> str:
    now = int(time.time())
    return jwt.encode({
        "sub": user_id,
        "jti": session_id,
        "iat": now,
        "nbf": now,
        "exp": now + ACCESS_TOKEN_SECONDS,
        "iss": JWT_ISSUER,
        "aud": JWT_AUDIENCE,
    }, jwt_secret(), algorithm="HS256")


def decode_access_token(token: str) -> dict:
    return jwt.decode(
        token,
        jwt_secret(),
        algorithms=["HS256"],
        issuer=JWT_ISSUER,
        audience=JWT_AUDIENCE,
        options={"require": ["sub", "jti", "iat", "exp", "iss", "aud"]},
    )


def _fernet() -> Fernet:
    key = hashlib.sha256(jwt_secret().encode() + b":totp-encryption").digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_totp_secret(secret: str) -> str:
    return _fernet().encrypt(secret.encode()).decode()


def decrypt_totp_secret(encrypted: str) -> str:
    try:
        return _fernet().decrypt(encrypted.encode()).decode()
    except (InvalidToken, ValueError) as exc:
        raise RuntimeError("Stored TOTP secret cannot be decrypted; verify AUTH_SECRET.") from exc


def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def totp_code(secret: str, timestamp: float | None = None) -> str:
    counter = int((time.time() if timestamp is None else timestamp) // TOTP_PERIOD)
    key = base64.b32decode(secret + "=" * ((8 - len(secret) % 8) % 8), casefold=True)
    digest_value = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest_value[-1] & 0x0F
    value = struct.unpack(">I", digest_value[offset:offset + 4])[0] & 0x7FFFFFFF
    return f"{value % 1_000_000:06d}"


def verify_totp(secret: str, code: str, timestamp: float | None = None) -> bool:
    now = time.time() if timestamp is None else timestamp
    return any(
        hmac.compare_digest(totp_code(secret, now + step * TOTP_PERIOD), code)
        for step in (-1, 0, 1)
    )
