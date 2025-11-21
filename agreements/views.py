# agreements/views.py
from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal
from typing import List

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.forms.formsets import BaseFormSet
from django.http import (
    HttpRequest,
    HttpResponse,
    HttpResponseForbidden,
    HttpResponseNotAllowed,
)
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse, NoReverseMatch
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.permissions import require_role
from finance.models import Invoice
from marketplace.models import Request, Offer

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
        req.offers.filter(status=getattr(Offer.Status, "SELECTED", "selected"))
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
    new_status = getattr(Request.Status, "AGREEMENT_PENDING", "agreement_pending")
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
    in_progress = getattr(Request.Status, "IN_PROGRESS", "in_progress")
    updates = ["status"]
    req.status = in_progress
    if _has_db_field(req, "updated_at"):
        req.updated_at = timezone.now()
        updates.append("updated_at")
    req.save(update_fields=updates)


def _touch_request_in_progress(req: Request) -> None:
    early = {
        getattr(Request.Status, "NEW", "new"),
        getattr(Request.Status, "OFFER_SELECTED", "offer_selected"),
        getattr(Request.Status, "AGREEMENT_PENDING", "agreement_pending"),
    }
    try:
        if getattr(req, "status", None) in early:
            _move_request_on_accept(req)
    except Exception as exc:
        logger.warning("_touch_request_in_progress failed req=%s: %s", getattr(req, "pk", None), exc)


