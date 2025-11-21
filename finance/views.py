# finance/views.py
from __future__ import annotations

import csv
import hashlib
import hmac
import logging
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from typing import Dict, List, Optional, Tuple

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST, require_http_methods

from agreements.models import Agreement
from marketplace.models import Request

from .forms import FinanceSettingsForm
from .models import FinanceSettings, Invoice
from .permissions import is_finance
from .utils import get_finance_cfg, invalidate_finance_cfg_cache

logger = logging.getLogger(__name__)

# ===========================
# تسعير — استيراد آمن مع Backoff
# ===========================
# تفضيل: finance.services.pricing.breakdown_for_agreement
# بديل احتياطي: finance.pricing.breakdown_for_agreement
try:
    from finance.services.pricing import breakdown_for_agreement  # النوع المفضل
except Exception:
    try:
        # احتياطي إذا كانت وحدة التسعير داخل نفس التطبيق
        from .pricing import breakdown_for_agreement  # type: ignore
    except Exception:
        breakdown_for_agreement = None  # سيتم توليد بديل مبسّط بالأسفل

# ===========================
# إعدادات عامة قابلة للتهيئة
# ===========================
BANK_NAME = getattr(settings, "BANK_NAME", "SAUDI BANK")
BANK_ACCOUNT_NAME = getattr(settings, "BANK_ACCOUNT_NAME", "SamiLink LLC")
BANK_IBAN = getattr(settings, "BANK_IBAN", "SA00 0000 0000 0000 0000 0000")
PAYMENT_WEBHOOK_SECRET = getattr(settings, "PAYMENT_WEBHOOK_SECRET", None)

# ===========================
# أدوات مساعدة عامة
# ===========================
def _first_existing_url(names: List[str]) -> Optional[str]:
    """يرجع أول reverse ناجح من قائمة أسماء URL، أو None إن لم توجد."""
    for n in names:
        try:
            return reverse(n)
        except NoReverseMatch:
            continue
        except Exception:
            continue
    return None


def _q2(v: Decimal) -> Decimal:
    return Decimal(v or 0).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _as_decimal(v) -> Decimal:
    if isinstance(v, Decimal):
        return v
    try:
        return Decimal(str(v))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _mask_iban(iban: str) -> str:
    s = "".join(ch for ch in (iban or "") if ch.isalnum())
    if len(s) <= 8:
        return iban or ""
    return f"{s[:4]} **** **** **** {s[-4:]}"


def _agreement_completed_value() -> str:
    try:
        return getattr(getattr(Agreement, "Status", None), "COMPLETED", "completed")
    except Exception:
        return "completed"


def _set_agreement_completed_safe(ag: Agreement) -> bool:
    """
    وسم الاتفاقية كمكتملة بشكل آمن مع احترام الحقول والاختيارات.
    (قد تُستخدم لاحقًا في سيناريوهات خاصة، لكن لم نَعُد نُكمل الطلب عند السداد فقط.)
    """
    try:
        if not hasattr(ag, "status"):
            return False

        status_val = _agreement_completed_value()
        try:
            sf = ag._meta.get_field("status")
            choices = getattr(sf, "choices", ()) or ()
            if choices:
                valid = {c[0] for c in choices}
                if status_val not in valid:
                    return False
        except Exception:
            pass

        updates = ["status"]
        ag.status = status_val
        if hasattr(ag, "updated_at"):
            ag.updated_at = timezone.now()
            updates.append("updated_at")
        ag.save(update_fields=updates)
        return True
    except Exception:
        logger.exception(
            "failed to set agreement completed safely (agreement_id=%s)",
            getattr(ag, "id", None),
        )
        return False


def _is_finance(user) -> bool:
    """اعتبر المستخدم من المالية إن كان لديه سمة مالية أو staff/superuser."""
    try:
        return bool(
            is_finance(user)
            or getattr(user, "is_staff", False)
            or getattr(user, "is_superuser", False)
        )
    except Exception:
        return False


def _writable_attr(obj, name: str) -> bool:
    """هل الاسم حقل قابل للكتابة (وليس @property)؟"""
    if not hasattr(obj, name):
        return False
    attr = getattr(type(obj), name, None)
    return not isinstance(attr, property)


def _invoice_has_milestone_fk() -> bool:
    """
    هل نموذج Invoice يحتوي على حقل milestone؟
    نستخدمه لتفادي الأخطاء في المشاريع التي حُذفت فيها علاقة المراحل.
    """
    try:
        for f in Invoice._meta.get_fields():
            if getattr(f, "name", None) == "milestone":
                return True
    except Exception:
        pass
    return False


def _mark_agreement_started_and_sync(ag: Agreement) -> None:
    """
    عند سداد فاتورة الاتفاقية:
    - نُحدد أن الاتفاقية قد بدأت (mark_started).
    - نترك sync_request_state يتكفل بتحويل حالة الطلب إلى (in_progress / completed) حسب المراحل.
    """
    try:
        if hasattr(ag, "mark_started"):
            ag.mark_started(save=True)
        if hasattr(ag, "sync_request_state"):
            # save_request=True ليتم تحديث حالة الطلب وفق منطق الاتفاقية
            ag.sync_request_state(save_request=True)
    except Exception:
        logger.exception(
            "failed to mark agreement started and sync request state (agreement_id=%s)",
            getattr(ag, "id", None),
        )


