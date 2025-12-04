# disputes/forms.py
from __future__ import annotations

import re
from typing import Iterable

from django import forms
from django.core.exceptions import ValidationError
from django.utils.html import strip_tags

from .models import Dispute, DisputeMessage

# محظورات: منع تسريب وسائل الاتصال خارج المنصّة
PHONE_RE = re.compile(r"(?:\+?\d[\d\-\s]{7,}\d)")
EMAIL_RE = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.IGNORECASE)
LINK_RE = re.compile(r"(https?://|www\.)", re.IGNORECASE)

# أنواع الملفات المسموحة للدلائل (يمكن مواءمتها مع نظام المرفقات لديك)
ALLOWED_CONTENT_TYPES: set[str] = {
    "image/jpeg", "image/png", "image/webp", "application/pdf",
}
MAX_FILE_SIZE_MB = 10


def _sanitize_text(text: str) -> str:
    """تنظيف أساسي للنص لتقليل المخاطر (XSS/HTML) وتقليص المسافات."""
    text = strip_tags(text or "")
    text = re.sub(r"[ \t]+", " ", text).strip()
    return text


def _forbid_external_contacts(text: str, field_label: str) -> None:
    """رفض وجود هاتف/إيميل/روابط أثناء النزاع، التزامًا بسياسة المنصّة."""
    if PHONE_RE.search(text):
        raise ValidationError(f"يُحظر وضع أرقام اتصال داخل حقل «{field_label}».")
    if EMAIL_RE.search(text):
        raise ValidationError(f"يُحظر وضع بريد إلكتروني داخل حقل «{field_label}».")
    if LINK_RE.search(text):
        raise ValidationError(f"يُحظر وضع روابط خارجية داخل حقل «{field_label}». استخدم مرفقات الدلائل.")


class DisputeForm(forms.ModelForm):
    """
    نموذج فتح/تحديث نزاع.
    - يفرض نظافة الإدخال ويمنع وسائل الاتصال والروابط داخل النص.
    - يقدّم حقل مرفقات اختياري (غير مرتبط بالموديل) لحمل الدلائل (متعددة الملفات).
    """

    # حقل دلائل اختياري (غير مرتبط بالموديل) — للملفات المتعددة
    evidence = forms.FileField(
        required=False,
        widget=forms.ClearableFileInput(attrs={
            "class": "file-input",
            "accept": ".jpg,.jpeg,.png,.webp,.pdf",
        }),
        label="دلائل (اختياري)",
        help_text="الأنواع المسموحة: JPG/PNG/WebP/PDF — الحد الأقصى 10MB لكل ملف. يمكنك رفع ملف واحد في المرة.",
    )

    class Meta:
        model = Dispute
        # لاحظ: نفترض أن الموديل يملك هذه الحقول نصًا
        fields = ["title", "reason", "details"]
        labels = {
            "title": "عنوان النزاع",
            "reason": "سبب النزاع",
            "details": "تفاصيل إضافية",
        }
        widgets = {
            "title": forms.TextInput(attrs={
                "class": "input",
                "placeholder": "مثال: خلاف حول نطاق العمل",
                "maxlength": "150",
                "autofocus": "autofocus",
            }),
            "reason": forms.TextInput(attrs={
                "class": "input",
                "placeholder": "السبب المختصر (مثال: تسليم غير مطابق)",
                "maxlength": "150",
            }),
            "details": forms.Textarea(attrs={
                "class": "textarea",
                "rows": 5,
                "placeholder": "اشرح المشكلة بإيجاز مع الوقائع والتواريخ وما الذي تتوقعه كحل.",
                "maxlength": "5000",
            }),
        }
        help_texts = {
            "details": "تجنّب وضع أي معلومات اتصال، ويمكنك إرفاق مستندات كدلائل.",
        }

    # -------- تنقيح الحقول منفردة --------
    def clean_title(self) -> str:
        title = _sanitize_text(self.cleaned_data.get("title", ""))
        if len(title) < 4:
            raise ValidationError("العنوان قصير جدًا (4 أحرف على الأقل).")
        _forbid_external_contacts(title, "العنوان")
        return title

    def clean_reason(self) -> str:
        reason = _sanitize_text(self.cleaned_data.get("reason", ""))
        if len(reason) < 3:
            raise ValidationError("السبب قصير جدًا.")
        _forbid_external_contacts(reason, "السبب")
        return reason

    def clean_details(self) -> str:
        details = _sanitize_text(self.cleaned_data.get("details", ""))
        if len(details) < 20:
            raise ValidationError("التفاصيل قصيرة جدًا. رجاءً قدّم وصفًا أوضح (20 حرفًا على الأقل).")
        _forbid_external_contacts(details, "التفاصيل")
        return details


class DisputeMessageForm(forms.ModelForm):
    """نموذج إضافة رسالة/رد في النزاع."""
    class Meta:
        model = DisputeMessage
        fields = ["content", "attachment"]
        widgets = {
            "content": forms.Textarea(attrs={
                "rows": 3,
                "class": "form-control",
                "placeholder": "اكتب ردك هنا..."
            }),
            "attachment": forms.FileInput(attrs={"class": "form-control"}),
        }

    def clean_content(self):
        data = self.cleaned_data.get("content", "")
        data = _sanitize_text(data)
        _forbid_external_contacts(data, "نص الرسالة")
        return data

    def clean_attachment(self):
        f = self.cleaned_data.get("attachment")
        if not f:
            return f
        
        ctype = getattr(f, "content_type", "") or ""
        size = getattr(f, "size", 0) or 0
        
        if ctype not in ALLOWED_CONTENT_TYPES:
            raise ValidationError(f"نوع ملف غير مسموح: {ctype}")
        if size > MAX_FILE_SIZE_MB * 1024 * 1024:
            raise ValidationError(f"حجم الملف يتجاوز {MAX_FILE_SIZE_MB}MB.")
        return f

