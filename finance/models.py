from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from typing import Any, Dict, Iterable, Optional, Tuple

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MaxValueValidator, MinValueValidator
from django.db import models, transaction
from django.db.models import Q, Sum
from django.urls import reverse
from django.utils import timezone

logger = logging.getLogger(__name__)

User = settings.AUTH_USER_MODEL


# =========================================================
# أدوات مالية مساعدة (quantize / Decimal)
# =========================================================
def _as_decimal(val: Any) -> Decimal:
    if isinstance(val, Decimal):
        return val
    try:
        return Decimal(str(val if val is not None else "0"))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _q2(val: Any) -> Decimal:
    return _as_decimal(val).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _q4(val: Any) -> Decimal:
    return _as_decimal(val).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def _compute_breakdown(P: Any, pf: Any, vp: Any) -> Dict[str, Decimal]:
    """
    السياسة المعتمدة:
    - العميل لا يتحمل عمولة المنصّة
    - إجمالي العميل = P + VAT(P)
    - صافي الموظف = P - Fee(P)
    """
    P = _q2(P)
    pf = _q4(pf)
    vp = _q4(vp)

    fee_value = _q2(P * pf)   # تخصم من الموظف
    vat_value = _q2(P * vp)   # ضريبة على السعر فقط

    client_total = _q2(P + vat_value)
    tech_net = _q2(P - fee_value) if P >= fee_value else Decimal("0.00")

    return {
        "P": P,
        "fee_percent": pf,
        "fee_value": fee_value,
        "vat_percent": vp,
        "vat_value": vat_value,
        "subtotal": P,
        "client_total": client_total,
        "tech_net": tech_net,
    }


