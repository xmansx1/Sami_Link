from django.shortcuts import get_object_or_404, redirect, render
from django.http import HttpResponseForbidden
from django.utils import timezone
from .models import Agreement
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import HttpRequest, HttpResponse
from django.contrib.auth.decorators import login_required

# ========================= طلب تمديد المهلة للاتفاقية =========================
@login_required
@transaction.atomic
def approve_extension(request: HttpRequest, pk: int) -> HttpResponse:
    ag = get_object_or_404(Agreement, pk=pk)
    # فقط العميل يحق له الموافقة
    if request.user.id != getattr(ag.request, "client_id", None):
        return HttpResponseForbidden("غير مصرح لك.")
    if ag.extension_requested_days and ag.extension_requested_days > 0:
        ag.duration_days += ag.extension_requested_days
        ag.extension_requested_days = None
        if hasattr(ag, "updated_at"):
            ag.updated_at = timezone.now()
        ag.save(update_fields=["duration_days", "extension_requested_days"] + (["updated_at"] if hasattr(ag, "updated_at") else []))
        # إشعار الموظف
        from notifications.utils import create_notification
        create_notification(
            recipient=ag.employee,
            title="تمت الموافقة على طلب تمديد المهلة",
            body=f"تمت الموافقة من العميل على تمديد مدة الاتفاقية #{ag.pk}.",
            url=ag.get_absolute_url(),
            actor=request.user,
            target=ag,
        )
        messages.success(request, "تمت الموافقة على تمديد المهلة بنجاح.")
    else:
        messages.error(request, "لا يوجد طلب تمديد مهلة بانتظار الموافقة.")
    return redirect("agreements:detail", pk=ag.pk)

@login_required
@transaction.atomic
def reject_extension(request: HttpRequest, pk: int) -> HttpResponse:
    ag = get_object_or_404(Agreement, pk=pk)
    # فقط العميل يحق له الرفض
    if request.user.id != getattr(ag.request, "client_id", None):
        return HttpResponseForbidden("غير مصرح لك.")
    if ag.extension_requested_days and ag.extension_requested_days > 0:
        ag.extension_requested_days = None
        if hasattr(ag, "updated_at"):
            ag.updated_at = timezone.now()
        ag.save(update_fields=["extension_requested_days"] + (["updated_at"] if hasattr(ag, "updated_at") else []))
        # إشعار الموظف
        from notifications.utils import create_notification
        create_notification(
            recipient=ag.employee,
            title="تم رفض طلب تمديد المهلة",
            body=f"تم رفض طلب تمديد المهلة للاتفاقية #{ag.pk} من العميل.",
            url=ag.get_absolute_url(),
            actor=request.user,
            target=ag,
        )
        messages.success(request, "تم رفض طلب تمديد المهلة.")
    else:
        messages.error(request, "لا يوجد طلب تمديد مهلة بانتظار الموافقة.")
    return redirect("agreements:detail", pk=ag.pk)

from django.views.decorators.http import require_POST
from django.http import HttpRequest, HttpResponse
from django.db import transaction
from django.contrib.auth.decorators import login_required


@login_required
@transaction.atomic
def request_extension(request: HttpRequest, pk: int) -> HttpResponse:
    ag = get_object_or_404(Agreement.objects.select_related("request"), pk=pk)
    req = ag.request
    # تحقق من الصلاحية: فقط الموظف المسند
    if request.user.id != getattr(req, "assigned_employee_id", None):
        return HttpResponseForbidden("غير مصرح لك بطلب تمديد المهلة.")

    if request.method == "POST":
        try:
            extra_days = int(request.POST.get("extra_days", "0"))
        except Exception:
            extra_days = 0
        if extra_days < 1:
            messages.error(request, "يجب إدخال عدد أيام صحيح للتمديد.")
        else:
            ag.extension_requested_days = extra_days
            if hasattr(ag, "updated_at"):
                ag.updated_at = timezone.now()
            ag.save(update_fields=["extension_requested_days"] + (["updated_at"] if hasattr(ag, "updated_at") else []))
            # إشعار العميل
            from notifications.utils import create_notification
            client = getattr(ag.request, "client", None)
            create_notification(
                recipient=client,
                title="طلب تمديد مهلة التنفيذ",
                body=f"قام الموظف بطلب تمديد مدة الاتفاقية #{ag.pk} بمقدار {extra_days} يوم. يمكنك الموافقة أو الرفض من صفحة الطلب.",
                url=ag.get_absolute_url(),
                actor=request.user,
                target=ag,
            )
            messages.success(request, f"تم إرسال طلب تمديد المهلة ({extra_days} يوم) للعميل بنجاح.")
            return redirect("agreements:detail", pk=ag.pk)

    return render(request, "agreements/agreement_extension_request.html", {"agreement": ag})

