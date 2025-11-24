# finance/views.py
from __future__ import annotations

import csv
import hashlib
import hmac
import logging
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from typing import Dict, List, Optional, Set, Tuple

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import (
    Count, DecimalField, F, Prefetch, Q, QuerySet, Sum, Value
)
from django.db.models.functions import Coalesce
from django.http import HttpRequest, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import NoReverseMatch, reverse
from django.utils import timezone
from django.views.decorators.http import (
    require_GET, require_POST, require_http_methods
)

from agreements.models import Agreement
from marketplace.models import Request

from .forms import FinanceSettingsForm, TaxRemittanceForm, RefundCreateForm
from .models import FinanceSettings, Invoice, TaxRemittance, Payout, Refund
from .permissions import is_finance
from .utils import get_finance_cfg, invalidate_finance_cfg_cache

logger = logging.getLogger(__name__)

# ===========================
# Ledger (دفتر الخزينة) — استيراد آمن
# ===========================
try:
    from finance.models import LedgerEntry  # الموديل الجديد
except Exception:
    LedgerEntry = None


# ===========================
# تسعير — استيراد آمن مع Backoff
# ===========================
try:
    from finance.services.pricing import breakdown_for_agreement  # النوع المفضل
except Exception:
    try:
        from .pricing import breakdown_for_agreement  # type: ignore
    except Exception:
        breakdown_for_agreement = None


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


def _normalize_rate(v) -> Decimal:
    """
    يحوّل النسبة إلى معدل:
    - إن كانت 15 => 0.15
    - إن كانت 0.15 تبقى كما هي
    """
    d = _as_decimal(v)
    if d > 1:
        return d / Decimal("100")
    return d


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
    """وسم الاتفاقية كمكتملة بشكل آمن مع احترام الحقول والاختيارات."""
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
    """هل نموذج Invoice يحتوي على حقل milestone؟"""
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
    - mark_started
    - sync_request_state
    """
    try:
        if hasattr(ag, "mark_started"):
            ag.mark_started(save=True)
        if hasattr(ag, "sync_request_state"):
            ag.sync_request_state(save_request=True)
    except Exception:
        logger.exception(
            "failed to mark agreement started and sync request state (agreement_id=%s)",
            getattr(ag, "id", None),
        )


def _agreement_P(ag: Agreement) -> Decimal:
    """قيمة المشروع الأساسية P."""
    return _as_decimal(getattr(ag, "p_amount", None) or getattr(ag, "total_amount", 0))


def _invoice_client_total(inv: Invoice, ag: Optional[Agreement] = None) -> Decimal:
    """
    إجمالي العميل من الفاتورة وفق السياسة الجديدة:
    الأفضلية للكاش داخل الفاتورة، ثم fallback للحساب من الاتفاقية.
    """
    for field in ("client_total_amount", "client_total", "total_amount"):
        if hasattr(inv, field):
            val = getattr(inv, field, None)
            if val is not None:
                d = _as_decimal(val)
                if d > 0:
                    return _q2(d)

    # fallback: إن كان لدينا اتفاقية نحسب منها
    if ag:
        try:
            totals = compute_agreement_totals(ag)
            gt = _as_decimal(totals.get("grand_total", 0))
            if gt > 0:
                return _q2(gt)
        except Exception:
            pass

    return _q2(_as_decimal(getattr(inv, "amount", 0)))


def _invoice_breakdown(inv: Invoice) -> Dict[str, Decimal]:
    """
    Breakdown موحد لكل الفواتير:
    - إن كانت مرتبطة باتفاقية: نستخدم compute_agreement_totals
    - وإلا نحسب من amount كصافي P
    """
    ag = getattr(inv, "agreement", None)
    if ag:
        try:
            return compute_agreement_totals(ag)
        except Exception:
            logger.exception("_invoice_breakdown: compute_agreement_totals failed inv=%s", inv.pk)

    from finance.utils import calculate_financials_from_net
    net_amount = _as_decimal(getattr(inv, "amount", 0))
    platform_fee_percent = _normalize_rate(getattr(inv, "platform_fee_percent", None))
    vat_rate = _normalize_rate(getattr(inv, "vat_percent", None))
    return calculate_financials_from_net(
        net_amount,
        platform_fee_percent=platform_fee_percent,
        vat_rate=vat_rate,
    )


# ===========================
# Ledger helpers (رصيد الخزينة)
# ===========================
def _treasury_balance() -> Decimal:
    """رصيد الخزينة = مجموع الدخول - مجموع الخروج."""
    if LedgerEntry is None:
        return Decimal("0.00")
    agg = LedgerEntry.objects.aggregate(
        ins=Sum("amount", filter=Q(direction=LedgerEntry.Direction.IN_)),
        outs=Sum("amount", filter=Q(direction=LedgerEntry.Direction.OUT)),
    )
    ins = agg["ins"] or Decimal("0.00")
    outs = agg["outs"] or Decimal("0.00")
    return _q2(ins - outs)


def _log_ledger_once(
    *, entry_type: str, direction: str, amount: Decimal,
    invoice=None, payout=None, refund=None, tax_remittance=None,
    user=None, note: str = ""
) -> None:
    """تسجيل قيد خزينة مرة واحدة (idempotent)."""
    if LedgerEntry is None:
        return

    qs = LedgerEntry.objects.filter(
        entry_type=entry_type,
        direction=direction,
        invoice=invoice if invoice else None,
        payout=payout if payout else None,
        refund=refund if refund else None,
        tax_remittance=tax_remittance if tax_remittance else None,
    )
    if qs.exists():
        return

    LedgerEntry.objects.create(
        entry_type=entry_type,
        direction=direction,
        amount=_q2(amount),
        invoice=invoice,
        payout=payout,
        refund=refund,
        tax_remittance=tax_remittance,
        created_by=user,
        note=note[:255] if note else "",
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
    السياسة الجديدة:
    - العميل يدفع: P + VAT
    - عمولة المنصّة تُخصم من الموظف
    - مستحق الموظف = P - platform_fee
    """
    P = _agreement_P(ag)
    fee, vat = FinanceSettings.current_rates()

    platform_fee = _q2(P * fee)
    vat_amount = _q2(P * vat)
    client_total = _q2(P + vat_amount)
    net_for_employee = _q2(P - platform_fee)

    return {
        "P": _q2(P),
        "fee_percent": fee * Decimal("100"),
        "platform_fee": platform_fee,
        "taxable": P,
        "vat_percent": vat * Decimal("100"),
        "vat_amount": vat_amount,
        "grand_total": client_total,
        "net_for_employee": net_for_employee,
    }


def compute_agreement_totals(ag: Agreement) -> Dict[str, Decimal]:
    """
    يرجع:
    P, platform_fee, vat_amount,
    grand_total (P + VAT),
    net_for_employee (P - platform_fee)
    """
    if callable(breakdown_for_agreement):
        try:
            bd = breakdown_for_agreement(ag)
            return {
                "P": _q2(bd.project_price),
                "fee_percent": _q2(bd.fee_percent * Decimal("100")),
                "platform_fee": _q2(bd.platform_fee_value),
                "taxable": _q2(bd.taxable_base),
                "vat_percent": _q2(bd.vat_rate * Decimal("100")),
                "vat_amount": _q2(bd.vat_amount),
                "grand_total": _q2(bd.client_total),
                "net_for_employee": _q2(bd.net_for_employee),
            }
        except Exception:
            logger.exception("pricing.breakdown_for_agreement failed — using fallback")
    return _fallback_agreement_totals(ag)


