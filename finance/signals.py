# finance/signals.py
from __future__ import annotations

import logging
from decimal import Decimal

from django.apps import apps
from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models.signals import post_migrate, post_save, pre_save
from django.dispatch import receiver
from django.utils import timezone

from .models import FinanceSettings, Invoice
from .utils import invalidate_finance_cfg_cache

logger = logging.getLogger(__name__)
FIN_AUTOCOMPLETE = getattr(settings, "FINANCE_AUTOCOMPLETE_ON_PAID", True)


@receiver(post_save, sender=FinanceSettings)
def finance_settings_saved(sender, instance: FinanceSettings, **kwargs):
    try:
        invalidate_finance_cfg_cache()
    except Exception:
        logger.exception("failed to invalidate finance cache after FinanceSettings save")


@receiver(post_migrate)
def ensure_finance_settings_exists(sender, **kwargs):
    try:
        app_label = getattr(sender, "name", "") or ""
        if app_label.split(".")[-1] != "finance":
            return
        FinanceSettings.get_solo()
    except Exception:
        logger.exception("failed to ensure FinanceSettings singleton on post_migrate")


def _status_value(model_cls, name: str, fallback: str) -> str:
    Status = getattr(model_cls, "Status", None)
    return getattr(Status, name, fallback) if Status else fallback


def _is_writable(obj, field: str) -> bool:
    if not hasattr(obj, field):
        return False
    attr = getattr(type(obj), field, None)
    return not isinstance(attr, property)


def _get_req_status(req) -> str:
    val = getattr(req, "status", None)
    if val is None and hasattr(req, "state"):
        val = getattr(req, "state", "")
    return (str(val or "")).strip().lower()


def _set_req_status(req, new_val: str) -> None:
    if hasattr(req, "status") and _is_writable(req, "status"):
        req.status = new_val
    elif hasattr(req, "state") and _is_writable(req, "state"):
        req.state = new_val


def _all_positive_invoices_paid(agreement) -> bool:
    PAID_VAL = _status_value(Invoice, "PAID", "paid")
    invs = list(
        Invoice.objects.select_for_update()
        .filter(agreement_id=agreement.id)
        .only("id", "total_amount", "status")
    )
    for inv in invs:
        total = getattr(inv, "total_amount", None) or Decimal("0.00")
        if total <= 0:
            continue
        if str(inv.status).lower() != str(PAID_VAL).lower():
            return False
    return True


def _all_milestones_client_approved(agreement) -> bool:
    try:
        Milestone = apps.get_model("agreements", "Milestone")
    except Exception:
        return False

    qs = Milestone.objects.filter(agreement_id=agreement.id)
    if not qs.exists():
        return True

    if hasattr(Milestone, "is_approved"):
        return not qs.filter(is_approved=False).exists()

    if hasattr(Milestone, "approved_at"):
        return not qs.filter(approved_at__isnull=True).exists()

    if hasattr(Milestone, "status"):
        approved_val = _status_value(Milestone, "APPROVED", "approved")
        return not qs.exclude(status=approved_val).exists()

    return False


def _try_set_request_in_progress(req) -> None:
    inprog_val = _status_value(type(req), "IN_PROGRESS", "in_progress")

    # لو عندك دالة رسمية
    if hasattr(req, "mark_paid_and_start"):
        try:
            req.mark_paid_and_start()
            return
        except ValidationError:
            return
        except Exception:
            logger.exception("mark_paid_and_start failed for request %s", getattr(req, "pk", None))

    current = _get_req_status(req)
    final_states = {
        _status_value(type(req), "COMPLETED", "completed"),
        _status_value(type(req), "CANCELLED", "cancelled"),
        _status_value(type(req), "DISPUTED", "disputed"),
    }
    if current in final_states:
        return

    _set_req_status(req, inprog_val)
    fields = []
    if hasattr(req, "status"):
        fields.append("status")
    if hasattr(req, "state"):
        fields.append("state")
    if hasattr(req, "updated_at") and _is_writable(req, "updated_at"):
        req.updated_at = timezone.now()
        fields.append("updated_at")
    req.save(update_fields=fields)


def _try_set_request_completed(req) -> None:
    COMPLETED = _status_value(type(req), "COMPLETED", "completed")
    DISPUTED = _status_value(type(req), "DISPUTED", "disputed")
    CANCELLED = _status_value(type(req), "CANCELLED", "cancelled")

    cur = _get_req_status(req)
    if cur in {COMPLETED, DISPUTED, CANCELLED}:
        return

    if hasattr(req, "mark_completed"):
        try:
            req.mark_completed()
            return
        except ValidationError:
            return
        except Exception:
            logger.exception("mark_completed failed for request %s", getattr(req, "pk", None))

    _set_req_status(req, COMPLETED)
    fields = []
    if hasattr(req, "status"):
        fields.append("status")
    if hasattr(req, "state"):
        fields.append("state")
    if hasattr(req, "completed_at") and _is_writable(req, "completed_at"):
        req.completed_at = timezone.now()
        fields.append("completed_at")
    if hasattr(req, "updated_at") and _is_writable(req, "updated_at"):
        req.updated_at = timezone.now()
        fields.append("updated_at")
    req.save(update_fields=fields)


@receiver(pre_save, sender=Invoice)
def _invoice_pre_save_track_status(sender, instance: Invoice, **kwargs):
    try:
        instance.__old_status = None
        if instance.pk:
            old = sender.objects.only("status").filter(pk=instance.pk).first()
            if old:
                instance.__old_status = old.status
    except Exception:
        logger.exception("failed to snapshot previous invoice status (id=%s)", getattr(instance, "pk", None))


@receiver(post_save, sender=Invoice)
def _invoice_post_save_sync_request(sender, instance: Invoice, created: bool, **kwargs):
    try:
        PAID_VAL = _status_value(Invoice, "PAID", "paid")
        old_status = getattr(instance, "__old_status", None)
        new_status = getattr(instance, "status", None)

        # فقط عند transition إلى PAID
        if str(new_status).lower() != str(PAID_VAL).lower() or str(new_status).lower() == str(old_status or "").lower():
            return

        agreement = getattr(instance, "agreement", None)
        if not agreement:
            return

        req = getattr(agreement, "request", None)
        if not req:
            return

        with transaction.atomic():
            # 1) أي فاتورة تُدفع -> الطلب قيد التنفيذ
            _try_set_request_in_progress(req)

            if not FIN_AUTOCOMPLETE:
                return

            # 2) شرط الفواتير
            if not _all_positive_invoices_paid(agreement):
                return

            # 3) شرط اعتماد المراحل
            if not _all_milestones_client_approved(agreement):
                return

            # 4) الآن فقط نكمل
            _try_set_request_completed(req)

    except Exception:
        logger.exception("failed to sync request after invoice paid (invoice_id=%s)", getattr(instance, "pk", None))
