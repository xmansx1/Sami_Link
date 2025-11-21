# disputes/views.py
from __future__ import annotations

import logging
from typing import Optional, Tuple

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from marketplace.models import Request
from .forms import DisputeForm
from .models import Dispute

logger = logging.getLogger(__name__)

# ======================================================
# إشعار آمن (يسقط بأمان عند غياب إحدى الأدوات)
# ======================================================
try:
    from core.utils import notify_user as _notify_user  # type: ignore
except Exception:
    _notify_user = None  # type: ignore

try:
    from notifications.utils import create_notification as _create_notification  # type: ignore
except Exception:
    _create_notification = None  # type: ignore


def _notify_safe(user, title: str, body: str, url: Optional[str] = None) -> None:
    """محاولة إرسال إشعار داخلي + إشعار النظام؛ لا تكسر التدفق عند الخطأ."""
    if not user:
        return
    try:
        if _notify_user:
            _notify_user(user, title=title, body=body, link=url)
            return
        if _create_notification:
            _create_notification(recipient=user, title=title, body=body, url=url or "")
    except Exception:
        pass


# ======================================================
# Helpers (الصلاحيات / التجميد)
# ======================================================
def _is_admin(user) -> bool:
    """صلاحيات إدارية موسعة (admin/staff/finance)."""
    role = getattr(user, "role", "") or ""
    return bool(getattr(user, "is_superuser", False) or getattr(user, "is_staff", False) or role in {"admin", "finance"})


def _can_open_dispute(user, req: Request) -> Tuple[bool, str]:
    """
    من يحق له فتح نزاع:
    - العميل صاحب الطلب
    - الموظف المعيَّن على الطلب
    - الإدارة/المالية
    يعاد (مسموح؟, 'client'/'employee'/'admin' أو '')
    """
    if not user or not user.is_authenticated:
        return False, ""
    if getattr(req, "client_id", None) == getattr(user, "id", None):
        return True, "client"
    if getattr(req, "assigned_employee_id", None) == getattr(user, "id", None):
        return True, "employee"
    if _is_admin(user):
        return True, "admin"
    return False, ""


def _freeze_request(req: Request) -> None:
    """
    تجميد الطلب أثناء النزاع:
    - تحويل الحالة إلى DISPUTED (إن توفرت) وإلا 'disputed'
    - is_frozen=True إن وُجد الحقل
    """
    updated = []

    # الحالة
    if hasattr(Request, "Status") and hasattr(Request.Status, "DISPUTED"):
        if req.status != Request.Status.DISPUTED:
            req.status = Request.Status.DISPUTED
            updated.append("status")
    else:
        if getattr(req, "status", None) != "disputed":
            req.status = "disputed"
            updated.append("status")

    # علم التجميد
    if hasattr(req, "is_frozen") and not getattr(req, "is_frozen", False):
        req.is_frozen = True
        updated.append("is_frozen")

    if updated:
        try:
            req.save(update_fields=updated)
        except Exception:
            logger.exception("فشل حفظ حالة التجميد لطلب #%s", getattr(req, "id", None))


def _unfreeze_request(req: Request) -> None:
    """
    فكّ التجميد بعد إنهاء/إلغاء النزاع:
    - إذا كانت الحالة DISPUTED نعيدها منطقيًا:
        * IN_PROGRESS عند وجود اتفاقية/تنفيذ جارٍ
        * NEW خلاف ذلك
    - is_frozen=False إن وُجد الحقل
    """
    updated = []

    is_disputed = (
        hasattr(Request, "Status")
        and hasattr(Request.Status, "DISPUTED")
        and req.status == Request.Status.DISPUTED
    ) or (getattr(req, "status", None) == "disputed")

    if is_disputed:
        if getattr(req, "agreement", None):
            fallback = Request.Status.IN_PROGRESS if hasattr(Request.Status, "IN_PROGRESS") else "in_progress"
        else:
            fallback = Request.Status.NEW if hasattr(Request.Status, "NEW") else "new"
        if req.status != fallback:
            req.status = fallback
            updated.append("status")

    if hasattr(req, "is_frozen") and getattr(req, "is_frozen", False):
        req.is_frozen = False
        updated.append("is_frozen")

    if updated:
        try:
            req.save(update_fields=updated)
        except Exception:
            logger.exception("فشل فكّ التجميد لطلب #%s", getattr(req, "id", None))


