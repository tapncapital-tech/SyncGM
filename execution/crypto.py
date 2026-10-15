"""AES-GCM-256 Token Encryption & Decryption Utilities (Section 9)."""
from __future__ import annotations

import base64
import hashlib
import os
from typing import Optional
from Crypto.Cipher import AES


class TokenEncryptor:
    """Provides AES-GCM-256 authenticated encryption for OAuth refresh tokens."""

    DEFAULT_ENV_KEY_NAME = "SYNCGM_ENCRYPTION_KEY"
    NONCE_BYTES = 12

    def __init__(self, secret_key: Optional[str | bytes] = None) -> None:
        key_source = secret_key or os.getenv(self.DEFAULT_ENV_KEY_NAME, "syncgm-default-dev-master-key-32b")
        if isinstance(key_source, str):
            # Derive fixed 32-byte key via SHA-256
            self.key = hashlib.sha256(key_source.encode("utf-8")).digest()
        else:
            self.key = key_source[:32].ljust(32, b"\0")

    def encrypt(self, plaintext: str) -> str:
        """Encrypts a string into a base64-encoded payload (nonce + tag + ciphertext)."""
        if not plaintext:
            return ""

        nonce = os.urandom(self.NONCE_BYTES)
        cipher = AES.new(self.key, AES.MODE_GCM, nonce=nonce)
        ciphertext, tag = cipher.encrypt_and_digest(plaintext.encode("utf-8"))

        # Pack: nonce (12B) + tag (16B) + ciphertext
        payload = nonce + tag + ciphertext
        return base64.b64encode(payload).decode("ascii")

    def decrypt(self, encoded_payload: str) -> str:
        """Decrypts a base64-encoded payload into the original plaintext string."""
        if not encoded_payload:
            return ""

        raw_data = base64.b64decode(encoded_payload.encode("ascii"))
        if len(raw_data) < (self.NONCE_BYTES + 16):
            raise ValueError("Invalid encrypted payload: data too short for nonce and tag.")

        nonce = raw_data[: self.NONCE_BYTES]
        tag = raw_data[self.NONCE_BYTES : self.NONCE_BYTES + 16]
        ciphertext = raw_data[self.NONCE_BYTES + 16 :]

        cipher = AES.new(self.key, AES.MODE_GCM, nonce=nonce)
        plaintext = cipher.decrypt_and_verify(ciphertext, tag)
        return plaintext.decode("utf-8")


# Default Global Instance
DEFAULT_ENCRYPTOR = TokenEncryptor()


def encrypt_token(token: str) -> str:
    """Helper to encrypt an OAuth token using the default encryptor."""
    return DEFAULT_ENCRYPTOR.encrypt(token)


def decrypt_token(encrypted_token: str) -> str:
    """Helper to decrypt an OAuth token using the default encryptor."""
    return DEFAULT_ENCRYPTOR.decrypt(encrypted_token)


def hash_password(password: str) -> str:
    """Hashes a plain password with a secure 16-byte random salt using PBKDF2-HMAC-SHA256."""
    import hmac
    import secrets
    salt = secrets.token_hex(16)
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100000)
    return f"pbkdf2_sha256${salt}${key.hex()}"


def verify_password(plain_password: str, hashed_password: Optional[str]) -> bool:
    """Verifies a plain password against a stored PBKDF2-HMAC-SHA256 password hash."""
    if not hashed_password or "$" not in hashed_password:
        return False
    try:
        import hmac
        scheme, salt, key_hex = hashed_password.split("$", 2)
        if scheme != "pbkdf2_sha256":
            return False
        expected = hashlib.pbkdf2_hmac("sha256", plain_password.encode("utf-8"), salt.encode("utf-8"), 100000)
        return hmac.compare_digest(expected.hex(), key_hex)
    except Exception:
        return False
