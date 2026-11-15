"""Values accepted for an upload's ``pii_policy``."""

from __future__ import annotations

from .exceptions import ValidationError

PII_POLICIES = ("redact", "keep")


def check_pii_policy(value: str) -> str:
    """Return ``value`` if the API accepts it, else raise ``ValidationError``.

    ``"redact"`` replaces emails, phone numbers... with placeholders;
    ``"keep"`` stores the text as sent.
    """
    if value not in PII_POLICIES:
        raise ValidationError(
            f'pii_policy must be "redact" or "keep", got {value!r}', field="pii_policy"
        )
    return value
