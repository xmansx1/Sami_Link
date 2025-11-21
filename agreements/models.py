from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Q, Sum
from django.urls import reverse
from django.utils import timezone
from django.utils.html import strip_tags

from finance.models import FinanceSettings

User = settings.AUTH_USER_MODEL

# ============================== عربية للتواريخ ==============================
_AR_WEEKDAYS = {
    0: "الاثنين",
    1: "الثلاثاء",
    2: "الأربعاء",
    3: "الخميس",
    4: "الجمعة",
    5: "السبت",
    6: "الأحد",
}
_AR_MONTHS = {
    1: "يناير",
    2: "فبراير",
    3: "مارس",
    4: "أبريل",
    5: "مايو",
    6: "يونيو",
    7: "يوليو",
    8: "أغسطس",
    9: "سبتمبر",
    10: "أكتوبر",
    11: "نوفمبر",
    12: "ديسمبر",
}

# =============================================================================
# اتفاقية (Agreement)
# =============================================================================
class Agreement(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "مسودة"
        PENDING = "pending", "بانتظار موافقة العميل"
        ACCEPTED = "accepted", "تمت الموافقة"
        REJECTED = "rejected", "مرفوضة"

    # ------------------------- ربطات أساسية -------------------------
    request = models.OneToOneField(
        "marketplace.Request",
        on_delete=models.CASCADE,
        related_name="agreement",
        verbose_name="الطلب",
    )
    employee = models.ForeignKey(
        User,
        on_delete=models.PROTECT,
        related_name="agreements_as_employee",
        verbose_name="الموظف",
    )

    # ------------------------- بيانات الاتفاقية -------------------------
    title = models.CharField("عنوان الاتفاقية", max_length=200)
    text = models.TextField("نص الاتفاقية", blank=True)

    duration_days = models.PositiveIntegerField("المدة (أيام)", default=7)
    total_amount = models.DecimalField(
        "قيمة المشروع P (ريال)",
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
    )

    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.DRAFT,
    )
    rejection_reason = models.TextField("سبب الرفض (إن وُجد)", blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # تاريخ بداية التنفيذ الفعلي (يُضبط عند تأكيد الدفع من المالية)
    started_at = models.DateField(
        "تاريخ بداية التنفيذ",
        null=True,
        blank=True,
        help_text="يُضبط تلقائيًا عند تأكيد دفع الاتفاقية من المالية.",
    )

    # ------------------------- ضرائب ورسوم (من FinanceSettings فقط) -------------------------
    @staticmethod
    def vat_percent() -> Decimal:
        """
        نسبة الضريبة VAT — تُقرأ فقط من FinanceSettings.current_rates().
        (لا يُسمح بأي مصدر آخر للنِّسب.)
        """
        _, vat = FinanceSettings.current_rates()
        return Decimal(vat).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)

    @staticmethod
    def platform_fee_percent() -> Decimal:
        """
        نسبة عمولة المنصّة — تُقرأ فقط من FinanceSettings.current_rates().
        (لا يُسمح بأي مصدر آخر للنِّسب.)
        """
        fee, _ = FinanceSettings.current_rates()
        return Decimal(fee).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)

    # ------------------------- خصائص مشتقة مالية -------------------------
    @property
    def p_amount(self) -> Decimal:
        """
        قيمة المشروع الأساسية P بعد التقريب إلى خانتين عشريتين (تعتمد فقط على المبلغ المدخل).
        """
        return Decimal(self.total_amount or 0).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    @property
    def fee_amount(self) -> Decimal:
        """
        لا يوجد أي حساب تلقائي للعمولة، تعيد نفس مبلغ المشروع الأساسي.
        """
        return self.p_amount

    @property
    def vat_base(self) -> Decimal:
        """
        الأساس الخاضع للضريبة = المبلغ المدخل فقط.
        """
        return self.p_amount

    @property
    def vat_amount(self) -> Decimal:
        """
        لا يوجد أي حساب تلقائي للضريبة، تعيد نفس مبلغ المشروع الأساسي.
        """
        return self.p_amount

    @property
    def grand_total(self) -> Decimal:
        """
        الإجمالي النهائي = المبلغ المدخل فقط.
        """
        return self.p_amount

    @property
    def employee_net_amount(self) -> Decimal | None:
        """
        صافي الموظف = المبلغ المدخل فقط.
        """
        if self.p_amount is None:
            return None
        return self.p_amount

    # ------------------------- منطق سير التنفيذ -------------------------
    def mark_started(self, when=None, save: bool = True) -> None:
        """
        تُستدعى عند تأكيد الدفع من المالية:
        - تضبط started_at إذا لم يكن مضبوطًا.
        - لا تغيّر شيئًا إن كانت البداية مضبوطة مسبقًا.
        """
        if self.started_at:
            return

        dt = when or timezone.now()
        self.started_at = dt.date()
        if save:
            self.save(update_fields=["started_at", "updated_at"])

    @property
    def days_since_start(self) -> Optional[int]:
        """
        عدد الأيام التي مضت منذ بداية التنفيذ.
        يرجع None إذا لم يبدأ التنفيذ بعد.
        """
        if not self.started_at:
            return None
        return (timezone.now().date() - self.started_at).days

    @property
    def days_remaining(self) -> Optional[int]:
        """
        عدد الأيام المتبقية حتى انتهاء المدة المتفق عليها.
        يرجع None إن لم يكن هناك مدة أو لم يبدأ التنفيذ.
        """
        if not self.started_at or not self.duration_days:
            return None
        passed = self.days_since_start
        if passed is None:
            return None
        remaining = self.duration_days - passed
        return remaining if remaining >= 0 else 0

    @property
    def all_milestones_approved(self) -> bool:
        """
        ترجع True إذا كانت جميع المراحل المرتبطة بهذه الاتفاقية
        في حالة APPROVED.
        (لا علاقة مالية بالمراحل، هذا فقط لضبط حالة "مكتمل").
        """
        # نعتبر الاتفاقية مكتملة إذا لم توجد مراحل غير معتمدة
        return not self.milestones.exclude(status=Milestone.Status.APPROVED).exists()

    def sync_request_state(self, save_request: bool = True) -> None:
        """
        تزامن حالة الطلب المرتبط بناءً على:
        - بداية التنفيذ (started_at).
        - اعتماد جميع المراحل (all_milestones_approved).

        القاعدة:
        - إذا started_at موجودة والاتفاقية في حالة ACCEPTED:
            * لو جميع المراحل معتمدة → الطلب "مكتمل".
            * لو هناك مراحل لم تُعتمد بعد → الطلب "قيد التنفيذ".
        - إن لم تبدأ بعد → لا تغيّر حالة الطلب.

        ملاحظة مهمة:
        - لا يتم استدعاء هذه الدالة عند قبول الاتفاقية فقط،
          بل من منطق المالية بعد تأكيد دفع الفاتورة (invoice.mark_paid).
        """
        from marketplace.models import Request  # import متأخر لتجنّب الدوران

        req = getattr(self, "request", None)
        if not req:
            return

        new_state = req.state

        state_enum = getattr(Request, "State", None)
        in_progress_value = None
        completed_value = None

        if state_enum is not None:
            in_progress_value = getattr(state_enum, "IN_PROGRESS", None)
            if in_progress_value is None:
                in_progress_value = getattr(state_enum, "INPROGRESS", None)
            completed_value = getattr(state_enum, "COMPLETED", None)

        if in_progress_value is None:
            in_progress_value = "in_progress"
        if completed_value is None:
            completed_value = "completed"

        # شرط إضافي: لا تحول الطلب إلى قيد التنفيذ إلا إذا كانت الفاتورة مدفوعة فعلاً
        invoice_paid = False
        try:
            invoice = getattr(self, "invoice", None)
            if invoice and hasattr(invoice, "status"):
                PAID_VAL = getattr(getattr(invoice.__class__, "Status", None), "PAID", "paid")
                invoice_paid = (getattr(invoice, "status", None) or "").lower() == (PAID_VAL or "").lower()
        except Exception:
            pass

        if self.started_at and self.status == self.Status.ACCEPTED and invoice_paid:
            if self.all_milestones_approved:
                new_state = completed_value
            else:
                new_state = in_progress_value

        if new_state != req.state:
            req.state = new_state
            if hasattr(req, "updated_at"):
                req.updated_at = timezone.now()
                if save_request:
                    req.save(update_fields=["state", "updated_at"])
            else:
                if save_request:
                    req.save(update_fields=["state"])

    # ------------------------- تحققات وتنقيات -------------------------
    def clean(self) -> None:
        # صلاحية دور الموظف
        role = getattr(self.employee, "role", None)
        if role and role not in {"employee", "admin", "manager"}:
            raise ValidationError("يجب أن يكون الموظف بدور 'employee' أو 'admin/manager'.")

        # توافق الموظف مع الطلب المُسنَّد
        assigned = getattr(self.request, "assigned_employee_id", None)
        if assigned and assigned != self.employee_id:
            raise ValidationError("الموظف في الاتفاقية يجب أن يطابق الموظف المُسنَّد على الطلب.")

        if self.duration_days < 1:
            raise ValidationError("المدة يجب أن تكون رقمًا موجبًا.")

        if self.total_amount is None or Decimal(self.total_amount) < 0:
            raise ValidationError("إجمالي المشروع لا يمكن أن يكون سالبًا.")
        self.total_amount = Decimal(self.total_amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

        if self.text:
            self.text = strip_tags(self.text).strip()
        if self.rejection_reason:
            self.rejection_reason = strip_tags(self.rejection_reason).strip()

        # قفل المدة/الإجمالي بعد مغادرة DRAFT
        if self.pk:
            try:
                prev = Agreement.objects.only("duration_days", "total_amount", "status").get(pk=self.pk)
            except Agreement.DoesNotExist:
                prev = None
            if prev and prev.status != Agreement.Status.DRAFT:
                if prev.duration_days != self.duration_days or prev.total_amount != self.total_amount:
                    raise ValidationError("لا يُسمح بتعديل المدة أو إجمالي المشروع بعد مغادرة المسودة.")

        # ❌ لا نفرض أي علاقة مالية مع المراحل:
        # لا يوجد أي شرط يربط مجموع مبالغ المراحل بقيمة المشروع.

    def save(self, *args, **kwargs):
        self.full_clean()
        super().save(*args, **kwargs)

    # ------------------------- عرض عربي -------------------------
    def __str__(self) -> str:  # pragma: no cover
        return f"Agreement#{self.pk} R{self.request_id} — {self.get_status_display()}"

    def get_absolute_url(self) -> str:
        return reverse("agreements:agreement_detail", kwargs={"pk": self.pk})

    def get_day_name_ar(self) -> str:
        dt = getattr(self, "created_at", None) or timezone.now()
        return _AR_WEEKDAYS[dt.weekday()]

    def get_date_text_ar(self) -> str:
        dt = getattr(self, "created_at", None) or timezone.now()
        return f"{dt.day} {_AR_MONTHS[dt.month]} {dt.year}"

    @property
    def client_display(self) -> str:
        req = self.request
        for attr in ("client", "customer", "user", "owner", "created_by"):
            obj: Any = getattr(req, attr, None)
            if obj:
                if hasattr(obj, "get_full_name"):
                    try:
                        return obj.get_full_name() or str(obj)
                    except Exception:
                        return str(obj)
                name = getattr(obj, "name", None) or getattr(obj, "username", None) or getattr(obj, "email", None)
                return str(name or obj)
        return "—"

    @property
    def employee_display(self) -> str:
        emp = getattr(self, "employee", None)
        if not emp:
            return "—"
        if hasattr(emp, "get_full_name"):
            try:
                return emp.get_full_name() or str(emp)
            except Exception:
                return str(emp)
        return str(getattr(emp, "name", None) or getattr(emp, "email", None) or emp)

    def get_intro_paragraph_ar(self) -> str:
        client = self.client_display
        employee = self.employee_display
        day_name = self.get_day_name_ar()
        date_text = self.get_date_text_ar()
        title = self.title or (getattr(self.request, "title", "") or "الاتفاق")
        return (
            f"أنه في يوم {day_name} الموافق {date_text}، "
            f"اتفق الطرف الأول (العميل) {client} مع الطرف الثاني (الموظف) {employee} "
            f"على تنفيذ “{title}” بمبلغ {self.total_amount} ر.س "
            f"ومدة تنفيذ {self.duration_days} يومًا."
        )

    class Meta:
        indexes = [
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["employee"]),
        ]
        constraints = [
            models.CheckConstraint(check=Q(total_amount__gte=0), name="agreement_total_amount_gte_0"),
            models.CheckConstraint(check=Q(duration_days__gte=1), name="agreement_duration_days_gte_1"),
        ]
        verbose_name = "اتفاقية"
        verbose_name_plural = "اتفاقيات"


