import re
from django.conf import settings

_patterns = [re.compile(p, re.IGNORECASE) for p in getattr(settings, "CONTACT_REDACTION_PATTERNS", [])]
_replacement = getattr(settings, "CONTACT_REDACTION_REPLACEMENT", "•••")

def redact_contacts(text: str) -> str:
    if not text:
        return text
    redacted = text
    for rx in _patterns:
        redacted = rx.sub(_replacement, redacted)
    return redacted

def maybe_redact(text: str, *, should_redact: bool) -> str:
    return redact_contacts(text) if (should_redact and text) else text
