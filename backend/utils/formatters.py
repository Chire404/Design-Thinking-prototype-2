"""Formatting, UTC timestamps, card numbers, and unique transaction reference generators."""

from __future__ import annotations

import secrets
import string
from datetime import datetime, timezone


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_iso(ts: str) -> datetime:
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def format_vnd(amount: int | None) -> str:
    """Format integer VND using Vietnamese dot thousands separator (e.g. 100.000 VNĐ)."""
    if amount is None:
        return "0 VNĐ"
    val = int(amount)
    sign = "-" if val < 0 else ""
    grouped = f"{abs(val):,}".replace(",", ".")
    return f"{sign}{grouped} VNĐ"


def generate_card_number() -> str:
    """Generate a distinctive VeloPass transit card number: VP-XXXX-XXXX-XXXX with Luhn check digit."""
    digits = [secrets.randbelow(10) for _ in range(11)]
    total = 0
    for idx, d in enumerate(reversed(digits)):
        if idx % 2 == 0:
            doubled = d * 2
            total += doubled - 9 if doubled > 9 else doubled
        else:
            total += d
    check_digit = (10 - (total % 10)) % 10
    full = "".join(str(d) for d in digits) + str(check_digit)
    return f"VP-{full[0:4]}-{full[4:8]}-{full[8:12]}"


def generate_tx_code(prefix: str) -> str:
    """Generate collision-resistant transaction identifier, e.g. USE-20261126-8F4C2A19B0."""
    date_part = utc_now().strftime("%Y%m%d")
    rand_part = secrets.token_hex(5).upper()
    return f"{prefix.upper()}-{date_part}-{rand_part}"


def generate_transfer_content(prefix: str = "VP") -> str:
    """Generate bank-safe uppercase alphanumeric transfer content (no punctuation)."""
    clean_prefix = "".join(ch for ch in prefix.upper() if ch.isalnum())[:4] or "VP"
    alphabet = string.ascii_uppercase + string.digits
    body = "".join(secrets.choice(alphabet) for _ in range(10))
    return f"{clean_prefix}{body}"
