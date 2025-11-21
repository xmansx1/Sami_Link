# finance/services/pricing.py
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from typing import Any, Optional, Mapping, Hashable, Tuple

from django.conf import settings

# نقرأ النِّسَب من FinanceSettings عند الحاجة (مصدر الحقيقة الوحيد)
# لا توجد دورة استيراد لأن models لا تستورد هذا الملف.
try:
    from finance.models import FinanceSettings  # type: ignore
except Exception:  # أثناء الهجرات المبكرة قد يفشل
    FinanceSettings = None  # سيتم التعامل مع ذلك في _current_rates لاحقًا


# ========= الإعدادات العامة (ليست للنِّسَب) =========
def _get_setting_decimal(name: str, default_str: str) -> Decimal:
    val = getattr(settings, name, None)
    try:
        return Decimal(str(val if val is not None else default_str))
    except Exception:
        return Decimal(default_str)


MONEY_QUANT = _get_setting_decimal("MONEY_QUANT", "0.01")
ROUNDING = getattr(settings, "MONEY_ROUNDING", ROUND_HALF_UP)

FEE_OVERRIDES: Mapping[str, Mapping[Hashable, Any]] = getattr(settings, "PLATFORM_FEE_OVERRIDES", {}) or {}
DEFAULT_PAYOUT_MODE = getattr(settings, "PAYOUT_MODE", "net_after_fee")


# ========= أدوات دقيقة للأموال =========
def _to_decimal(value: Any, field_name: str = "value") -> Decimal:
    if value is None:
        raise ValueError(f"{field_name} لا يمكن أن يكون None")
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise ValueError(f"قيمة غير صالحة للحقل {field_name}: {value!r}")


def _q(amount: Decimal) -> Decimal:
    return amount.quantize(MONEY_QUANT, rounding=ROUNDING)


def _normalize_percent(x: Any, field_name: str) -> Decimal:
    """
    يطبع النسبة كنطاق [0..1]. إذا كانت بين 1 و100 نفترض أنها مئوية (÷100).
    """
    d = _to_decimal(x, field_name)
    if d < 0:
        raise ValueError(f"{field_name} يجب أن يكون >= 0")
    if d > 1 and d <= 100:
        d = d / Decimal("100")
    if d > 1:
        raise ValueError(f"{field_name} يجب أن يكون <= 1 (أو <=100 قبل القسمة)")
    return d


# ========= نماذج بيانات =========
@dataclass(frozen=True)
class PriceInput:
    project_price: Decimal
    fee_percent: Decimal
    vat_rate: Decimal
    payout_mode: str = "net_after_fee"

    def __post_init__(self):
        if self.project_price < 0:
            raise ValueError("قيمة المشروع (P) لا يمكن أن تكون سالبة.")
        if not (Decimal("0") <= self.fee_percent <= Decimal("1")):
            raise ValueError("نسبة العمولة يجب أن تكون بين 0 و 1.")
        if not (Decimal("0") <= self.vat_rate <= Decimal("1")):
            raise ValueError("نسبة الضريبة يجب أن تكون بين 0 و 1.")
        if self.payout_mode not in {"net_after_fee", "gross_to_tech"}:
            raise ValueError("payout_mode يجب أن يكون 'net_after_fee' أو 'gross_to_tech'.")


@dataclass(frozen=True)
class PriceBreakdown:
    project_price: Decimal
    fee_percent: Decimal
    vat_rate: Decimal
    platform_fee_value: Decimal
    taxable_base: Decimal
    vat_amount: Decimal
    client_total: Decimal
    tech_payout: Decimal

    def as_dict(self) -> dict[str, str]:
        return {
            "project_price": str(self.project_price),
            "fee_percent": str(self.fee_percent),
            "vat_rate": str(self.vat_rate),
            "platform_fee_value": str(self.platform_fee_value),
            "taxable_base": str(self.taxable_base),
            "vat_amount": str(self.vat_amount),
            "client_total": str(self.client_total),
            "tech_payout": str(self.tech_payout),
        }


