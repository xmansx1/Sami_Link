from django import template
from core.utils.redaction import maybe_redact

register = template.Library()

@register.filter(name="redact_if")
def redact_if(value, flag):
    """
    usage: {{ value|redact_if:REDACT_CONTACTS }}
    flag متغير بولياني جاهز في الـ context
    """
    return maybe_redact(value, should_redact=bool(flag))