# ===========================
# فترات (تقارير)
# ===========================
def _period_bounds(request: HttpRequest) -> Tuple[Optional[date], Optional[date]]:
    """
    GET['period']: today | 7d | 30d | custom (with from/to YYYY-MM-DD)
    يعود (date_from, date_to) شامِلَين. الافتراضي: آخر 30 يومًا.
    """
    p = (request.GET.get("period") or "").strip()
    today = date.today()
    if p == "today":
        return today, today
    if p == "7d":
        return today - timedelta(days=6), today
    if p == "30d":
        return today - timedelta(days=29), today
    if p == "custom":
        df = (request.GET.get("from") or "").strip()
        dt = (request.GET.get("to") or "").strip()
        try:
            d1 = date.fromisoformat(df) if df else None
        except Exception:
            d1 = None
        try:
            d2 = date.fromisoformat(dt) if dt else None
        except Exception:
            d2 = None
        return d1, d2
    return today - timedelta(days=29), today


# ===========================
# حساب المبالغ للاتفاقية (دفعة واحدة)
# ===========================
def _fallback_agreement_totals(ag: Agreement) -> Dict[str, Decimal]:
    """
    بديل مبسّط إذا لم تتوفر وحدة التسعير:
    P من agreement.total_amount، النِّسب من FinanceSettings.current_rates().
    VAT على (P + عمولة المنصة).
    """
    P = _as_decimal(getattr(ag, "total_amount", 0))
    fee, vat = FinanceSettings.current_rates()
    fee_val = _q2(P * fee)
    vat_val = _q2(P * vat)  # الضريبة فقط على قيمة المشروع
    grand = _q2(P + fee_val + vat_val)
    return {
        "P": _q2(P),
        "fee_percent": fee * Decimal("100"),
        "platform_fee": fee_val,
        "taxable": P,  # الأساس الخاضع للضريبة هو قيمة المشروع فقط
        "vat_percent": vat * Decimal("100"),
        "vat_amount": vat_val,
        "grand_total": grand,
    }


def compute_agreement_totals(ag: Agreement) -> Dict[str, Decimal]:
    """
    يعتمد أولاً على finance.services.pricing.breakdown_for_agreement (إن وُجد)،
    وإلا يسقط إلى بديل مبسّط آمن.
    يرجع: P, fee_percent(٪), platform_fee, taxable, vat_percent(٪), vat_amount, grand_total.
    """
    if callable(breakdown_for_agreement):
        try:
            bd = breakdown_for_agreement(ag)  # يجب أن يعيد كائنًا بخصائص أدناه
            return {
                "P": _q2(bd.project_price),
                "fee_percent": _q2(bd.fee_percent * Decimal("100")),
                "platform_fee": _q2(bd.platform_fee_value),
                "taxable": _q2(bd.taxable_base),
                "vat_percent": _q2(bd.vat_rate * Decimal("100")),
                "vat_amount": _q2(bd.vat_amount),
                "grand_total": _q2(bd.client_total),
            }
        except Exception:
            logger.exception("pricing.breakdown_for_agreement failed — using fallback")
    return _fallback_agreement_totals(ag)


def _build_invoice_summary(qs, paid_val: str, unpaid_val: str) -> Dict[str, Decimal]:
    """
    ملخص تفصيلي لمجموعة فواتير:
    - مجموع مدفوعات العميل (client_total/paid/unpaid/refunded)
    - مجموع عمولة المنصة والضريبة
    - مجموع مستحقات الموظف (employee_total/paid/unpaid/cancelled)
    يُعتمد على compute_agreement_totals حتى لو لم تُخزَّن القيم في الفاتورة.
    """
    cancelled_val = getattr(getattr(Invoice, "Status", None), "CANCELLED", "cancelled")
    refunded_val = getattr(getattr(Invoice, "Status", None), "REFUNDED", "refunded")

    summary = {
        "count": Decimal("0"),
        "client_total": Decimal("0.00"),
        "client_paid": Decimal("0.00"),
        "client_unpaid": Decimal("0.00"),
        "client_refunded": Decimal("0.00"),
        "platform_fee_total": Decimal("0.00"),
        "vat_total": Decimal("0.00"),
        "employee_total": Decimal("0.00"),
        "employee_paid": Decimal("0.00"),
        "employee_unpaid": Decimal("0.00"),
        "employee_cancelled": Decimal("0.00"),
    }

    inv_list = list(qs.select_related("agreement"))
    for inv in inv_list:
        ag = getattr(inv, "agreement", None)
        if not ag:
            continue

        try:
            totals = compute_agreement_totals(ag)
        except Exception:
            logger.exception(
                "failed to compute agreement totals in summary (agreement_id=%s)",
                getattr(ag, "id", None),
            )
            continue

        P = _as_decimal(totals.get("P", 0))
        fee = _as_decimal(totals.get("platform_fee", 0))
        vat = _as_decimal(totals.get("vat_amount", 0))
        client_total = _as_decimal(totals.get("grand_total", 0))

        summary["count"] += Decimal("1")
        summary["client_total"] += client_total
        summary["platform_fee_total"] += fee
        summary["vat_total"] += vat
        summary["employee_total"] += P

        status = (getattr(inv, "status", "") or "").lower()
        if status == (paid_val or "").lower():
            summary["client_paid"] += client_total
            summary["employee_paid"] += P
        elif status == (unpaid_val or "").lower():
            summary["client_unpaid"] += client_total
            summary["employee_unpaid"] += P
        elif status in {(cancelled_val or "").lower(), (refunded_val or "").lower()}:
            summary["client_refunded"] += client_total
            summary["employee_cancelled"] += P

    # كاختيار يمكنك إرجاع القيم مكّوَنة على خانتين
    for key in list(summary.keys()):
        if key in {"count"}:
            continue
        summary[key] = _q2(summary[key])

    return summary