# ========= مصدر النِّسَب (FinanceSettings فقط) =========
def _current_rates() -> Tuple[Decimal, Decimal]:
    """
    يرجّع (fee, vat) بالاعتماد على FinanceSettings فقط.

    - النِّسَب تُضبط من لوحة المالية (FinanceSettings).
    - لا يتم الرجوع إلى settings أو متغيّرات بيئة للنِّسَب.
    """
    # 1) من FinanceSettings إن وُجد
    if FinanceSettings is not None:
        try:
            fee, vat = FinanceSettings.current_rates()  # يفترض أنها كنِسب (0..1)
            fee = _normalize_percent(fee, "fee_percent")
            vat = _normalize_percent(vat, "vat_rate")
            return fee, vat
        except Exception:
            # في حال حدوث أي خطأ (مثل عدم وجود السجل بعد)
            pass

    # 2) fallback نادر جدًا (مثلاً أثناء الهجرات الأولى)
    fee = Decimal("0.10")  # 10%
    vat = Decimal("0.15")  # 15%
    return _normalize_percent(fee, "fee_percent"), _normalize_percent(vat, "vat_rate")


# ========= Overrides للعمولة =========
def resolve_fee_percent(
    *,
    default_fee: Optional[Decimal] = None,
    client_id: Optional[int] = None,
    employee_id: Optional[int] = None,
    category: Optional[str] = None,
    campaign: Optional[str] = None,
) -> Decimal:
    """
    أولوية: campaign > client_id > employee_id > category > default.

    - base هو FinanceSettings.current_rates() عبر _current_rates.
    - يمكن تعديل النسبة عبر FEE_OVERRIDES في settings (اختياري).
    """
    if default_fee is None:
        fee_default, _ = _current_rates()
        fee = fee_default
    else:
        fee = default_fee

    try:
        by_campaign = FEE_OVERRIDES.get("by_campaign") or {}
        by_client = FEE_OVERRIDES.get("by_client_id") or {}
        by_employee = FEE_OVERRIDES.get("by_employee_id") or {}
        by_category = FEE_OVERRIDES.get("by_category") or {}

        if campaign and campaign in by_campaign:
            fee = _to_decimal(by_campaign[campaign], "campaign_fee")

        if client_id is not None and client_id in by_client:
            fee = _to_decimal(by_client[client_id], "client_fee")

        if employee_id is not None and employee_id in by_employee:
            fee = _to_decimal(by_employee[employee_id], "employee_fee")

        if category and category in by_category:
            fee = _to_decimal(by_category[category], "category_fee")

    except Exception:
        # في حال تنسيق overrides غير صحيح، نتجاهل ونعود للـ default
        pass

    # تطبيع وحدود
    fee = _normalize_percent(fee, "fee_percent")
    return fee


# ========= الحاسبة =========
def compute_breakdown(
    project_price: Any,
    *,
    fee_percent: Optional[Any] = None,
    vat_rate: Optional[Any] = None,
    payout_mode: Optional[str] = None,
) -> PriceBreakdown:
    """
    منهجية الاحتساب:
      - platform_fee_value = P × F
      - taxable_base = P + platform_fee_value
      - vat_amount = taxable_base × V
      - client_total = taxable_base + vat_amount
      - tech_payout = P - platform_fee_value  (net_after_fee)
                    أو  tech_payout = P       (gross_to_tech)
    """
    P = _to_decimal(project_price, "project_price")
    if P < 0:
        raise ValueError("project_price لا يمكن أن يكون سالبًا.")

    if fee_percent is None or vat_rate is None:
        # استحضار القيم الحالية عند الحاجة من FinanceSettings فقط
        default_fee, default_vat = _current_rates()
        F = _normalize_percent(fee_percent if fee_percent is not None else default_fee, "fee_percent")
        V = _normalize_percent(vat_rate if vat_rate is not None else default_vat, "vat_rate")
    else:
        F = _normalize_percent(fee_percent, "fee_percent")
        V = _normalize_percent(vat_rate, "vat_rate")

    mode = (payout_mode or DEFAULT_PAYOUT_MODE).strip()
    if mode not in {"net_after_fee", "gross_to_tech"}:
        raise ValueError("payout_mode غير معروف.")

    platform_fee_value = _q(P * F)
    taxable_base = _q(P)  # الضريبة فقط على قيمة المشروع
    vat_amount = _q(taxable_base * V)
    client_total = _q(P + platform_fee_value + vat_amount)

    tech_payout = _q(P - platform_fee_value) if mode == "net_after_fee" else _q(P)

    return PriceBreakdown(
        project_price=_q(P),
        fee_percent=F,  # نُبقيها كنسبة (0..1)
        vat_rate=V,
        platform_fee_value=platform_fee_value,
        taxable_base=taxable_base,
        vat_amount=vat_amount,
        client_total=client_total,
        tech_payout=tech_payout,
    )


