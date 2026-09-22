import os
from functools import lru_cache

from cryptography.fernet import Fernet


@lru_cache
def _get_fernet() -> Fernet:
    key = os.environ["STORAGE_SECRETS_KEY"]
    return Fernet(key.encode() if isinstance(key, str) else key)


def encrypt(plain: str) -> str:
    return _get_fernet().encrypt(plain.encode()).decode()


def decrypt(token: str) -> str:
    return _get_fernet().decrypt(token.encode()).decode()
