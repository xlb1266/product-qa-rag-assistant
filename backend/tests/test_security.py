"""鉴权单测：pbkdf2 密码哈希、自签 HMAC Token 的签发/校验/篡改/过期。"""

import pytest

from app.config import settings
from app.core.security import (
    create_token,
    decode_token,
    generate_salt,
    hash_password,
    verify_password,
)


def test_password_hash_roundtrip():
    salt = generate_salt()
    digest = hash_password("123456", salt)
    assert verify_password("123456", salt, digest)
    assert not verify_password("wrong", salt, digest)


def test_same_password_different_salt_different_hash():
    a, b = generate_salt(), generate_salt()
    assert hash_password("123456", a) != hash_password("123456", b)


def test_token_roundtrip_carries_uid_and_email():
    payload = decode_token(create_token(7, "u@app.com"))
    assert payload is not None
    assert payload["uid"] == 7
    assert payload["email"] == "u@app.com"


def test_token_tampered_payload_rejected():
    token_a = create_token(1, "a@app.com")
    token_b = create_token(2, "b@app.com")
    # 用 B 的 payload 拼 A 的签名：签名校验必须失败
    forged = f"{token_b.split('.')[0]}.{token_a.split('.')[1]}"
    assert decode_token(forged) is None


@pytest.mark.parametrize("bad", ["", "abc", "a.b", "a.b.c", ".."])
def test_garbage_token_rejected(bad: str):
    assert decode_token(bad) is None


def test_expired_token_rejected(monkeypatch):
    monkeypatch.setattr(settings, "token_expire_days", -1)
    assert decode_token(create_token(1, "a@app.com")) is None
