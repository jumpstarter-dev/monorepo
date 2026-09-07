import base64
import json
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

# Default maximum lead time before expiry to attempt token refresh (7 days).
# Matches the warning threshold used across Jumpstarter.
DEFAULT_TOKEN_REFRESH_LEAD_TIME: float = 7 * 24 * 3600.0

# Default fraction of total lifetime at which to trigger token refresh (20%).
DEFAULT_TOKEN_REFRESH_FRACTION: float = 0.2

# Minimum lead time before expiry to attempt token refresh (60 seconds).
MIN_TOKEN_REFRESH_LEAD_TIME: float = 60.0


def decode_jwt_payload(token: str) -> dict[str, Any]:
    """Decode the JSON payload from an unverified JWT token.

    Args:
        token: JWT string (header.payload.signature)

    Returns:
        Decoded payload dictionary

    Raises:
        ValueError: If the token format is invalid or cannot be decoded
    """
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError(f"Invalid JWT format: expected 3 parts, got {len(parts)}")

    payload_b64 = parts[1]
    # Handle base64 padding
    padded = payload_b64 + "=" * (-len(payload_b64) % 4)
    try:
        payload_bytes = base64.urlsafe_b64decode(padded)
        return json.loads(payload_bytes)
    except Exception as e:
        raise ValueError(f"Failed to decode JWT payload: {e}") from e


def get_token_expiry(token: str) -> float | None:
    """Extract expiry timestamp (Unix epoch seconds) from a JWT token.

    Returns:
        float timestamp if 'exp' claim is present and valid, otherwise None.
    """
    try:
        payload = decode_jwt_payload(token)
    except ValueError:
        return None

    exp = payload.get("exp")
    if isinstance(exp, (int, float)):
        return float(exp)
    return None


def get_token_remaining_seconds(token: str, now: float | None = None) -> float | None:
    """Calculate the seconds remaining until token expiration.

    Args:
        token: JWT string
        now: Optional current timestamp (defaults to time.time())

    Returns:
        Remaining seconds (positive if valid, negative if expired), or None if no exp claim.
    """
    exp = get_token_expiry(token)
    if exp is None:
        return None
    if now is None:
        now = time.time()
    return exp - now


def calculate_token_refresh_sleep(
    token: str,
    now: float | None = None,
    lead_time: float = DEFAULT_TOKEN_REFRESH_LEAD_TIME,
    fraction: float = DEFAULT_TOKEN_REFRESH_FRACTION,
    min_lead_time: float = MIN_TOKEN_REFRESH_LEAD_TIME,
) -> float | None:
    """Calculate the number of seconds to sleep before requesting a new token.

    When the token has expired or is within the refresh threshold, returns 0.0
    indicating a refresh should be attempted immediately.

    Args:
        token: JWT string
        now: Optional current timestamp (defaults to time.time())
        lead_time: Maximum lead time before expiry to refresh (default: 7 days)
        fraction: Fraction of total lifetime remaining to trigger refresh (default: 0.2)
        min_lead_time: Minimum lead time before expiry to attempt refresh (default: 60s)

    Returns:
        float >= 0: Seconds to sleep before refreshing (0.0 means refresh now)
        None: Token is invalid or does not have an 'exp' claim (no refresh needed)
    """
    if now is None:
        now = time.time()

    try:
        payload = decode_jwt_payload(token)
    except ValueError:
        return None

    exp = payload.get("exp")
    if exp is None or not isinstance(exp, (int, float)):
        return None
    exp_f = float(exp)

    remaining = exp_f - now
    if remaining <= 0:
        return 0.0

    iat = payload.get("iat")
    if iat is not None and isinstance(iat, (int, float)) and float(iat) < exp_f:
        total_lifetime = exp_f - float(iat)
        lead = min(lead_time, max(total_lifetime * fraction, min_lead_time))
        # Ensure lead time never exceeds half the token's total lifetime
        lead = min(lead, total_lifetime * 0.5)
    else:
        lead = min(lead_time, max(remaining * fraction, min_lead_time))
        lead = min(lead, remaining * 0.5)

    sleep_seconds = remaining - lead
    return max(0.0, sleep_seconds)