# ===========================
# يضمن/ينشئ فاتورة واحدة لإجمالي الاتفاقية
# ===========================
@transaction.atomic
def _ensure_single_invoice_with_amount(ag: Agreement, *, amount: Decimal) -> Invoice:
    """
    يضمن وجود فاتورة واحدة فقط للاتفاقية وبالمبلغ الصحيح.
    Idempotent + select_for_update لمنع السباق.
    لا يعتمد على وجود علاقة milestone في Invoice.
    """
    unpaid_val = getattr(getattr(Invoice, "Status", None), "UNPAID", "unpaid")
    paid_val = getattr(getattr(Invoice, "Status", None), "PAID", "paid")

    has_milestone = _invoice_has_milestone_fk()

    base_qs = Invoice.objects.select_for_update().filter(agreement=ag)
    if has_milestone:
        base_qs = base_qs.filter(milestone__isnull=True)

    inv = base_qs.order_by("id").first()
    now = timezone.now()
    new_amount = _q2(amount)

    if inv:
        # لا نُعدل المدفوعة؛ فقط نتأكد أن غير المدفوعة بقيمة صحيحة
        if getattr(inv, "status", None) != paid_val and (
            not inv.amount or inv.amount != new_amount
        ):
            inv.amount = new_amount
            updates = ["amount"]

            # مزامنة total_amount مع نفس القيمة (فاتورة دفعة واحدة)
            if hasattr(inv, "total_amount"):
                inv.total_amount = new_amount
                updates.append("total_amount")

            if not getattr(inv, "issued_at", None):
                inv.issued_at = now
                updates.append("issued_at")

            if hasattr(inv, "updated_at"):
                inv.updated_at = now
                updates.append("updated_at")

            inv.save(update_fields=updates)
        return inv

    # إنشاء لأول مرة
    fields = {
        "agreement": ag,
        "amount": new_amount,
        "status": unpaid_val,
        "issued_at": now,
        "method": "bank",
        "ref_code": getattr(ag, "ref_code", "") or f"AG{ag.id}-DEP",
    }
    # لو يوجد حقل total_amount في النموذج، عبيه بنفس القيمة
    if hasattr(Invoice, "total_amount"):
        fields["total_amount"] = new_amount

    inv = Invoice.objects.create(**fields)
    return inv


# ===========================
# صفحات المالية
# ===========================
@login_required
@require_GET
def finance_home(request: HttpRequest):
    if not _is_finance(request.user):
        messages.error(request, "غير مصرح بعرض لوحة المالية.")
        return redirect("website:home")

    paid_val = getattr(getattr(Invoice, "Status", None), "PAID", "paid")
    unpaid_val = getattr(getattr(Invoice, "Status", None), "UNPAID", "unpaid")

    # طلبات قيد التنفيذ
    inprog_val = getattr(getattr(Request, "Status", None), "IN_PROGRESS", "in_progress")
    inprog = Request.objects.filter(status=inprog_val)

    # إجمالي مبالغ الاتفاقيات للطلبات قيد التنفيذ
    total_agreements_amount = (
        Agreement.objects.filter(request__in=inprog).aggregate(s=Sum("total_amount"))["s"]
        or Decimal("0.00")
    )


    # مجاميع الفواتير (باستخدام دالة الحساب الموحدة)
    from finance.utils import calculate_financials_from_net
    invs = Invoice.objects.all().select_related("agreement")
    def _sum_breakdown(qs, key):
        total = Decimal("0.00")
        for inv in qs:
            if inv.agreement:
                net_amount = inv.agreement.p_amount
            else:
                net_amount = inv.amount
            platform_fee_percent = inv.platform_fee_percent if inv.platform_fee_percent else None
            vat_rate = inv.vat_percent if inv.vat_percent else None
            breakdown = calculate_financials_from_net(
                net_amount,
                platform_fee_percent=platform_fee_percent,
                vat_rate=vat_rate,
            )
            total += breakdown.get(key, Decimal("0.00"))
        return total

    paid_total_amount = _sum_breakdown(invs.filter(status=paid_val), "client_total")
    unpaid_total_amount = _sum_breakdown(invs.filter(status=unpaid_val), "client_total")
    fee_all = _sum_breakdown(invs, "platform_fee")
    fee_paid = _sum_breakdown(invs.filter(status=paid_val), "platform_fee")
    vat_all = _sum_breakdown(invs, "vat_amount")
    vat_paid = _sum_breakdown(invs.filter(status=paid_val), "vat_amount")
    # صافي الموظف
    employee_paid_net = _sum_breakdown(invs.filter(status=paid_val), "net_for_employee")
    employee_unpaid_net = _sum_breakdown(invs.filter(status=unpaid_val), "net_for_employee")
    disputed_val = getattr(getattr(Request, "Status", None), "DISPUTED", "disputed")
    employee_held_dispute = _sum_breakdown(invs.filter(agreement__request__status=disputed_val), "net_for_employee")

    # آخر فواتير تحتاج تأكيد (غير مدفوعة + لديها مرجع تحويل)
    pending_bank_confirms = (
        invs.filter(status=unpaid_val)
        .exclude(paid_ref__isnull=True)
        .exclude(paid_ref__exact="")
        .select_related("agreement", "agreement__request")[:10]
    )

    # رابط إعدادات آمن (لمنع NoReverseMatch)
    settings_url = _first_existing_url(
        [
            "finance:settings",
            "accounts:settings",
            "profiles:settings",
            "core:settings",
            "website:settings",
            "settings",
        ]
    )

    ctx = {
        "inprogress_count": inprog.count(),
        "total_agreements_amount": total_agreements_amount,
        "paid_sum": paid_total_amount,
        "unpaid_sum": unpaid_total_amount,
        "platform_fee_total": fee_all,
        "vat_total": vat_all,
        "disputed_total": employee_held_dispute,
        "employee_dues_total": employee_paid_net,
        "pending_bank_confirms": pending_bank_confirms,
        "settings_url": settings_url,  # استخدمه بالقالب بدل {% url 'finance:settings' %}
    }
    return render(request, "finance/home.html", ctx)