def _return_request_to_offer_selected(req: Request) -> None:
    if hasattr(Request, "Status") and hasattr(Request.Status, "OFFER_SELECTED"):
        req.status = Request.Status.OFFER_SELECTED
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
    """
    حفظ formset للمراحل مع تنفيذ الحذف/الإضافة فعليًا (بدون إخفاء)،
    وضمان amount=0 لكل مرحلة (حسب السياسة الجديدة).
    """
    # حفظ كائنات بدون commit لمعالجة القيم الإضافية
    instances = formset.save(commit=False)

    # احذف ما تم تعليمُه بالحذف
    for obj in getattr(formset, "deleted_objects", []):
        try:
            obj.delete()
        except Exception as exc:
            logger.warning("فشل حذف المرحلة id=%s: %s", getattr(obj, "pk", None), exc)

    # عيّن الاتفاقية للمراحل الجديدة، واضبط amount=0 وآمن الحقول
    for obj in instances:
        if getattr(obj, "agreement_id", None) != agreement.id:
            obj.agreement = agreement
        # إجبار المبلغ صفرًا وفق التحديثات الأخيرة
        if _has_db_field(obj, "amount"):
            try:
                obj.amount = Decimal("0.00")
            except Exception:
                obj.amount = 0
        # ترتيب احتياطي إن لم يأتِ من الحقول
        if not getattr(obj, "order", None):
            try:
                obj.order = (agreement.milestones.count() or 0) + 1
            except Exception:
                obj.order = 1
        obj.save()

    # احفظ أي نماذج many-to-many لو وُجدت
    formset.save_m2m()


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

    ag = Agreement.objects.create(
        request=req,
        employee=(getattr(req, "assigned_employee", None) or selected.employee or request.user),
        title=req.title or f"اتفاقية طلب #{req.pk}",
        duration_days=selected.proposed_duration_days or 7,
        total_amount=selected.proposed_price or Decimal("0.00"),
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

    # حساب breakdown وcfg بنفس منطق شاشة التعديل
    from finance.utils import calculate_financials_from_net, get_finance_cfg
    net_amount = ag.total_amount or 0
    if not net_amount or net_amount == 0:
        breakdown = {
            "net_for_employee": 0,
            "platform_fee": 0,
            "vat_amount": 0,
            "client_total": 0,
        }
        cfg = None
    else:
        cfg = get_finance_cfg()
        breakdown = calculate_financials_from_net(
            net_amount,
            platform_fee_percent=cfg.platform_fee_percent,
            vat_rate=cfg.vat_rate,
        )

    return render(
        request,
        "agreements/agreement_detail.html",
        {"agreement": ag, "req": req, "rejection_reason": ag.rejection_reason, "breakdown": breakdown, "cfg": cfg},
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


    # مزامنة مبلغ الاتفاقية مع العرض المختار دائماً
    selected_offer = getattr(req, "selected_offer", None)
    if selected_offer and ag.total_amount != selected_offer.proposed_price:
        ag.total_amount = selected_offer.proposed_price
        ag.save(update_fields=["total_amount", "updated_at"] if hasattr(ag, "updated_at") else ["total_amount"])

    from finance.utils import calculate_financials_from_net, get_finance_cfg
    net_amount = ag.total_amount or 0
    if not net_amount or net_amount == 0:
        breakdown = {
            "net_for_employee": 0,
            "platform_fee": 0,
            "vat_amount": 0,
            "client_total": 0,
        }
    else:
        cfg = get_finance_cfg()
        breakdown = calculate_financials_from_net(
            net_amount,
            platform_fee_percent=cfg.platform_fee_percent,
            vat_rate=cfg.vat_rate,
        )

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
                # تحقق من تساوي مجموع الأيام بناءً على القيمة المدخلة
                duration_days = form.cleaned_data["duration_days"]
                milestones_days = sum([
                    f.cleaned_data.get("due_days", 0)
                    for f in formset.forms
                    if f.cleaned_data and not f.cleaned_data.get("DELETE")
                ])
                if duration_days != milestones_days:
                    form.add_error(None, "مجموع مدة الأيام المتفق عليها يجب أن يساوي مجموع مدة الأيام في جميع المراحل.")
                else:
                    try:
                        ag = form.save()
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
            # أضف الخطأ إلى الأخطاء العامة للنموذج
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

    # GET
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
def accept_by_request(request: HttpRequest, request_id: int) -> HttpResponse:
    req = get_object_or_404(Request.objects.select_related("client"), pk=request_id)
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
    updates = ["status"]
    if hasattr(ag, "updated_at"):
        ag.updated_at = timezone.now()
        updates.append("updated_at")
    ag.save(update_fields=updates)

    _move_request_on_accept(req)
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

    reason = (request.POST.get("reason") or "").strip()
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
    """
    يدعم التواقيع:
      - /agreements/<agreement_id>/milestones/<milestone_id>/deliver/
      - /agreements/milestone/<milestone_id>/deliver/
    (الـ agreement_id يُمرَّر من الـ URL لكننا لا نعتمد عليه هنا.)
    """
    ms = Milestone.objects.select_related("agreement__request").select_for_update().get(pk=milestone_id)
    req = ms.agreement.request

    if getattr(req, "is_frozen", False) or str(getattr(req, "status", "")).lower() == "disputed":
        messages.error(request, "لا يمكن تسليم المرحلة: الطلب في حالة نزاع.")
        return _redirect_to_request_detail(ms)

    is_admin = _is_admin(request.user)
    is_assigned_employee = request.user.id == getattr(req, "assigned_employee_id", None)
    if not (is_admin or is_assigned_employee):
        messages.error(request, "ليست لديك صلاحية لتسليم هذه المرحلة.")
        return _redirect_to_request_detail(ms)

    # منع التسليم بعد الاعتماد/السداد
    try:
        invoice_paid = False
        inv = getattr(ms, "invoice", None)
        if inv is not None:
            invoice_paid = (str(getattr(inv, "status", "")).lower() == "paid")
    except Exception:
        invoice_paid = False

    if ms.is_approved or ms.is_paid or invoice_paid:
        messages.info(request, "لا يمكن تسليم المرحلة بعد اعتمادها أو سدادها.")
        return _redirect_to_request_detail(ms)

    note = (request.POST.get("note") or "").strip()
    updates: List[str] = []
    now = timezone.now()
    _set_db_field(ms, "delivered_at", now, updates)
    _set_db_field(ms, "delivered_note", note, updates)
    _set_db_field(ms, "status", Milestone.Status.DELIVERED, updates)
    _set_db_field(ms, "approved_at", None, updates)
    _set_db_field(ms, "rejected_reason", "", updates)
    ms.save(update_fields=updates or None)

    _touch_request_in_progress(req)
    messages.success(request, "تم تسليم المرحلة — أُرسلت للمراجعة لدى العميل.")
    return _redirect_to_request_detail(ms)


@require_role("client")
@transaction.atomic
def milestone_review(request: HttpRequest, agreement_id: int, milestone_id: int) -> HttpResponse:
    m = get_object_or_404(
        Milestone.objects.select_related("agreement__request"),
        pk=milestone_id,
        agreement_id=agreement_id,
    )
    req = m.agreement.request
    if getattr(req, "is_frozen", False):
        return HttpResponseForbidden("الطلب في نزاع")

    action = (request.POST.get("action") or "").strip()  # approve / reject
    if action == "approve":
        updates: List[str] = []
        _set_db_field(m, "approved_at", timezone.now(), updates)
        _set_db_field(m, "status", Milestone.Status.APPROVED, updates)
        _set_db_field(m, "rejected_reason", "", updates)
        m.save(update_fields=updates or None)
        messages.success(request, "تم اعتماد المرحلة.")
    else:
        reason = (request.POST.get("reason") or "").strip()
        if not reason:
            messages.error(request, "اذكر سبب الرفض")
            return redirect("marketplace:request_detail", pk=req.pk)
        updates: List[str] = []
        _set_db_field(m, "status", Milestone.Status.REJECTED, updates)
        _set_db_field(m, "rejected_reason", reason[:500], updates)
        m.save(update_fields=updates or None)
        messages.warning(request, "تم رفض المرحلة. يرجى توضيح المطلوب للموظف")

    return redirect("marketplace:request_detail", pk=req.pk)


@login_required
@transaction.atomic
def milestone_approve(request: HttpRequest, milestone_id: int, *args, **kwargs) -> HttpResponse:
    """نسخة بديلة للاعتماد تستهدف العميل/الإدارة، وتعمل مع المسارين الطويل/القصير."""
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

    if not ms.is_pending_review:
        messages.warning(request, "لا يمكن اعتماد المرحلة في وضعها الحالي.")
        return _redirect_to_request_detail(ms)

    if ms.is_paid:
        messages.info(request, "هذه المرحلة مدفوعة بالفعل.")
        return _redirect_to_request_detail(ms)

    try:
        ms_updates: List[str] = []
        _set_db_field(ms, "approved_at", timezone.now(), ms_updates)
        _set_db_field(ms, "status", Milestone.Status.APPROVED, ms_updates)
        ms.save(update_fields=ms_updates or None)

        # لا تحول الطلب إلى قيد التنفيذ إلا إذا كانت فاتورة الاتفاقية مدفوعة
        agreement = getattr(ms, "agreement", None)
        invoice = getattr(agreement, "invoice", None) if agreement else None
        invoice_paid = False
        if invoice and hasattr(invoice, "status"):
            PAID_VAL = getattr(getattr(invoice.__class__, "Status", None), "PAID", "paid")
            invoice_paid = (getattr(invoice, "status", None) or "").lower() == (PAID_VAL or "").lower()
        if invoice_paid:
            _touch_request_in_progress(req)

    except Exception as exc:
        logger.exception("milestone_approve failed (milestone_id=%s): %s", milestone_id, exc)
        messages.error(request, "حدث خطأ غير متوقع أثناء اعتماد المرحلة.")
        return _redirect_to_request_detail(ms)

    messages.success(request, "تم اعتماد المرحلة.")
    return _redirect_to_request_detail(ms)


@login_required
@transaction.atomic
def milestone_reject(request: HttpRequest, milestone_id: int, *args, **kwargs) -> HttpResponse:
    """نسخة بديلة للرفض تستهدف العميل/الإدارة، وتعمل مع المسارين الطويل/القصير."""
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])

    ms = get_object_or_404(Milestone.objects.select_related("agreement__request"), pk=milestone_id)
    req = ms.agreement.request

    is_admin = _is_admin(request.user)
    is_request_client = (request.user.id == getattr(req, "client_id", None))
    if not (is_admin or is_request_client):
        return HttpResponseForbidden("ليست لديك صلاحية لرفض هذه المرحلة")

    if not ms.is_pending_review:
        messages.warning(request, "لا يمكن رفض المرحلة في وضعها الحالي.")
        return _redirect_to_request_detail(ms)

    reason = (request.POST.get("reason") or "").strip()
    if len(reason) < 3:
        messages.error(request, "فضلاً أدخل سببًا واضحًا (٣ أحرف على الأقل).")
        return _redirect_to_request_detail(ms)

    updates: List[str] = []
    _set_db_field(ms, "status", Milestone.Status.REJECTED, updates)
    _set_db_field(ms, "rejected_reason", reason[:500], updates)
    ms.save(update_fields=updates or None)

    messages.info(request, "تم رفض المرحلة. يمكن للموظف إعادة التسليم بعد التصحيح.")
    return _redirect_to_request_detail(ms)