# ======================================================
# فتح نزاع (نموذج كامل)
# ======================================================
@login_required
@transaction.atomic
def dispute_create(request, request_id: int):
    """
    فتح نزاع عبر نموذج DisputeForm (title/reason/details[, milestone_id]):
    - يتحقق من صلاحيات الفاتح
    - يمنع السباق بــ select_for_update
    - ينشئ النزاع ويجمّد الطلب
    - يرسل إشعارات للعميل والموظف
    """
    req = get_object_or_404(Request.objects.select_for_update(), pk=request_id)

    ok, role = _can_open_dispute(request.user, req)
    if not ok:
        raise PermissionDenied("لا تملك صلاحية فتح نزاع على هذا الطلب.")

    if request.method == "POST":
        form = DisputeForm(request.POST)
        if form.is_valid():
            dispute = form.save(commit=False)
            dispute.request = req
            dispute.opened_by = request.user
            if hasattr(dispute, "opener_role"):
                dispute.opener_role = role

            # milestone_id اختياري
            mid = (request.POST.get("milestone_id") or "").strip()
            if mid and hasattr(dispute, "milestone_id"):
                try:
                    dispute.milestone_id = int(mid)
                except ValueError:
                    pass

            dispute.save()
            _freeze_request(req)


            detail_url = reverse("marketplace:request_detail", args=[req.pk])
            if getattr(req, "client", None):
                _notify_safe(req.client, "تم فتح نزاع", f"فُتح نزاع على طلبك #{req.pk}: {dispute.title}", url=detail_url)
            if getattr(req, "assigned_employee", None):
                _notify_safe(
                    req.assigned_employee, "تم فتح نزاع",
                    f"فُتح نزاع على طلب #{req.pk}: {dispute.title}", url=detail_url
                )

            # Notify all managers and finance
            try:
                from accounts.models import User
                managers = User.objects.filter(role=User.Role.ADMIN, is_active=True)
                finance = User.objects.filter(role=User.Role.FINANCE, is_active=True)
                for user in list(managers) + list(finance):
                    _notify_safe(user, "تم فتح نزاع جديد", f"تم فتح نزاع على الطلب #{req.pk} بعنوان: {dispute.title}", url=detail_url)
            except Exception:
                logger.exception("فشل إرسال إشعار للمديرين أو المالية عند فتح نزاع.")

            messages.warning(request, "تم فتح النزاع وتجميد الطلب مؤقتًا حتى الحسم.")
            return redirect(detail_url)
        else:
            messages.error(request, "فضلًا صحّح الأخطاء في النموذج.")
    else:
        form = DisputeForm()

    return render(request, "disputes/open.html", {"form": form, "req": req})


# ======================================================
# فتح نزاع سريع (POST بسيط: reason) — بديل مبسّط
# ======================================================
@login_required
@transaction.atomic
def dispute_open_quick(request, request_id: int):
    """
    فتح نزاع بشكل سريع عبر POST يحتوي على 'reason' فقط.
    يَستخدم نفس منطق الصلاحيات والتجميد.
    """
    if request.method != "POST":
        return redirect("marketplace:request_detail", pk=request_id)

    req = get_object_or_404(Request.objects.select_for_update(), pk=request_id)
    ok, role = _can_open_dispute(request.user, req)
    if not ok:
        raise PermissionDenied("لا تملك صلاحية فتح نزاع على هذا الطلب.")

    reason = (request.POST.get("reason") or "").strip()
    if not reason:
        messages.error(request, "الرجاء ذكر سبب النزاع.")
        return redirect("marketplace:request_detail", pk=req.pk)

    # امنع ازدواج نزاع مفتوح لنفس الطلب (لو عندك قيود نموذج/قاعدة سيتكفّل بها)
    try:
        dispute = Dispute.objects.create(
            request=req,
            opened_by=request.user,
            reason=reason,
            opener_role=role if hasattr(Dispute, "opener_role") else None,
        )
    except Exception:
        logger.exception("فشل إنشاء نزاع سريع للطلب #%s", req.pk)
        messages.error(request, "تعذّر فتح النزاع. حاول مرة أخرى.")
        return redirect("marketplace:request_detail", pk=req.pk)

    _freeze_request(req)

    detail_url = reverse("marketplace:request_detail", args=[req.pk])
    if getattr(req, "client", None):
        _notify_safe(req.client, "تم فتح نزاع", f"فُتح نزاع على طلبك #{req.pk}: {getattr(dispute, 'title', reason)}", url=detail_url)
    if getattr(req, "assigned_employee", None):
        _notify_safe(req.assigned_employee, "تم فتح نزاع", f"فُتح نزاع على طلب #{req.pk}.", url=detail_url)

    # Notify all managers and finance
    try:
        from accounts.models import User
        managers = User.objects.filter(role=User.Role.ADMIN, is_active=True)
        finance = User.objects.filter(role=User.Role.FINANCE, is_active=True)
        for user in list(managers) + list(finance):
            _notify_safe(user, "تم فتح نزاع جديد", f"تم فتح نزاع على الطلب #{req.pk} بعنوان: {getattr(dispute, 'title', reason)}", url=detail_url)
    except Exception:
        logger.exception("فشل إرسال إشعار للمديرين أو المالية عند فتح نزاع.")

    messages.warning(request, "تم فتح النزاع وإيقاف جميع العمليات لحين المراجعة.")
    return redirect(detail_url)


