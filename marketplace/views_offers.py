from __future__ import annotations

"""
عروض الطلبات – منطق إنشاء/اختيار/رفض العرض مع ضوابط أمان وسياسات السوق.

نقاط بارزة:
- نافذة العروض: افتراضيًا 5 أيام من إنشاء الطلب (قابلة للتعديل عبر settings.OFFERS_WINDOW_DAYS).
- موظف واحد/عرض واحد على نفس الطلب (يُمنع التكرار على PENDING/SELECTED).
- لا عروض على طلب مُسند أو خارج حالة NEW أو في نزاع.
- اختيار العرض: يرفض بقية العروض، ويسند الطلب للموظف، ويحوّل الحالة إلى OFFER_SELECTED.
- تعامل آمن مع الحقول الاختيارية (offer_selected_at/updated_at) بدون كسر التوافق.
- تنظيف مدخلات النصوص، والتحقق الصارم من السعر كـ Decimal موجبة ضمن نطاق منطقي.
- سجلات (logging) للأحداث المهمة لتتبّع الإنتاج.
"""

import logging
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from django.conf import settings
from django.utils import timezone
from django.utils.html import strip_tags
from django.core.exceptions import FieldDoesNotExist
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import (
    HttpRequest,
    HttpResponse,
    HttpResponseForbidden,
)
from django.shortcuts import get_object_or_404, redirect
from django.views.decorators.http import require_POST

from core.permissions import require_role  # ✅
from .models import Request, Offer

logger = logging.getLogger(__name__)

# =========================
# إعدادات وسياسات
# =========================
OFFERS_WINDOW_DAYS: int = int(getattr(settings, "OFFERS_WINDOW_DAYS", 5))
MAX_PRICE: Decimal = Decimal(getattr(settings, "OFFERS_MAX_PRICE", "1000000"))
MIN_PRICE: Decimal = Decimal("1")

# محاولة استيراد المُخطر من views؛ إن لم يوجد وفّر بديل صامت
try:
    from .views import _notify_offer_selected  # موجود غالبًا في marketplace/views.py
except Exception:  # pragma: no cover
    def _notify_offer_selected(off: Offer) -> None:  # noqa: N802
        return


# =========================
# Helpers (RBAC / meta)
# =========================
def _is_admin(user) -> bool:
    """اعتبار المستخدم إداريًا إذا كان staff/superuser أو دوره admin/manager."""
    return bool(
        getattr(user, "is_staff", False)
        or getattr(user, "is_superuser", False)
        or getattr(user, "role", "") in {"admin", "manager"}
    )


def _model_has_field(model_cls, field_name: str) -> bool:
    """تحقق آمن من وجود الحقل في الموديل قبل استخدامه في update_fields."""
    try:
        model_cls._meta.get_field(field_name)
        return True
    except FieldDoesNotExist:
        return False


def _within_offers_window(req: Request) -> bool:
    """
    التحقق أن الطلب ضمن نافذة استقبال العروض.
    إن غاب created_at نعتبره ضمن النافذة (توافقًا للخلف).
    """
    created = getattr(req, "created_at", None)
    if not created:
        return True
    deadline = created + timezone.timedelta(days=OFFERS_WINDOW_DAYS)
    return timezone.now() <= deadline


def _sanitize_notes(value: str, max_len: int = 2000) -> str:
    """تنظيف ملاحظات النص من الوسوم وتقليل الطول."""
    value = (value or "").strip()
    value = strip_tags(value)
    if len(value) > max_len:
        value = value[:max_len]
    return value


def _parse_price(raw: str) -> Decimal | None:
    """
    تحويل السعر إلى Decimal موجبة مع تقريب إلى خانتين.
    يعيد None عند عدم صحة الإدخال.
    """
    try:
        price = Decimal((raw or "").strip())
        if price <= 0:
            return None
        # تقريب إلى خانتين (مثل العملات)
        price = price.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if price < MIN_PRICE or price > MAX_PRICE:
            return None
        return price
    except (InvalidOperation, ValueError, TypeError):
        return None


