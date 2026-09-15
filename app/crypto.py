"""
Encrypts Binance API keys/secrets before they touch the database. Never
store exchange credentials in plaintext.

The encryption key comes from the ENCRYPTION_KEY env var. If it's not set,
one is generated and persisted to backend/.encryption_key on first run —
fine for local development, but for any real deployment you MUST set
ENCRYPTION_KEY yourself (e.g. `python -c "from cryptography.fernet import
Fernet; print(Fernet.generate_key().decode())"`) and keep it stable and
secret. If this key is ever lost, every stored API secret becomes
permanently undecryptable — regenerate it and have users reconnect.
"""
import os

from cryptography.fernet import Fernet

from .config import BASE_DIR, log

_KEY_FILE = BASE_DIR / ".encryption_key"


def _load_or_create_key() -> bytes:
    env_key = os.getenv("ENCRYPTION_KEY")
    if env_key:
        return env_key.encode()
    if _KEY_FILE.exists():
        return _KEY_FILE.read_bytes().strip()
    key = Fernet.generate_key()
    try:
        _KEY_FILE.write_bytes(key)
        log.warning(
            "ENCRYPTION_KEY not set — generated one at %s. Set ENCRYPTION_KEY "
            "explicitly for any real deployment, or stored broker credentials "
            "won't survive moving/redeploying this file.", _KEY_FILE,
        )
    except Exception as e:
        log.error("Could not persist generated encryption key: %s", e)
    return key


_fernet = Fernet(_load_or_create_key())


def encrypt(value: str) -> str:
    return _fernet.encrypt(value.encode()).decode()


def decrypt(token: str) -> str | None:
    try:
        return _fernet.decrypt(token.encode()).decode()
    except Exception:
        log.error("Failed to decrypt stored credential — key may have changed")
        return None