@login_required
@require_GET
def inprogress_requests(request: HttpRequest):
    if not _is_finance(request.user):
        messages.error(request, "غير مصرح بعرض هذه الصفحة.")
        return redirect("website:home")

    inprog_val = getattr(getattr(Request, "Status", None), "IN_PROGRESS", "in_progress")
    qs = (
        Request.objects.filter(status=inprog_val)
        .select_related("client", "assigned_employee")
        .order_by("-updated_at", "-id")
    )
    total_reqs = qs.count()
    total_amount = (
        Agreement.objects.filter(request__in=qs).aggregate(s=Sum("total_amount"))["s"]
        or Decimal("0.00")
    )

    unpaid_val = getattr(getattr(Invoice, "Status", None), "UNPAID", "unpaid")
    unpaid_total = (
        Invoice.objects.filter(agreement__request__in=qs, status=unpaid_val).aggregate(
            s=Sum("amount")
        )["s"]
        or Decimal("0.00")
    )

    return render(
        request,
        "finance/inprogress_list.html",
        {
            "requests": qs,
            "total_reqs": total_reqs,
            "total_amount": total_amount,
            "unpaid_total": unpaid_total,
        },
    )


# ===========================
# Checkout (فاتورة واحدة)
# ===========================
@login_required
def checkout_agreement(request: HttpRequest, agreement_id: int):
    """
    شاشة الدفع: تُظهر P + (P×F) + VAT وتُولّد/تثبت فاتورة الإيداع الوحيدة للاتفاقية.
    يسمح للمالك (العميل) أو المالية/الإدارة.
    لا تُغيّر حالة الطلب هنا؛ حالة الطلب تُصبح (in_progress) بعد سداد الفاتورة.
    """
    ag = get_object_or_404(
        Agreement.objects.select_related("request", "employee", "request__client"),
        pk=agreement_id,
    )

    u = request.user
    is_owner = getattr(ag.request, "client_id", None) == u.id
    if not (is_owner or _is_finance(u)):
        messages.error(request, "غير مصرح لك بفتح صفحة الدفع لهذه الاتفاقية.")
        return redirect("website:home")

    # استخدم خصائص الاتفاقية مباشرة
    inv = _ensure_single_invoice_with_amount(ag, amount=ag.grand_total)

    # تمرير breakdown الموحد
    breakdown = compute_agreement_totals(ag)
    ctx = {
        "agreement": ag,
        "invoice": inv,
        "breakdown": breakdown,
        "BANK_NAME": BANK_NAME,
        "BANK_ACCOUNT_NAME": BANK_ACCOUNT_NAME,
        "BANK_IBAN_MASKED": _mask_iban(BANK_IBAN),
        "BANK_IBAN": BANK_IBAN,
    }
    return render(request, "finance/checkout.html", ctx)


# ===========================
# تأكيد مرجع التحويل (لا يوسم كمدفوع)
# ===========================
@login_required
@require_POST
@transaction.atomic
def confirm_bank_transfer(request: HttpRequest, invoice_id: int):
    """
    العميل يُسجّل مرجع التحويل فقط. الوسم كمدفوع يتم من المالية بعد التحقق.
    """
    inv = get_object_or_404(
        Invoice.objects.select_for_update().select_related("agreement", "agreement__request"),
        pk=invoice_id,
    )
    u = request.user
    is_owner = bool(getattr(getattr(inv, "agreement", None), "request", None)) and (
        inv.agreement.request.client_id == u.id
    )
    if not (is_owner or _is_finance(u)):
        messages.error(request, "غير مصرح بتنفيذ هذا الإجراء.")
        return redirect("website:home")

    paid_ref = (request.POST.get("paid_ref") or "").strip()
    if len(paid_ref) < 4:
        messages.error(request, "الرجاء إدخال مرجع تحويل صحيح (4 أحرف/أرقام على الأقل).")
        return redirect(request.META.get("HTTP_REFERER", "/"))

    updates: List[str] = []
    if _writable_attr(inv, "paid_ref"):
        inv.paid_ref = paid_ref[:64]
        updates.append("paid_ref")
    if _writable_attr(inv, "updated_at"):
        inv.updated_at = timezone.now()
        updates.append("updated_at")


    if updates:
        inv.save(update_fields=updates)

    # إشعار المالية (يمكن تطويره لاحقاً ليكون إشعار داخلي أو بريد)
    logger.info(f"[FINANCE] حوالة جديدة بحاجة للمتابعة: Invoice #{inv.pk} - Agreement #{inv.agreement_id} - Ref: {paid_ref}")

    messages.success(
        request,
        "تم تسجيل بيانات الحوالة بنجاح. سيتم مراجعة الحوالة من المالية وتحويل الطلب إلى قيد التنفيذ بعد التأكيد."
    )
    # إعادة العميل إلى صفحة تفاصيل الطلب
    return redirect("marketplace:request_detail", pk=inv.agreement.request_id)