# =============================================================================
# دفعات/مراحل الاتفاقية (Milestone) — بدون أي ربط مالي بالفواتير
# =============================================================================
class Milestone(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "قيد التنفيذ"
        DELIVERED = "delivered", "تم التسليم"
        APPROVED = "approved", "معتمدة"
        REJECTED = "rejected", "مرفوضة"
        PAID = "paid", "مدفوعة"  # تبقى احتياطياً لو احتجناها في منطق داخلي، بدون ربط بالفواتير

    agreement = models.ForeignKey(
        "agreements.Agreement",
        on_delete=models.CASCADE,
        related_name="milestones",
        verbose_name="الاتفاقية",
    )

    title = models.CharField("عنوان المرحلة", max_length=160)

    # حقل المبلغ يبقى احتياطيًا، لا علاقة له بالفواتير في الوضع الحالي
    amount = models.DecimalField(
        "المبلغ (ريال)",
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="لا تُستخدم هذه القيمة في الفواتير حاليًا، حقل احتياطي فقط.",
    )

    order = models.PositiveIntegerField("الترتيب", default=1)

    # مدة المرحلة بالأيام (ليست مدة استحقاق مالي)
    due_days = models.PositiveIntegerField(
        "مدة المرحلة (أيام)",
        null=True,
        blank=True,
        help_text="مثال: 10 أيام للتسليم الأولي. مجموع مدد المراحل لا يتجاوز مدة المشروع المتفق عليها.",
    )

    status = models.CharField("الحالة", max_length=12, choices=Status.choices, default=Status.PENDING)

    delivered_at = models.DateTimeField("وقت التسليم", null=True, blank=True)
    delivered_note = models.TextField("ملاحظة التسليم", blank=True)

    approved_at = models.DateTimeField("وقت الاعتماد", null=True, blank=True)
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="approved_milestones",
        verbose_name="اعتمدت بواسطة",
    )
    rejected_reason = models.TextField("سبب الرفض", blank=True)

    paid_at = models.DateTimeField("وقت السداد", null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["order", "id"]
        indexes = [
            models.Index(fields=["agreement", "order"]),
            models.Index(fields=["status"]),
            models.Index(fields=["delivered_at"]),
            models.Index(fields=["approved_at"]),
            models.Index(fields=["paid_at"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["agreement", "order"],
                name="uniq_milestone_order_per_agreement",
            ),
            models.CheckConstraint(
                check=Q(amount__gte=0),
                name="milestone_amount_gte_0",
            ),
            models.CheckConstraint(
                check=Q(order__gte=1),
                name="milestone_order_gte_1",
            ),
            models.CheckConstraint(
                check=Q(due_days__isnull=True) | Q(due_days__gte=1),
                name="milestone_due_days_gte_1_or_null",
            ),
        ]
        verbose_name = "مرحلة"
        verbose_name_plural = "مراحل"

    def clean(self) -> None:
        """
        - التأكد من أن amount غير سالب (مع تقريب نقدي).
        - التأكد من أن order >= 1.
        - التأكد من أن مدة المرحلة (due_days) >= 1 إن وُجدت.
        - التأكد من أن مجموع مدد المراحل لنفس الاتفاقية
          لا يتجاوز مدة المشروع Agreement.duration_days.
        - منع إضافة أكثر من 30 مرحلة لنفس الاتفاقية.
        """
        self.amount = Decimal(self.amount or 0).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if self.amount < 0:
            raise ValidationError("مبلغ المرحلة يجب أن يكون رقمًا موجبًا أو صفرًا.")

        if self.order < 1:
            raise ValidationError("ترتيب المرحلة يجب أن يكون 1 أو أكبر.")

        if self.due_days is not None and self.due_days < 1:
            raise ValidationError({"due_days": "مدة المرحلة يجب أن تكون يومًا واحدًا على الأقل."})

        if not self.agreement_id:
            return

        # حد أقصى 30 مرحلة لكل اتفاقية
        qs_for_count = Milestone.objects.filter(agreement_id=self.agreement_id)
        if self.pk:
            qs_for_count = qs_for_count.exclude(pk=self.pk)
        count_existing = qs_for_count.count()
        if count_existing + 1 > 30:
            raise ValidationError("لا يمكن إضافة أكثر من 30 مرحلة لنفس الاتفاقية (الحد الأقصى 30 مرحلة).")

        # مجموع مدد المراحل لا يتجاوز مدة المشروع
        max_days = getattr(self.agreement, "duration_days", None)
        if max_days and self.due_days:
            qs_for_sum = Milestone.objects.filter(agreement_id=self.agreement_id)
            if self.pk:
                qs_for_sum = qs_for_sum.exclude(pk=self.pk)

            agg = qs_for_sum.aggregate(total=Sum("due_days"))
            total_existing = agg["total"] or 0
            total_with_current = total_existing + self.due_days

            if total_with_current > max_days:
                raise ValidationError(
                    {
                        "due_days": (
                            f"مجموع مدد المراحل ({total_with_current} يومًا) "
                            f"يتجاوز مدة المشروع المتفق عليها ({max_days} يومًا)."
                        )
                    }
                )

    def __str__(self) -> str:  # pragma: no cover
        return f"Milestone#{self.pk} A{self.agreement_id} — {self.title} ({self.order})"

    def get_absolute_url(self) -> str:
        return reverse("agreements:milestone_detail", kwargs={"pk": self.pk})

    # -------- خصائص حالة مريحة --------
    @property
    def is_delivered(self) -> bool:
        return self.status == self.Status.DELIVERED

    @is_delivered.setter
    def is_delivered(self, value: bool) -> None:
        if bool(value):
            self.mark_delivered(note=(self.delivered_note or "").strip())
        else:
            if self.is_paid:
                raise ValidationError("لا يمكن إلغاء التسليم بعد السداد.")
            self.status = self.Status.PENDING
            self.delivered_at = None
            self.rejected_reason = ""
            self.save(update_fields=["status", "delivered_at", "rejected_reason"])

    @property
    def is_pending_review(self) -> bool:
        return self.status == self.Status.DELIVERED

    @property
    def is_approved(self) -> bool:
        return self.status == self.Status.APPROVED

    @property
    def is_rejected(self) -> bool:
        return self.status == self.Status.REJECTED

    @property
    def is_paid(self) -> bool:
        return self.status == self.Status.PAID

    # -------- انتقالات منطقية --------
    def mark_delivered(self, note: str = "") -> None:
        if self.is_approved or self.is_paid:
            raise ValidationError("لا يمكن تسليم مرحلة معتمَدة أو مدفوعة.")
        self.status = self.Status.DELIVERED
        self.delivered_at = timezone.now()
        self.delivered_note = (note or "").strip()
        self.rejected_reason = ""
        self.approved_at = None
        self.approved_by = None
        self.save(
            update_fields=[
                "status",
                "delivered_at",
                "delivered_note",
                "rejected_reason",
                "approved_at",
                "approved_by",
            ]
        )

    def approve(self, user) -> None:
        if self.is_paid:
            raise ValidationError("لا يمكن اعتماد مرحلة مدفوعة.")
        if not self.is_pending_review:
            raise ValidationError("لا يمكن الاعتماد قبل التسليم.")
        self.status = self.Status.APPROVED
        self.approved_at = timezone.now()
        self.approved_by = user
        self.rejected_reason = ""
        self.save(update_fields=["status", "approved_at", "approved_by", "rejected_reason"])

    def reject(self, reason: str) -> None:
        reason = (reason or "").strip()
        if len(reason) < 3:
            raise ValidationError("سبب الرفض قصير جدًا.")
        if self.is_paid:
            raise ValidationError("لا يمكن رفض مرحلة مدفوعة.")
        if not self.is_pending_review:
            raise ValidationError("لا يمكن الرفض قبل التسليم.")
        self.status = self.Status.REJECTED
        self.approved_at = None
        self.approved_by = None
        self.rejected_reason = reason
        self.save(update_fields=["status", "approved_at", "approved_by", "rejected_reason"])

    def mark_paid(self) -> None:
        """
        منطق داخلي فقط إن رغبت لاحقًا بوسم مرحلة كمدفوعة،
        لكن بدون أي ربط مباشر بفواتير المالية.
        """
        if not self.is_approved:
            raise ValidationError("لا يمكن السداد قبل اعتماد المرحلة.")
        self.status = self.Status.PAID
        self.paid_at = timezone.now()
        self.save(update_fields=["status", "paid_at"])


# =============================================================================
# بنود الاتفاقية (Templates + Items)
# =============================================================================
class AgreementClause(models.Model):
    key = models.SlugField("المعرّف الفريد", unique=True, help_text="معرّف (إنجليزي) للبند")
    title = models.CharField("عنوان البند", max_length=200)
    body = models.TextField("نص البند")
    is_active = models.BooleanField("مفعل؟", default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "بند اتفاقية"
        verbose_name_plural = "بنود الاتفاقية"
        ordering = ["title"]

    def __str__(self) -> str:  # pragma: no cover
        return f"{self.title} ({'مفعل' if self.is_active else 'موقوف'})"


class AgreementClauseItem(models.Model):
    agreement = models.ForeignKey(
        "agreements.Agreement",
        on_delete=models.CASCADE,
        related_name="clause_items",
        verbose_name="الاتفاقية",
    )
    clause = models.ForeignKey(
        AgreementClause,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        verbose_name="البند الجاهز",
    )
    custom_text = models.TextField("نص مخصص", blank=True)
    position = models.PositiveIntegerField("الترتيب", default=1)

    class Meta:
        verbose_name = "بند ضمن الاتفاقية"
        verbose_name_plural = "بنود الاتفاقية المختارة"
        ordering = ["position", "id"]
        constraints = [
            models.UniqueConstraint(fields=["agreement", "position"], name="uniq_clauseitem_position_per_agreement"),
        ]

    def clean(self) -> None:
        if not self.clause and not (self.custom_text or "").strip():
            raise ValidationError("يجب تحديد بند جاهز أو كتابة نص مخصص.")
        if self.custom_text:
            cleaned = strip_tags(self.custom_text).strip()
            if len(cleaned) > 2000:
                raise ValidationError("النص المخصص طويل جدًا (أقصى 2000 حرف).")
            self.custom_text = cleaned
        if self.position < 1:
            raise ValidationError("ترتيب البند يجب أن يكون 1 أو أكبر.")

    def __str__(self) -> str:  # pragma: no cover
        if self.clause:
            return f"[{self.position}] {self.clause.title}"
        return f"[{self.position}] بند مخصص: {self.custom_text[:30]}..."

    @property
    def display_text(self) -> str:
        return self.clause.body if self.clause else (self.custom_text or "")


# ------------------- تكامل المالية — فاتورة واحدة فقط للاتفاقية -------------------
def _finance_create_total_invoice(agreement: Agreement) -> Optional[int]:
    """
    ينشئ أو يضمن وجود **فاتورة واحدة غير مدفوعة** للاتفاقية.
    لا يتم إنشاء فواتير لكل مرحلة إطلاقًا.
    """
    try:
        from finance.models import Invoice
    except Exception:
        return None

    try:
        inv = Invoice.ensure_single_unpaid_for_agreement(
            agreement=agreement,
            amount=agreement.p_amount,
        )
        return getattr(inv, "id", None)
    except Exception:
        return None


def _no_finance_invoices_exist(agreement: Agreement) -> bool:
    """
    يفحص ما إذا كانت لا توجد أي فواتير مرتبطة بالاتفاقية.
    """
    try:
        from finance.models import Invoice
    except Exception:
        return True

    try:
        return not Invoice.objects.filter(agreement=agreement).exists()
    except Exception:
        return True


# إشارة بعد حفظ الاتفاقية: إنشاء فاتورة واحدة عند القبول فقط
from django.db.models.signals import post_save
from django.dispatch import receiver


@receiver(post_save, sender=Agreement)
def agreement_post_save(sender, instance: Agreement, created: bool, **kwargs):
    """
    - عند أن تكون حالة الاتفاقية ACCEPTED ولا توجد فواتير مرتبطة بها:
        → يتم إنشاء فاتورة واحدة غير مدفوعة بالاعتماد على FinanceSettings.
    - لا نقوم هنا بتغيير حالة الطلب إلى "قيد التنفيذ".
      هذا يتم عند تأكيد الدفع من المالية عبر:
        * agreement.mark_started()
        * agreement.sync_request_state()
    """
    try:
        if instance.status == Agreement.Status.ACCEPTED and _no_finance_invoices_exist(instance):
            with transaction.atomic():
                _finance_create_total_invoice(instance)
    except Exception:
        # لا نفشل حفظ الاتفاقية بسبب أي خطأ في تكامل المالية
        pass