# ======================================================
# تحديث حالة نزاع (resolve/cancel/review/reopen)
# ======================================================
@login_required
@transaction.atomic
def dispute_update_status(request, pk: int):
    """
    تحديث حالة نزاع — للمسؤولين/المالية فقط.
    action ∈ {resolve, cancel, review, reopen}
    - resolve/cancel: فكّ التجميد مع إشعار الأطراف
    - review: تحويل إلى IN_REVIEW
    - reopen: إعادة فتح وتجميد الطلب
    """
    dispute = get_object_or_404(
        Dispute.objects.select_for_update().select_related("request", "opened_by"),
        pk=pk,
    )
    req = dispute.request

    if not _is_admin(request.user):
        raise PermissionDenied("صلاحيات غير كافية لإدارة النزاع.")

    action = (request.POST.get("action") or "").strip().lower()
    if action not in {"resolve", "cancel", "review", "reopen"}:
        messages.error(request, "طلب غير صحيح.")
        return redirect(reverse("marketplace:request_detail", args=[req.pk]))

    # ترجمة الأكشن إلى حالة الموديل
    if action == "resolve":
        new_status = getattr(getattr(Dispute, "Status", None), "RESOLVED", "resolved")
    elif action == "cancel":
        new_status = getattr(getattr(Dispute, "Status", None), "CANCELED", "canceled")
    elif action == "review":
        new_status = getattr(getattr(Dispute, "Status", None), "IN_REVIEW", "in_review")
    else:  # reopen
        new_status = getattr(getattr(Dispute, "Status", None), "OPEN", "open")

    dispute.status = new_status
    update_fields = ["status"]

    if new_status in {getattr(getattr(Dispute, "Status", None), "RESOLVED", "resolved"),
                      getattr(getattr(Dispute, "Status", None), "CANCELED", "canceled")}:
        if hasattr(dispute, "resolved_by"):
            dispute.resolved_by = request.user
            update_fields.append("resolved_by")
        if hasattr(dispute, "resolved_note"):
            dispute.resolved_note = (request.POST.get("resolved_note") or "").strip()
            update_fields.append("resolved_note")

    dispute.save(update_fields=update_fields)

    # إدارة التجميد/فكه + إشعارات
    detail_url = reverse("marketplace:request_detail", args=[req.pk])
    if new_status in {"resolved", "canceled", getattr(getattr(Dispute, "Status", None), "RESOLVED", "resolved"),
                      getattr(getattr(Dispute, "Status", None), "CANCELED", "canceled")}:
        _unfreeze_request(req)
        messages.success(request, "تم إنهاء النزاع وفكّ التجميد.")
        if getattr(req, "client", None):
            _notify_safe(req.client, "تم إنهاء النزاع", f"تم إنهاء النزاع على طلب #{req.pk}.", url=detail_url)
        if getattr(req, "assigned_employee", None):
            _notify_safe(req.assigned_employee, "تم إنهاء النزاع", f"تم إنهاء النزاع على طلب #{req.pk}.", url=detail_url)
    elif new_status in {"open", getattr(getattr(Dispute, "Status", None), "OPEN", "open")}:
        _freeze_request(req)
        messages.warning(request, "تم إعادة فتح النزاع وتمّ تجميد الطلب.")
    else:
        messages.info(request, "تم تحديث حالة النزاع.")

    return redirect(detail_url)


# ======================================================
# عرض نزاع
# ======================================================

def dispute_detail(request, pk):
    dispute = get_object_or_404(Dispute, pk=pk)
    events = dispute.events.all().order_by("created_at") if hasattr(dispute, "events") else None
    return render(request, "disputes/dispute_detail.html", {
        "dispute": dispute,
        "events": events,
    })