# ===========================
# وسم الفاتورة كمدفوعة
# ===========================
@login_required
@require_POST
@transaction.atomic
def mark_invoice_paid(request: HttpRequest, pk: int):
    if not _is_finance(request.user):
        messages.error(request, "لا تملك صلاحية مالية لتنفيذ هذا الإجراء.")
        return redirect(request.META.get("HTTP_REFERER", "/"))

    inv = get_object_or_404(
        Invoice.objects.select_for_update().select_related("agreement", "agreement__request"),
        pk=pk,
    )
    ag: Agreement | None = getattr(inv, "agreement", None)

    PAID_VAL = getattr(getattr(Invoice, "Status", None), "PAID", "paid")

    # إعادة احتساب الإجماليات احتياطيًا
    try:
        if hasattr(inv, "recompute_totals"):
            inv.recompute_totals()
    except Exception:
        logger.exception("mark_invoice_paid: failed to recompute totals for inv=%s", inv.pk)

    # منع وسم فاتورة إجماليها 0
    try:
        total = Decimal(getattr(inv, "total_amount", 0) or inv.amount or 0)
        if total <= Decimal("0.00"):
            messages.error(request, "لا يمكن وسم فاتورة إجماليها 0 كمدفوعة.")
            return redirect(request.META.get("HTTP_REFERER", "/"))
    except Exception:
        logger.exception("mark_invoice_paid: total_amount check failed for inv=%s", inv.pk)

    # مدفوعة مسبقًا؟
    if (getattr(inv, "status", None) or "").lower() == (PAID_VAL or "").lower():
        messages.info(request, "الفاتورة مدفوعة مسبقًا.")
        return redirect(request.META.get("HTTP_REFERER", "/"))

    try:
        now = timezone.now()
        updates: List[str] = []

        # الحالة والتواريخ
        if _writable_attr(inv, "status"):
            inv.status = PAID_VAL
            updates.append("status")
        if _writable_attr(inv, "paid_at") and not getattr(inv, "paid_at", None):
            inv.paid_at = now
            updates.append("paid_at")
        if _writable_attr(inv, "updated_at"):
            inv.updated_at = now
            updates.append("updated_at")

        # مدخلات اختيارية من الفورم
        method = (request.POST.get("method") or "").strip()
        refcode = (request.POST.get("ref_code") or "").strip()
        paidref = (request.POST.get("paid_ref") or "").strip()

        if method and _writable_attr(inv, "method"):
            inv.method = method[:50]
            updates.append("method")
        if refcode and _writable_attr(inv, "ref_code"):
            inv.ref_code = refcode[:100]
            updates.append("ref_code")
        if paidref and _writable_attr(inv, "paid_ref"):
            inv.paid_ref = paidref[:64]
            updates.append("paid_ref")

        if updates:
            inv.save(update_fields=updates)

        # عند سداد الفاتورة:
        # - نعتبر الاتفاقية بدأت
        # - sync_request_state تتكفل بتحويل حالة الطلب إلى in_progress
        if ag:
            _mark_agreement_started_and_sync(ag)

        messages.success(
            request,
            "تم وسم الفاتورة كمدفوعة، وسيتم بدء تنفيذ الطلب وفق حالة الاتفاقية.",
        )
        return redirect(request.META.get("HTTP_REFERER", "/"))

    except Exception as e:
        logger.exception("فشل وسم الفاتورة كمدفوعة: %s", e)
        transaction.set_rollback(True)
        if getattr(settings, "DEBUG", False):
            messages.error(request, f"تعذّر حفظ الدفع: {type(e).__name__} — {e}")
        else:
            messages.error(
                request, "حدث خطأ غير متوقع أثناء تحديث الدفع. لم يتم حفظ أي تغييرات."
            )
        return redirect(request.META.get("HTTP_REFERER", "/"))


# ===========================
# عرض الفواتير
# ===========================
@login_required
def invoice_detail(request: HttpRequest, pk: int):
    inv = get_object_or_404(
        Invoice.objects.select_related("agreement", "agreement__request"),
        pk=pk,
    )
    # breakdown المالي الموحد (من مبلغ الاتفاقية أو الفاتورة)
    if inv.agreement:
        net_amount = inv.agreement.p_amount
    else:
        net_amount = inv.amount

    # جلب النسب من الفاتورة إذا كانت موجودة، وإلا من الإعدادات
    platform_fee_percent = inv.platform_fee_percent if inv.platform_fee_percent else None
    vat_rate = inv.vat_percent if inv.vat_percent else None

    from finance.utils import calculate_financials_from_net
    breakdown = calculate_financials_from_net(
        net_amount,
        platform_fee_percent=platform_fee_percent,
        vat_rate=vat_rate,
    )

    return render(
        request,
        "finance/invoice_detail.html",
        {
            "inv": inv,
            "invoice": inv,
            "breakdown": breakdown,
        },
    )


@login_required
def invoice_list(request: HttpRequest):
    qs = Invoice.objects.all().select_related("agreement")
    status = (request.GET.get("status") or "").strip().lower()
    PAID_VAL = getattr(getattr(Invoice, "Status", None), "PAID", "paid")
    UNPAID_VAL = getattr(getattr(Invoice, "Status", None), "UNPAID", "unpaid")
    CANCEL_VAL = getattr(getattr(Invoice, "Status", None), "CANCELLED", "cancelled")

    if status in ("unpaid", "paid", "cancelled"):
        map_val = {"unpaid": UNPAID_VAL, "paid": PAID_VAL, "cancelled": CANCEL_VAL}
        qs = qs.filter(status=map_val[status])

    from finance.utils import calculate_financials_from_net
    invoices_with_breakdown = []
    for inv in qs:
        if inv.agreement:
            net_amount = inv.agreement.p_amount
        else:
            net_amount = inv.amount
        platform_fee_percent = inv.platform_fee_percent if inv.platform_fee_percent else None
        vat_rate = inv.vat_percent if inv.vat_percent else None
        breakdown = calculate_financials_from_net(
            net_amount,
            platform_fee_percent=platform_fee_percent,
            vat_rate=vat_rate,
        )
        invoices_with_breakdown.append({
            "inv": inv,
            "breakdown": breakdown,
        })

    ctx = {"invoices": invoices_with_breakdown, "object_list": qs}
    return render(request, "finance/invoice_list.html", ctx)