# =========================
# عروض الموظفين
# =========================
@login_required
@require_POST
@transaction.atomic
def offer_create(request: HttpRequest, request_id: int) -> HttpResponse:
    """
    الموظف يرسل عرضًا على طلب جديد.

    الأمان والمنطق:
    - POST فقط.
    - رفض عند النزاع/التجميد.
    - يُسمح فقط للموظف أو للإدارة.
    - منع التكرار لنفس الموظف (PENDING/SELECTED).
    - يجب أن يكون الطلب في حالة NEW وغير مُسنَد.
    - ضمن نافذة العروض OFFERS_WINDOW_DAYS.
    - التحقق من السعر كـ Decimal موجبة ضمن حدود منطقية.
    """
    req = get_object_or_404(Request.objects.select_for_update(), pk=request_id)

    role = getattr(request.user, "role", "")
    if not (_is_admin(request.user) or role == "employee"):
        logger.warning("offer_create: unauthorized user tried to offer", extra={"user_id": request.user.id, "req": req.pk})
        return HttpResponseForbidden("ليست لديك صلاحية لإرسال عرض على هذا الطلب.")

    # منع العروض أثناء النزاع/التجميد
    status_str = str(getattr(req, "status", "")).lower()
    if getattr(req, "is_frozen", False) or status_str == "disputed":
        messages.error(request, "لا يمكن إرسال عرض: الطلب في حالة نزاع.")
        return redirect("marketplace:request_detail", pk=req.pk)

    # لا عروض إلا على NEW وغير المُسنَد
    new_value = getattr(Request.Status, "NEW", "new")
    if str(getattr(req, "status", "")).lower() != str(new_value).lower():
        messages.info(request, "لا يمكن إرسال عرض إلا على الطلبات الجديدة.")
        return redirect("marketplace:request_detail", pk=req.pk)

    if getattr(req, "assigned_employee_id", None):
        messages.info(request, "تم إسناد الطلب بالفعل.")
        return redirect("marketplace:request_detail", pk=req.pk)

    # نافذة العروض
    if not _within_offers_window(req):
        messages.error(
            request,
            f"انتهت نافذة استقبال العروض ({OFFERS_WINDOW_DAYS} أيام من إنشاء الطلب)."
        )
        return redirect("marketplace:request_detail", pk=req.pk)

    # منع تكرار عروض الموظف نفسه على نفس الطلب (pending/selected)
    if Offer.objects.filter(
        request=req,
        employee=request.user,
        status__in=[
            getattr(Offer.Status, "PENDING", "pending"),
            getattr(Offer.Status, "SELECTED", "selected"),
        ],
    ).exists():
        messages.info(request, "لديك عرض سابق على هذا الطلب.")
        return redirect("marketplace:request_detail", pk=req.pk)

    # التحقق من السعر
    price = _parse_price(request.POST.get("price"))
    if price is None:
        messages.error(
            request,
            f"قيمة السعر غير صحيحة. يُسمح من {MIN_PRICE} إلى {MAX_PRICE} وبخانتين عشريتين."
        )
        return redirect("marketplace:request_detail", pk=req.pk)

    notes = _sanitize_notes(request.POST.get("notes"))

    # إنشاء العرض
    Offer.objects.create(
        request=req,
        employee=request.user,
        price=price,
        notes=notes,
        status=getattr(Offer.Status, "PENDING", "pending"),
    )

    logger.info(
        "offer_create: created",
        extra={"request_id": req.pk, "employee_id": request.user.id, "price": str(price)}
    )
    messages.success(request, "تم إرسال العرض بنجاح.")
    return redirect("marketplace:request_detail", pk=req.pk)


