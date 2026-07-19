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


def encrypt(plaintext: str) -> str:
    """Generic encrypt function — alias for encrypt_session.
    
    Used by AI Platform API provider config storage.
    """
    return encrypt_session(plaintext)


def decrypt(ciphertext: str) -> str:
    """Generic decrypt function — alias for decrypt_session.
    
    Used by AI Platform API provider config retrieval.
    """
    return decrypt_session(ciphertext)