import logging
from decimal import Decimal
from typing import List

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.forms.formsets import BaseFormSet
from django.http import (
    HttpRequest,
    HttpResponse,
    HttpResponseForbidden,
    HttpResponseNotAllowed,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.permissions import require_role
from marketplace.models import Request, Offer, Status

from .forms import AgreementEditForm, MilestoneFormSet, AgreementClauseSelectForm
from .models import Agreement, AgreementClauseItem, Milestone

logger = logging.getLogger(__name__)


# ============================== صلاحيات مساعدة ==============================
def _is_admin(user) -> bool:
    return bool(
        getattr(user, "is_superuser", False)
        or getattr(user, "is_staff", False)
        or getattr(user, "role", "") == "admin"
    )


def _is_emp_or_admin(user) -> bool:
    return bool(_is_admin(user) or getattr(user, "role", "") == "employee")


# =============================== أدوات مساعدة ===============================
def _get_selected_offer(req: Request) -> Offer | None:
    off = getattr(req, "selected_offer", None)
    if off:
        return off
    return (
        req.offers.filter(status=getattr(Status, "SELECTED", "selected"))
        .select_related("employee")
        .first()
    )


def _has_db_field(instance, field_name: str) -> bool:
    try:
        instance._meta.get_field(field_name)
        return True
    except Exception:
        return False


def _set_db_field(instance, field_name: str, value, update_fields: list[str]) -> None:
    try:
        instance._meta.get_field(field_name)
    except Exception:
        return
    setattr(instance, field_name, value)
    update_fields.append(field_name)


def _update_request_status_on_send(req: Request) -> None:
    new_status = getattr(Status, "AGREEMENT_PENDING", "agreement_pending")
    try:
        req.status = new_status
        updates = ["status"]
        if _has_db_field(req, "updated_at"):
            req.updated_at = timezone.now()
            updates.append("updated_at")
        req.save(update_fields=updates)
    except Exception as exc:
        logger.warning(
            "فشل تحديث حالة الطلب عند إرسال الاتفاقية req=%s: %s",
            getattr(req, "pk", None),
            exc,
        )


def _move_request_on_accept(req: Request) -> None:
    awaiting = getattr(
        Status, "AWAITING_PAYMENT_CONFIRMATION", "awaiting_payment_confirmation"
    )
    updates = ["status"]
    req.status = awaiting
    if _has_db_field(req, "updated_at"):
        req.updated_at = timezone.now()
        updates.append("updated_at")
    req.save(update_fields=updates)


def _touch_request_in_progress(req: Request) -> None:
    """
    توافق/فولباك فقط:
    لو الفاتورة مدفوعة والميلستون اعتمدت، نحرك الطلب للتنفيذ.
    الموديل الجديد يحقق ذلك تلقائياً عند وسم الفاتورة مدفوعة،
    لكن نُبقيها كحماية إن كان فيه تدفق قديم.
    """
    try:
        if hasattr(req, "mark_paid_and_start"):
            req.mark_paid_and_start()
            return
    except Exception:
        pass

    in_progress = getattr(Status, "IN_PROGRESS", "in_progress")
    early = {
        getattr(Status, "AWAITING_PAYMENT_CONFIRMATION", "awaiting_payment_confirmation"),
        getattr(Status, "AGREEMENT_PENDING", "agreement_pending"),
    }
    try:
        if getattr(req, "status", None) in early:
            req.status = in_progress
            updates = ["status"]
            if _has_db_field(req, "updated_at"):
                req.updated_at = timezone.now()
                updates.append("updated_at")
            req.save(update_fields=updates)
    except Exception as exc:
        logger.warning("_touch_request_in_progress failed req=%s: %s", getattr(req, "pk", None), exc)


def _return_request_to_offer_selected(req: Request) -> None:
    if hasattr(Status, "OFFER_SELECTED"):
        req.status = Status.OFFER_SELECTED
        updates = ["status"]
        if _has_db_field(req, "updated_at"):
            req.updated_at = timezone.now()
            updates.append("updated_at")
        req.save(update_fields=updates)


def _redirect_to_request_detail(ms: Milestone) -> HttpResponse:
    req = getattr(getattr(ms, "agreement", None), "request", None)
    if not req:
        return redirect("/")
    try:
        url = req.get_absolute_url()
    except Exception:
        url = reverse("marketplace:request_detail", args=[req.id])
    return redirect(f"{url}#ms-{ms.id}")


def _save_formset_strict(formset: BaseFormSet, agreement: Agreement) -> None:
    instances = formset.save(commit=False)

    for obj in getattr(formset, "deleted_objects", []):
        try:
            obj.delete()
        except Exception as exc:
            logger.warning("فشل حذف المرحلة id=%s: %s", getattr(obj, "pk", None), exc)

    for obj in instances:
        if getattr(obj, "agreement_id", None) != agreement.id:
            obj.agreement = agreement

        if _has_db_field(obj, "amount"):
            try:
                obj.amount = Decimal("0.00")
            except Exception:
                obj.amount = 0

        if not getattr(obj, "order", None):
            try:
                obj.order = (agreement.milestones.count() or 0) + 1
            except Exception:
                obj.order = 1

        obj.save()

    formset.save_m2m()


# ========================= نزاعات =========================
@login_required
@transaction.atomic
def close_dispute_view(request: HttpRequest, request_id: int) -> HttpResponse:
    req = get_object_or_404(Request, pk=request_id)
    if not _is_admin(request.user):
        return HttpResponseForbidden("غير مصرح لك بإغلاق النزاع.")
    req.close_dispute()
    messages.success(request, "تم إغلاق النزاع واستئناف الطلب.")
    return redirect("marketplace:request_detail", pk=req.pk)


# ========================= Agreement: فتح/تفاصيل =========================
@login_required
def open_by_request(request: HttpRequest, request_id: int) -> HttpResponse:
    req = get_object_or_404(
        Request.objects.select_related("assigned_employee", "client"),
        pk=request_id,
    )

    ag = getattr(req, "agreement", None)
    if ag:
        messages.info(request, "تم فتح الاتفاقية الموجودة.")
        return redirect("agreements:detail", pk=ag.pk)

    if not _is_emp_or_admin(request.user):
        messages.error(request, "غير مصرح بإنشاء اتفاقية لهذا الطلب.")
        return redirect("marketplace:request_detail", pk=req.pk)

    selected = _get_selected_offer(req)
    if not selected:
        messages.error(request, "لا يمكن إنشاء اتفاقية بدون وجود عرض مختار.")
        return redirect("marketplace:request_detail", pk=req.pk)

    # استخدم أول قيمة موجبة (أكبر من 0) من المعدل أو المقترح، وإلا الافتراضي 7
    duration = (
        selected.modified_duration_days if (selected.modified_duration_days and selected.modified_duration_days > 0)
        else selected.proposed_duration_days if (selected.proposed_duration_days and selected.proposed_duration_days > 0)
        else 7
    )
    if not duration or duration <= 0:
        duration = 7
    price = (
        selected.modified_price
        if selected.modified_price is not None
        else (selected.proposed_price or Decimal("0.00"))
    )
    ag = Agreement.objects.create(
        request=req,
        employee=(getattr(req, "assigned_employee", None) or selected.employee or request.user),
        title=req.title or f"اتفاقية طلب #{req.pk}",
        duration_days=duration,
        total_amount=price,
        status=Agreement.Status.DRAFT,
    )

    messages.success(request, "تم إنشاء مسودة الاتفاقية. يمكنك تحريرها وإرسالها للعميل.")
    return redirect("agreements:edit", pk=ag.pk)


@login_required
def detail(request: HttpRequest, pk: int) -> HttpResponse:
    ag = get_object_or_404(
        Agreement.objects.select_related("request", "employee", "request__client")
        .prefetch_related("clause_items__clause", "milestones"),
        pk=pk,
    )
    req = ag.request
    user = request.user

    allowed = (
        user.id == req.client_id
        or user.id == getattr(req, "assigned_employee_id", None)
        or user.id == ag.employee_id
        or _is_admin(user)
    )
    if not allowed:
        messages.error(request, "غير مصرح بعرض هذه الاتفاقية.")
        return redirect("marketplace:request_detail", pk=req.pk)

    # نستخدم تفكيك الموديل الجديد أولاً، مع fallback لو احتجت
    breakdown = {
        "net_for_employee": ag.employee_net_amount or 0,
        "platform_fee": ag.fee_amount or 0,
        "vat_amount": ag.vat_amount or 0,
        "client_total": ag.grand_total or 0,
    }
    cfg = None
    try:
        from finance.utils import get_finance_cfg
        cfg = get_finance_cfg()
    except Exception:
        cfg = None

    return render(
        request,
        "agreements/agreement_detail.html",
        {
            "agreement": ag,
            "req": req,
            "rejection_reason": ag.rejection_reason,
            "breakdown": breakdown,
            "cfg": cfg,
        },
    )


# ========================= Agreement: تحرير/إرسال =========================
@login_required
@transaction.atomic
def edit(request: HttpRequest, pk: int) -> HttpResponse:
    ag = get_object_or_404(Agreement.objects.select_related("request", "employee"), pk=pk)
    req = ag.request

    if not _is_emp_or_admin(request.user):
        messages.error(request, "غير مصرح بتحرير الاتفاقية.")
        return redirect("agreements:detail", pk=ag.pk)

    selected_offer = getattr(req, "selected_offer", None)
    if selected_offer and ag.total_amount != selected_offer.proposed_price:
        ag.total_amount = selected_offer.proposed_price
        ag.save(update_fields=["total_amount", "updated_at"] if hasattr(ag, "updated_at") else ["total_amount"])

    breakdown = {
        "net_for_employee": ag.employee_net_amount or 0,
        "platform_fee": ag.fee_amount or 0,
        "vat_amount": ag.vat_amount or 0,
        "client_total": ag.grand_total or 0,
    }

    if request.method == "POST":
        post_data = request.POST.copy()
        if not post_data.get("title"):
            post_data["title"] = req.title or f"اتفاقية طلب #{req.pk}"

        action = (post_data.get("action") or "save").strip()  # save | send
        form = AgreementEditForm(post_data, instance=ag)
        formset: BaseFormSet = MilestoneFormSet(post_data, instance=ag)

        extra_errors = []

        try:
            if form.is_valid() and formset.is_valid():
                # حساب مجموع مدد المراحل
                milestones_days = sum(
                    [
                        f.cleaned_data.get("due_days", 0)
                        for f in formset.forms
                        if f.cleaned_data and not f.cleaned_data.get("DELETE")
                    ]
                )

                # تحديث مدة الاتفاقية مباشرة من مجموع مدد المراحل
                ag.duration_days = milestones_days

                # تحقق أن المدة ليست صفرية
                if milestones_days <= 0:
                    form.add_error(None, "يجب أن يكون مجموع مدة الأيام في جميع المراحل أكبر من صفر.")
                else:
                    try:
                        ag = form.save(commit=False)
                        ag.duration_days = milestones_days
                        ag.save()
                        _save_formset_strict(formset, ag)
                    except Exception as e:
                        extra_errors.append(str(e))
                        raise

                    if action == "send":
                        updates = ["status"]
                        ag.status = Agreement.Status.PENDING
                        if hasattr(ag, "updated_at"):
                            ag.updated_at = timezone.now()
                            updates.append("updated_at")
                        ag.save(update_fields=updates)

                        _update_request_status_on_send(req)

                        messages.success(request, "تم حفظ الاتفاقية وإرسالها للعميل.")
                        return redirect("agreements:detail", pk=ag.pk)

                    updates = ["status"]
                    ag.status = Agreement.Status.DRAFT
                    if hasattr(ag, "updated_at"):
                        ag.updated_at = timezone.now()
                        updates.append("updated_at")
                    ag.save(update_fields=updates)

                    messages.success(request, "تم حفظ التعديلات (مسودة).")
                    return redirect("agreements:edit", pk=ag.pk)

        except Exception as e:
            form.add_error(None, f"خطأ أثناء الحفظ: {e}")

        if extra_errors:
            for err in extra_errors:
                form.add_error(None, err)

        messages.error(request, "لم يتم الحفظ. الرجاء تصحيح الأخطاء.")
        return render(
            request,
            "agreements/agreement_form.html",
            {"agreement": ag, "req": req, "form": form, "formset": formset, "breakdown": breakdown},
        )

    initial = {"title": req.title or f"اتفاقية طلب #{req.pk}"}
    form = AgreementEditForm(instance=ag, initial=initial)
    formset: BaseFormSet = MilestoneFormSet(instance=ag)

    return render(
        request,
        "agreements/agreement_form.html",
        {"agreement": ag, "req": req, "form": form, "formset": formset, "breakdown": breakdown},
    )


# ========================= Agreement: قبول/رفض =========================
@login_required
@transaction.atomic
def accept(request: HttpRequest, pk: int) -> HttpResponse:
    ag = get_object_or_404(Agreement.objects.select_related("request"), pk=pk)
    return accept_by_request(request, ag.request.pk)


@login_required
@transaction.atomic
def accept_by_request(request: HttpRequest, request_id: int) -> HttpResponse:
    req = get_object_or_404(
        Request.objects.select_for_update().select_related("client"),
        pk=request_id
    )
    ag = getattr(req, "agreement", None)
    if not ag:
        messages.error(request, "لا توجد اتفاقية لهذا الطلب.")
        return redirect("marketplace:request_detail", pk=req.pk)

    if request.user.id != req.client_id and not _is_admin(request.user):
        messages.error(request, "غير مصرح بالموافقة على هذه الاتفاقية.")
        return redirect("agreements:detail", pk=ag.pk)

    if ag.status == Agreement.Status.ACCEPTED:
        messages.info(request, "الاتفاقية مقبولة مسبقًا.")
        return _go_checkout_or_detail(ag)

    ag.status = Agreement.Status.ACCEPTED
    updates: List[str] = ["status"]
    if hasattr(ag, "accepted_at") and not getattr(ag, "accepted_at", None):
        ag.accepted_at = timezone.now()
        updates.append("accepted_at")
    if hasattr(ag, "updated_at"):
        ag.updated_at = timezone.now()
        updates.append("updated_at")
    ag.save(update_fields=updates)

    try:
        req.accept_agreement_and_wait_payment()
    except Exception:
        req.status = Status.AWAITING_PAYMENT_CONFIRMATION
        req.save(update_fields=["status", "updated_at"])

    messages.success(request, "تمت الموافقة على الاتفاقية. جارٍ تحويلك للدفع الآمن.")
    return _go_checkout_or_detail(ag)


@login_required
def reject_by_request(request: HttpRequest, request_id: int) -> HttpResponse:
    req = get_object_or_404(Request.objects.select_related("client"), pk=request_id)
    ag = getattr(req, "agreement", None)
    if not ag:
        messages.error(request, "لا توجد اتفاقية لهذا الطلب.")
        return redirect("marketplace:request_detail", pk=req.pk)

    if request.user.id != req.client_id and not _is_admin(request.user):
        messages.error(request, "غير مصرح برفض هذه الاتفاقية.")
        return redirect("agreements:detail", pk=ag.pk)

    return render(request, "agreements/agreement_reject.html", {"agreement": ag, "req": req})


@login_required
@require_POST
def reject(request: HttpRequest, pk: int) -> HttpResponse:
    ag = get_object_or_404(Agreement.objects.select_related("request"), pk=pk)
    req = ag.request

    if request.user.id != req.client_id and not _is_admin(request.user):
        messages.error(request, "غير مصرح برفض هذه الاتفاقية.")
        return redirect("agreements:detail", pk=ag.pk)

    reason = (request.POST.get("rejection_reason") or request.POST.get("reason") or "").strip()
    if len(reason) < 5:
        messages.error(request, "الرجاء توضيح سبب الرفض (5 أحرف على الأقل).")
        return render(request, "agreements/agreement_reject.html", {"agreement": ag, "req": req})

    updates = ["rejection_reason", "status"]
    ag.rejection_reason = reason[:1000]
    ag.status = Agreement.Status.REJECTED
    if hasattr(ag, "updated_at"):
        ag.updated_at = timezone.now()
        updates.append("updated_at")
    ag.save(update_fields=updates)

    _return_request_to_offer_selected(req)

    messages.success(request, "تم رفض الاتفاقية وإعادتها إلى مرحلة العروض.")
    return redirect("agreements:detail", pk=ag.pk)


# ==================== Milestones: تسليم/اعتماد/رفض/مراجعة ====================
@login_required
@require_POST
@transaction.atomic
def milestone_deliver(request: HttpRequest, milestone_id: int, *args, **kwargs) -> HttpResponse:
    ms = (
        Milestone.objects.select_related("agreement__request", "agreement")
        .select_for_update()
        .get(pk=milestone_id)
    )
    req = ms.agreement.request

    is_admin = _is_admin(request.user)
    if (getattr(req, "is_frozen", False) or str(getattr(req, "status", "")).lower() == "disputed") and not is_admin:
        messages.error(request, "لا يمكن تسليم المرحلة: الطلب في حالة نزاع.")
        return _redirect_to_request_detail(ms)

    is_assigned_employee = request.user.id == getattr(req, "assigned_employee_id", None)
    if not (is_admin or is_assigned_employee):
        messages.error(request, "ليست لديك صلاحية لتسليم هذه المرحلة.")
        return _redirect_to_request_detail(ms)

    note = (request.POST.get("note") or "").strip()

    try:
        ms.mark_delivered(note=note)
    except ValidationError as ve:
        messages.error(request, ve.message if hasattr(ve, "message") else str(ve))
        return _redirect_to_request_detail(ms)
    except Exception as exc:
        logger.exception("milestone_deliver failed (milestone_id=%s): %s", milestone_id, exc)
        messages.error(request, "حدث خطأ أثناء تسليم المرحلة.")
        return _redirect_to_request_detail(ms)

    messages.success(request, "تم تسليم المرحلة — أُرسلت للمراجعة لدى العميل.")
    return _redirect_to_request_detail(ms)


@require_role("client")
@require_POST
@transaction.atomic
def milestone_review(request: HttpRequest, agreement_id: int, milestone_id: int) -> HttpResponse:
    ms = get_object_or_404(
        Milestone.objects.select_related("agreement__request", "agreement"),
        pk=milestone_id,
        agreement_id=agreement_id,
    )
    req = ms.agreement.request
    if getattr(req, "is_frozen", False):
        return HttpResponseForbidden("الطلب في نزاع")

    action = (request.POST.get("action") or "").strip()  # approve / reject

    try:
        if action == "approve":
            ms.approve(request.user)
            messages.success(request, "تم اعتماد المرحلة.")
        else:
            reason = (request.POST.get("reason") or "").strip()
            ms.reject(reason)
            messages.warning(request, "تم رفض المرحلة. يرجى توضيح المطلوب للموظف")
    except ValidationError as ve:
        messages.error(request, ve.message if hasattr(ve, "message") else str(ve))
    except Exception as exc:
        logger.exception("milestone_review failed (milestone_id=%s): %s", milestone_id, exc)
        messages.error(request, "حدث خطأ أثناء مراجعة المرحلة.")

    return redirect("marketplace:request_detail", pk=req.pk)


@login_required
@transaction.atomic
def milestone_approve(request: HttpRequest, milestone_id: int, *args, **kwargs) -> HttpResponse:
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    ms = get_object_or_404(
        Milestone.objects.select_related("agreement__request", "agreement"),
        pk=milestone_id,
    )
    req = ms.agreement.request

    is_admin = _is_admin(request.user)
    is_request_client = (request.user.id == getattr(req, "client_id", None))
    if not (is_admin or is_request_client):
        return HttpResponseForbidden("ليست لديك صلاحية لاعتماد هذه المرحلة")

    try:
        ms.approve(request.user)
    except ValidationError as ve:
        messages.error(request, ve.message if hasattr(ve, "message") else str(ve))
        return _redirect_to_request_detail(ms)
    except Exception as exc:
        logger.exception("milestone_approve failed (milestone_id=%s): %s", milestone_id, exc)
        messages.error(request, "حدث خطأ غير متوقع أثناء اعتماد المرحلة.")
        return _redirect_to_request_detail(ms)

    # توافق: لو الفاتورة مدفوعة والطلب ما تحول للتنفيذ لأي سبب
    try:
        agreement = ms.agreement
        if agreement and agreement.invoices_all_paid:
            _touch_request_in_progress(req)
    except Exception:
        pass

    messages.success(request, "تم اعتماد المرحلة.")
    return _redirect_to_request_detail(ms)


@login_required
@transaction.atomic
def milestone_reject(request: HttpRequest, milestone_id: int, *args, **kwargs) -> HttpResponse:
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    ms = get_object_or_404(
        Milestone.objects.select_related("agreement__request", "agreement"),
        pk=milestone_id
    )
    req = ms.agreement.request

    is_admin = _is_admin(request.user)
    is_request_client = (request.user.id == getattr(req, "client_id", None))
    if not (is_admin or is_request_client):
        return HttpResponseForbidden("ليست لديك صلاحية لرفض هذه المرحلة")

    reason = (request.POST.get("reason") or "").strip()
    if len(reason) < 3:
        messages.error(request, "فضلاً أدخل سببًا واضحًا (٣ أحرف على الأقل).")
        return _redirect_to_request_detail(ms)

    try:
        ms.reject(reason)
    except ValidationError as ve:
        messages.error(request, ve.message if hasattr(ve, "message") else str(ve))
        return _redirect_to_request_detail(ms)
    except Exception as exc:
        logger.exception("milestone_reject failed (milestone_id=%s): %s", milestone_id, exc)
        messages.error(request, "حدث خطأ أثناء رفض المرحلة.")
        return _redirect_to_request_detail(ms)

    messages.info(request, "تم رفض المرحلة. يمكن للموظف إعادة التسليم بعد التصحيح.")
    return _redirect_to_request_detail(ms)


# ========================= واجهات بديلة توافقية =========================
@require_role("employee", "manager")
@transaction.atomic
def agreement_edit(request: HttpRequest, request_id: int) -> HttpResponse:
    req = get_object_or_404(Request, pk=request_id)
    if getattr(req, "is_frozen", False):
        return HttpResponseForbidden("الطلب في نزاع")
    if request.user != getattr(req, "assigned_employee", None) and not _is_admin(request.user):
        return HttpResponseForbidden("غير مسموح")


    ag, _ = Agreement.objects.get_or_create(
        request=req,
        defaults={
            "title": req.title or f"اتفاقية طلب #{req.pk}",
            "total_amount": Decimal("0.00"),
            "status": Agreement.Status.DRAFT,
        },
    )

    # منع تحرير الاتفاقية إذا كانت مقبولة
    if ag.status == Agreement.Status.ACCEPTED:
        messages.error(request, "لا يمكن تعديل الاتفاقية بعد قبولها من العميل.")
        return redirect("agreements:detail", pk=ag.pk)

    breakdown = {
        "net_for_employee": ag.employee_net_amount or 0,
        "platform_fee": ag.fee_amount or 0,
        "vat_amount": ag.vat_amount or 0,
        "client_total": ag.grand_total or 0,
    }

    if request.method == "POST":
        action = (request.POST.get("action") or "save").strip()
        form = AgreementEditForm(request.POST, instance=ag)
        formset: BaseFormSet = MilestoneFormSet(request.POST, instance=ag)

        if form.is_valid() and formset.is_valid():
            duration_days = form.cleaned_data.get("duration_days") or ag.duration_days or 0
            milestones_days = sum(
                [
                    f.cleaned_data.get("due_days", 0)
                    for f in formset.forms
                    if f.cleaned_data and not f.cleaned_data.get("DELETE")
                ]
            )
            if duration_days != milestones_days:
                form.add_error(None, "مجموع مدة الأيام المتفق عليها يجب أن يساوي مجموع مدة الأيام في جميع المراحل.")
            else:
                ag = form.save()
                _save_formset_strict(formset, ag)

                if action == "send":
                    updates = ["status"]
                    ag.status = Agreement.Status.PENDING
                    if hasattr(ag, "updated_at"):
                        ag.updated_at = timezone.now()
                        updates.append("updated_at")
                    ag.save(update_fields=updates)

                    _update_request_status_on_send(req)

                    messages.success(request, "تم إرسال الاتفاقية للعميل.")
                    return redirect("marketplace:request_detail", pk=req.pk)

                updates = ["status"]
                ag.status = Agreement.Status.DRAFT
                if hasattr(ag, "updated_at"):
                    ag.updated_at = timezone.now()
                    updates.append("updated_at")
                ag.save(update_fields=updates)

                messages.success(request, "تم الحفظ (مسودة).")
                return redirect("agreements:agreement_edit", request_id=req.pk)

        messages.error(request, "لم يتم الحفظ. تأكد من صحة الحقول.")
        return render(
            request,
            "agreements/agreement_form.html",
            {"agreement": ag, "req": req, "form": form, "formset": formset, "breakdown": breakdown},
        )

    form = AgreementEditForm(instance=ag)
    formset: BaseFormSet = MilestoneFormSet(instance=ag)
    return render(
        request,
        "agreements/agreement_form.html",
        {"agreement": ag, "req": req, "form": form, "formset": formset, "breakdown": breakdown},
    )


@login_required
@transaction.atomic
def finalize_clauses(request: HttpRequest, pk: int) -> HttpResponse:
    ag = get_object_or_404(Agreement.objects.select_related("request"), pk=pk)

    if request.user != getattr(ag.request, "assigned_employee", None) and not _is_admin(request.user):
        return HttpResponseForbidden("غير مسموح")

    if request.method == "POST":
        form = AgreementClauseSelectForm(request.POST)
        if not form.is_valid():
            messages.error(request, "تعذّر حفظ البنود. تأكد من الاختيارات.")
            return render(
                request,
                "agreements/finalize_clauses.html",
                {"agreement": ag, "form": form},
                status=400,
            )

        AgreementClauseItem.objects.filter(agreement=ag).delete()

        chosen = list(form.cleaned_data.get("clauses") or [])
        custom_lines = form.cleaned_custom_lines()

        items: list[AgreementClauseItem] = []
        for c in chosen:
            items.append(AgreementClauseItem(agreement=ag, clause=c, custom_text=""))
        for line in custom_lines:
            items.append(AgreementClauseItem(agreement=ag, clause=None, custom_text=line))

        for idx, item in enumerate(items, start=1):
            item.position = idx

        if items:
            AgreementClauseItem.objects.bulk_create(items)

        messages.success(request, "تم تثبيت البنود بنجاح.")
        try:
            return redirect("agreements:agreement_edit", request_id=ag.request_id)
        except NoReverseMatch:
            return redirect("agreements:edit", pk=ag.pk)

    form = AgreementClauseSelectForm()
    return render(request, "agreements/finalize_clauses.html", {"agreement": ag, "form": form})


@require_role("client")
@transaction.atomic
def agreement_decide(request: HttpRequest, request_id: int) -> HttpResponse:
    req = get_object_or_404(Request, pk=request_id)
    if getattr(req, "is_frozen", False):
        return HttpResponseForbidden("الطلب في نزاع")

    ag = getattr(req, "agreement", None)
    if not ag:
        messages.error(request, "لا توجد اتفاقية لهذا الطلب.")
        return redirect("marketplace:request_detail", pk=req.pk)

    action = (request.POST.get("action") or "").strip()  # accept / reject

    if action == "accept":
        updates = ["status"]
        ag.status = Agreement.Status.ACCEPTED
        if hasattr(ag, "updated_at"):
            ag.updated_at = timezone.now()
            updates.append("updated_at")
        ag.save(update_fields=updates)

        try:
            if hasattr(req, "start_awaiting_payment"):
                req.start_awaiting_payment()
            else:
                _move_request_on_accept(req)
        except Exception:
            _move_request_on_accept(req)

        messages.success(request, "تم قبول الاتفاقية. جارٍ تحويلك للدفع الآمن.")
        return _go_checkout_or_detail(ag)

    reason = (request.POST.get("reason") or "").strip()
    if not reason:
        messages.error(request, "اذكر سبب الرفض.")
        return redirect("agreements:detail", pk=ag.pk)

    ag.rejection_reason = reason[:1000]
    ag.status = Agreement.Status.REJECTED
    updates = ["rejection_reason", "status"]
    if hasattr(ag, "updated_at"):
        ag.updated_at = timezone.now()
        updates.append("updated_at")
    ag.save(update_fields=updates)

    _return_request_to_offer_selected(req)

    messages.warning(request, "تم رفض الاتفاقية. يمكنك طلب تعديلها.")
    return redirect("agreements:detail", pk=ag.pk)


# =========================== تحويل للدفع/تفاصيل ===========================
def _go_checkout_or_detail(ag: Agreement) -> HttpResponse:
    try:
        return redirect("finance:checkout_agreement", agreement_id=ag.pk)
    except NoReverseMatch:
        try:
            return redirect("finance:checkout_agreement", request_id=ag.request_id)
        except NoReverseMatch:
            return redirect("agreements:detail", pk=ag.pk)