@require_role("client")
@require_POST
@transaction.atomic
def offer_select(request: HttpRequest, offer_id: int) -> HttpResponse:
    """
    اختيار العرض من العميل (الديكوريتر يسمح للـ staff/admin أيضًا).
    - يرفض بقية العروض.
    - يحدّث حالة العرض المختار.
    - يسند الطلب للموظف ويحوّل حالته إلى OFFER_SELECTED.
    - يتعامل بحذر مع الحقول الاختيارية مثل offer_selected_at/updated_at.
    """
    off = get_object_or_404(
        Offer.objects.select_related("request", "employee").select_for_update(),
        pk=offer_id,
    )
    req = off.request

    # السماح للعميل أو الإدارة فقط (require_role يسمح للـ staff/admin، لكن نضيف تحققًا صريحًا للعميل مالك الطلب)
    if req.client != request.user and not getattr(request.user, "is_staff", False):
        logger.warning("offer_select: forbidden user", extra={"user_id": request.user.id, "req": req.pk, "offer": off.pk})
        return HttpResponseForbidden("غير مسموح")

    # منع أثناء النزاع/التجميد
    if getattr(req, "is_frozen", False) or str(getattr(req, "status", "")).lower() == "disputed":
        messages.error(request, "لا يمكن اختيار عرض: الطلب في حالة نزاع.")
        return redirect("marketplace:request_detail", pk=req.pk)

    # صلاحية العرض للحظة الاختيار
    if hasattr(off, "can_select") and not off.can_select(request.user):
        return HttpResponseForbidden("لا يمكن اختيار هذا العرض.")
    if getattr(off, "status", None) != getattr(Offer.Status, "PENDING", "pending"):
        messages.info(request, "لا يمكن اختيار عرض غير معلّق.")
        return redirect("marketplace:request_detail", pk=req.pk)

    # ارفض بقية العروض
    rejected_count = (
        Offer.objects.filter(request=req)
        .exclude(pk=off.pk)
        .update(status=getattr(Offer.Status, "REJECTED", "rejected"))
    )

    # اختر هذا العرض
    off.status = getattr(Offer.Status, "SELECTED", "selected")
    offer_update_fields = ["status"]

    if _model_has_field(Offer, "updated_at"):
        off.updated_at = timezone.now()
        offer_update_fields.append("updated_at")

    if _model_has_field(Offer, "selected_at"):
        off.selected_at = timezone.now()
        offer_update_fields.append("selected_at")

    off.save(update_fields=offer_update_fields)

    # إسناد الطلب وتحديث حالته
    req.assigned_employee = off.employee
    req.status = getattr(Request.Status, "OFFER_SELECTED", "offer_selected")

    request_update_fields = ["assigned_employee", "status"]

    if _model_has_field(Request, "offer_selected_at"):
        req.offer_selected_at = timezone.now()
        request_update_fields.append("offer_selected_at")

    if _model_has_field(Request, "updated_at"):
        req.updated_at = timezone.now()
        request_update_fields.append("updated_at")

    req.save(update_fields=request_update_fields)

    # إشعار (إن وجد)
    try:
        _notify_offer_selected(off)
    except Exception:  # pragma: no cover
        logger.exception("offer_select: notify failed (silently handled)")

    logger.info(
        "offer_select: selected",
        extra={
            "request_id": req.pk,
            "selected_offer": off.pk,
            "employee_id": off.employee_id,
            "rejected_others": rejected_count,
        },
    )
    messages.success(request, "تم اختيار العرض وإسناد الطلب.")
    return redirect("marketplace:request_detail", pk=req.pk)


@login_required
@require_POST
@transaction.atomic
def offer_reject(request: HttpRequest, offer_id: int) -> HttpResponse:
    """
    رفض عرض من قِبل العميل (أو الإدارة).
    - POST فقط.
    - لا يؤثر على العروض الأخرى.
    - لا رفض أثناء حالة النزاع.
    - لا يُسمح برفض عروض ليست PENDING.
    """
    off = get_object_or_404(
        Offer.objects.select_related("request").select_for_update(),
        pk=offer_id,
    )
    req = Request.objects.select_for_update().get(pk=off.request_id)

    is_client = (request.user == req.client)
    if not (is_client or _is_admin(request.user)):
        logger.warning("offer_reject: forbidden", extra={"user_id": request.user.id, "offer": off.pk})
        return HttpResponseForbidden("ليست لديك صلاحية لرفض العرض.")

    if getattr(req, "is_frozen", False) or str(getattr(req, "status", "")).lower() == "disputed":
        messages.error(request, "لا يمكن رفض عرض: الطلب في حالة نزاع.")
        return redirect("marketplace:request_detail", pk=req.pk)

    if off.status != getattr(Offer.Status, "PENDING", "pending"):
        messages.info(request, "لا يمكن رفض هذا العرض في حالته الحالية.")
        return redirect("marketplace:request_detail", pk=req.pk)

    off.status = getattr(Offer.Status, "REJECTED", "rejected")
    update_fields = ["status"]

    if _model_has_field(Offer, "updated_at"):
        off.updated_at = timezone.now()
        update_fields.append("updated_at")

    off.save(update_fields=update_fields)

    logger.info(
        "offer_reject: rejected",
        extra={"request_id": req.pk, "offer_id": off.pk, "by_user": request.user.id},
    )
    messages.success(request, "تم رفض العرض.")
    return redirect("marketplace:request_detail", pk=req.pk)

@login_required
def offer_withdraw(request, offer_id):
    offer = get_object_or_404(Offer, pk=offer_id, technician=request.user)
    req = offer.request
    if not req.offers_open:
        messages.error(request, "انتهت نافذة العروض؛ لا يمكن سحب العرض الآن.")
        return redirect(req.get_absolute_url())
    offer.delete()
    messages.success(request, "تم سحب العرض، يمكنك إعادة التقديم طالما النافذة مفتوحة.")
    return redirect(req.get_absolute_url())