def _build_invoice_summary(qs: QuerySet, paid_val: str, unpaid_val: str) -> Dict[str, Decimal]:
    """ملخص تفصيلي لمجموعة فواتير وفق السياسة الجديدة."""
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

        client_total = _invoice_client_total(inv, ag)

        fee = vat = net_emp = None
        fee_raw = getattr(inv, "platform_fee_amount", None)
        vat_raw = getattr(inv, "vat_amount", None)
        net_raw = getattr(inv, "net_for_employee", None) or getattr(inv, "tech_payout", None)

        if fee_raw is not None:
            fee = _as_decimal(fee_raw)
        if vat_raw is not None:
            vat = _as_decimal(vat_raw)
        if net_raw is not None:
            net_emp = _as_decimal(net_raw)

        if fee is None or vat is None or net_emp is None:
            totals = {}
            if ag:
                try:
                    totals = compute_agreement_totals(ag)
                except Exception:
                    logger.exception(
                        "failed to compute agreement totals in summary (agreement_id=%s)",
                        getattr(ag, "id", None),
                    )
            P = _as_decimal(totals.get("P", getattr(inv, "amount", 0) or 0))
            fee = fee if fee is not None else _as_decimal(totals.get("platform_fee", 0))
            vat = vat if vat is not None else _as_decimal(totals.get("vat_amount", 0))
            if net_emp is None:
                net_emp = _as_decimal(totals.get("net_for_employee", P - fee))

        if net_emp < 0:
            net_emp = Decimal("0.00")

        summary["count"] += Decimal("1")
        summary["client_total"] += client_total
        summary["platform_fee_total"] += fee
        summary["vat_total"] += vat
        summary["employee_total"] += net_emp

        status = (getattr(inv, "status", "") or "").lower()
        if status == (paid_val or "").lower():
            summary["client_paid"] += client_total
            summary["employee_paid"] += net_emp
        elif status == (unpaid_val or "").lower():
            summary["client_unpaid"] += client_total
            summary["employee_unpaid"] += net_emp
        elif status in {(cancelled_val or "").lower(), (refunded_val or "").lower()}:
            summary["client_refunded"] += client_total
            summary["employee_cancelled"] += net_emp

    for key in list(summary.keys()):
        if key == "count":
            continue
        summary[key] = _q2(summary[key])

    return summary


# ===========================
# يضمن/ينشئ فاتورة واحدة لإجمالي الاتفاقية
# ===========================
@transaction.atomic
def _ensure_single_invoice_with_amount(ag: Agreement, *, amount: Decimal) -> Invoice:
    """يضمن وجود فاتورة واحدة فقط للاتفاقية وبالمبلغ الصحيح."""
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
        if getattr(inv, "status", None) != paid_val and (
            not inv.amount or inv.amount != new_amount
        ):
            inv.amount = new_amount
            updates = ["amount"]

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

    fields = {
        "agreement": ag,
        "amount": new_amount,
        "status": unpaid_val,
        "issued_at": now,
        "method": "bank",
        "ref_code": getattr(ag, "ref_code", "") or f"AG{ag.id}-DEP",
    }
    if hasattr(Invoice, "total_amount"):
        fields["total_amount"] = new_amount

    return Invoice.objects.create(**fields)


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

    inprog_val = getattr(getattr(Request, "Status", None), "IN_PROGRESS", "in_progress")
    disputed_val = getattr(getattr(Request, "Status", None), "DISPUTED", "disputed")

    inprog = Request.objects.filter(status=inprog_val)

    total_agreements_amount = (
        Agreement.objects.filter(request__in=inprog)
        .aggregate(s=Sum("total_amount"))["s"]
        or Decimal("0.00")
    )

    invs = Invoice.objects.all().select_related("agreement", "agreement__request")

    def _sum_breakdown(qs, key):
        total = Decimal("0.00")
        seen = set()
        for inv in qs:
            ag = getattr(inv, "agreement", None)
            if ag:
                if ag.id in seen:
                    continue
                seen.add(ag.id)
            bd = _invoice_breakdown(inv)
            total += _as_decimal(bd.get(key, Decimal("0.00")))
        return total

    paid_invs = invs.filter(status=paid_val)
    unpaid_invs = invs.filter(status=unpaid_val)

    paid_total_amount_raw = _sum_breakdown(paid_invs, "grand_total")
    unpaid_total_amount_raw = _sum_breakdown(unpaid_invs, "grand_total")

    fee_all_raw = _sum_breakdown(invs, "platform_fee")
    fee_paid_raw = _sum_breakdown(paid_invs, "platform_fee")

    vat_all_raw = _sum_breakdown(invs, "vat_amount")
    vat_paid_raw = _sum_breakdown(paid_invs, "vat_amount")

    employee_paid_net_raw = _sum_breakdown(paid_invs, "net_for_employee")
    employee_unpaid_net_raw = _sum_breakdown(unpaid_invs, "net_for_employee")

    employee_held_dispute_raw = _sum_breakdown(
        invs.filter(agreement__request__status=disputed_val),
        "net_for_employee",
    )

    pending_bank_confirms = (
        unpaid_invs
        .exclude(paid_ref__isnull=True)
        .exclude(paid_ref__exact="")
        .select_related("agreement", "agreement__request")[:10]
    )

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

    payouts_unpaid = Payout.objects.filter(status=Payout.Status.PENDING)
    payouts_paid = Payout.objects.filter(status=Payout.Status.PAID)

    payouts_paid_total_raw = (
        payouts_paid.aggregate(s=Sum("amount"))["s"] or Decimal("0.00")
    )
    payouts_unpaid_total_raw = (
        payouts_unpaid.aggregate(s=Sum("amount"))["s"] or Decimal("0.00")
    )

    refunds_sent_total_raw = (
        Refund.objects
        .filter(status=Refund.Status.SENT)
        .aggregate(s=Sum("amount"))["s"] or Decimal("0.00")
    )
    refunds_pending_total_raw = (
        Refund.objects
        .filter(status=Refund.Status.PENDING)
        .aggregate(s=Sum("amount"))["s"] or Decimal("0.00")
    )

    remitted_total_raw = (
        TaxRemittance.objects
        .filter(status=TaxRemittance.Status.SENT)
        .aggregate(s=Sum("amount"))["s"] or Decimal("0.00")
    )

    vat_payable_raw = vat_paid_raw - remitted_total_raw
    if vat_payable_raw < 0:
        vat_payable_raw = Decimal("0.00")

    customer_liability_raw = paid_total_amount_raw - refunds_sent_total_raw - payouts_paid_total_raw
    if customer_liability_raw < 0:
        customer_liability_raw = Decimal("0.00")

    computed_treasury_raw = (
        paid_total_amount_raw
        - payouts_paid_total_raw
        - refunds_sent_total_raw
        - remitted_total_raw
    )
    if computed_treasury_raw < 0:
        computed_treasury_raw = Decimal("0.00")

    ledger_treasury_raw = _treasury_balance()
    if ledger_treasury_raw is None:
        ledger_treasury_raw = Decimal("0.00")

    try:
        from disputes.models import Dispute
    except Exception:
        Dispute = None

    disputes_active = []
    if Dispute is not None:
        disputes_active = (
            Dispute.objects.filter(status__in=[Dispute.Status.OPEN, Dispute.Status.IN_REVIEW])
            .select_related("request")
        )
        for dispute in disputes_active:
            inv = (
                Invoice.objects.filter(agreement__request=dispute.request)
                .order_by("-issued_at")
                .first()
            )
            dispute.invoice = inv

    ctx = {
        "inprogress_count": inprog.count(),
        "total_agreements_amount": _q2(total_agreements_amount),

        "paid_sum": _q2(paid_total_amount_raw),
        "unpaid_sum": _q2(unpaid_total_amount_raw),
        "platform_fee_total": _q2(fee_all_raw),
        "platform_fee_paid": _q2(fee_paid_raw),
        "vat_total": _q2(vat_all_raw),
        "vat_paid": _q2(vat_paid_raw),

        "employee_paid_net": _q2(employee_paid_net_raw),
        "employee_unpaid_net": _q2(employee_unpaid_net_raw),
        "disputed_total": _q2(employee_held_dispute_raw),

        "employee_dues_total": _q2(payouts_paid_total_raw),
        "employee_unpaid_total": _q2(payouts_unpaid_total_raw),
        "payouts_unpaid": payouts_unpaid,
        "payouts_paid": payouts_paid,

        "pending_bank_confirms": pending_bank_confirms,
        "disputes_active": disputes_active,

        "settings_url": settings_url,

        "treasury_balance": _q2(computed_treasury_raw),
        "computed_treasury_balance": _q2(computed_treasury_raw),
        "ledger_treasury_balance": _q2(ledger_treasury_raw),

        "vat_payable": _q2(vat_payable_raw),
        "customer_liability": _q2(customer_liability_raw),
        "refunds_sent_total": _q2(refunds_sent_total_raw),
        "refunds_pending_total": _q2(refunds_pending_total_raw),
        "remitted_total": _q2(remitted_total_raw),
    }

    return render(request, "finance/home.html", ctx)


