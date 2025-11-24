from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Q, Sum
from django.db.models.signals import post_save
from django.dispatch import receiver
from django.urls import reverse
from django.utils import timezone
from django.utils.functional import cached_property
from django.utils.html import strip_tags

# FinanceSettings مصدر الحقيقة للنسب، لكن نحمي الاستيراد لتجنب دورة/هجرات مبكرة
try:
    from finance.models import FinanceSettings
except Exception:
    FinanceSettings = None

User = settings.AUTH_USER_MODEL

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


class Agreement(models.Model):
    class Status(models.TextChoices):
        DRAFT = "draft", "مسودة"
        PENDING = "pending", "بانتظار موافقة العميل"
        ACCEPTED = "accepted", "تمت الموافقة"
        REJECTED = "rejected", "مرفوضة"

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

    started_at = models.DateField(
        "تاريخ بداية التنفيذ",
        null=True,
        blank=True,
        help_text="يُضبط تلقائيًا عند تأكيد دفع الاتفاقية من المالية.",
    )

    @staticmethod
    def vat_percent() -> Decimal:
        if FinanceSettings is None:
            return Decimal("0.0000")
        _, vat = FinanceSettings.current_rates()
        return Decimal(vat).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)

    @staticmethod
    def platform_fee_percent() -> Decimal:
        if FinanceSettings is None:
            return Decimal("0.0000")
        fee, _ = FinanceSettings.current_rates()
        return Decimal(fee).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)

    @property
    def p_amount(self) -> Decimal:
        return Decimal(self.total_amount or 0).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    @cached_property
    def _breakdown(self):
        from finance.services.pricing import compute_breakdown
        return compute_breakdown(
            self.p_amount,
            fee_percent=self.platform_fee_percent(),
            vat_rate=self.vat_percent(),
        )

    @property
    def fee_amount(self) -> Decimal:
        return self._breakdown.platform_fee_value

    @property
    def vat_base(self) -> Decimal:
        return self._breakdown.taxable_base

    @property
    def vat_amount(self) -> Decimal:
        return self._breakdown.vat_amount

    @property
    def employee_net_amount(self) -> Decimal:
        return self._breakdown.tech_payout

    @property
    def grand_total(self) -> Decimal:
        return self._breakdown.client_total

    def _get_invoices_qs(self):
        mgr = getattr(self, "invoices", None)
        if mgr is None:
            return None
        try:
            return mgr.all()
        except Exception:
            return None

    @property
    def invoices_all_paid(self) -> bool:
        try:
            from finance.models import Invoice
        except Exception:
            return False

        qs = self._get_invoices_qs()
        if qs is not None:
            if not qs.exists():
                return False
            paid_val = getattr(Invoice.Status, "PAID", "paid")
            return not qs.exclude(status=paid_val).exists()

        inv = getattr(self, "invoice", None)
        if inv is None:
            return False
        try:
            paid_val = getattr(getattr(inv.__class__, "Status", None), "PAID", "paid")
            return (getattr(inv, "status", "") or "").lower() == (paid_val or "").lower()
        except Exception:
            return False

    @property
    def last_paid_invoice(self):
        try:
            from finance.models import Invoice
        except Exception:
            return None

        qs = self._get_invoices_qs()
        if qs is not None:
            paid_val = getattr(Invoice.Status, "PAID", "paid")
            return qs.filter(status=paid_val).order_by("-paid_at", "-id").first()

        inv = getattr(self, "invoice", None)
        return inv if inv and self.invoices_all_paid else None

    def mark_started(self, when=None, save: bool = True) -> None:
        if self.started_at:
            return
        dt = when or timezone.now()
        self.started_at = dt.date()
        if save:
            self.save(update_fields=["started_at", "updated_at"])

    @property
    def days_since_start(self) -> Optional[int]:
        if not self.started_at:
            return None
        return (timezone.now().date() - self.started_at).days

    @property
    def days_remaining(self) -> Optional[int]:
        if not self.started_at or not self.duration_days:
            return None
        passed = self.days_since_start
        if passed is None:
            return None
        remaining = self.duration_days - passed
        return remaining if remaining >= 0 else 0

    @property
    def all_milestones_approved(self) -> bool:
        ms_mgr = getattr(self, "milestones", None)
        if ms_mgr is None:
            return True

        done_statuses = {
            Milestone.Status.APPROVED,
            Milestone.Status.PAID,
        }
        return not ms_mgr.exclude(status__in=done_statuses).exists()

    def check_completion_after_milestone(self) -> None:
        """
        يتحقق من اكتمال الطلب بعد اعتماد كل المراحل.
        لا يقوم بأي إجراء عند دفع الفاتورة.
        يُستدعى فقط بعد تسليم/اعتماد/رفض المراحل.
        """
        from marketplace.models import Request

        req = getattr(self, "request", None)
        if not req:
            return

        status_field = "status" if hasattr(req, "status") else ("state" if hasattr(req, "state") else None)
        if not status_field:
            return

        ReqStatus = getattr(Request, "Status", None) or getattr(Request, "State", None)

        in_progress_val = getattr(ReqStatus, "IN_PROGRESS", "in_progress")
        completed_val = getattr(ReqStatus, "COMPLETED", "completed")

        current = (getattr(req, status_field, "") or "").strip().lower()

        # لا نكمل إلا لو الطلب بالفعل في التنفيذ
        if current != str(in_progress_val).lower():
            return

        ms_mgr = getattr(self, "milestones", None)
        if ms_mgr is None:
            return

        milestones = ms_mgr.all()
        if not milestones.exists():
            return

        done_statuses = {
            Milestone.Status.APPROVED,
            Milestone.Status.PAID,
        }

        if milestones.exclude(status__in=done_statuses).exists():
            return

        setattr(req, status_field, completed_val)
        update_fields = [status_field]
        if hasattr(req, "updated_at"):
            req.updated_at = timezone.now()
            update_fields.append("updated_at")
        req.save(update_fields=update_fields)

    def sync_request_state(
        self,
        *,
        save_request: bool = True,
        force: bool = False,
        logger=None,
    ) -> None:
        """
        يزامن حالة الطلب مع حالة الاتفاقية/الفواتير:

        - draft/pending/rejected => agreement_pending
        - accepted => awaiting_payment_confirmation
        - بعد سداد الفواتير + accepted => in_progress
        - ملاحظة: الاكتمال لا يتم هنا، بل عبر check_completion_after_milestone().
        """
        from marketplace.models import Request

        req = getattr(self, "request", None)
        if not req:
            return

        status_field = "status" if hasattr(req, "status") else ("state" if hasattr(req, "state") else None)
        if not status_field:
            return

        ReqStatus = getattr(Request, "Status", None) or getattr(Request, "State", None)

        new_val = getattr(ReqStatus, "NEW", "new")
        offer_selected_val = getattr(ReqStatus, "OFFER_SELECTED", "offer_selected")
        agreement_pending_val = getattr(ReqStatus, "AGREEMENT_PENDING", "agreement_pending")
        awaiting_payment_val = getattr(
            ReqStatus,
            "AWAITING_PAYMENT_CONFIRMATION",
            "awaiting_payment_confirmation",
        )
        in_progress_val = getattr(ReqStatus, "IN_PROGRESS", "in_progress")
        completed_val = getattr(ReqStatus, "COMPLETED", "completed")
        disputed_val = getattr(ReqStatus, "DISPUTED", "disputed")
        cancelled_val = getattr(ReqStatus, "CANCELLED", "cancelled")

        current = (getattr(req, status_field, "") or "").strip()
        if current in {completed_val, disputed_val, cancelled_val} and not force:
            return

        invoice_paid = self.invoices_all_paid
        paid_invoice = self.last_paid_invoice

        new_status = current

        if self.status in {self.Status.DRAFT, self.Status.PENDING, self.Status.REJECTED}:
            if current in {new_val, offer_selected_val, agreement_pending_val, awaiting_payment_val} or force:
                new_status = agreement_pending_val

        elif self.status == self.Status.ACCEPTED:
            new_status = awaiting_payment_val

            if invoice_paid:
                if not self.started_at:
                    try:
                        paid_at = getattr(paid_invoice, "paid_at", None) if paid_invoice else None
                        self.started_at = (paid_at.date() if paid_at else timezone.now().date())
                        self.save(update_fields=["started_at", "updated_at"])
                    except Exception:
                        pass

                # بعد الدفع فقط -> IN_PROGRESS (لا نكمل هنا)
                new_status = in_progress_val

                # إنشاء Payout مرة واحدة عند أول دفع
                try:
                    from finance.models import Payout
                    existing = None
                    if paid_invoice:
                        existing = Payout.objects.filter(agreement=self, invoice=paid_invoice).first()
                    if not existing and self.employee and paid_invoice:
                        Payout.objects.create(
                            employee=self.employee,
                            agreement=self,
                            invoice=paid_invoice,
                            amount=self.employee_net_amount or Decimal("0.00"),
                            status=Payout.Status.PENDING,
                        )
                except Exception:
                    import logging
                    logging.getLogger(__name__).exception(
                        f"Failed to auto-create payout for agreement {self.pk}"
                    )

        if not force and new_status == current:
            return

        setattr(req, status_field, new_status)

        if not save_request:
            return

        update_fields = [status_field]
        if hasattr(req, "updated_at"):
            req.updated_at = timezone.now()
            update_fields.append("updated_at")

        try:
            req.save(update_fields=update_fields)
        except Exception as exc:
            if logger:
                logger.warning(
                    "sync_request_state failed req=%s field=%s -> %s: %s",
                    getattr(req, "pk", None),
                    status_field,
                    new_status,
                    exc,
                )

    def clean(self) -> None:
        role = getattr(self.employee, "role", None)
        if role and role not in {"employee", "admin", "manager"}:
            raise ValidationError("يجب أن يكون الموظف بدور 'employee' أو 'admin/manager'.")

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
        if self.rejection_reason and self.status != self.Status.REJECTED:
            self.rejection_reason = ""

        if self.pk:
            try:
                prev = Agreement.objects.only("duration_days", "total_amount", "status").get(pk=self.pk)
            except Agreement.DoesNotExist:
                prev = None
            if prev and prev.status != Agreement.Status.DRAFT:
                if prev.duration_days != self.duration_days or prev.total_amount != self.total_amount:
                    raise ValidationError("لا يُسمح بتعديل المدة أو إجمالي المشروع بعد مغادرة المسودة.")

    def save(self, *args, **kwargs):
        old_status = None
        if self.pk:
            try:
                prev = Agreement.objects.only("status").get(pk=self.pk)
                old_status = prev.status
            except Agreement.DoesNotExist:
                old_status = None

        self.full_clean()
        super().save(*args, **kwargs)

        try:
            self.sync_request_state(save_request=True, force=False)
        except Exception:
            pass

        try:
            if old_status and self.status != old_status:
                from notifications.utils import create_notification
                employee = getattr(self, "employee", None)
                req = getattr(self, "request", None)
                client = getattr(req, "client", None) if req else None

                if self.status == self.Status.ACCEPTED:
                    create_notification(
                        recipient=employee,
                        title=f"تمت موافقة العميل على الاتفاقية للطلب #{req.pk}",
                        body=f"قام العميل {client} بالموافقة على الاتفاقية للطلب '{req.title}'. بانتظار تأكيد الدفع.",
                        url=self.get_absolute_url(),
                        actor=client,
                        target=self,
                    )
                elif self.status == self.Status.REJECTED:
                    create_notification(
                        recipient=employee,
                        title=f"تم رفض الاتفاقية من العميل للطلب #{req.pk}",
                        body=f"قام العميل {client} برفض الاتفاقية للطلب '{req.title}'. يمكنك مراجعة السبب واتخاذ الإجراء المناسب.",
                        url=self.get_absolute_url(),
                        actor=client,
                        target=self,
                    )
        except Exception:
            pass

    def __str__(self) -> str:
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
            models.CheckConstraint(condition=Q(total_amount__gte=0), name="agreement_total_amount_gte_0"),
            models.CheckConstraint(condition=Q(duration_days__gte=1), name="agreement_duration_days_gte_1"),
        ]
        verbose_name = "اتفاقية"
        verbose_name_plural = "اتفاقيات"


