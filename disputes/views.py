# disputes/views.py
from __future__ import annotations

import logging
from typing import Optional, Tuple

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q
from django.http import HttpRequest
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

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
        # لا نكسر التطبيق بسبب إشعار
        logger.debug("تعذر إرسال إشعار: %s", title, exc_info=True)


# ======================================================
# Helpers (الصلاحيات / التجميد)
# ======================================================
def _is_admin(user) -> bool:
    """صلاحيات إدارية موسعة (admin/staff/finance)."""
    role = getattr(user, "role", "") or ""
    return bool(
        getattr(user, "is_superuser", False)
        or getattr(user, "is_staff", False)
        or role in {"admin", "finance", "manager", "gm"}
    )


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


def _can_view_dispute(user, dispute: Dispute) -> bool:
    """مشاهدة النزاع للأطراف أو الإدارة/المالية."""
    if not user or not user.is_authenticated:
        return False
    if _is_admin(user):
        return True
    req = getattr(dispute, "request", None)
    if not req:
        return False
    uid = getattr(user, "id", None)
    return bool(
        getattr(req, "client_id", None) == uid
        or getattr(req, "assigned_employee_id", None) == uid
        or getattr(dispute, "opened_by_id", None) == uid
    )


def _freeze_request(req: Request, reason: str = "dispute_opened") -> None:
    """
    تجميد الطلب أثناء النزاع (دفاعي):
    - إن كانت عندك دالة req.freeze() نستخدمها.
    - وإلا نعمل fallback:
        * status -> DISPUTED أو 'disputed'
        * is_frozen=True إن وُجد
    """
    try:
        # دالة مخصّصة لديك؟
        if hasattr(req, "freeze") and callable(getattr(req, "freeze")):
            req.freeze()
            return

        updated = []

        # الحالة
        if hasattr(Request, "Status") and hasattr(Request.Status, "DISPUTED"):
            if getattr(req, "status", None) != Request.Status.DISPUTED:
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

        # سبب التجميد إن وجد
        if hasattr(req, "freeze_reason"):
            req.freeze_reason = reason
            updated.append("freeze_reason")

        if updated:
            req.save(update_fields=list(dict.fromkeys(updated)))
    except Exception:
        logger.exception("فشل تجميد الطلب #%s", getattr(req, "id", None))


def _unfreeze_request(req: Request) -> None:
    """
    فكّ التجميد بعد إنهاء/إلغاء النزاع (دفاعي):
    - إن كانت عندك دالة req.unfreeze() نستخدمها.
    - وإلا fallback:
        * إذا status=DISPUTED نرجّعها منطقياً
        * is_frozen=False
    """
    try:
        if hasattr(req, "unfreeze") and callable(getattr(req, "unfreeze")):
            req.unfreeze()
            return

        updated = []

        is_disputed = (
            hasattr(Request, "Status")
            and hasattr(Request.Status, "DISPUTED")
            and getattr(req, "status", None) == Request.Status.DISPUTED
        ) or (getattr(req, "status", None) == "disputed")

        if is_disputed:
            if getattr(req, "agreement", None):
                fallback = Request.Status.IN_PROGRESS if hasattr(Request.Status, "IN_PROGRESS") else "in_progress"
            else:
                fallback = Request.Status.NEW if hasattr(Request.Status, "NEW") else "new"

            if getattr(req, "status", None) != fallback:
                req.status = fallback
                updated.append("status")

        if hasattr(req, "is_frozen") and getattr(req, "is_frozen", False):
            req.is_frozen = False
            updated.append("is_frozen")

        if hasattr(req, "freeze_reason") and getattr(req, "freeze_reason", ""):
            req.freeze_reason = ""
            updated.append("freeze_reason")

        if updated:
            req.save(update_fields=list(dict.fromkeys(updated)))
    except Exception:
        logger.exception("فشل فكّ تجميد الطلب #%s", getattr(req, "id", None))