# ===========================
# تأكيد تحويلات العملاء (مرجع التحويل)
# ===========================
@login_required
@require_GET
def confirm_transfers(request: HttpRequest):
    if not _is_finance(request.user):
        messages.error(request, "غير مصرح بهذه الصفحة.")
        return redirect("website:home")

    unpaid_val = getattr(getattr(Invoice, "Status", None), "UNPAID", "unpaid")
    qs = (
        Invoice.objects.filter(status=unpaid_val)
        .exclude(paid_ref__isnull=True)
        .exclude(paid_ref__exact="")
        .select_related("agreement", "agreement__request")
        .order_by("-issued_at", "-id")
    )

    return render(request, "finance/confirm_transfers.html", {"invoices": qs})


# ===========================
# فواتير اتفاقية محددة (دفعة واحدة)
# ===========================
@login_required
@transaction.atomic
def agreement_invoices(request: HttpRequest, agreement_id: int):
    """
    يضمن وجود فاتورة واحدة فقط للاتفاقية ويعرضها ضمن نفس قالب القائمة.
    لا يُنشئ فواتير للمراحل.
    """
    if not _is_finance(request.user):
        messages.error(request, "غير مصرح بعرض هذه الصفحة.")
        return redirect("website:home")

    ag = get_object_or_404(
        Agreement.objects.select_related("request", "employee").prefetch_related(
            "invoices"
        ),
        pk=agreement_id,
    )

    totals = compute_agreement_totals(ag)
    inv = _ensure_single_invoice_with_amount(ag, amount=totals["grand_total"])

    paid_val = getattr(getattr(Invoice, "Status", None), "PAID", "paid")
    is_paid = (getattr(inv, "status", None) == paid_val) or bool(
        getattr(inv, "is_paid", False)
    )
    summary = {
        "total": inv.amount,
        "unpaid": inv.amount if not is_paid else None,
        "paid": inv.amount if is_paid else None,
    }

    invoices_qs = Invoice.objects.filter(pk=inv.pk)
    ctx = {
        "agreement": ag,
        "invoice": inv,
        "invoices": invoices_qs,
        "totals": summary,
    }
    return render(request, "finance/invoice_list.html", ctx)


# ===========================
# صفحات العميل: مدفوعاتي
# ===========================
@login_required
@require_GET
def client_payments(request: HttpRequest):
    user = request.user
    role = getattr(user, "role", "")
    if role != "client" and not _is_finance(user):
        messages.error(request, "هذه الصفحة مخصّصة للعميل.")
        return redirect("website:home")

    status_q = (request.GET.get("status") or "").strip().lower()  # unpaid/paid/all
    method_q = (request.GET.get("method") or "").strip()
    q = (request.GET.get("q") or "").strip()
    date_from = (request.GET.get("from") or "").strip()
    date_to = (request.GET.get("to") or "").strip()

    paid_val = getattr(getattr(Invoice, "Status", None), "PAID", "paid")
    unpaid_val = getattr(getattr(Invoice, "Status", None), "UNPAID", "unpaid")

    invs = (
        Invoice.objects.select_related("agreement", "agreement__request")
        .filter(agreement__request__client_id=user.id)
        .order_by("-issued_at", "-id")
    )
    if status_q == "unpaid":
        invs = invs.filter(status=unpaid_val)
    elif status_q == "paid":
        invs = invs.filter(status=paid_val)

    if method_q:
        invs = invs.filter(method__iexact=method_q)

    if q:
        invs = invs.filter(
            Q(agreement__request__id__icontains=q)
            | Q(agreement__id__icontains=q)
            | Q(ref_code__icontains=q)
        )
    if date_from:
        invs = invs.filter(issued_at__date__gte=date_from)
    if date_to:
        invs = invs.filter(issued_at__date__lte=date_to)

    totals = invs.aggregate(
        total=Sum("amount"),
        unpaid=Sum("amount", filter=Q(status=unpaid_val)),
        paid=Sum("amount", filter=Q(status=paid_val)),
    )
    methods = (
        Invoice.objects.filter(agreement__request__client_id=user.id)
        .exclude(method__isnull=True)
        .exclude(method__exact="")
        .values_list("method", flat=True)
        .distinct()
        .order_by("method")
    )

    # ملخص تفصيلي للعميل: ما دفعه وما المتبقي وما تم ردّه
    summary = _build_invoice_summary(invs, paid_val=paid_val, unpaid_val=unpaid_val)

    return render(
        request,
        "finance/client_payments.html",
        {
            "invoices": invs,
            "totals": totals,  # تجميع خام على amount
            "summary": summary,  # ملخص P / platform / VAT / client totals
            "status_q": status_q,
            "q": q,
            "date_from": date_from,
            "date_to": date_to,
            "method_q": method_q,
            "methods": methods,
        },
    )