class Milestone(models.Model):
    class Status(models.TextChoices):
        PENDING = "pending", "قيد التنفيذ"
        DELIVERED = "delivered", "تم التسليم"
        APPROVED = "approved", "معتمدة"
        REJECTED = "rejected", "مرفوضة"
        PAID = "paid", "مدفوعة"

    agreement = models.ForeignKey(
        "agreements.Agreement",
        on_delete=models.CASCADE,
        related_name="milestones",
        verbose_name="الاتفاقية",
    )
    title = models.CharField("عنوان المرحلة", max_length=160)

    amount = models.DecimalField(
        "المبلغ (ريال)",
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        help_text="لا تُستخدم هذه القيمة في الفواتير حاليًا، حقل احتياطي فقط.",
    )

    order = models.PositiveIntegerField("الترتيب", default=1)
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
                condition=Q(amount__gte=0),
                name="milestone_amount_gte_0",
            ),
            models.CheckConstraint(
                condition=Q(order__gte=1),
                name="milestone_order_gte_1",
            ),
            models.CheckConstraint(
                condition=Q(due_days__isnull=True) | Q(due_days__gte=1),
                name="milestone_due_days_gte_1_or_null",
            ),
        ]
        verbose_name = "مرحلة"
        verbose_name_plural = "مراحل"

    def clean(self) -> None:
        self.amount = Decimal(self.amount or 0).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if self.amount < 0:
            raise ValidationError("مبلغ المرحلة يجب أن يكون رقمًا موجبًا أو صفرًا.")

        if self.order < 1:
            raise ValidationError("ترتيب المرحلة يجب أن يكون 1 أو أكبر.")

        if self.due_days is not None and self.due_days < 1:
            raise ValidationError({"due_days": "مدة المرحلة يجب أن تكون يومًا واحدًا على الأقل."})

        if not self.agreement_id:
            return

        qs_for_count = Milestone.objects.filter(agreement_id=self.agreement_id)
        if self.pk:
            qs_for_count = qs_for_count.exclude(pk=self.pk)
        count_existing = qs_for_count.count()
        if count_existing + 1 > 30:
            raise ValidationError("لا يمكن إضافة أكثر من 30 مرحلة لنفس الاتفاقية (الحد الأقصى 30 مرحلة).")

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

    def __str__(self) -> str:
        return f"Milestone#{self.pk} A{self.agreement_id} — {self.title} ({self.order})"

    def get_absolute_url(self) -> str:
        return reverse("agreements:milestone_detail", kwargs={"pk": self.pk})

    def _sync_parent(self) -> None:
        try:
            ag = getattr(self, "agreement", None)
            if ag:
                ag.check_completion_after_milestone()
        except Exception:
            pass

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
            self._sync_parent()

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
        self._sync_parent()

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
        self._sync_parent()

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
        self._sync_parent()

    def mark_paid(self) -> None:
        if not self.is_approved:
            raise ValidationError("لا يمكن السداد قبل اعتماد المرحلة.")
        self.status = self.Status.PAID
        self.paid_at = timezone.now()
        self.save(update_fields=["status", "paid_at"])
        self._sync_parent()


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

    def __str__(self) -> str:
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

    def __str__(self) -> str:
        if self.clause:
            return f"[{self.position}] {self.clause.title}"
        return f"[{self.position}] بند مخصص: {self.custom_text[:30]}..."

    @property
    def display_text(self) -> str:
        return self.clause.body if self.clause else (self.custom_text or "")


def _finance_create_total_invoice(agreement: Agreement) -> Optional[int]:
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
    try:
        from finance.models import Invoice
    except Exception:
        return True

    try:
        return not Invoice.objects.filter(agreement=agreement).exists()
    except Exception:
        return True


@receiver(post_save, sender=Agreement)
def agreement_post_save(sender, instance: Agreement, created: bool, **kwargs):
    try:
        if instance.status == Agreement.Status.ACCEPTED and _no_finance_invoices_exist(instance):
            with transaction.atomic():
                _finance_create_total_invoice(instance)
    except Exception:
        pass
