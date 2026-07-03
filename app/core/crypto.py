from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings


@lru_cache
def _fernet() -> Fernet:
    return Fernet(settings.encryption_key.encode())


def encrypt_session(session_string: str) -> str:
    return _fernet().encrypt(session_string.encode()).decode()


def decrypt_session(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise ValueError("세션 데이터를 복호화할 수 없습니다 (ENCRYPTION_KEY 불일치 또는 손상된 데이터).") from exc
