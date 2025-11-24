# marketplace/signals.py
from __future__ import annotations

import logging

from django.core.exceptions import FieldDoesNotExist
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from django.utils import timezone

from .models import Offer, Request

logger = logging.getLogger(__name__)


def _model_has_field(model_cls, field_name: str) -> bool:
    try:
        model_cls._meta.get_field(field_name)
        return True
    except FieldDoesNotExist:
        return False


def _get_req_status(req) -> str:
    """
    يدعم status أو state حسب الموجود في Request.
    """
    val = getattr(req, "status", None)
    if val is None and hasattr(req, "state"):
        val = getattr(req, "state", "")
    return (str(val or "")).strip().lower()


def _set_req_status(req, new_val: str) -> None:
    """
    يكتب في status أو state حسب الموجود (بدون كسر).
    """
    if _model_has_field(Request, "status"):
        req.status = new_val
    elif _model_has_field(Request, "state"):
        req.state = new_val


def _status_value(model_cls, name: str, fallback: str) -> str:
    Status = getattr(model_cls, "Status", None)
    return getattr(Status, name, fallback) if Status else fallback


# نخزن حالة العرض السابقة حتى نعرف هل صار انتقال فعلي
@receiver(pre_save, sender=Offer)
def _offer_pre_save_snapshot(sender, instance: Offer, **kwargs):
    instance.__old_status = None
    if instance.pk:
        old = sender.objects.only("status").filter(pk=instance.pk).first()
        if old:
            instance.__old_status = old.status


@receiver(post_save, sender=Offer)
def handle_offer_selection(sender, instance: Offer, created: bool, **kwargs):
    """
    عند انتقال العرض إلى SELECTED (transition فقط):
    - نحدّث الطلب (الموظف المعيّن + الحالة إلى OFFER_SELECTED)
    - لا نُخفض حالة طلب متقدمة (awaiting_payment/in_progress/completed...)
    """
    if created:
        return

    off = instance
    SELECTED = _status_value(Offer, "SELECTED", "selected")
    old_status = getattr(off, "__old_status", None)
    new_status = getattr(off, "status", None)

    # اشتغل فقط عند transition إلى SELECTED
    if new_status != SELECTED or new_status == old_status:
        return

    req = off.request

    # حالات متقدمة يمنع تعديلها أو إرجاعها للخلف
    advanced_states = {
        _status_value(Request, "AGREEMENT_PENDING", "agreement_pending"),
        _status_value(Request, "AWAITING_PAYMENT", "awaiting_payment"),
        _status_value(Request, "IN_PROGRESS", "in_progress"),
        _status_value(Request, "AWAITING_REVIEW", "awaiting_review"),
        _status_value(Request, "COMPLETED", "completed"),
        _status_value(Request, "CANCELLED", "cancelled"),
        _status_value(Request, "DISPUTED", "disputed"),
    }

    current_req_status = _get_req_status(req)
    if current_req_status in advanced_states:
        # لا نلمس الطلب إذا كان متقدم
        return

    # حدّث الطلب آمنًا
    if _model_has_field(Request, "assigned_employee"):
        req.assigned_employee = off.employee
    elif _model_has_field(Request, "assigned_to"):
        req.assigned_to = off.employee

    OFFER_SELECTED = _status_value(Request, "OFFER_SELECTED", "offer_selected")
    _set_req_status(req, OFFER_SELECTED)

    update_fields = []

    if _model_has_field(Request, "assigned_employee"):
        update_fields.append("assigned_employee")
    if _model_has_field(Request, "assigned_to"):
        update_fields.append("assigned_to")

    if _model_has_field(Request, "status"):
        update_fields.append("status")
    if _model_has_field(Request, "state"):
        update_fields.append("state")

    if _model_has_field(Request, "offer_selected_at"):
        req.offer_selected_at = timezone.now()
        update_fields.append("offer_selected_at")

    if _model_has_field(Request, "updated_at"):
        req.updated_at = timezone.now()
        update_fields.append("updated_at")

    try:
        req.save(update_fields=update_fields)
    except Exception:
        logger.exception(
            "Failed to sync request after offer selected. "
            "offer=%s request=%s",
            getattr(off, "pk", None),
            getattr(req, "pk", None),
        )
        return

    # إشعار للعميل بوصول عرض مختار
    try:
        from notifications.utils import create_notification

        client = getattr(req, "client", None)
        if client:
            create_notification(
                recipient=client,
                title=f"تم اختيار عرض جديد لطلبك #{req.pk}",
                body=(
                    f"تم اختيار عرض الموظف {off.employee} لطلبك '{req.title}'. "
                    f"يمكنك مراجعة التفاصيل والموافقة على الاتفاقية."
                ),
                url=req.get_absolute_url(),
                actor=off.employee,
                target=off,
            )
    except Exception:
        pass

    # إشعار للموظف عند اختيار عرضه
    try:
        from notifications.utils import create_notification

        employee = getattr(off, "employee", None)
        if employee:
            create_notification(
                recipient=employee,
                title=f"تم اختيار عرضك للطلب #{req.pk}",
                body=(
                    f"قام العميل {getattr(req, 'client', '')} باختيار عرضك للطلب '{req.title}'. "
                    f"يمكنك متابعة الاتفاقية والمشروع."
                ),
                url=req.get_absolute_url(),
                actor=getattr(req, "client", None),
                target=off,
            )
    except Exception:
        pass