# ========================= واجهات بديلة توافقية =========================
@require_role("employee", "manager")
@transaction.atomic
def agreement_edit(request: HttpRequest, request_id: int) -> HttpResponse:
    """
    محرر بديل وفق request_id للحفاظ على التوافق مع مسارات قديمة.
    """
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


    from finance.utils import calculate_financials_from_net, get_finance_cfg
    # استخدم دائماً مبلغ الاتفاقية الرسمي ونسب المالية من الإعدادات
    net_amount = ag.total_amount or 0
    if not net_amount or net_amount == 0:
        breakdown = {
            "net_for_employee": 0,
            "platform_fee": 0,
            "vat_amount": 0,
            "client_total": 0,
        }
    else:
        cfg = get_finance_cfg()
        breakdown = calculate_financials_from_net(
            net_amount,
            platform_fee_percent=cfg.platform_fee_percent,
            vat_rate=cfg.vat_rate,
        )

    if request.method == "POST":
        action = (request.POST.get("action") or "save").strip()
        form = AgreementEditForm(request.POST, instance=ag)
        formset: BaseFormSet = MilestoneFormSet(request.POST, instance=ag)
        if form.is_valid() and formset.is_valid():
            duration_days = ag.duration_days if not hasattr(form, "cleaned_data") or "duration_days" not in form.cleaned_data else form.cleaned_data["duration_days"]
            milestones_days = sum([
                f.cleaned_data.get("due_days", 0)
                for f in formset.forms
                if f.cleaned_data and not f.cleaned_data.get("DELETE")
            ])
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
    """
    صفحة تثبيت/تعديل البنود الجاهزة والمخصّصة للاتفاقية.
    يعمل مع المسار: /agreements/<pk>/finalize-clauses/
    """
    ag = get_object_or_404(Agreement.objects.select_related("request"), pk=pk)

    # السماح: الموظف المعيّن على الطلب أو الإدارة
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


        # مسح البنود القديمة ثم إنشاء الجديدة
        AgreementClauseItem.objects.filter(agreement=ag).delete()

        chosen = list(form.cleaned_data.get("clauses") or [])
        custom_lines = form.cleaned_custom_lines()

        items: list[AgreementClauseItem] = []
        # اجمع البنود الجاهزة أولاً ثم البنود المخصصة
        for c in chosen:
            items.append(AgreementClauseItem(agreement=ag, clause=c, custom_text=""))
        for line in custom_lines:
            items.append(AgreementClauseItem(agreement=ag, clause=None, custom_text=line))

        # أعد ترقيم البنود: position=1,2,3,...
        for idx, item in enumerate(items, start=1):
            item.position = idx

        if items:
            AgreementClauseItem.objects.bulk_create(items)

        messages.success(request, "تم تثبيت البنود بنجاح.")
        # نُعيد المستخدم لمحرر الاتفاقية (نسخة by-request لضمان السياق)
        try:
            return redirect("agreements:agreement_edit", request_id=ag.request_id)
        except NoReverseMatch:
            return redirect("agreements:edit", pk=ag.pk)

    # GET
    form = AgreementClauseSelectForm()
    return render(request, "agreements/finalize_clauses.html", {"agreement": ag, "form": form})


@require_role("client")
@transaction.atomic
def agreement_decide(request: HttpRequest, request_id: int) -> HttpResponse:
    """
    قرار العميل النهائي (قبول/رفض) عبر request_id — للتوافق مع واجهات قديمة.
    """
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
        # لا تغيّر حالة الطلب هنا، التحويل يتم بعد دفع الفاتورة فقط
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