# ===========================
# صفحات الموظف: مستحقاتي
# ===========================
@login_required
@require_GET
def employee_dues(request: HttpRequest):
    user = request.user
    role = getattr(user, "role", "")
    if role != "employee" and not _is_finance(user):
        messages.error(request, "هذه الصفحة مخصّصة للموظف.")
        return redirect("website:home")

    status_q = (request.GET.get("status") or "").strip().lower()
    method_q = (request.GET.get("method") or "").strip()
    q = (request.GET.get("q") or "").strip()
    date_from = (request.GET.get("from") or "").strip()
    date_to = (request.GET.get("to") or "").strip()

    paid_val = getattr(getattr(Invoice, "Status", None), "PAID", "paid")
    unpaid_val = getattr(getattr(Invoice, "Status", None), "UNPAID", "unpaid")

    invs = (
        Invoice.objects.select_related("agreement", "agreement__request")
        .filter(agreement__employee_id=user.id)
        .order_by("-issued_at", "-id")
    )
    if status_q == "unpaid":
        invs = invs.filter(status=unpaid_val)
    elif status_q == "paid":
        invs = invs.filter(status=paid_val)

    if method_q:
        invs = invs.filter(method__iexact=method_q)

    if q:
        invs = invs.filter(
            Q(agreement__request__id__icontains=q)
            | Q(agreement__id__icontains=q)
            | Q(ref_code__icontains=q)
        )
    if date_from:
        invs = invs.filter(issued_at__date__gte=date_from)
    if date_to:
        invs = invs.filter(issued_at__date__lte=date_to)

    totals = invs.aggregate(
        total=Sum("amount"),
        unpaid=Sum("amount", filter=Q(status=unpaid_val)),
        paid=Sum("amount", filter=Q(status=paid_val)),
    )
    methods = (
        Invoice.objects.filter(agreement__employee_id=user.id)
        .exclude(method__isnull=True)
        .exclude(method__exact="")
        .values_list("method", flat=True)
        .distinct()
        .order_by("method")
    )

    # ملخص مستحقات الموظف: P (مجموع المشاريع) + ما تم صرفه وما المتبقي (حسب حالة الفواتير)
    summary = _build_invoice_summary(invs, paid_val=paid_val, unpaid_val=unpaid_val)

    return render(
        request,
        "finance/employee_dues.html",
        {
            "invoices": invs,
            "totals": totals,
            "summary": summary,
            "status_q": status_q,
            "q": q,
            "date_from": date_from,
            "date_to": date_to,
            "method_q": method_q,
            "methods": methods,
        },
    )


# ===========================
# تقرير التحصيل + تصدير CSV
# ===========================
@login_required
@require_GET
def collections_report(request: HttpRequest):
    if not _is_finance(request.user):
        messages.error(request, "غير مصرح بعرض هذا التقرير.")
        return redirect("website:home")

    status_q = (request.GET.get("status") or "").strip().lower()  # unpaid/paid/all
    method_q = (request.GET.get("method") or "").strip()
    q = (request.GET.get("q") or "").strip()
    d1, d2 = _period_bounds(request)

    paid_val = getattr(getattr(Invoice, "Status", None), "PAID", "paid")
    unpaid_val = getattr(getattr(Invoice, "Status", None), "UNPAID", "unpaid")

    invs = Invoice.objects.select_related("agreement", "agreement__request").all()

    # نطاق زمني يعتمد paid_at إن كانت مدفوعة وإلا issued_at
    if d1:
        invs = invs.filter(
            Q(paid_at__date__gte=d1) | Q(paid_at__isnull=True, issued_at__date__gte=d1)
        )
    if d2:
        invs = invs.filter(
            Q(paid_at__date__lte=d2) | Q(paid_at__isnull=True, issued_at__date__lte=d2)
        )

    if status_q == "unpaid":
        invs = invs.filter(status=unpaid_val)
    elif status_q == "paid":
        invs = invs.filter(status=paid_val)

    if method_q:
        invs = invs.filter(method__iexact=method_q)

    if q:
        invs = invs.filter(
            Q(agreement__request__id__icontains=q)
            | Q(agreement__id__icontains=q)
            | Q(ref_code__icontains=q)
        )

    invs = invs.order_by("-paid_at", "-issued_at", "-id")

    totals = invs.aggregate(
        total=Sum("amount"),
        unpaid=Sum("amount", filter=Q(status=unpaid_val)),
        paid=Sum("amount", filter=Q(status=paid_val)),
    )
    methods = (
        Invoice.objects.exclude(method__isnull=True)
        .exclude(method__exact="")
        .values_list("method", flat=True)
        .distinct()
        .order_by("method")
    )
    by_method = (
        invs.values("method").annotate(cnt=Count("id"), amt=Sum("amount")).order_by("method")
    )

    summary = _build_invoice_summary(invs, paid_val=paid_val, unpaid_val=unpaid_val)

    return render(
        request,
        "finance/collections_report.html",
        {
            "invoices": invs,
            "totals": totals,
            "summary": summary,
            "methods": methods,
            "status_q": status_q,
            "method_q": method_q,
            "q": q,
            "period": (request.GET.get("period") or ""),
            "from": request.GET.get("from") or "",
            "to": request.GET.get("to") or "",
            "d1": d1,
            "d2": d2,
            "by_method": by_method,
        },
    )


@login_required
@require_GET
def export_invoices_csv(request: HttpRequest):
    """تصدير CSV حسب مرشحات تقرير التحصيل (مالية فقط)."""
    if not _is_finance(request.user):
        messages.error(request, "غير مصرح بالتصدير.")
        return redirect("website:home")

    status_q = (request.GET.get("status") or "").strip().lower()
    method_q = (request.GET.get("method") or "").strip()
    q = (request.GET.get("q") or "").strip()
    d1, d2 = _period_bounds(request)

    paid_val = getattr(getattr(Invoice, "Status", None), "PAID", "paid")
    unpaid_val = getattr(getattr(Invoice, "Status", None), "UNPAID", "unpaid")

    invs = Invoice.objects.select_related("agreement", "agreement__request").all()
    if d1:
        invs = invs.filter(
            Q(paid_at__date__gte=d1) | Q(paid_at__isnull=True, issued_at__date__gte=d1)
        )
    if d2:
        invs = invs.filter(
            Q(paid_at__date__lte=d2) | Q(paid_at__isnull=True, issued_at__date__lte=d2)
        )
    if status_q == "unpaid":
        invs = invs.filter(status=unpaid_val)
    elif status_q == "paid":
        invs = invs.filter(status=paid_val)
    if method_q:
        invs = invs.filter(method__iexact=method_q)
    if q:
        invs = invs.filter(
            Q(agreement__request__id__icontains=q)
            | Q(agreement__id__icontains=q)
            | Q(ref_code__icontains=q)
        )
    invs = invs.order_by("-paid_at", "-issued_at", "-id")

    resp = HttpResponse(content_type="text/csv; charset=utf-8")
    resp["Content-Disposition"] = 'attachment; filename="invoices_export.csv"'
    writer = csv.writer(resp)
    writer.writerow(
        [
            "InvoiceID",
            "AgreementID",
            "RequestID",
            "Milestone",
            "Amount",
            "Status",
            "IssuedAt",
            "PaidAt",
            "Method",
            "RefCode",
        ]
    )
    for inv in invs:
        try:
            milestone_title = ""
            ms = getattr(inv, "milestone", None)
            if ms is not None:
                milestone_title = getattr(ms, "title", "") or ""
        except Exception:
            milestone_title = ""

        writer.writerow(
            [
                inv.id,
                inv.agreement_id,
                getattr(getattr(inv, "agreement", None), "request_id", ""),
                milestone_title,
                f"{inv.amount}",
                inv.get_status_display()
                if hasattr(inv, "get_status_display")
                else getattr(inv, "status", ""),
                inv.issued_at.strftime("%Y-%m-%d %H:%M")
                if getattr(inv, "issued_at", None)
                else "",
                inv.paid_at.strftime("%Y-%m-%d %H:%M")
                if getattr(inv, "paid_at", None)
                else "",
                getattr(inv, "method", "") or "",
                getattr(inv, "ref_code", "") or "",
            ]
        )
    return resp