# ===========================
# لوحة الضرائب VAT (GET/POST)
# ===========================
@login_required
@require_http_methods(["GET", "POST"])
def tax_dashboard(request: HttpRequest):
    if not _is_finance(request.user):
        messages.error(request, "غير مصرح بهذه الصفحة.")
        return redirect("website:home")

    if request.method == "POST":
        remit_form = TaxRemittanceForm(request.POST)
        if remit_form.is_valid():
            with transaction.atomic():
                tr = remit_form.save(commit=False)
                if remit_form.cleaned_data.get("sent_now"):
                    tr.status = TaxRemittance.Status.SENT
                    tr.sent_at = timezone.now()
                tr.save()

                _log_ledger_once(
                    entry_type=getattr(LedgerEntry.Type, "VAT_REMITTANCE", "vat_remittance") if LedgerEntry else "vat_remittance",
                    direction=getattr(LedgerEntry.Direction, "OUT", "out") if LedgerEntry else "out",
                    amount=tr.amount,
                    tax_remittance=tr,
                    user=request.user,
                    note=f"توريد ضريبة #{tr.pk}"
                )

            messages.success(request, "تم إضافة توريد الضريبة بنجاح.")
            return redirect("finance:tax_dashboard")
        messages.error(request, "تعذر حفظ التوريد. يرجى مراجعة الحقول.")
    else:
        remit_form = TaxRemittanceForm()

    PAID_VAL = getattr(getattr(Invoice, "Status", None), "PAID", "paid")
    CANCEL_VAL = getattr(getattr(Invoice, "Status", None), "CANCELLED", "cancelled")

    invoices = (
        Invoice.objects
        .exclude(status=CANCEL_VAL)
        .select_related("agreement", "agreement__request", "agreement__employee")
        .order_by("-issued_at", "-id")
    )

    seen_agreements = set()
    vat_collected = Decimal("0.00")
    vat_pending = Decimal("0.00")
    rows = []

    for inv in invoices:
        ag = getattr(inv, "agreement", None)
        if not ag or ag.id in seen_agreements:
            continue
        seen_agreements.add(ag.id)

        try:
            totals = compute_agreement_totals(ag)
        except Exception:
            logger.exception("compute_agreement_totals failed in tax_dashboard")
            continue

        vat_amount = _as_decimal(totals.get("vat_amount", 0))
        taxable = _as_decimal(totals.get("taxable", 0))
        grand_total = _as_decimal(totals.get("grand_total", 0))
        P = _as_decimal(totals.get("P", 0))
        fee = _as_decimal(totals.get("platform_fee", 0))

        is_paid = (getattr(inv, "status", "") == PAID_VAL)

        if is_paid:
            vat_collected += vat_amount
        else:
            vat_pending += vat_amount

        req = getattr(ag, "request", None)
        rows.append({
            "invoice": inv,
            "agreement": ag,
            "request": req,
            "employee": getattr(ag, "employee", None),
            "is_paid": is_paid,
            "P": _q2(P),
            "platform_fee": _q2(fee),
            "taxable": _q2(taxable),
            "vat_amount": _q2(vat_amount),
            "grand_total": _q2(grand_total),
            "issued_at": getattr(inv, "issued_at", None),
            "paid_at": getattr(inv, "paid_at", None),
        })

    vat_collected = _q2(vat_collected)
    vat_pending = _q2(vat_pending)

    remitted_total = (
        TaxRemittance.objects
        .filter(status=TaxRemittance.Status.SENT)
        .aggregate(s=Sum("amount"))["s"]
        or Decimal("0.00")
    )
    remitted_total = _q2(remitted_total)

    tax_stock = vat_collected - remitted_total
    if tax_stock < 0:
        tax_stock = Decimal("0.00")
    tax_stock = _q2(tax_stock)

    last_remittances = TaxRemittance.objects.all().order_by("-created_at")[:5]

    ctx = {
        "rows": rows,
        "vat_collected": vat_collected,
        "vat_pending": vat_pending,
        "remitted_total": remitted_total,
        "tax_stock": tax_stock,
        "last_remittances": last_remittances,
        "remit_form": remit_form,
    }
    return render(request, "finance/tax_dashboard.html", ctx)


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
    ag = get_object_or_404(
        Agreement.objects.select_related("request", "employee", "request__client"),
        pk=agreement_id,
    )

    u = request.user
    is_owner = getattr(ag.request, "client_id", None) == u.id
    if not (is_owner or _is_finance(u)):
        messages.error(request, "غير مصرح لك بفتح صفحة الدفع لهذه الاتفاقية.")
        return redirect("website:home")

    breakdown = compute_agreement_totals(ag)
    grand_total = _as_decimal(breakdown.get("grand_total", getattr(ag, "total_amount", 0)))

    inv = _ensure_single_invoice_with_amount(ag, amount=grand_total)

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

    logger.info(
        "[FINANCE] حوالة جديدة بحاجة للمتابعة: Invoice #%s - Agreement #%s - Ref: %s",
        inv.pk, inv.agreement_id, paid_ref
    )

    messages.success(
        request,
        "تم تسجيل بيانات الحوالة بنجاح. سيتم مراجعة الحوالة من المالية وتحويل الطلب إلى قيد التنفيذ بعد التأكيد."
    )
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
    req: Request | None = getattr(ag, "request", None) if ag else None

    PAID_VAL = getattr(getattr(Invoice, "Status", None), "PAID", "paid")

    # ===== إعادة احتساب الفاتورة =====
    try:
        if hasattr(inv, "recompute_totals"):
            inv.recompute_totals()
    except Exception:
        logger.exception("mark_invoice_paid: failed to recompute totals for inv=%s", inv.pk)

    # ===== تحقق إجمالي العميل =====
    try:
        client_total = _invoice_client_total(inv, ag)
        if client_total <= Decimal("0.00"):
            messages.error(request, "لا يمكن وسم فاتورة إجماليها 0 كمدفوعة.")
            return redirect(request.META.get("HTTP_REFERER", "/"))
    except Exception:
        logger.exception("mark_invoice_paid: client_total check failed for inv=%s", inv.pk)
        messages.error(request, "تعذّر التحقق من إجمالي الفاتورة.")
        return redirect(request.META.get("HTTP_REFERER", "/"))

    # ===== منع التكرار =====
    if (getattr(inv, "status", None) or "").lower() == str(PAID_VAL).lower():
        messages.info(request, "الفاتورة مدفوعة مسبقًا.")
        return redirect(request.META.get("HTTP_REFERER", "/"))

    try:
        now = timezone.now()
        updates: List[str] = []

        # ===== تحديث الفاتورة =====
        if _writable_attr(inv, "status"):
            inv.status = PAID_VAL
            updates.append("status")

        if _writable_attr(inv, "paid_at") and not getattr(inv, "paid_at", None):
            inv.paid_at = now
            updates.append("paid_at")

        if _writable_attr(inv, "updated_at"):
            inv.updated_at = now
            updates.append("updated_at")

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

        # ===== إشعال بدء الاتفاقية إن لم تكن قد بدأت =====
        if ag:
            _mark_agreement_started_and_sync(ag)

        # ============================================================
        #             <<<<<  تحويل الطلب إلى قيد التنفيذ  >>>>>
        # ============================================================
        if req:
            now = timezone.now()

            # قراءة الحالة الفعلية (status أو state)
            current = (
                getattr(req, "status", None)
                or getattr(req, "state", "")
                or ""
            ).strip().lower()

            final_states = {"completed", "cancelled", "disputed"}

            if current not in final_states:

                # القيمة الصحيحة لحالة IN_PROGRESS
                inprog_val = getattr(
                    getattr(Request, "Status", None),
                    "IN_PROGRESS",
                    "in_progress"
                )

                # تأكد أن القيمة موجودة في choices إن وجدت
                try:
                    field = (
                        req._meta.get_field("status")
                        if hasattr(req, "status")
                        else req._meta.get_field("state")
                    )
                    choices = getattr(field, "choices", ()) or ()
                    if choices:
                        valid = {c[0] for c in choices}
                        if inprog_val not in valid:
                            inprog_val = "in_progress"
                except Exception:
                    inprog_val = "in_progress"

                # كتابة الحالة الصحيحة
                fields = []
                if hasattr(req, "status") and _writable_attr(req, "status"):
                    req.status = inprog_val
                    fields.append("status")
                elif hasattr(req, "state") and _writable_attr(req, "state"):
                    req.state = inprog_val
                    fields.append("state")

                if hasattr(req, "updated_at") and _writable_attr(req, "updated_at"):
                    req.updated_at = now
                    fields.append("updated_at")

                if fields:
                    req.save(update_fields=fields)

        # ===== تسجيل الدفعة في Ledger =====
        _log_ledger_once(
            entry_type=getattr(LedgerEntry.Type, "CLIENT_PAYMENT", "client_payment") if LedgerEntry else "client_payment",
            direction=getattr(LedgerEntry.Direction, "IN_", "in") if LedgerEntry else "in",
            amount=client_total,
            invoice=inv,
            user=request.user,
            note=f"تحصيل فاتورة #{inv.pk}",
        )

        messages.success(
            request,
            "تم وسم الفاتورة كمدفوعة وتم تحويل الطلب إلى قيد التنفيذ بنجاح."
        )
        return redirect(request.META.get("HTTP_REFERER", "/"))

    except Exception as e:
        logger.exception("فشل وسم الفاتورة كمدفوعة: %s", e)
        transaction.set_rollback(True)
        if getattr(settings, "DEBUG", False):
            messages.error(request, f"تعذّر حفظ الدفع: {type(e).__name__} — {e}")
        else:
            messages.error(request, "حدث خطأ غير متوقع أثناء تحديث الدفع. لم يتم حفظ أي تغييرات.")
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

    u = request.user
    ag = getattr(inv, "agreement", None)
    req = getattr(ag, "request", None) if ag else None

    is_owner = req and getattr(req, "client_id", None) == u.id
    is_employee = ag and getattr(ag, "employee_id", None) == u.id

    if not (_is_finance(u) or is_owner or is_employee):
        messages.error(request, "غير مصرح لك بعرض هذه الفاتورة.")
        return redirect("website:home")

    breakdown = _invoice_breakdown(inv)

    return render(
        request,
        "finance/invoice_detail.html",
        {"inv": inv, "invoice": inv, "breakdown": breakdown},
    )