# =========================================================
# إعدادات مالية عامة (نِسب عمولة المنصّة والضريبة VAT)
# =========================================================
class FinanceSettings(models.Model):
    """
    إعدادات مالية عامة (صف واحد فقط).
    - platform_fee_percent و vat_rate كنِسب بين 0..1
    """

    platform_fee_percent = models.DecimalField(
        "نسبة عمولة المنصّة (0..1)",
        max_digits=5,
        decimal_places=4,
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("1"))],
        default=Decimal("0.10"),
    )
    vat_rate = models.DecimalField(
        "نسبة VAT (0..1)",
        max_digits=5,
        decimal_places=4,
        validators=[MinValueValidator(Decimal("0")), MaxValueValidator(Decimal("1"))],
        default=Decimal("0.15"),
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "إعدادات مالية"
        verbose_name_plural = "إعدادات مالية"

    def __str__(self) -> str:
        return f"FinanceSettings(fee={self.platform_fee_percent}, vat={self.vat_rate})"

    @classmethod
    def get_solo(cls) -> "FinanceSettings":
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    @classmethod
    def current_rates(cls) -> Tuple[Decimal, Decimal]:
        try:
            cfg = cls.get_solo()
            fee = cfg.platform_fee_percent
            vat = cfg.vat_rate
        except Exception:
            fee = Decimal("0.10")
            vat = Decimal("0.15")

        return _q4(fee), _q4(vat)


# =========================================================
# أوامر صرف مستحقات الموظفين
# =========================================================
class Payout(models.Model):
    """سجل صرف مستحقات موظف (الصافي المستحق بعد خصم عمولة المنصّة)."""

    class Status(models.TextChoices):
        PENDING = "pending", "قيد المراجعة"
        PAID = "paid", "مدفوع"
        CANCELLED = "cancelled", "ملغي"

    employee = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name="payouts",
        verbose_name="الموظف",
    )
    agreement = models.ForeignKey(
        "agreements.Agreement",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="payouts",
        verbose_name="الاتفاقية",
    )
    invoice = models.ForeignKey(
        "finance.Invoice",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="payouts",
        verbose_name="الفاتورة",
    )

    amount = models.DecimalField(
        "المبلغ المصروف للموظف",
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0"))],
        help_text="صافي المبلغ بعد خصم عمولة المنصّة",
    )
    status = models.CharField(
        "الحالة",
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    method = models.CharField("طريقة الصرف", max_length=50, blank=True, default="")
    ref_code = models.CharField("مرجع العملية", max_length=100, blank=True, default="")
    note = models.CharField("ملاحظة", max_length=255, blank=True, default="")
    issued_at = models.DateTimeField("تاريخ الإنشاء", default=timezone.now, db_index=True)
    paid_at = models.DateTimeField("تاريخ الصرف", null=True, blank=True, db_index=True)
    updated_at = models.DateTimeField("آخر تحديث", auto_now=True)

    class Meta:
        ordering = ["-issued_at", "-id"]
        indexes = [
            models.Index(fields=["status", "issued_at"]),
            models.Index(fields=["employee", "status"]),
        ]
        verbose_name = "أمر صرف"
        verbose_name_plural = "أوامر الصرف"

    def __str__(self) -> str:
        return f"Payout#{self.pk} to {self.employee_id} — {self.amount} ({self.status})"

    @property
    def is_paid(self) -> bool:
        return self.status == self.Status.PAID

    @property
    def is_pending(self) -> bool:
        return self.status == self.Status.PENDING

    def mark_paid(
        self,
        *,
        method: str = "",
        ref: str = "",
        paid_at=None,
        by_user: Optional[object] = None,
        save: bool = True,
    ) -> "Payout":
        if self.status == self.Status.PAID:
            return self

        with transaction.atomic():
            self.status = self.Status.PAID
            if method:
                self.method = (method or "")[:50]
            if ref:
                self.ref_code = (ref or "")[:100]
            if not self.paid_at:
                self.paid_at = paid_at or timezone.now()

            if by_user and hasattr(self, "updated_by"):
                setattr(self, "updated_by", by_user)

            if save:
                self.save(
                    update_fields=[
                        "status",
                        "method",
                        "ref_code",
                        "paid_at",
                        "updated_at",
                    ]
                )

            agreement = getattr(self, "agreement", None)
            req = getattr(agreement, "request", None) if agreement else None
            if req and hasattr(req, "mark_paid_and_start"):
                try:
                    req.mark_paid_and_start()
                except Exception as exc:
                    logger.exception(
                        "Failed to update request to IN_PROGRESS after payout paid. "
                        f"payout={self.pk} request={getattr(req, 'pk', None)} err={exc}"
                    )

        return self


# =========================================================
# توريدات الضريبة (VAT Remittance)
# =========================================================
class TaxRemittance(models.Model):
    """
    يمثل عملية توريد/خصم ضريبة VAT للجهة الحكومية.
    """

    class Status(models.TextChoices):
        PENDING = "pending", "قيد الانتظار"
        SENT = "sent", "تم التوريد"
        CANCELLED = "cancelled", "ملغي"

    amount = models.DecimalField(
        "مبلغ التوريد",
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0.01"))],
        help_text="إجمالي الضريبة المُورَّدة في هذه العملية.",
    )
    period_from = models.DateField("من تاريخ", null=True, blank=True)
    period_to = models.DateField("إلى تاريخ", null=True, blank=True)

    status = models.CharField(
        "الحالة",
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    ref_code = models.CharField(
        "مرجع التوريد",
        max_length=100,
        blank=True,
        default="",
        db_index=True,
        help_text="رقم مرجع السداد/التحويل للضريبة.",
    )
    note = models.CharField("ملاحظة", max_length=255, blank=True, default="")

    created_at = models.DateTimeField("تاريخ الإنشاء", auto_now_add=True, db_index=True)
    sent_at = models.DateTimeField("تاريخ التوريد", null=True, blank=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["period_from", "period_to"]),
        ]
        verbose_name = "توريد ضريبة"
        verbose_name_plural = "توريدات الضريبة"

    def __str__(self) -> str:
        return f"TaxRemittance#{self.pk} {self.amount} ({self.get_status_display()})"

    def mark_sent(self, *, ref: str = "") -> None:
        if self.status == self.Status.SENT:
            return
        self.status = self.Status.SENT
        if ref:
            self.ref_code = ref[:100]
        if not self.sent_at:
            self.sent_at = timezone.now()
        self.save(update_fields=["status", "ref_code", "sent_at", "updated_at"])


# =========================================================
# QuerySet مخصّص للفواتير
# =========================================================
class InvoiceQuerySet(models.QuerySet):
    def unpaid(self):
        return self.filter(status=Invoice.Status.UNPAID)

    def paid(self):
        return self.filter(status=Invoice.Status.PAID)

    def cancelled(self):
        return self.filter(status=Invoice.Status.CANCELLED)

    def for_agreement(self, agreement_id: int):
        return self.filter(agreement_id=agreement_id)

    def overdue(self):
        now = timezone.now()
        return self.unpaid().filter(due_at__isnull=False, due_at__lt=now)

    def totals(self) -> Dict[str, Decimal]:
        agg = self.aggregate(
            amount=Sum("amount"),
            fee=Sum("platform_fee_amount"),
            vat=Sum("vat_amount"),
            total=Sum("total_amount"),
        )
        return {
            "amount": agg["amount"] or Decimal("0.00"),
            "fee": agg["fee"] or Decimal("0.00"),
            "vat": agg["vat"] or Decimal("0.00"),
            "total": agg["total"] or Decimal("0.00"),
        }


# =========================================================
# نموذج الفاتورة
# =========================================================
class Invoice(models.Model):
    """
    فاتورة مالية مرتبطة باتفاقية (Agreement)، وقد تُسند إلى مرحلة (Milestone).
    """

    class Status(models.TextChoices):
        UNPAID = "unpaid", "غير مدفوعة"
        PAID = "paid", "مدفوعة"
        CANCELLED = "cancelled", "ملغاة"

    agreement = models.ForeignKey(
        "agreements.Agreement",
        on_delete=models.CASCADE,
        related_name="invoices",
        verbose_name="الاتفاقية",
    )
    milestone = models.ForeignKey(
        "agreements.Milestone",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="invoices",
        verbose_name="الدفعة/المرحلة",
        help_text="يُفضّل فاتورة واحدة لكل مرحلة.",
    )

    amount = models.DecimalField(
        "المبلغ (P)",
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
    )

    platform_fee_percent = models.DecimalField(
        "نسبة المنصّة (0..1)",
        max_digits=5,
        decimal_places=4,
        default=Decimal("0.00"),
        help_text="تُخصم من الموظف فقط.",
    )
    platform_fee_amount = models.DecimalField(
        "قيمة عمولة المنصّة",
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
    )

    vat_percent = models.DecimalField(
        "نسبة الضريبة VAT (0..1)",
        max_digits=5,
        decimal_places=4,
        default=Decimal("0.00"),
        help_text="ضريبة على السعر المقترح فقط.",
    )
    vat_amount = models.DecimalField(
        "قيمة الضريبة",
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
    )

    subtotal = models.DecimalField(
        "المجموع الفرعي (P)",
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="يساوي P للعرض.",
    )
    total_amount = models.DecimalField(
        "الإجمالي المستحق من العميل",
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="= P + ضريبة P.",
    )

    status = models.CharField(
        "الحالة",
        max_length=16,
        choices=Status.choices,
        default=Status.UNPAID,
        db_index=True,
    )
    issued_at = models.DateTimeField("تاريخ الإصدار", default=timezone.now, db_index=True)
    due_at = models.DateTimeField("موعد السداد", null=True, blank=True, db_index=True)
    paid_at = models.DateTimeField("تاريخ السداد", null=True, blank=True, db_index=True)

    method = models.CharField("طريقة السداد", max_length=50, blank=True)
    ref_code = models.CharField(
        "مرجع العملية",
        max_length=100,
        blank=True,
        db_index=True,
        help_text="مرجع الدفع من بوابة/حوالة (قد لا يكون فريدًا).",
    )
    paid_ref = models.CharField(
        "مرجع الدفع البنكي",
        max_length=64,
        blank=True,
        null=True,
        help_text="مرجع التحويل البنكي الذي يرسله العميل.",
    )

    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="invoices_created",
        verbose_name="أنشأها",
    )
    updated_at = models.DateTimeField(auto_now=True)

    objects = InvoiceQuerySet.as_manager()

    class Meta:
        indexes = [
            models.Index(fields=["status", "issued_at"]),
            models.Index(fields=["agreement"]),
            models.Index(fields=["paid_at"]),
            models.Index(fields=["due_at"]),
            models.Index(fields=["paid_ref"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["milestone"],
                condition=Q(milestone__isnull=False),
                name="uniq_invoice_per_milestone",
            ),
        ]
        ordering = ["-issued_at", "-id"]
        verbose_name = "فاتورة"
        verbose_name_plural = "فواتير"

    def __str__(self) -> str:
        return f"Invoice#{self.pk} A{self.agreement_id} — {self.get_status_display()} {self.total_amount}"

    def get_absolute_url(self) -> str:
        return reverse("finance:invoice_detail", kwargs={"pk": self.pk})

    def get_mark_paid_url(self) -> str:
        return reverse("finance:mark_invoice_paid", kwargs={"pk": self.pk})

    @property
    def is_unpaid(self) -> bool:
        return self.status == self.Status.UNPAID

    @property
    def is_paid(self) -> bool:
        return self.status == self.Status.PAID

    @property
    def is_cancelled(self) -> bool:
        return self.status == self.Status.CANCELLED

    @property
    def is_overdue(self) -> bool:
        return bool(self.is_unpaid and self.due_at and self.due_at < timezone.now())

    @property
    def effective_date(self):
        return self.paid_at or self.issued_at

    @property
    def tech_net(self) -> Decimal:
        return _compute_breakdown(self.amount, self.platform_fee_percent, self.vat_percent)["tech_net"]

    @property
    def client_total_amount(self) -> Decimal:
        return _compute_breakdown(self.amount, self.platform_fee_percent, self.vat_percent)["client_total"]

    @property
    def as_breakdown(self) -> Dict[str, Decimal]:
        bd = _compute_breakdown(self.amount, self.platform_fee_percent, self.vat_percent)
        return {
            "P": bd["P"],
            "fee_percent": bd["fee_percent"],
            "fee_value": bd["fee_value"],
            "subtotal": bd["subtotal"],
            "vat_percent": bd["vat_percent"],
            "vat_value": bd["vat_value"],
            "total": bd["client_total"],
            "tech_net": bd["tech_net"],
        }

    def recompute_totals(self) -> None:
        pf, vp = FinanceSettings.current_rates()

        if not self.platform_fee_percent or self.platform_fee_percent == Decimal("0.00"):
            self.platform_fee_percent = pf
        if not self.vat_percent or self.vat_percent == Decimal("0.00"):
            self.vat_percent = vp

        bd = _compute_breakdown(self.amount, self.platform_fee_percent, self.vat_percent)
        self.platform_fee_amount = bd["fee_value"]
        self.vat_amount = bd["vat_value"]
        self.subtotal = bd["subtotal"]
        self.total_amount = bd["client_total"]

    def recalc_and_save(self, *, update_timestamps: bool = True) -> None:
        self.recompute_totals()
        fields = [
            "platform_fee_percent",
            "platform_fee_amount",
            "subtotal",
            "vat_percent",
            "vat_amount",
            "total_amount",
        ]
        if update_timestamps:
            fields.append("updated_at")
        self.save(update_fields=fields)

    def clean(self):
        super().clean()

        for fld in ("amount", "platform_fee_amount", "subtotal", "vat_amount", "total_amount"):
            val = _as_decimal(getattr(self, fld, None))
            if val < 0:
                raise ValidationError({fld: "لا يمكن أن يكون سالبًا."})

        for fld in ("platform_fee_percent", "vat_percent"):
            v = _as_decimal(getattr(self, fld, 0))
            if v < 0 or v > 1:
                raise ValidationError({fld: "النسبة يجب أن تكون بين 0 و 1."})

        if self.milestone_id and self.agreement_id:
            ms_agreement_id = getattr(self.milestone, "agreement_id", None)
            if ms_agreement_id and ms_agreement_id != self.agreement_id:
                raise ValidationError("الاتفاقية المرتبطة لا تتطابق مع اتفاقية المرحلة.")

        if self.due_at and self.issued_at and self.due_at < self.issued_at:
            raise ValidationError({"due_at": "موعد السداد لا يمكن أن يسبق تاريخ الإصدار."})

        if self.paid_at and self.issued_at and self.paid_at < self.issued_at:
            raise ValidationError({"paid_at": "تاريخ السداد لا يمكن أن يسبق تاريخ الإصدار."})

    def save(self, *args, **kwargs):
        is_new = self.pk is None
        self.recompute_totals()
        result = super().save(*args, **kwargs)

        try:
            if is_new:
                from core.notifications.utils import notify_finance_of_invoice
                notify_finance_of_invoice(self)
        except Exception:
            pass

        try:
            from django.contrib.auth import get_user_model
            from notifications.utils import create_notification

            UserModel = get_user_model()
            admin_users = UserModel.objects.filter(role="admin", is_active=True)

            if self.status != self.Status.PAID and self.issued_at:
                overdue_days = 3
                if (timezone.now() - self.issued_at).days >= overdue_days:
                    for user in admin_users:
                        create_notification(
                            recipient=user,
                            title=f"فاتورة متأخرة #{self.pk}",
                            body=(
                                f"فاتورة بقيمة {self.amount} ر.س للطلب المرتبط "
                                f"لم تُدفع منذ أكثر من {overdue_days} أيام."
                            ),
                            url=self.get_absolute_url(),
                            actor=getattr(self.agreement, "employee", None),
                            target=self,
                        )
        except Exception:
            pass

        return result

    def mark_paid(
        self,
        *,
        by_user: Optional[object] = None,
        method: str = "",
        ref_code: str = "",
        paid_ref: str = "",
        paid_at=None,
        save: bool = True,
    ) -> "Invoice":
        if self.status == self.Status.PAID:
            return self

        self.status = self.Status.PAID
        self.method = (method or self.method or "")[:50]
        self.ref_code = (ref_code or self.ref_code or "")[:100]
        if paid_ref:
            self.paid_ref = (paid_ref or "")[:64]
        self.paid_at = paid_at or self.paid_at or timezone.now()

        if by_user and hasattr(self, "updated_by"):
            setattr(self, "updated_by", by_user)

        if save:
            fields = [
                "status",
                "method",
                "ref_code",
                "paid_ref",
                "paid_at",
                "updated_at",
                "platform_fee_percent",
                "platform_fee_amount",
                "subtotal",
                "vat_percent",
                "vat_amount",
                "total_amount",
            ]
            self.save(update_fields=fields)

        return self

    def cancel(self, *, by_user: Optional[object] = None, reason: str = "", save: bool = True) -> "Invoice":
        if self.status == self.Status.CANCELLED:
            return self
        self.status = self.Status.CANCELLED
        if by_user and hasattr(self, "updated_by"):
            setattr(self, "updated_by", by_user)
        if save:
            self.save(update_fields=["status", "updated_at"])
        return self

    @classmethod
    def unpaid_for_agreement(cls, agreement_id: int):
        return cls.objects.for_agreement(agreement_id).unpaid()

    @classmethod
    def all_paid_for_agreement(cls, agreement_id: int) -> bool:
        return not cls.unpaid_for_agreement(agreement_id).exists()

    @classmethod
    def totals_by_status(cls) -> Dict[str, Decimal]:
        paid = cls.objects.paid().totals()["total"]
        unpaid = cls.objects.unpaid().totals()["total"]
        agg_all = cls.objects.all().totals()
        return {
            "paid": paid,
            "unpaid": unpaid,
            "fee": agg_all["fee"],
            "vat": agg_all["vat"],
            "total": agg_all["total"],
        }

    def set_due_in_days(self, days: int = 3, save: bool = True):
        self.due_at = timezone.now() + timedelta(days=max(0, int(days)))
        if save:
            self.save(update_fields=["due_at", "updated_at"])

    @classmethod
    def _defaults_from_settings_or_cfg(
        cls,
        platform_fee_percent: Optional[Decimal],
        vat_percent: Optional[Decimal],
    ) -> Tuple[Decimal, Decimal]:
        if platform_fee_percent is not None and vat_percent is not None:
            return _q4(platform_fee_percent), _q4(vat_percent)

        fee, vat = FinanceSettings.current_rates()
        pf = platform_fee_percent if platform_fee_percent is not None else fee
        vp = vat_percent if vat_percent is not None else vat
        return _q4(pf), _q4(vp)

    @classmethod
    def create_for_milestone(
        cls,
        *,
        milestone,
        amount: Optional[Decimal] = None,
        due_days: int = 3,
        created_by=None,
        platform_fee_percent: Optional[Decimal] = None,
        vat_percent: Optional[Decimal] = None,
    ) -> "Invoice":
        if milestone is None:
            raise ValidationError("لا يمكن إنشاء فاتورة: المرحلة غير مرفقة.")

        pf, vp = cls._defaults_from_settings_or_cfg(platform_fee_percent, vat_percent)

        with transaction.atomic():
            inv, _ = cls.objects.select_for_update().get_or_create(
                milestone=milestone,
                defaults={
                    "agreement": milestone.agreement,
                    "amount": (
                        amount
                        if amount is not None
                        else getattr(milestone, "amount", None)
                        or (
                            milestone.agreement.total_amount
                            / max(milestone.agreement.milestones.count(), 1)
                        )
                    ),
                    "platform_fee_percent": pf,
                    "vat_percent": vp,
                    "status": cls.Status.UNPAID,
                    "issued_at": timezone.now(),
                    "created_by": created_by if created_by else None,
                },
            )
            if not inv.due_at:
                inv.set_due_in_days(days=due_days, save=True)
            return inv

    @classmethod
    def create_full_upfront_for_agreement(
        cls,
        *,
        agreement,
        created_by=None,
        platform_fee_percent: Optional[Decimal] = None,
        vat_percent: Optional[Decimal] = None,
        due_days: int = 3,
    ) -> "Invoice":
        if agreement is None:
            raise ValidationError("الاتفاقية غير موجودة.")

        P = getattr(agreement, "total_amount", None)
        if P is None:
            parts: Iterable[Decimal] = []
            for m in agreement.milestones.all():
                amt = getattr(m, "amount", None)
                if amt is not None:
                    parts.append(Decimal(amt))
            P = sum(parts, start=Decimal("0.00"))

        if P is None:
            raise ValidationError("تعذر تحديد مبلغ الاتفاقية لإنشاء الفاتورة.")

        pf, vp = cls._defaults_from_settings_or_cfg(platform_fee_percent, vat_percent)

        with transaction.atomic():
            inv = cls.objects.create(
                agreement=agreement,
                amount=P,
                platform_fee_percent=pf,
                vat_percent=vp,
                status=cls.Status.UNPAID,
                issued_at=timezone.now(),
                created_by=created_by if created_by else None,
            )
            inv.set_due_in_days(days=due_days, save=True)
            return inv

    @classmethod
    def ensure_single_unpaid_for_agreement(
        cls,
        *,
        agreement,
        amount: Optional[Decimal] = None,
        platform_fee_percent: Optional[Decimal] = None,
        vat_percent: Optional[Decimal] = None,
    ) -> "Invoice":
        if agreement is None:
            raise ValidationError("الاتفاقية غير موجودة.")

        pf, vp = cls._defaults_from_settings_or_cfg(platform_fee_percent, vat_percent)

        with transaction.atomic():
            inv = (
                cls.objects.select_for_update()
                .filter(agreement=agreement, milestone__isnull=True)
                .order_by("id")
                .first()
            )
            if inv:
                if amount is not None and inv.amount != amount:
                    inv.amount = amount
                inv.platform_fee_percent = inv.platform_fee_percent or pf
                inv.vat_percent = inv.vat_percent or vp
                inv.recalc_and_save()
                return inv

            return cls.objects.create(
                agreement=agreement,
                amount=amount if amount is not None else Decimal("0.00"),
                platform_fee_percent=pf,
                vat_percent=vp,
                status=cls.Status.UNPAID,
                issued_at=timezone.now(),
            )


# =========================================================
# مُساعد عام لحساب صافي الموظف من فواتير مدفوعة
# =========================================================
def employee_net_from_paid_invoices(employee_id: int) -> Decimal:
    paid_val = Invoice.Status.PAID
    qs = Invoice.objects.filter(agreement__employee_id=employee_id, status=paid_val)
    agg = qs.aggregate(p=Sum("amount"), fee=Sum("platform_fee_amount"))
    P = agg["p"] or Decimal("0.00")
    F = agg["fee"] or Decimal("0.00")
    return (P - F) if P >= F else Decimal("0.00")


# =========================================================
# Refunds / مرتجعات العملاء
# =========================================================
class Refund(models.Model):
    """مرتجع مالي للعميل مرتبط بفاتورة مدفوعة."""

    class Status(models.TextChoices):
        PENDING = "pending", "بانتظار التنفيذ"
        SENT = "sent", "تم الإرجاع"
        FAILED = "failed", "فشل الإرجاع"
        CANCELLED = "cancelled", "ملغي"

    invoice = models.ForeignKey(
        Invoice,
        on_delete=models.CASCADE,
        related_name="refunds",
        verbose_name="الفاتورة",
    )
    request = models.ForeignKey(
        "marketplace.Request",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="refunds",
        verbose_name="الطلب",
    )

    amount = models.DecimalField(
        "المبلغ المرجع",
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
    )
    reason = models.TextField("سبب الإرجاع", blank=True)

    status = models.CharField(
        "الحالة",
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
    )

    method = models.CharField("طريقة الإرجاع", max_length=50, blank=True)
    ref_code = models.CharField("مرجع الإرجاع", max_length=100, blank=True)

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_refunds",
        verbose_name="أنشئ بواسطة",
    )

    created_at = models.DateTimeField("تاريخ الإنشاء", auto_now_add=True)
    updated_at = models.DateTimeField("آخر تحديث", auto_now=True)
    sent_at = models.DateTimeField("تاريخ الإرجاع", null=True, blank=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        verbose_name = "مرتجع عميل"
        verbose_name_plural = "مرتجعات العملاء"

    def __str__(self) -> str:
        return f"Refund #{self.pk} - Invoice #{self.invoice_id}"

    @property
    def is_done(self) -> bool:
        return self.status == self.Status.SENT

    def clean(self):
        super().clean()
        if self.amount < 0:
            raise ValidationError({"amount": "لا يمكن أن يكون سالبًا."})
        if self.invoice_id:
            inv_total = _as_decimal(getattr(self.invoice, "total_amount", 0))
            if self.amount > inv_total:
                raise ValidationError({"amount": "لا يمكن أن يتجاوز مبلغ الإرجاع إجمالي الفاتورة."})

    def mark_sent(self, *, method: str = "", ref: str = "") -> None:
        self.status = self.Status.SENT
        if method:
            self.method = method[:50]
        if ref:
            self.ref_code = ref[:100]
        if not self.sent_at:
            self.sent_at = timezone.now()
        self.save(update_fields=["status", "method", "ref_code", "sent_at", "updated_at"])

    def mark_failed(self, reason: str = "") -> None:
        self.status = self.Status.FAILED
        if reason:
            self.reason = (self.reason + "\n" + reason).strip()
        self.save(update_fields=["status", "reason", "updated_at"])

    def cancel(self, reason: str = "") -> None:
        self.status = self.Status.CANCELLED
        if reason:
            self.reason = (self.reason + "\n" + reason).strip()
        self.save(update_fields=["status", "reason", "updated_at"])


# =========================================================
# Ledger Entry — سجل الخزنة / القيود
# =========================================================
class LedgerEntry(models.Model):
    class Type(models.TextChoices):
        CLIENT_PAYMENT = "client_payment", "تحصيل عميل"
        EMPLOYEE_PAYOUT = "employee_payout", "صرف موظف"
        CLIENT_REFUND = "client_refund", "إرجاع عميل"
        VAT_REMITTANCE = "vat_remittance", "توريد ضريبة"

    class Direction(models.TextChoices):
        IN_ = "in", "دخول"
        OUT = "out", "خروج"

    entry_type = models.CharField("نوع القيد", max_length=32, choices=Type.choices)
    direction = models.CharField("الاتجاه", max_length=8, choices=Direction.choices)

    amount = models.DecimalField(
        "المبلغ",
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0"))],
    )

    invoice = models.ForeignKey(
        "finance.Invoice",
        null=True, blank=True, on_delete=models.SET_NULL,
        related_name="ledger_entries",
        verbose_name="فاتورة"
    )
    payout = models.ForeignKey(
        "finance.Payout",
        null=True, blank=True, on_delete=models.SET_NULL,
        related_name="ledger_entries",
        verbose_name="صرف"
    )
    refund = models.ForeignKey(
        "finance.Refund",
        null=True, blank=True, on_delete=models.SET_NULL,
        related_name="ledger_entries",
        verbose_name="مرتجع"
    )
    tax_remittance = models.ForeignKey(
        "finance.TaxRemittance",
        null=True, blank=True, on_delete=models.SET_NULL,
        related_name="ledger_entries",
        verbose_name="توريد ضريبة"
    )

    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True, blank=True, on_delete=models.SET_NULL,
        related_name="ledger_created",
        verbose_name="أنشئ بواسطة"
    )
    note = models.CharField("ملاحظة", max_length=255, blank=True, default="")
    created_at = models.DateTimeField("التاريخ", default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["entry_type", "direction"]),
            models.Index(fields=["created_at"]),
        ]
        verbose_name = "قيد خزنة"
        verbose_name_plural = "قيود الخزنة"

    def __str__(self) -> str:
        return f"{self.get_entry_type_display()} {self.amount} ({self.direction})"


# =========================================================
# توافق خلفي: إبقاء اسم FinanceConfig مستخدمًا سابقًا
# =========================================================
FinanceConfig = FinanceSettings
