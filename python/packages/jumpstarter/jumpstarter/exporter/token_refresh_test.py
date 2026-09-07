import base64
import json

import pytest

from jumpstarter.exporter.token_refresh import (
    calculate_token_refresh_sleep,
    decode_jwt_payload,
    get_token_expiry,
    get_token_remaining_seconds,
)


def _make_jwt(payload: dict) -> str:
    header = {"alg": "ES256", "typ": "JWT"}
    h_b64 = base64.urlsafe_b64encode(json.dumps(header).encode()).rstrip(b"=").decode()
    p_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"{h_b64}.{p_b64}.dummy_signature"


def test_decode_jwt_payload_valid():
    payload = {"sub": "exporter-1", "exp": 1234567890}
    token = _make_jwt(payload)
    decoded = decode_jwt_payload(token)
    assert decoded["sub"] == "exporter-1"
    assert decoded["exp"] == 1234567890


def test_decode_jwt_payload_invalid():
    with pytest.raises(ValueError, match="expected 3 parts"):
        decode_jwt_payload("invalid.token")

    with pytest.raises(ValueError, match="Failed to decode"):
        decode_jwt_payload("part1.!!!notbase64!!!.part3")


def test_get_token_expiry():
    token = _make_jwt({"sub": "test", "exp": 1700000000})
    assert get_token_expiry(token) == 1700000000.0

    token_no_exp = _make_jwt({"sub": "test"})
    assert get_token_expiry(token_no_exp) is None

    assert get_token_expiry("invalid") is None


def test_get_token_remaining_seconds():
    now = 1000.0
    token = _make_jwt({"exp": 1500})
    assert get_token_remaining_seconds(token, now=now) == 500.0

    expired_token = _make_jwt({"exp": 900})
    assert get_token_remaining_seconds(expired_token, now=now) == -100.0


def test_calculate_token_refresh_sleep_365_days():
    # 365 days token: 31,536,000s
    now = 1_000_000.0
    iat = now
    exp = now + 365 * 24 * 3600
    token = _make_jwt({"iat": iat, "exp": exp})

    # Lead time is capped by DEFAULT_TOKEN_REFRESH_LEAD_TIME (7 days = 604,800s)
    # Expected sleep: 365 days - 7 days = 358 days
    sleep_s = calculate_token_refresh_sleep(token, now=now)
    assert sleep_s is not None
    assert pytest.approx(sleep_s, rel=1e-3) == (365 - 7) * 24 * 3600


def test_calculate_token_refresh_sleep_fraction():
    # 10 days token: 20% is 2 days (172,800s), which is < 7 days
    now = 1_000_000.0
    iat = now
    exp = now + 10 * 24 * 3600
    token = _make_jwt({"iat": iat, "exp": exp})

    # Lead time = 2 days
    # Expected sleep: 8 days
    sleep_s = calculate_token_refresh_sleep(token, now=now)
    assert sleep_s is not None
    assert pytest.approx(sleep_s, rel=1e-3) == 8 * 24 * 3600


def test_calculate_token_refresh_sleep_short_token():
    # 100 seconds token: 50% cap applies (lead = 50s)
    now = 1_000_000.0
    iat = now
    exp = now + 100
    token = _make_jwt({"iat": iat, "exp": exp})

    # Expected sleep: 50s
    sleep_s = calculate_token_refresh_sleep(token, now=now)
    assert sleep_s is not None
    assert pytest.approx(sleep_s, rel=1e-3) == 50.0


def test_calculate_token_refresh_sleep_near_expiry():
    # Token expiring in 3 days (less than 7 days lead time on a 365-day token)
    now = 1_000_000.0
    iat = now - 362 * 24 * 3600
    exp = now + 3 * 24 * 3600
    token = _make_jwt({"iat": iat, "exp": exp})

    sleep_s = calculate_token_refresh_sleep(token, now=now)
    assert sleep_s == 0.0


def test_calculate_token_refresh_sleep_expired():
    now = 1_000_000.0
    exp = now - 100
    token = _make_jwt({"exp": exp})

    sleep_s = calculate_token_refresh_sleep(token, now=now)
    assert sleep_s == 0.0


def test_calculate_token_refresh_sleep_no_exp_or_invalid():
    token_no_exp = _make_jwt({"sub": "test"})
    assert calculate_token_refresh_sleep(token_no_exp) is None
    assert calculate_token_refresh_sleep("not-a-token") is None