# ======================================================
# قائمة النزاعات للإدارة/المالية
# url name = disputes:list
# ======================================================
@login_required
def dispute_list(request: HttpRequest):
    """
    قائمة جميع النزاعات (للإدارة/المالية فقط).
    Filters: status, q, request_id
    """
    if not _is_admin(request.user):
        raise PermissionDenied("لا تملك صلاحية عرض النزاعات.")

    qs = Dispute.objects.select_related("request", "opened_by", "resolved_by").order_by("-opened_at")

    status = (request.GET.get("status") or "").strip()
    q = (request.GET.get("q") or "").strip()
    request_id = (request.GET.get("request_id") or "").strip()

    if status in {"open", "in_review", "resolved", "canceled", "closed"}:
        qs = qs.filter(status=status)

    if request_id.isdigit():
        qs = qs.filter(request_id=int(request_id))

    if q:
        qs = qs.filter(
            Q(title__icontains=q)
            | Q(reason__icontains=q)
            | Q(details__icontains=q)
            | Q(request__title__icontains=q)
            | Q(request__short_code__icontains=q)
        )

    paginator = Paginator(qs, 20)
    page_obj = paginator.get_page(request.GET.get("page"))

    return render(request, "disputes/disputes_admin_list.html", {
        "page_obj": page_obj,
        "status": status,
        "q": q,
        "request_id": request_id,
    })


# ======================================================
# فتح نزاع (نموذج كامل)
# ======================================================
@login_required
@transaction.atomic
def dispute_create(request: HttpRequest, request_id: int):
    """
    فتح نزاع عبر نموذج DisputeForm (title/reason/details[, milestone_id]):
    - يتحقق من صلاحيات الفاتح
    - يمنع السباق بــ select_for_update
    - ينشئ النزاع ويجمّد الطلب
    - يرسل إشعارات للعميل والموظف + المالية/الإدارة
    """
    req = get_object_or_404(Request.objects.select_for_update(), pk=request_id)

    ok, role = _can_open_dispute(request.user, req)
    if not ok:
        raise PermissionDenied("لا تملك صلاحية فتح نزاع على هذا الطلب.")

    if request.method == "POST":
        form = DisputeForm(request.POST, request.FILES)
        if form.is_valid():
            # تحقق من وجود نزاع سابق مفتوح أو قيد المراجعة
            existing_dispute = req.disputes.filter(status__in=["open", "in_review"]).first()
            if existing_dispute:
                from django.contrib import messages
                messages.error(request, "لا يمكن فتح نزاع بسبب فتح نزاع سابق وتم حله من قبل الإدارة.")
                return render(request, "disputes/open.html", {"form": form, "req": req})

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
                    form.add_error(None, "رقم المرحلة غير صحيح.")
                    return render(request, "disputes/open.html", {"form": form, "req": req})

            dispute.save()

            # حفظ الدلائل عبر uploads app (إن أردت)
            try:
                form.save_evidence(request, dispute)
            except Exception:
                logger.debug("تعذر حفظ دلائل النزاع", exc_info=True)

            _freeze_request(req, reason="dispute_opened")

            detail_url = reverse("marketplace:request_detail", args=[req.pk])

            # إشعارات الأطراف
            if getattr(req, "client", None):
                _notify_safe(req.client, "تم فتح نزاع", f"فُتح نزاع على طلبك #{req.pk}: {dispute.title}", url=detail_url)
            if getattr(req, "assigned_employee", None):
                _notify_safe(req.assigned_employee, "تم فتح نزاع", f"فُتح نزاع على طلب #{req.pk}: {dispute.title}", url=detail_url)

            # إشعار المالية/المديرين
            try:
                from accounts.models import User
                managers = User.objects.filter(role=User.Role.ADMIN, is_active=True)
                finance = User.objects.filter(role=User.Role.FINANCE, is_active=True)
                for u in list(managers) + list(finance):
                    _notify_safe(u, "تم فتح نزاع جديد", f"نزاع على الطلب #{req.pk} بعنوان: {dispute.title}", url=detail_url)
            except Exception:
                logger.exception("فشل إرسال إشعار للمديرين أو المالية عند فتح نزاع.")

            messages.warning(request, "تم فتح النزاع وتجميد الطلب مؤقتًا حتى الحسم.")
            return redirect(detail_url)

        messages.error(request, "فضلًا صحّح الأخطاء في النموذج.")
    else:
        form = DisputeForm()

    return render(request, "disputes/open.html", {"form": form, "req": req})