@login_required
def invoice_list(request: HttpRequest):
    u = request.user
    qs = Invoice.objects.all().select_related("agreement", "agreement__request")

    if not _is_finance(u):
        role = getattr(u, "role", "")
        if role == "client":
            qs = qs.filter(agreement__request__client_id=u.id)
        elif role == "employee":
            qs = qs.filter(agreement__employee_id=u.id)
        else:
            messages.error(request, "غير مصرح بعرض الفواتير.")
            return redirect("website:home")

    status = (request.GET.get("status") or "").strip().lower()
    PAID_VAL = getattr(getattr(Invoice, "Status", None), "PAID", "paid")
    UNPAID_VAL = getattr(getattr(Invoice, "Status", None), "UNPAID", "unpaid")
    CANCEL_VAL = getattr(getattr(Invoice, "Status", None), "CANCELLED", "cancelled")

    if status in ("unpaid", "paid", "cancelled"):
        map_val = {"unpaid": UNPAID_VAL, "paid": PAID_VAL, "cancelled": CANCEL_VAL}
        qs = qs.filter(status=map_val[status])

    invoices_with_breakdown = []
    for inv in qs:
        invoices_with_breakdown.append({"inv": inv, "breakdown": _invoice_breakdown(inv)})

    return render(request, "finance/invoice_list.html", {
        "invoices": invoices_with_breakdown,
        "object_list": qs,
    })


# ===========================
# تأكيد تحويلات العملاء
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
# فواتير اتفاقية محددة
# ===========================
@login_required
@transaction.atomic
def agreement_invoices(request: HttpRequest, agreement_id: int):
    if not _is_finance(request.user):
        messages.error(request, "غير مصرح بعرض هذه الصفحة.")
        return redirect("website:home")

    ag = get_object_or_404(
        Agreement.objects.select_related("request", "employee").prefetch_related("invoices"),
        pk=agreement_id,
    )

    totals = compute_agreement_totals(ag)
    inv = _ensure_single_invoice_with_amount(ag, amount=totals["grand_total"])

    paid_val = getattr(getattr(Invoice, "Status", None), "PAID", "paid")
    is_paid = (getattr(inv, "status", None) == paid_val) or bool(getattr(inv, "is_paid", False))

    summary = {
        "total": inv.amount,
        "unpaid": inv.amount if not is_paid else None,
        "paid": inv.amount if is_paid else None,
    }

    invoices_with_breakdown = [{"inv": inv, "breakdown": totals}]
    return render(request, "finance/invoice_list.html", {
        "agreement": ag,
        "invoice": inv,
        "invoices": invoices_with_breakdown,
        "totals": summary,
    })