# ===========================
# Callback / Webhook (اختياري)
# ===========================
@require_POST
def payment_callback(request: HttpRequest):
    """Placeholder لعودة المتصفح من بوابة الدفع (غير مستخدم حاليًا)."""
    return HttpResponse(status=204)


@require_POST
def payment_webhook(request: HttpRequest):
    """
    Webhook مع توقيع HMAC-SHA256(body, PAYMENT_WEBHOOK_SECRET).
    يتوقع JSON: {"invoice_id": 123, "status": "paid", "reference": "..."}
    """
    try:
        if not PAYMENT_WEBHOOK_SECRET:
            return HttpResponse(status=204)

        raw = request.body or b""
        given_sig = request.headers.get("X-Payment-Signature") or ""
        calc = hmac.new(
            PAYMENT_WEBHOOK_SECRET.encode("utf-8"), raw, hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(given_sig, calc):
            return HttpResponse("invalid signature", status=401)

        import json

        payload = json.loads(raw.decode("utf-8"))
        inv_id = int(payload.get("invoice_id"))
        status = (payload.get("status") or "").lower()
        reference = (payload.get("reference") or "")[:64]

        if status != "paid":
            return HttpResponse(status=204)

        # تنفيذ داخلي آمن دون المرور بـ login_required
        PAID_VAL = getattr(getattr(Invoice, "Status", None), "PAID", "paid")
        with transaction.atomic():
            inv = (
                Invoice.objects.select_for_update()
                .select_related("agreement", "agreement__request")
                .get(pk=inv_id)
            )

            # وسم الفاتورة
            updates: List[str] = []
            inv.status = PAID_VAL
            updates.append("status")
            if not getattr(inv, "paid_at", None):
                inv.paid_at = timezone.now()
                updates.append("paid_at")
            if reference and _writable_attr(inv, "paid_ref"):
                inv.paid_ref = reference
                updates.append("paid_ref")
            if _writable_attr(inv, "updated_at"):
                inv.updated_at = timezone.now()
                updates.append("updated_at")
            inv.save(update_fields=updates)

            # عند السداد من بوابة الدفع: بدء الاتفاقية وتحديث حالة الطلب
            ag = getattr(inv, "agreement", None)
            if ag:
                _mark_agreement_started_and_sync(ag)

        return HttpResponse(status=204)

    except Exception:
        logger.exception("webhook error")
        return HttpResponse(status=400)


# ===========================
# صرفيات (Placeholder لواجهة قادمة)
# ===========================
@login_required
@require_GET
def payouts_list(request: HttpRequest):
    """
    صفحة صرفيات/تحويلات الموظفين (Placeholder مؤقت).
    لاحقًا يمكن ربطها بنموذج Payout وعرض صافي المستحقات المدفوعة والمتبقية
    بالاعتماد على ملخصات employee_paid / employee_unpaid.
    """
    if not _is_finance(request.user):
        messages.error(request, "غير مصرح بهذه الصفحة.")
        return redirect("website:home")
    return render(request, "finance/payouts_list.html", {})


# ===========================
# إعدادات النِّسَب
# ===========================
@login_required
@require_http_methods(["GET", "POST"])
def settings_view(request: HttpRequest):
    """
    صفحة إعدادات المالية (نسبة عمولة المنصة + VAT) كنِسب بين 0..1.
    - تتعامل مع GET/POST على نفس المسار /finance/settings/
    - يوجد alias اسمه finance:settings_save للأكواد/القوالب القديمة.
    """
    if not _is_finance(request.user):
        messages.error(request, "غير مصرح بالوصول إلى إعدادات المالية.")
        return redirect("website:home")

    cfg = FinanceSettings.get_solo()
    if request.method == "POST":
        form = FinanceSettingsForm(request.POST, instance=cfg)
        if form.is_valid():
            form.save()
            # امسح الكاش كي تعتمد القيم مباشرة
            invalidate_finance_cfg_cache()
            messages.success(request, "تم حفظ إعدادات المالية بنجاح.")
            return redirect("finance:settings")
        else:
            messages.error(request, "تحقّق من القيم المُدخلة ثم أعد المحاولة.")
    else:
        form = FinanceSettingsForm(instance=cfg)

    # للعرض فقط: قراءة القيم الحالية من الكاش بعد آخر حفظ
    cached = get_finance_cfg(force=False)

    return render(
        request,
        "finance/settings.html",
        {
            "form": form,
            "cached_fee": cached.platform_fee_percent,  # 0..1
            "cached_vat": cached.vat_rate,  # 0..1
        },
    )