# ======================================================
# فتح نزاع سريع (POST بسيط: reason) — بديل مبسّط
# ======================================================
@login_required
@require_POST
@transaction.atomic
def dispute_open_quick(request: HttpRequest, request_id: int):
    """
    فتح نزاع بشكل سريع عبر POST يحتوي على 'reason' فقط.
    يَستخدم نفس منطق الصلاحيات والتجميد.
    """
    req = get_object_or_404(Request.objects.select_for_update(), pk=request_id)
    ok, role = _can_open_dispute(request.user, req)
    if not ok:
        raise PermissionDenied("لا تملك صلاحية فتح نزاع على هذا الطلب.")

    reason = (request.POST.get("reason") or "").strip()
    if not reason:
        messages.error(request, "الرجاء ذكر سبب النزاع.")
        return redirect("marketplace:request_detail", pk=req.pk)

    try:
        dispute = Dispute.objects.create(
            request=req,
            opened_by=request.user,
            title=reason[:120],  # عنوان سريع اختياري
            reason=reason,
            opener_role=role if hasattr(Dispute, "opener_role") else role,
        )
    except Exception:
        logger.exception("فشل إنشاء نزاع سريع للطلب #%s", req.pk)
        messages.error(request, "تعذّر فتح النزاع. حاول مرة أخرى.")
        return redirect("marketplace:request_detail", pk=req.pk)

    _freeze_request(req, reason="dispute_opened")

    detail_url = reverse("marketplace:request_detail", args=[req.pk])
    if getattr(req, "client", None):
        _notify_safe(req.client, "تم فتح نزاع", f"فُتح نزاع على طلبك #{req.pk}: {dispute.title}", url=detail_url)
    if getattr(req, "assigned_employee", None):
        _notify_safe(req.assigned_employee, "تم فتح نزاع", f"فُتح نزاع على طلب #{req.pk}.", url=detail_url)

    # Notify all managers and finance
    try:
        from accounts.models import User
        managers = User.objects.filter(role=User.Role.ADMIN, is_active=True)
        finance = User.objects.filter(role=User.Role.FINANCE, is_active=True)
        for u in list(managers) + list(finance):
            _notify_safe(u, "تم فتح نزاع جديد", f"تم فتح نزاع على الطلب #{req.pk}.", url=detail_url)
    except Exception:
        logger.exception("فشل إرسال إشعار للمديرين أو المالية عند فتح نزاع.")

    messages.warning(request, "تم فتح النزاع وإيقاف جميع العمليات لحين المراجعة.")
    return redirect(detail_url)


# ======================================================
# تحديث حالة نزاع (resolve/cancel/review/reopen)
# ======================================================
@login_required
@require_POST
@transaction.atomic
def dispute_update_status(request: HttpRequest, pk: int):
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

    finished_vals = {
        getattr(getattr(Dispute, "Status", None), "RESOLVED", "resolved"),
        getattr(getattr(Dispute, "Status", None), "CANCELED", "canceled"),
        "resolved",
        "canceled",
        "closed",
    }

    if new_status in finished_vals:
        if hasattr(dispute, "resolved_by"):
            dispute.resolved_by = request.user
            update_fields.append("resolved_by")
        if hasattr(dispute, "resolved_note"):
            dispute.resolved_note = (request.POST.get("resolved_note") or "").strip()
            update_fields.append("resolved_note")
        if hasattr(dispute, "resolved_at"):
            from django.utils import timezone
            dispute.resolved_at = timezone.now()
            update_fields.append("resolved_at")

    dispute.save(update_fields=list(dict.fromkeys(update_fields)))

    # إدارة التجميد/فكه + إشعارات
    detail_url = reverse("marketplace:request_detail", args=[req.pk])

    if new_status in finished_vals:
        _unfreeze_request(req)
        messages.success(request, "تم إنهاء النزاع وفكّ التجميد.")
        if getattr(req, "client", None):
            _notify_safe(req.client, "تم إنهاء النزاع", f"تم إنهاء النزاع على طلب #{req.pk}.", url=detail_url)
        if getattr(req, "assigned_employee", None):
            _notify_safe(req.assigned_employee, "تم إنهاء النزاع", f"تم إنهاء النزاع على طلب #{req.pk}.", url=detail_url)

    elif new_status in {"open", getattr(getattr(Dispute, "Status", None), "OPEN", "open")}:
        _freeze_request(req, reason="dispute_reopened")
        messages.warning(request, "تم إعادة فتح النزاع وتمّ تجميد الطلب.")

    else:
        messages.info(request, "تم تحديث حالة النزاع.")

    return redirect(detail_url)


# ======================================================
# عرض نزاع
# ======================================================
@login_required
def dispute_detail(request: HttpRequest, pk: int):
    dispute = get_object_or_404(
        Dispute.objects.select_related("request", "opened_by", "resolved_by"),
        pk=pk,
    )
    if not _can_view_dispute(request.user, dispute):
        raise PermissionDenied("لا تملك صلاحية مشاهدة هذا النزاع.")

    events = dispute.events.all().order_by("created_at") if hasattr(dispute, "events") else None
    return render(request, "disputes/dispute_detail.html", {
        "dispute": dispute,
        "events": events,
    })