# ========= ربط بالموديلات =========
def _pick_first(*values):
    for v in values:
        if v is not None:
            return v
    return None


def breakdown_for_offer(offer) -> PriceBreakdown:
    P = _pick_first(
        getattr(offer, "proposed_price", None),
        getattr(offer, "estimated_price", None),
        getattr(offer, "final_price", None),
        getattr(offer, "amount", None),
        getattr(offer, "price", None),
    )
    if P is None:
        raise ValueError("Offer لا يحتوي على قيمة سعر صالحة.")

    client_id = None
    category = None
    campaign = None
    try:
        if getattr(offer, "request", None):
            client_id = getattr(offer.request, "client_id", None)
            category = getattr(offer.request, "category", None)
            campaign = getattr(offer.request, "campaign_code", None)
    except Exception:
        pass

    employee_id = getattr(offer, "employee_id", None)

    F = resolve_fee_percent(
        client_id=client_id,
        employee_id=employee_id,
        category=category,
        campaign=campaign,
    )
    # نقرأ VAT الحالي دومًا من FinanceSettings عبر _current_rates
    _, default_vat = _current_rates()
    return compute_breakdown(P, fee_percent=F, vat_rate=default_vat)


def breakdown_for_agreement(agreement) -> PriceBreakdown:
    P = _pick_first(
        getattr(agreement, "total_project_price", None),
        getattr(agreement, "project_price", None),
        getattr(agreement, "total_amount", None),
        getattr(agreement, "amount", None),
    )
    if P is None:
        raise ValueError("Agreement لا يحتوي على قيمة سعر صالحة.")

    client_id = getattr(getattr(agreement, "client", None), "id", None) or getattr(agreement, "client_id", None)
    category = getattr(agreement, "category", None)
    campaign = getattr(agreement, "campaign_code", None)
    employee_id = getattr(getattr(agreement, "employee", None), "id", None) or getattr(agreement, "employee_id", None)

    F = resolve_fee_percent(
        client_id=client_id,
        employee_id=employee_id,
        category=category,
        campaign=campaign,
    )
    _, default_vat = _current_rates()
    return compute_breakdown(P, fee_percent=F, vat_rate=default_vat)


# ========= عرض مناسب للواجهات =========
def _fmt_money(x: Decimal, currency: str | None = None, thousands_sep: str = ",") -> str:
    s = f"{x:,.2f}".replace(",", "X").replace(".", ",").replace("X", thousands_sep)
    return f"{s} {currency}" if currency else s


def format_breakdown_for_display(bd: PriceBreakdown, *, currency: str | None = None, thousands_sep: str = ",") -> dict[str, str]:
    return {
      "قيمة المشروع (P)": _fmt_money(bd.project_price, currency, thousands_sep),
      "نسبة المنصّة (F)": f"{(bd.fee_percent * 100).quantize(Decimal('0.01'))}%",
      "قيمة عمولة المنصّة": _fmt_money(bd.platform_fee_value, currency, thousands_sep),
      "وعاء الضريبة": _fmt_money(bd.taxable_base, currency, thousands_sep),
      "الضريبة (VAT)": _fmt_money(bd.vat_amount, currency, thousands_sep),
      "الإجمالي المطلوب من العميل": _fmt_money(bd.client_total, currency, thousands_sep),
      "صافي المصروف للتقني": _fmt_money(bd.tech_payout, currency, thousands_sep),
    }


# ========= سياسة الـ Escrow =========
def client_should_pay_now(bd: PriceBreakdown) -> Decimal:
    return bd.client_total


def expected_tech_payout_on_complete(bd: PriceBreakdown) -> Decimal:
    return bd.tech_payout