# ===========================
# صفحات العميل: مدفوعاتي
# ===========================
@login_required
@require_GET
def client_payments(request: HttpRequest) -> HttpResponse:
    user = request.user
    role = getattr(user, "role", "")
    if role != "client" and not _is_finance(user):
        messages.error(request, "هذه الصفحة مخصّصة للعميل.")
        return redirect("website:home")

    status_q = (request.GET.get("status") or "").strip().lower()
    method_q = (request.GET.get("method") or "").strip()
    q = (request.GET.get("q") or "").strip()
    date_from = (request.GET.get("from") or "").strip()
    date_to = (request.GET.get("to") or "").strip()

    paid_val = getattr(getattr(Invoice, "Status", None), "PAID", "paid")
    unpaid_val = getattr(getattr(Invoice, "Status", None), "UNPAID", "unpaid")

    invs = (
        Invoice.objects
        .select_related("agreement", "agreement__request")
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

    all_invoices = (
        Invoice.objects
        .select_related("agreement", "agreement__request")
        .filter(agreement__request__client_id=user.id)
        .order_by("-issued_at", "-id")
    )

    summary_all = _build_invoice_summary(all_invoices, paid_val=paid_val, unpaid_val=unpaid_val)
    totals = {
        "total": summary_all["client_total"],
        "paid": summary_all["client_paid"],
        "unpaid": summary_all["client_unpaid"],
    }

    methods = (
        Invoice.objects.filter(agreement__request__client_id=user.id)
        .exclude(method__isnull=True)
        .exclude(method__exact="")
        .values_list("method", flat=True)
        .distinct()
        .order_by("method")
    )

    summary = _build_invoice_summary(invs, paid_val=paid_val, unpaid_val=unpaid_val)

    return render(
        request,
        "finance/client_payments.html",
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
# صفحات الموظف: مستحقاتي
# ===========================
def _get_request_completed_at(req: Optional[Request]) -> Optional[timezone.datetime]:
    """محاولة ذكية للحصول على وقت اكتمال الطلب."""
    if not req:
        return None

    for field in ("completed_at", "finished_at", "completed_on"):
        val = getattr(req, field, None)
        if val:
            return val

    status_val = str(getattr(req, "status", getattr(req, "state", "")) or "")
    completed_req_val = getattr(getattr(Request, "Status", None), "COMPLETED", "completed")
    if status_val == completed_req_val:
        return getattr(req, "updated_at", None)

    return None


@login_required
def employee_dues(request: HttpRequest) -> HttpResponse:
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

    completed_req_val = getattr(getattr(Request, "Status", None), "COMPLETED", "completed")
    disputed_req_val = getattr(getattr(Request, "Status", None), "DISPUTED", "disputed")

    safety_days = 3
    safety_delta = timedelta(days=safety_days)
    now = timezone.now()

    invs = (
        Invoice.objects
        .select_related("agreement", "agreement__request")
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

    methods = (
        Invoice.objects.filter(agreement__employee_id=user.id)
        .exclude(method__isnull=True)
        .exclude(method__exact="")
        .values_list("method", flat=True)
        .distinct()
        .order_by("method")
    )

    agreement_ids = list(invs.values_list("agreement_id", flat=True).distinct())

    existing_payouts = (
        Payout.objects
        .filter(agreement_id__in=agreement_ids)
        .exclude(status=Payout.Status.CANCELLED)
        .select_related("agreement", "agreement__request")
        .order_by("-id")
    )
    payout_by_agreement: Dict[int, Payout] = {
        p.agreement_id: p for p in existing_payouts if p.agreement_id
    }

    agreements_map: Dict[int, Agreement] = {
        ag.id: ag for ag in Agreement.objects.select_related("request").filter(id__in=agreement_ids)
    }

    breakdown_by_agreement: Dict[int, dict] = {}
    for ag_id, ag in agreements_map.items():
        try:
            breakdown_by_agreement[ag_id] = compute_agreement_totals(ag)
        except Exception:
            logger.exception("employee_dues: compute_agreement_totals failed ag=%s", ag_id)
            breakdown_by_agreement[ag_id] = {}

    rows: List[dict] = []
    seen_agreements: Set[int] = set()
    net_total = Decimal("0.00")

    for inv in invs:
        ag = getattr(inv, "agreement", None)
        if not ag:
            continue

        req_obj = getattr(ag, "request", None)

        bd = breakdown_by_agreement.get(ag.id, {}) or {}
        P = _as_decimal(bd.get("P", inv.amount or 0))
        fee = _as_decimal(bd.get("platform_fee", 0))
        net_emp = _as_decimal(bd.get("net_for_employee", P - fee))
        if net_emp < 0:
            net_emp = Decimal("0.00")

        if ag.id not in seen_agreements:
            net_total += net_emp
            seen_agreements.add(ag.id)

        held_reason = ""
        eligible_now = False
        ready_at = None

        req_status = str(getattr(req_obj, "status", getattr(req_obj, "state", "")) or "")
        if req_status == disputed_req_val:
            held_reason = "نزاع نشط — المستحق مجمّد"

        completed_at = _get_request_completed_at(req_obj)
        if completed_at:
            ready_at = completed_at + safety_delta
            if not held_reason and req_status == completed_req_val:
                eligible_now = now >= ready_at
        else:
            if not held_reason and req_status == completed_req_val:
                held_reason = "لم يُسجّل وقت اكتمال الطلب بعد"

        payout = payout_by_agreement.get(ag.id)

        rows.append({
            "invoice": inv,
            "agreement": ag,
            "request": req_obj,
            "P": _q2(P),
            "fee": _q2(fee),
            "net_for_employee": _q2(net_emp),
            "payout": payout,
            "invoice_status": str(getattr(inv, "status", "") or ""),
            "held_reason": held_reason,
            "ready_at": ready_at,
            "eligible_now": eligible_now,
        })

    paid_payout_val = getattr(getattr(Payout, "Status", None), "PAID", "paid")
    pending_payout_val = getattr(getattr(Payout, "Status", None), "PENDING", "pending")

    net_paid = Decimal("0.00")
    net_pending = Decimal("0.00")

    for p in existing_payouts:
        amt = _as_decimal(getattr(p, "amount", 0))
        st = str(getattr(p, "status", "") or "")
        if st == paid_payout_val:
            net_paid += amt
        elif st == pending_payout_val:
            net_pending += amt

    net_unpaid = net_total - net_paid
    if net_unpaid < 0:
        net_unpaid = Decimal("0.00")

    totals = {
        "net_total": _q2(net_total),
        "net_paid": _q2(net_paid),
        "net_pending": _q2(net_pending),
        "net_unpaid": _q2(net_unpaid),
    }

    return render(
        request,
        "finance/employee_dues.html",
        {
            "rows": rows,
            "totals": totals,
            "status_q": status_q,
            "q": q,
            "date_from": date_from,
            "date_to": date_to,
            "method_q": method_q,
            "methods": methods,
            "safety_days": safety_days,
            "now": now,
        },
    )


# ===========================
# مستحقات الموظفين (لوحة المالية)
# ===========================
@login_required
@require_http_methods(["GET", "POST"])
def employee_dues_admin(request: HttpRequest) -> HttpResponse:
    if not _is_finance(request.user):
        messages.error(request, "غير مصرح بهذه الصفحة.")
        return redirect("website:home")

    paid_val = getattr(getattr(Invoice, "Status", None), "PAID", "paid")
    cancel_payout_val = getattr(getattr(Payout, "Status", None), "CANCELLED", "cancelled")
    completed_req_val = getattr(getattr(Request, "Status", None), "COMPLETED", "completed")
    disputed_req_val = getattr(getattr(Request, "Status", None), "DISPUTED", "disputed")

    safety_days = 3
    safety_delta = timedelta(days=safety_days)
    now = timezone.now()

    paid_invoices_qs = (
        Invoice.objects
        .filter(status=paid_val)
        .only("id", "agreement_id", "paid_at", "issued_at", "amount", "status")
        .order_by("-paid_at", "-issued_at", "-id")
    )

    agreements_qs = (
        Agreement.objects
        .select_related("request", "employee", "request__client")
        .filter(request__status=completed_req_val)
        .filter(invoices__status=paid_val)
        .distinct()
        .prefetch_related(
            Prefetch("invoices", queryset=paid_invoices_qs, to_attr="paid_invoices")
        )
        .order_by("-updated_at", "-id")
    )

    existing_payouts = (
        Payout.objects
        .filter(agreement__in=agreements_qs)
        .exclude(status=cancel_payout_val)
        .select_related("agreement", "invoice")
    )
    payout_by_agreement: Dict[int, Payout] = {
        p.agreement_id: p for p in existing_payouts if p.agreement_id
    }

    rows: List[dict] = []
    totals_by_employee: Dict[Optional[int], Decimal] = {}
    eligible_total_by_employee: Dict[Optional[int], Decimal] = {}

    for ag in agreements_qs:
        req_obj = getattr(ag, "request", None)

        held_reason = ""
        if req_obj and getattr(req_obj, "status", "") == disputed_req_val:
            held_reason = "نزاع نشط — المبلغ مجمّد"

        paid_invoices = getattr(ag, "paid_invoices", []) or []
        invoice_to_link = paid_invoices[0] if paid_invoices else None
        last_paid_at = invoice_to_link.paid_at or invoice_to_link.issued_at if invoice_to_link else None

        completed_at = None
        if req_obj:
            completed_at = (
                getattr(req_obj, "completed_at", None)
                or getattr(req_obj, "closed_at", None)
                or getattr(req_obj, "finished_at", None)
            )

        base_ready_at = completed_at or getattr(req_obj, "updated_at", None)

        ready_at = None
        eligible_now = False

        if not invoice_to_link:
            if not held_reason:
                held_reason = "لا توجد فاتورة مدفوعة مرتبطة"
        elif not base_ready_at:
            if not held_reason:
                held_reason = "لا يوجد تاريخ اكتمال صالح للطلب"
        else:
            ready_at = base_ready_at + safety_delta
            if not held_reason:
                eligible_now = now >= ready_at

        try:
            bd = compute_agreement_totals(ag)
        except Exception:
            logger.exception("employee_dues_admin: compute_agreement_totals failed ag=%s", ag.id)
            continue

        P = _as_decimal(bd.get("P", 0))
        fee = _as_decimal(bd.get("platform_fee", 0))
        net_emp = _as_decimal(bd.get("net_for_employee", P - fee))
        if net_emp < 0:
            net_emp = Decimal("0.00")

        emp = getattr(ag, "employee", None)
        emp_id = getattr(emp, "id", None)

        totals_by_employee.setdefault(emp_id, Decimal("0.00"))
        totals_by_employee[emp_id] += net_emp

        if eligible_now and ag.id not in payout_by_agreement:
            eligible_total_by_employee.setdefault(emp_id, Decimal("0.00"))
            eligible_total_by_employee[emp_id] += net_emp
        else:
            eligible_total_by_employee.setdefault(emp_id, Decimal("0.00"))

        rows.append({
            "agreement": ag,
            "request": req_obj,
            "employee": emp,
            "P": _q2(P),
            "fee": _q2(fee),
            "net_for_employee": _q2(net_emp),
            "payout": payout_by_agreement.get(ag.id),
            "last_paid_at": last_paid_at,
            "completed_at": completed_at,
            "ready_at": ready_at,
            "eligible_now": eligible_now,
            "held_reason": held_reason,
            "invoice_to_link": invoice_to_link,
        })

    for k in list(totals_by_employee.keys()):
        totals_by_employee[k] = _q2(totals_by_employee[k])
    for k in list(eligible_total_by_employee.keys()):
        eligible_total_by_employee[k] = _q2(eligible_total_by_employee[k])

    if request.method == "POST":
        ag_id_raw = (request.POST.get("agreement_id") or "").strip()
        try:
            ag_id = int(ag_id_raw)
        except Exception:
            ag_id = None

        if not ag_id:
            messages.error(request, "معرّف الاتفاقية غير صحيح.")
            return redirect("finance:employee_dues_admin")

        ag = agreements_qs.filter(id=ag_id).first()
        if not ag:
            messages.error(request, "الاتفاقية غير مستحقة أو غير موجودة.")
            return redirect("finance:employee_dues_admin")

        if ag.id in payout_by_agreement:
            p = payout_by_agreement[ag.id]
            messages.info(request, f"يوجد أمر صرف مسبق لهذه الاتفاقية (#{p.id}).")
            return redirect("finance:employee_dues_admin")

        try:
            with transaction.atomic():
                ag_locked = (
                    Agreement.objects
                    .select_for_update()
                    .select_related("request", "employee")
                    .prefetch_related(
                        Prefetch("invoices", queryset=paid_invoices_qs, to_attr="paid_invoices")
                    )
                    .get(pk=ag.id)
                )

                req_locked = getattr(ag_locked, "request", None)

                if not req_locked or getattr(req_locked, "status", "") != completed_req_val:
                    messages.error(request, "لا يمكن الصرف: الطلب غير مكتمل.")
                    return redirect("finance:employee_dues_admin")

                if getattr(req_locked, "status", "") == disputed_req_val:
                    messages.error(request, "لا يمكن الصرف: يوجد نزاع نشط على الطلب.")
                    return redirect("finance:employee_dues_admin")

                paid_invoices = getattr(ag_locked, "paid_invoices", []) or []
                if not paid_invoices:
                    messages.error(request, "لا يمكن الصرف: لا توجد فاتورة مدفوعة.")
                    return redirect("finance:employee_dues_admin")

                invoice_to_link = paid_invoices[0]

                completed_at = (
                    getattr(req_locked, "completed_at", None)
                    or getattr(req_locked, "closed_at", None)
                    or getattr(req_locked, "finished_at", None)
                    or getattr(req_locked, "updated_at", None)
                )
                if not completed_at:
                    messages.error(request, "لا يمكن الصرف: تاريخ اكتمال الطلب غير متوفر.")
                    return redirect("finance:employee_dues_admin")

                ready_at = completed_at + safety_delta
                if timezone.now() < ready_at:
                    messages.error(
                        request,
                        f"غير مؤهل للصرف بعد. متاح في {ready_at:%Y-%m-%d %H:%M}."
                    )
                    return redirect("finance:employee_dues_admin")

                bd = compute_agreement_totals(ag_locked)
                P = _as_decimal(bd.get("P", 0))
                fee = _as_decimal(bd.get("platform_fee", 0))
                net_emp = _as_decimal(bd.get("net_for_employee", P - fee))
                if net_emp < 0:
                    net_emp = Decimal("0.00")

                payout = Payout.objects.create(
                    employee=ag_locked.employee,
                    agreement=ag_locked,
                    invoice=invoice_to_link,
                    amount=_q2(net_emp),
                    status=Payout.Status.PENDING,
                    note=f"مستحقات بعد اكتمال R{ag_locked.request_id} (Invoice #{invoice_to_link.id})",
                )

                try:
                    from notifications.utils import create_notification
                    employee = getattr(ag_locked, "employee", None)
                    req = getattr(ag_locked, "request", None)
                    if employee:
                        create_notification(
                            recipient=employee,
                            title=f"تم إنشاء أمر صرف لمستحقاتك للطلب #{req.pk if req else ag_locked.request_id}",
                            body=f"تم إنشاء أمر صرف بقيمة {payout.amount} ر.س للاتفاقية المرتبطة بالطلب '{req.title if req else ''}'. يمكنك متابعة حالة الصرف في حسابك.",
                            url=payout.invoice.get_absolute_url() if hasattr(payout.invoice, "get_absolute_url") else None,
                            actor=req.client if req and hasattr(req, "client") else None,
                            target=payout,
                        )
                except Exception:
                    pass

            messages.success(request, f"تم إنشاء أمر صرف جديد #{payout.id}.")
        except Agreement.DoesNotExist:
            messages.error(request, "الاتفاقية غير موجودة.")
        except Exception:
            logger.exception("employee_dues_admin: create payout failed for ag=%s", ag_id)
            messages.error(request, "حدث خطأ أثناء إنشاء أمر الصرف.")

        return redirect("finance:employee_dues_admin")

    return render(
        request,
        "finance/employee_dues_admin.html",
        {
            "rows": rows,
            "completed_req_val": completed_req_val,
            "paid_val": paid_val,
            "safety_days": safety_days,
            "now": now,
            "totals_by_employee": totals_by_employee,
            "eligible_totals_by_employee": eligible_total_by_employee,
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
        invs.values("method")
        .annotate(cnt=Count("id"), amt=Sum("amount"))
        .order_by("method")
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
        milestone_title = ""
        try:
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
                inv.get_status_display() if hasattr(inv, "get_status_display") else getattr(inv, "status", ""),
                inv.issued_at.strftime("%Y-%m-%d %H:%M") if getattr(inv, "issued_at", None) else "",
                inv.paid_at.strftime("%Y-%m-%d %H:%M") if getattr(inv, "paid_at", None) else "",
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
    return HttpResponse(status=204)


@require_POST
def payment_webhook(request: HttpRequest):
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
        if "invoice_id" not in payload:
            return HttpResponse("missing invoice_id", status=400)

        inv_id = int(payload.get("invoice_id"))
        status = (payload.get("status") or "").lower()
        reference = (payload.get("reference") or "")[:64]

        if status != "paid":
            return HttpResponse(status=204)

        PAID_VAL = getattr(getattr(Invoice, "Status", None), "PAID", "paid")
        with transaction.atomic():
            inv = (
                Invoice.objects.select_for_update()
                .select_related("agreement", "agreement__request")
                .get(pk=inv_id)
            )

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

            ag = getattr(inv, "agreement", None)
            if ag:
                _mark_agreement_started_and_sync(ag)

            client_total = _invoice_client_total(inv, ag)

            _log_ledger_once(
                entry_type=getattr(LedgerEntry.Type, "CLIENT_PAYMENT", "client_payment") if LedgerEntry else "client_payment",
                direction=getattr(LedgerEntry.Direction, "IN_", "in") if LedgerEntry else "in",
                amount=_as_decimal(client_total),
                invoice=inv,
                user=getattr(request, "user", None),
                note=f"تحصيل Webhook فاتورة #{inv.pk}",
            )

        return HttpResponse(status=204)

    except Exception:
        logger.exception("webhook error")
        return HttpResponse(status=400)


# ===========================
# صرفيات — قائمة
# ===========================
@login_required
@require_GET
def payouts_list(request: HttpRequest):
    if not _is_finance(request.user):
        messages.error(request, "غير مصرح بهذه الصفحة.")
        return redirect("website:home")

    status_q = (request.GET.get("status") or "pending").strip().lower()
    q = (request.GET.get("q") or "").strip()
    d1, d2 = _period_bounds(request)

    qs = (
        Payout.objects
        .select_related("employee", "agreement", "agreement__request")
        .order_by("-issued_at", "-id")
    )

    if d1:
        qs = qs.filter(issued_at__date__gte=d1)
    if d2:
        qs = qs.filter(issued_at__date__lte=d2)

    if q:
        qs = qs.filter(
            Q(employee__name__icontains=q)
            | Q(employee__phone__icontains=q)
            | Q(agreement__id__icontains=q)
            | Q(agreement__request__id__icontains=q)
            | Q(agreement__request__title__icontains=q)
        )

    if status_q in ("pending", "paid", "cancelled"):
        qs = qs.filter(status=status_q)

    totals = qs.aggregate(
        total=Sum("amount"),
        pending=Sum("amount", filter=Q(status=Payout.Status.PENDING)),
        paid=Sum("amount", filter=Q(status=Payout.Status.PAID)),
        cancelled=Sum("amount", filter=Q(status=Payout.Status.CANCELLED)),
        count=Count("id"),
    )

    return render(request, "finance/payouts_list.html", {
        "payouts": qs,
        "totals": totals,
        "status_q": status_q,
        "q": q,
        "period": request.GET.get("period") or "",
        "from": request.GET.get("from") or "",
        "to": request.GET.get("to") or "",
        "d1": d1,
        "d2": d2,
    })


# ===========================
# وسم أمر الصرف كمدفوع
# ===========================
@login_required
@require_POST
@transaction.atomic
def mark_payout_paid(request: HttpRequest, pk: int):
    if not _is_finance(request.user):
        messages.error(request, "غير مصرح بهذا الإجراء.")
        return redirect("website:home")

    payout = get_object_or_404(
        Payout.objects.select_for_update().select_related("agreement", "agreement__request"),
        pk=pk,
    )

    if payout.status == Payout.Status.PAID:
        messages.info(request, "أمر الصرف مدفوع مسبقًا.")
        return redirect("finance:payouts_list")

    req = None
    ag = getattr(payout, "agreement", None)
    if ag:
        req = getattr(ag, "request", None)

    disputed_val = getattr(getattr(Request, "Status", None), "DISPUTED", "disputed")
    if req and (getattr(req, "status", "") == disputed_val):
        messages.error(request, "لا يمكن الصرف لأن الطلب في حالة نزاع.")
        return redirect("finance:payouts_list")

    method = (request.POST.get("method") or "").strip()
    ref_code = (request.POST.get("ref_code") or "").strip()
    note = (request.POST.get("note") or "").strip()

    try:
        payout.mark_paid(method=method, ref=ref_code)
        if note:
            payout.note = note[:255]
            payout.save(update_fields=["note", "updated_at"])

        _log_ledger_once(
            entry_type=getattr(LedgerEntry.Type, "EMPLOYEE_PAYOUT", "employee_payout") if LedgerEntry else "employee_payout",
            direction=getattr(LedgerEntry.Direction, "OUT", "out") if LedgerEntry else "out",
            amount=payout.amount,
            payout=payout,
            invoice=getattr(payout, "invoice", None),
            user=request.user,
            note=f"صرف مستحقات Payout #{payout.pk}",
        )

        messages.success(request, "تم وسم أمر الصرف كمدفوع وتسجيل الحركة في الخزينة.")
        return redirect("finance:payouts_list")
    except Exception as e:
        logger.exception("mark_payout_paid failed: %s", e)
        transaction.set_rollback(True)
        messages.error(request, "تعذر تحديث أمر الصرف. حاول مرة أخرى.")
        return redirect("finance:payouts_list")


# ===========================
# Refunds Dashboard
# ===========================
@login_required
@require_GET
def refunds_dashboard(request: HttpRequest):
    if not _is_finance(request.user):
        messages.error(request, "غير مصرح بهذه الصفحة.")
        return redirect("website:home")

    PAID_VAL = getattr(getattr(Invoice, "Status", None), "PAID", "paid")
    CANCEL_INV = getattr(getattr(Invoice, "Status", None), "CANCELLED", "cancelled")

    cancelled_vals = []
    try:
        st = getattr(Request, "Status", None)
        if st:
            for name in ["CANCELLED", "ADMIN_CANCELLED", "CANCELLED_BY_ADMIN"]:
                v = getattr(st, name, None)
                if v:
                    cancelled_vals.append(v)
    except Exception:
        pass
    if not cancelled_vals:
        cancelled_vals = ["cancelled"]

    cancelled_set = {str(v).lower() for v in cancelled_vals}

    invoices = (
        Invoice.objects
        .exclude(status=CANCEL_INV)
        .filter(status=PAID_VAL)
        .select_related("agreement", "agreement__request", "agreement__employee", "agreement__request__client")
        .order_by("-paid_at", "-issued_at", "-id")
    )

    refunds = (
        Refund.objects
        .select_related("invoice", "invoice__agreement", "invoice__agreement__request")
        .order_by("-created_at", "-id")
    )

    refunded_total = refunds.filter(status=Refund.Status.SENT).aggregate(s=Sum("amount"))["s"] or Decimal("0.00")
    pending_total = refunds.filter(status=Refund.Status.PENDING).aggregate(s=Sum("amount"))["s"] or Decimal("0.00")

    eligible_rows = []
    refundable_total = Decimal("0.00")

    for inv in invoices:
        ag = getattr(inv, "agreement", None)
        if not ag:
            continue
        req = getattr(ag, "request", None)
        if not req:
            continue

        is_cancelled = (str(getattr(req, "status", "")).lower() in cancelled_set)
        if not is_cancelled:
            continue

        gross = _invoice_client_total(inv, ag)

        refunded_so_far = (
            Refund.objects
            .filter(invoice=inv)
            .exclude(status=Refund.Status.CANCELLED)
            .aggregate(s=Sum("amount"))["s"]
            or Decimal("0.00")
        )

        refundable_left = gross - refunded_so_far
        if refundable_left <= 0:
            refundable_left = Decimal("0.00")

        refundable_total += refundable_left

        eligible_rows.append({
            "invoice": inv,
            "agreement": ag,
            "request": req,
            "gross": _q2(gross),
            "refunded_so_far": _q2(refunded_so_far),
            "refundable_left": _q2(refundable_left),
        })

    return render(request, "finance/refunds_dashboard.html", {
        "refunds": refunds,
        "eligible_rows": eligible_rows,
        "refunded_total": _q2(refunded_total),
        "pending_total": _q2(pending_total),
        "refundable_total": _q2(refundable_total),
    })


# ===========================
# Create Refund (POST)
# ===========================
@login_required
@require_POST
@transaction.atomic
def refund_create(request: HttpRequest, invoice_id: int):
    if not _is_finance(request.user):
        messages.error(request, "غير مصرح بهذا الإجراء.")
        return redirect("website:home")

    inv = get_object_or_404(
        Invoice.objects.select_for_update().select_related("agreement", "agreement__request"),
        pk=invoice_id
    )

    PAID_VAL = getattr(getattr(Invoice, "Status", None), "PAID", "paid")
    if (getattr(inv, "status", "") or "").lower() != (PAID_VAL or "").lower():
        messages.error(request, "لا يمكن إنشاء مرتجع لفاتورة غير مدفوعة.")
        return redirect("finance:refunds_dashboard")

    ag = getattr(inv, "agreement", None)
    req = getattr(ag, "request", None) if ag else None

    gross = _invoice_client_total(inv, ag)

    refunded_so_far = (
        Refund.objects
        .filter(invoice=inv)
        .exclude(status=Refund.Status.CANCELLED)
        .aggregate(s=Sum("amount"))["s"]
        or Decimal("0.00")
    )

    refundable_left = gross - refunded_so_far
    if refundable_left <= 0:
        messages.info(request, "تم إرجاع كامل المبلغ مسبقًا لهذه الفاتورة.")
        return redirect("finance:refunds_dashboard")

    form = RefundCreateForm(request.POST, max_amount=_q2(refundable_left))
    if not form.is_valid():
        messages.error(request, "تحقق من قيمة المبلغ.")
        return redirect("finance:refunds_dashboard")

    refund: Refund = form.save(commit=False)
    refund.invoice = inv
    refund.request = req
    refund.created_by = request.user
    refund.status = Refund.Status.PENDING
    refund.save()

    messages.success(request, f"تم إنشاء مرتجع جديد #{refund.id} بقيمة {refund.amount} ريال.")
    return redirect("finance:refunds_dashboard")


# ===========================
# Mark Refund Sent / Cancel
# ===========================
@login_required
@require_POST
@transaction.atomic
def refund_mark_sent(request: HttpRequest, pk: int):
    if not _is_finance(request.user):
        messages.error(request, "غير مصرح.")
        return redirect("website:home")

    refund = get_object_or_404(Refund.objects.select_for_update().select_related("invoice"), pk=pk)
    if refund.status != Refund.Status.PENDING:
        messages.info(request, "لا يمكن وسم هذا المرتجع لأنه ليس بانتظار التنفيذ.")
        return redirect("finance:refunds_dashboard")

    method = (request.POST.get("method") or "").strip()
    ref_code = (request.POST.get("ref_code") or "").strip()

    refund.mark_sent(method=method, ref=ref_code)

    _log_ledger_once(
        entry_type=getattr(LedgerEntry.Type, "CLIENT_REFUND", "client_refund") if LedgerEntry else "client_refund",
        direction=getattr(LedgerEntry.Direction, "OUT", "out") if LedgerEntry else "out",
        amount=refund.amount,
        refund=refund,
        invoice=refund.invoice,
        user=request.user,
        note=f"Refund للعميل #{refund.pk}",
    )

    messages.success(request, "تم وسم المرتجع كتم الإرجاع وتسجيل الحركة في الخزينة.")
    return redirect("finance:refunds_dashboard")


@login_required
@require_POST
def refund_cancel(request: HttpRequest, pk: int):
    if not _is_finance(request.user):
        messages.error(request, "غير مصرح.")
        return redirect("website:home")

    refund = get_object_or_404(Refund.objects.select_related("invoice"), pk=pk)
    if refund.status == Refund.Status.SENT:
        messages.error(request, "لا يمكن إلغاء مرتجع تم تنفيذه.")
        return redirect("finance:refunds_dashboard")

    reason = (request.POST.get("reason") or "").strip()
    refund.cancel(reason=reason)
    messages.success(request, "تم إلغاء المرتجع.")
    return redirect("finance:refunds_dashboard")


# ===========================
# إعدادات النِّسَب
# ===========================
@login_required
@require_http_methods(["GET", "POST"])
def settings_view(request: HttpRequest):
    if not _is_finance(request.user):
        messages.error(request, "غير مصرح بالوصول إلى إعدادات المالية.")
        return redirect("website:home")

    cfg = FinanceSettings.get_solo()
    if request.method == "POST":
        form = FinanceSettingsForm(request.POST, instance=cfg)
        if form.is_valid():
            form.save()
            invalidate_finance_cfg_cache()
            messages.success(request, "تم حفظ إعدادات المالية بنجاح.")
            return redirect("finance:settings")
        messages.error(request, "تحقّق من القيم المُدخلة ثم أعد المحاولة.")
    else:
        form = FinanceSettingsForm(instance=cfg)

    cached = get_finance_cfg(force=False)

    return render(
        request,
        "finance/settings.html",
        {
            "form": form,
            "cached_fee": cached.platform_fee_percent,
            "cached_vat": cached.vat_rate,
        },
    )


# ===========================
# لوحة المبالغ المجمدة بالنزاعات
# ===========================
try:
    from disputes.models import Dispute
except Exception:
    Dispute = None

RefundModel = Refund


@login_required
@require_GET
def disputes_dashboard(request: HttpRequest):
    if not _is_finance(request.user):
        messages.error(request, "غير مصرح بهذه الصفحة.")
        return redirect("website:home")

    if Dispute is None:
        messages.error(request, "تطبيق النزاعات غير متاح حاليًا.")
        return redirect("finance:finance_home")

    OPEN = getattr(getattr(Dispute, "Status", None), "OPEN", "open")
    IN_REVIEW = getattr(getattr(Dispute, "Status", None), "IN_REVIEW", "in_review")
    ACTIVE_SET = [OPEN, IN_REVIEW]

    PAID_VAL = getattr(getattr(Invoice, "Status", None), "PAID", "paid")
    CANCEL_VAL = getattr(getattr(Invoice, "Status", None), "CANCELLED", "cancelled")
    REFUNDED_VAL = getattr(getattr(Invoice, "Status", None), "REFUNDED", "refunded")

    disputes = (
        Dispute.objects.filter(status__in=ACTIVE_SET)
        .select_related("request")
        .order_by("-created_at", "-id")
    )

    rows = []
    hold_employee_total = Decimal("0.00")
    hold_client_total = Decimal("0.00")

    for d in disputes:
        req = getattr(d, "request", None)
        if not req:
            continue

        ag = Agreement.objects.filter(request=req).order_by("-id").first()
        inv = (
            Invoice.objects.filter(agreement__request=req)
            .exclude(status__in=[CANCEL_VAL, REFUNDED_VAL])
            .order_by("-issued_at", "-id")
            .first()
        )

        if not ag or not inv:
            continue

        totals = compute_agreement_totals(ag)
        P = _as_decimal(totals.get("P", 0))
        fee = _as_decimal(totals.get("platform_fee", 0))
        grand = _as_decimal(totals.get("grand_total", 0))

        employee_hold = _q2(P - fee)
        if employee_hold < 0:
            employee_hold = Decimal("0.00")

        is_invoice_paid = (getattr(inv, "status", "") == PAID_VAL)
        client_hold = grand if is_invoice_paid else Decimal("0.00")

        payout_latest = (
            Payout.objects.filter(agreement=ag)
            .order_by("-issued_at", "-id")
            .first()
        )

        refund_latest = None
        if RefundModel:
            try:
                refund_latest = (
                    RefundModel.objects.filter(request=req)
                    .order_by("-created_at", "-id")
                    .first()
                )
            except Exception:
                refund_latest = None

        hold_employee_total += employee_hold
        hold_client_total += client_hold

        rows.append({
            "dispute": d,
            "request": req,
            "agreement": ag,
            "invoice": inv,
            "is_paid": is_invoice_paid,
            "employee_hold": employee_hold,
            "client_hold": client_hold,
            "payout_latest": payout_latest,
            "refund_latest": refund_latest,
        })

    return render(request, "finance/disputes_dashboard.html", {
        "rows": rows,
        "hold_employee_total": _q2(hold_employee_total),
        "hold_client_total": _q2(hold_client_total),
    })


@login_required
@require_POST
@transaction.atomic
def dispute_release(request: HttpRequest, dispute_id: int):
    if not _is_finance(request.user):
        messages.error(request, "غير مصرح بهذا الإجراء.")
        return redirect("website:home")

    if Dispute is None:
        messages.error(request, "تطبيق النزاعات غير متاح.")
        return redirect("finance:disputes_dashboard")

    d = get_object_or_404(
        Dispute.objects.select_for_update().select_related("request"),
        pk=dispute_id,
    )
    req = d.request
    ag = Agreement.objects.filter(request=req).order_by("-id").first()
    inv = Invoice.objects.filter(agreement__request=req).order_by("-issued_at", "-id").first()

    if not ag or not inv:
        messages.error(request, "لا توجد اتفاقية/فاتورة مرتبطة.")
        return redirect("finance:disputes_dashboard")

    PAID_VAL = getattr(getattr(Invoice, "Status", None), "PAID", "paid")
    if getattr(inv, "status", "") != PAID_VAL:
        messages.error(request, "لا يمكن الصرف قبل سداد الفاتورة.")
        return redirect("finance:disputes_dashboard")

    already = Payout.objects.filter(
        agreement=ag,
        status__in=[Payout.Status.PENDING, Payout.Status.PAID]
    ).exists()
    if already:
        messages.info(request, "تم إنشاء أمر صرف لهذا النزاع مسبقًا.")
        return redirect("finance:disputes_dashboard")

    totals = compute_agreement_totals(ag)
    P = _as_decimal(totals.get("P", 0))
    fee = _as_decimal(totals.get("platform_fee", 0))
    net = _q2(P - fee)
    if net < 0:
        net = Decimal("0.00")

    Payout.objects.create(
        employee=ag.employee,
        agreement=ag,
        invoice=inv,
        amount=net,
        status=Payout.Status.PENDING,
        note=f"Release من نزاع #{d.pk}",
    )

    RESOLVED = getattr(getattr(Dispute, "Status", None), "RESOLVED", None)
    if RESOLVED:
        d.status = RESOLVED
        d.save(update_fields=["status"])

    messages.success(request, "تم إنشاء أمر صرف صافي الموظف بعد فك التجميد.")
    return redirect("finance:disputes_dashboard")


@login_required
@require_POST
@transaction.atomic
def dispute_refund(request: HttpRequest, dispute_id: int):
    if not _is_finance(request.user):
        messages.error(request, "غير مصرح بهذا الإجراء.")
        return redirect("website:home")

    if Dispute is None:
        messages.error(request, "تطبيق النزاعات غير متاح.")
        return redirect("finance:disputes_dashboard")

    d = get_object_or_404(
        Dispute.objects.select_for_update().select_related("request"),
        pk=dispute_id,
    )
    req = d.request
    ag = Agreement.objects.filter(request=req).order_by("-id").first()
    inv = Invoice.objects.filter(agreement__request=req).order_by("-issued_at", "-id").first()

    if not ag or not inv:
        messages.error(request, "لا توجد اتفاقية/فاتورة مرتبطة.")
        return redirect("finance:disputes_dashboard")

    PAID_VAL = getattr(getattr(Invoice, "Status", None), "PAID", "paid")
    if getattr(inv, "status", "") != PAID_VAL:
        messages.error(request, "لا يمكن رد مبلغ قبل سداد الفاتورة.")
        return redirect("finance:disputes_dashboard")

    totals = compute_agreement_totals(ag)
    grand = _as_decimal(totals.get("grand_total", 0))
    amount = _as_decimal(request.POST.get("amount") or grand)
    amount = _q2(amount)

    if amount <= 0:
        messages.error(request, "مبلغ الرد يجب أن يكون أكبر من صفر.")
        return redirect("finance:disputes_dashboard")

    if amount > grand:
        messages.error(request, "مبلغ الرد لا يمكن أن يتجاوز إجمالي العميل.")
        return redirect("finance:disputes_dashboard")

    Payout.objects.filter(
        agreement=ag,
        status=Payout.Status.PENDING
    ).update(status=Payout.Status.CANCELLED, note="تم الإلغاء بسبب Refund نزاع")

    refund_fields = {
        "request": req,
        "invoice": inv,
        "amount": amount,
        "status": Refund.Status.PENDING,
        "reason": f"Refund من نزاع #{d.pk}",
    }
    model_field_names = {f.name for f in RefundModel._meta.get_fields()}
    safe_fields = {k: v for k, v in refund_fields.items() if k in model_field_names}
    RefundModel.objects.create(**safe_fields)

    RESOLVED = getattr(getattr(Dispute, "Status", None), "RESOLVED", None)
    if RESOLVED:
        d.status = RESOLVED
        d.save(update_fields=["status"])

    messages.success(request, "تم إنشاء طلب Refund للعميل وفك التجميد.")
    return redirect("finance:disputes_dashboard")
