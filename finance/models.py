from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from datetime import timedelta
from typing import Optional, Iterable, Tuple, Dict

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, MaxValueValidator
from django.db import models, transaction
from django.db.models import Q, Sum
from django.urls import reverse
from django.utils import timezone


# ملاحظة: استخدام السلسلة من settings.AUTH_USER_MODEL آمن كقيمة ForeignKey
User = settings.AUTH_USER_MODEL


# =========================================================
# إعدادات مالية عامة (نِسب عمولة المنصّة والضريبة VAT)
# =========================================================
class FinanceSettings(models.Model):
    """
    إعدادات مالية عامة (صف واحد فقط).
    - platform_fee_percent و vat_rate كنِسب بين 0..1
      مثال: 0.10 = 10% ، 0.15 = 15%
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

    def __str__(self):
        return f"FinanceSettings(fee={self.platform_fee_percent}, vat={self.vat_rate})"

    @classmethod
    def get_solo(cls) -> "FinanceSettings":
        """
        يعيد السجل الوحيد المستخدم لإعدادات المالية.
        """
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    @classmethod
    def current_rates(cls) -> Tuple[Decimal, Decimal]:
        """
        تُرجِع (platform_fee_percent, vat_rate) من FinanceSettings فقط.

        - مصدر الحقيقة الوحيد للنِّسَب هو هذا الجدول (الذي يتم ضبطه من صفحة
          إعدادات المالية في لوحة التحكم).
        - لا يتم الاعتماد على settings ولا على متغيّرات بيئة في الحسابات التشغيلية.
        """
        try:
            cfg = cls.get_solo()
            fee = cfg.platform_fee_percent
            vat = cfg.vat_rate
        except Exception:
            # في الحالات الاستثنائية جدًا (قبل إنشاء السجل مثلًا)
            # نستخدم نفس القيم الافتراضية المعرفة في الحقول.
            fee = Decimal("0.10")
            vat = Decimal("0.15")

        fee = Decimal(fee).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        vat = Decimal(vat).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
        return fee, vat


# =========================================================
# أوامر صرف مستحقات الموظفين
# =========================================================
class Payout(models.Model):
    """سجل صرف مستحقات موظف (صافي حقيقي للموظف بعد خصم عمولة المنصّة)."""

    class Status(models.TextChoices):
        PENDING = "pending", "قيد المراجعة"
        PAID = "paid", "مدفوع"
        CANCELLED = "cancelled", "ملغي"

    employee = models.ForeignKey(
        User, on_delete=models.PROTECT, related_name="payouts", verbose_name="الموظف"
    )
    agreement = models.ForeignKey(
        "agreements.Agreement",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="payouts",
    )
    invoice = models.ForeignKey(
        "finance.Invoice",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="payouts",
    )

    amount = models.DecimalField(
        "المبلغ المصروف للموظف",
        max_digits=12,
        decimal_places=2,
        validators=[MinValueValidator(Decimal("0"))],
    )
    status = models.CharField(
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
    )
    method = models.CharField("طريقة الصرف", max_length=50, blank=True, default="")
    ref_code = models.CharField("مرجع العملية", max_length=100, blank=True, default="")
    note = models.CharField("ملاحظة", max_length=255, blank=True, default="")
    issued_at = models.DateTimeField("تاريخ الإنشاء", default=timezone.now, db_index=True)
    paid_at = models.DateTimeField("تاريخ الصرف", null=True, blank=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-issued_at", "-id"]
        indexes = [
            models.Index(fields=["status", "issued_at"]),
            models.Index(fields=["employee", "status"]),
        ]
        verbose_name = "أمر صرف"
        verbose_name_plural = "أوامر الصرف"

    def __str__(self):
        return f"Payout#{self.pk} to {self.employee_id} — {self.amount} ({self.status})"

    def mark_paid(self, *, method: str = "", ref: str = "") -> None:
        self.status = self.Status.PAID
        if method:
            self.method = method[:50]
        if ref:
            self.ref_code = ref[:100]
        if not self.paid_at:
            self.paid_at = timezone.now()
        self.save(update_fields=["status", "method", "ref_code", "paid_at", "updated_at"])


# =========================================================
#  QuerySet مخصّص للفواتير
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

    # تجميعات مفيدة للتقارير
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
#  نموذج الفاتورة
# =========================================================
class Invoice(models.Model):
    """
    فاتورة مالية مرتبطة باتفاقية (Agreement)، وقد تُسند إلى مرحلة (Milestone).
    • تدعم تحصيل كامل المبلغ مقدّمًا (Escrow).
    • VAT تُحتسب على (P + عمولة المنصّة).
    • توافق خلفي: الحقل amount هو الأساس (P) وباقي الإجماليات تُعاد اشتقاقها عند الحفظ.
    """

    class Status(models.TextChoices):
        UNPAID = "unpaid", "غير مدفوعة"
        PAID = "paid", "مدفوعة"
        CANCELLED = "cancelled", "ملغاة"

    # -------- ارتباطات --------
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

    # -------- مبالغ أساسية --------
    # P: قيمة الخدمة/المرحلة قبل الرسوم والضريبة (الأساس المحاسبي)
    amount = models.DecimalField(
        "المبلغ (P)",
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
    )

    # جميع الحقول المالية تعتمد فقط على المبلغ المدخل (amount)
    platform_fee_percent = models.DecimalField(
        "نسبة المنصّة",
        max_digits=5,
        decimal_places=4,
        default=Decimal("0.00"),
        help_text="تم تعطيل الحسابات التلقائية."
    )
    platform_fee_amount = models.DecimalField(
        "قيمة عمولة المنصّة",
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
    )
    vat_percent = models.DecimalField(
        "نسبة الضريبة VAT",
        max_digits=5,
        decimal_places=4,
        default=Decimal("0.00"),
        help_text="تم تعطيل الحسابات التلقائية."
    )
    vat_amount = models.DecimalField(
        "قيمة الضريبة",
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
    )
    subtotal = models.DecimalField(
        "المجموع الفرعي",
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="تم تعطيل الحسابات التلقائية."
    )
    total_amount = models.DecimalField(
        "الإجمالي المستحق",
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="المبلغ النهائي الذي يراه العميل ويدفعه (المبلغ المدخل فقط)",
    )

    # -------- الحالة والتواريخ --------
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.UNPAID,
    )
    issued_at = models.DateTimeField("تاريخ الإصدار", default=timezone.now, db_index=True)
    due_at = models.DateTimeField("موعد السداد", null=True, blank=True, db_index=True)
    paid_at = models.DateTimeField("تاريخ السداد", null=True, blank=True, db_index=True)

    # -------- معلومات الدفع --------
    method = models.CharField("طريقة السداد", max_length=50, blank=True)  # مثال: حوالة/مدى/فيزا
    ref_code = models.CharField(  # مرجع بوابة/عملية داخلي
        "مرجع العملية",
        max_length=100,
        blank=True,
        db_index=True,
        help_text="مرجع الدفع من بوابة/حوالة (قد لا يكون فريدًا).",
    )
    paid_ref = models.CharField(  # مرجع التحويل البنكي الحقيقي من العميل
        "مرجع الدفع البنكي",
        max_length=64,
        blank=True,
        null=True,
        help_text="رقم/مرجع التحويل البنكي الذي يرسله العميل أو تسجله المالية بعد المراجعة.",
    )

    # -------- تتبّع --------
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
            models.Index(fields=["paid_ref"]),  # للبحث السريع بمرجع التحويل
        ]
        constraints = [
            # فاتورة واحدة كحد أقصى لكل Milestone (إن وُجدت)
            models.UniqueConstraint(
                fields=["milestone"],
                condition=Q(milestone__isnull=False),
                name="uniq_invoice_per_milestone",
            ),
        ]
        ordering = ["-issued_at", "-id"]
        verbose_name = "فاتورة"
        verbose_name_plural = "فواتير"

    # ======================
    #  تمثيل وروابط
    # ======================
    def __str__(self) -> str:  # pragma: no cover
        return f"Invoice#{self.pk} A{self.agreement_id} — {self.get_status_display()} {self.total_amount}"

    def get_absolute_url(self) -> str:
        return reverse("finance:invoice_detail", kwargs={"pk": self.pk})

    def get_mark_paid_url(self) -> str:
        # تأكد من وجود المسار المماثل في finance/urls.py
        return reverse("finance:mark_invoice_paid", kwargs={"pk": self.pk})

    # ======================
    #  خصائص مشتقة
    # ======================
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
        """متأخرة: غير مدفوعة وتجاوزت موعد السداد."""
        return bool(self.is_unpaid and self.due_at and self.due_at < timezone.now())

    @property
    def effective_date(self):
        """eff_date: تاريخ احتساب للتقارير (paid_at أو issued_at)."""
        return self.paid_at or self.issued_at

    @property
    def tech_net(self) -> Decimal:
        """صافي الموظف من هذه الفاتورة (P - عمولة المنصة)."""
        P = self._as_decimal(self.amount)
        F = self._as_decimal(self.platform_fee_amount)
        return (P - F) if P >= F else Decimal("0.00")

    @property
    def as_breakdown(self) -> Dict[str, Decimal]:
        """تفصيل مبالغ الفاتورة كقاموس مفيد للواجهات."""
        return {
            "P": self._q2(self.amount),
            "fee_percent": self._q4(self.platform_fee_percent),
            "fee_value": self._q2(self.platform_fee_amount),
            "subtotal": self._q2(self.subtotal),
            "vat_percent": self._q4(self.vat_percent),
            "vat_value": self._q2(self.vat_amount),
            "total": self._q2(self.total_amount),
        }

    # ======================
    #  أدوات حسابية
    # ======================
    @staticmethod
    def _as_decimal(val) -> Decimal:
        """تحويل آمن إلى Decimal."""
        if isinstance(val, Decimal):
            return val
        try:
            return Decimal(str(val or "0"))
        except (InvalidOperation, TypeError, ValueError):
            return Decimal("0")

    @staticmethod
    def _q2(val: Decimal) -> Decimal:
        """تقريب إلى خانتين عشريتين بطريقة آمنة."""
        return Invoice._as_decimal(val).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    @staticmethod
    def _q4(val: Decimal) -> Decimal:
        """تقريب إلى أربع خانات (مفيد للنِّسب)."""
        return Invoice._as_decimal(val).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)

    def recompute_totals(self) -> None:
        """
        حساب تلقائي للحقول المالية:
        - platform_fee_amount = amount × platform_fee_percent
        - vat_amount = amount × vat_percent
        - total_amount = amount + platform_fee_amount + vat_amount
        """
        amount = self._as_decimal(self.amount)
        platform_fee_percent = self._as_decimal(self.platform_fee_percent)
        vat_percent = self._as_decimal(self.vat_percent)
        self.platform_fee_amount = (amount * platform_fee_percent).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        self.subtotal = amount
        self.vat_amount = (amount * vat_percent).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        self.total_amount = (amount + self.platform_fee_amount + self.vat_amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    def recalc_and_save(self, *, update_timestamps: bool = True) -> None:
        """
        تم تعطيل أي حساب تلقائي. جميع الحقول المالية تعتمد فقط على المبلغ المدخل (amount).
        """
        self.recompute_totals()
        fields = ["platform_fee_amount", "subtotal", "vat_amount", "total_amount"]
        if update_timestamps and hasattr(self, "updated_at"):
            self.updated_at = timezone.now()
            fields.append("updated_at")
        self.save(update_fields=fields)

    # ======================
    #  ضمان سلامة البيانات
    # ======================
    def clean(self):
        super().clean()

        # عدم السماح بالقيم السالبة
        for fld in ("amount", "platform_fee_amount", "subtotal", "vat_amount", "total_amount"):
            val = self._as_decimal(getattr(self, fld, None))
            if val < 0:
                raise ValidationError({fld: "لا يمكن أن يكون سالبًا."})

        # نسب داخل النطاق [0, 1]
        for fld in ("platform_fee_percent", "vat_percent"):
            v = self._as_decimal(getattr(self, fld, 0))
            if v < 0 or v > 1:
                raise ValidationError({fld: "النسبة يجب أن تكون بين 0 و 1."})

        # توافق الاتفاقية عند وجود milestone
        if self.milestone_id and self.agreement_id:
            ms_agreement_id = getattr(self.milestone, "agreement_id", None)
            if ms_agreement_id and ms_agreement_id != self.agreement_id:
                raise ValidationError("الاتفاقية المرتبطة لا تتطابق مع اتفاقية المرحلة.")

        # due_at إن وُجد يجب ألا يسبق issued_at
        if self.due_at and self.issued_at and self.due_at < self.issued_at:
            raise ValidationError({"due_at": "موعد السداد لا يمكن أن يسبق تاريخ الإصدار."})

        # paid_at إن وُجد يجب ألا يسبق issued_at
        if self.paid_at and self.issued_at and self.paid_at < self.issued_at:
            raise ValidationError({"paid_at": "تاريخ السداد لا يمكن أن يسبق تاريخ الإصدار."})

    def save(self, *args, **kwargs):
        """
        تم تعطيل أي منطق حسابي تلقائي. جميع الحقول المالية تعتمد فقط على المبلغ المدخل (amount).
        """
        self.recompute_totals()
        return super().save(*args, **kwargs)

    # ======================
    #  واجهة عمليات الدفع
    # ======================
    def mark_paid(
        self,
        *,
        by_user: Optional[object] = None,
        method: str = "",
        ref_code: str = "",
        paid_ref: str = "",
        paid_at=None,
        save: bool = True,
    ):
        """
        يوسم الفاتورة كمدفوعة ويحدّث الحقول ذات الصلة.
        يُفضّل استدعاؤها ضمن transaction.atomic() من الفيو.
        """
        if self.status == self.Status.PAID:
            return self  # لا تكرار

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
                "platform_fee_amount",
                "subtotal",
                "vat_amount",
                "total_amount",
            ]
            self.save(update_fields=fields)
        return self

    def cancel(self, *, by_user: Optional[object] = None, reason: str = "", save: bool = True):
        if self.status == self.Status.CANCELLED:
            return self
        self.status = self.Status.CANCELLED
        if by_user and hasattr(self, "updated_by"):
            setattr(self, "updated_by", by_user)
        if save:
            self.save(update_fields=["status", "updated_at"])
        return self

    # ======================
    #  دوال استعلام مساعدة
    # ======================
    @classmethod
    def unpaid_for_agreement(cls, agreement_id: int):
        return cls.objects.for_agreement(agreement_id).unpaid()

    @classmethod
    def all_paid_for_agreement(cls, agreement_id: int) -> bool:
        return not cls.unpaid_for_agreement(agreement_id).exists()

    @classmethod
    def totals_by_status(cls) -> Dict[str, Decimal]:
        """
        اختصار لتجميعات شائعة: paid / unpaid / fee / vat / total
        (مفيد للوحة النظرة العامة).
        """
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

    # ======================
    #  مُساعدات عملية/إنشاء
    # ======================
    def set_due_in_days(self, days: int = 3, save: bool = True):
        """
        يضبط موعد السداد بعد N أيام من الآن (الافتراضي 3 أيام — SLA معتمد).
        """
        self.due_at = timezone.now() + timedelta(days=max(0, int(days)))
        if save:
            self.save(update_fields=["due_at", "updated_at"])

    @classmethod
    def _defaults_from_settings_or_cfg(
        cls,
        platform_fee_percent: Optional[Decimal],
        vat_percent: Optional[Decimal],
    ) -> Tuple[Decimal, Decimal]:
        """
        مساعد داخلي لإحضار نسب افتراضية من FinanceSettings فقط.

        - إذا تم تمرير نسب مخصصة في الوسيطات تُستخدم كما هي.
        - إذا لم تُمرّر، يتم جلب النِّسَب من FinanceSettings.current_rates().
        """
        if platform_fee_percent is not None and vat_percent is not None:
            return platform_fee_percent, vat_percent

        fee, vat = FinanceSettings.current_rates()
        pf = platform_fee_percent if platform_fee_percent is not None else fee
        vp = vat_percent if vat_percent is not None else vat
        return (
            pf.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP),
            vp.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP),
        )

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
        """
        ينشئ فاتورة لمرحلة إذا لم تكن موجودة. يشتق الاتفاقية والمبلغ تلقائيًا عند الحاجة.
        - amount: إن لم يُمرّر، سيُستخدم milestone.amount إن وُجد، وإلا يُقسّم إجمالي الاتفاقية بالتساوي.
        - platform_fee_percent/vat_percent: إن لم تُمرّر، تُستخدم القيم من FinanceSettings.
        """
        if milestone is None:
            raise ValidationError("لا يمكن إنشاء فاتورة: المرحلة غير مرفقة.")

        pf, vp = cls._defaults_from_settings_or_cfg(platform_fee_percent, vat_percent)

        with transaction.atomic():
            inv, created = cls.objects.select_for_update().get_or_create(
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
            # اضبط due_at إن لم تكن محددة
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
        """
        ينشئ **فاتورة تحصيل كامل مقدمًا** للاتفاقية:
        - المبلغ الأساسي (P) يُؤخذ من agreement.total_amount إن وُجد، وإلا يُجمع مبالغ المراحل.
        - platform_fee_percent/vat_percent: إن لم تُمرّر، تُستخدم القيم من FinanceSettings.
        """
        if agreement is None:
            raise ValidationError("الاتفاقية غير موجودة.")

        # حساب الأساس P
        P = getattr(agreement, "total_amount", None)
        if P is None:
            # حاول جمع مبالغ المراحل
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
        """
        يضمن وجود فاتورة واحدة (غير مدفوعة) للاتفاقية بدون مرحلة (إيداع كامل).
        إن وُجدت تُحدّث قيمتها، وإلا تُنشأ.
        """
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
                # أبقِ النِّسب كما هي إن كانت مخصصة، وإلا استخدم الحالية من FinanceSettings
                if inv.platform_fee_percent is None:
                    inv.platform_fee_percent = pf
                if inv.vat_percent is None:
                    inv.vat_percent = vp
                inv.recalc_and_save()
                return inv

            inv = cls.objects.create(
                agreement=agreement,
                amount=amount if amount is not None else Decimal("0.00"),
                platform_fee_percent=pf,
                vat_percent=vp,
                status=cls.Status.UNPAID,
                issued_at=timezone.now(),
            )
            return inv


# =========================================================
# مُساعد عام لحساب صافي الموظف من فواتير مدفوعة (P - عمولة)
# =========================================================
def employee_net_from_paid_invoices(employee_id: int) -> Decimal:
    """
    صافي الموظف من الفواتير المدفوعة = مجموع P - مجموع عمولة المنصّة.
    """
    paid_val = getattr(getattr(Invoice, "Status", None), "PAID", "paid")
    qs = Invoice.objects.filter(agreement__employee_id=employee_id, status=paid_val)
    agg = qs.aggregate(p=Sum("amount"), fee=Sum("platform_fee_amount"))
    P = agg["p"] or Decimal("0.00")
    F = agg["fee"] or Decimal("0.00")
    return (P - F) if P >= F else Decimal("0.00")


# =========================================================
# توافق خلفي: إبقاء اسم FinanceConfig مستخدمًا سابقًا
# =========================================================
FinanceConfig = FinanceSettings
