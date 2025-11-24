from __future__ import annotations

from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone
from django.utils.functional import cached_property
from django.utils.html import strip_tags

User = settings.AUTH_USER_MODEL


def _normalize_percent(value) -> Decimal:
    """
    يحوّل القيمة إلى نسبة عشرية:
    - 10   -> 0.10
    - 0.10 -> 0.10
    """
    if value is None:
        return Decimal("0")
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    if value > 1:
        value = value / Decimal("100")
    return value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


class Request(models.Model):
    """
    دورة الطلب المعتمدة:
      NEW → OFFER_SELECTED → AGREEMENT_PENDING → AWAITING_PAYMENT_CONFIRMATION → IN_PROGRESS → (COMPLETED | DISPUTED | CANCELLED)
    """

    class Status(models.TextChoices):
        NEW = "new", "طلب جديد"
        OFFER_SELECTED = "offer_selected", "تم اختيار عرض"
        AGREEMENT_PENDING = "agreement_pending", "اتفاقية بانتظار الموافقة"
        AWAITING_PAYMENT_CONFIRMATION = "awaiting_payment_confirmation", "تم قبول الاتفاقية وبانتظار تأكيد الدفع"
        IN_PROGRESS = "in_progress", "قيد التنفيذ"
        COMPLETED = "completed", "مكتمل"
        DISPUTED = "disputed", "نزاع"
        CANCELLED = "cancelled", "ملغى"

    client = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="requests_as_client",
        verbose_name="العميل",
    )
    assigned_employee = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        related_name="requests_as_employee",
        verbose_name="الموظف المُسنّد",
        null=True,
        blank=True,
    )

    title = models.CharField("العنوان", max_length=160)
    details = models.TextField("التفاصيل", blank=True)
    estimated_duration_days = models.PositiveIntegerField("مدة تقديرية (أيام)", default=7)
    estimated_price = models.DecimalField("سعر تقريبي", max_digits=12, decimal_places=2, default=0)
    links = models.TextField("روابط مرتبطة (اختياري)", blank=True)

    status = models.CharField(
        max_length=32,
        choices=Status.choices,
        default=Status.NEW,
        db_index=True,
        verbose_name="الحالة",
    )

    has_milestones = models.BooleanField("يحتوي مراحل؟", default=False)
    has_dispute = models.BooleanField("به نزاع؟", default=False)

    offers_window_ends_at = models.DateTimeField(
        "نهاية نافذة استقبال العروض (5 أيام)",
        null=True,
        blank=True,
        db_index=True,
    )
    selected_at = models.DateTimeField(
        "وقت اختيار العرض (للحالة OFFER_SELECTED وما بعدها)",
        null=True,
        blank=True,
        db_index=True,
    )
    agreement_due_at = models.DateTimeField(
        "موعد استحقاق إرسال الاتفاقية (SLA 3 أيام)",
        null=True,
        blank=True,
    )
    sla_agreement_overdue = models.BooleanField(
        "تجاوز مهلة إنشاء الاتفاقية (تم التنبيه؟)",
        default=False,
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def clean(self):
        if self.assigned_employee and getattr(self.assigned_employee, "role", None) != "employee":
            raise ValidationError("الإسناد يجب أن يكون إلى مستخدم بدور 'employee'.")

        if self.estimated_duration_days <= 0:
            raise ValidationError("المدة التقديرية بالأيام يجب أن تكون أكبر من صفر.")

        if self.estimated_price < 0:
            raise ValidationError("السعر التقديري لا يمكن أن يكون سالبًا.")

        if self.links:
            self.links = strip_tags(self.links).strip()

    def save(self, *args, skip_clean: bool = False, **kwargs):
        old_status = None
        if self.pk:
            try:
                prev = Request.objects.only("status").get(pk=self.pk)
                old_status = prev.status
            except Request.DoesNotExist:
                pass

        if not skip_clean:
            self.full_clean()

        result = super().save(*args, **kwargs)

        try:
            from notifications.utils import create_notification

            employee = getattr(self, "assigned_employee", None)
            client = getattr(self, "client", None)

            if old_status and self.status != old_status:
                if employee:
                    create_notification(
                        recipient=employee,
                        title=f"تغيرت حالة الطلب #{self.pk}",
                        body=(
                            f"قام العميل {client} بتغيير حالة الطلب "
                            f"'{self.title}' إلى '{self.get_status_display()}'."
                        ),
                        url=self.get_absolute_url(),
                        actor=client,
                        target=self,
                    )
                if client:
                    create_notification(
                        recipient=client,
                        title=f"تم تحديث حالة طلبك #{self.pk}",
                        body=f"تم تغيير حالة طلبك '{self.title}' إلى '{self.get_status_display()}'.",
                        url=self.get_absolute_url(),
                        actor=employee,
                        target=self,
                    )
        except Exception:
            pass

        try:
            from django.contrib.auth import get_user_model
            from notifications.utils import create_notification

            UserModel = get_user_model()
            admin_users = UserModel.objects.filter(role="admin", is_active=True)

            employee = getattr(self, "assigned_employee", None)
            client = getattr(self, "client", None)

            deadline = getattr(self, "deadline", None)
            if not deadline:
                deadline = self.created_at + timedelta(days=self.estimated_duration_days)

            if deadline and timezone.now() > deadline and self.status not in (
                self.Status.COMPLETED,
                self.Status.CANCELLED,
            ):
                for user in admin_users:
                    create_notification(
                        recipient=user,
                        title=f"مشروع متأخر #{self.pk}",
                        body=f"المشروع '{self.title}' تجاوز موعده ولم يكتمل بعد.",
                        url=self.get_absolute_url(),
                        actor=employee or client,
                        target=self,
                    )
        except Exception:
            pass

        return result

    @property
    def agreement_overdue(self) -> bool:
        return bool(self.agreement_due_at and timezone.now() > self.agreement_due_at)

    @property
    def offers_window_active(self) -> bool:
        return bool(
            self.status == self.Status.NEW
            and self.offers_window_ends_at
            and timezone.now() <= self.offers_window_ends_at
        )

    @property
    def is_new(self) -> bool:
        return self.status == self.Status.NEW

    @property
    def is_offer_selected(self) -> bool:
        return self.status == self.Status.OFFER_SELECTED

    @property
    def is_agreement_pending(self) -> bool:
        return self.status == self.Status.AGREEMENT_PENDING

    @property
    def is_awaiting_payment_confirmation(self) -> bool:
        return self.status == self.Status.AWAITING_PAYMENT_CONFIRMATION

    @property
    def is_in_progress(self) -> bool:
        return self.status == self.Status.IN_PROGRESS

    @property
    def is_completed(self) -> bool:
        return self.status == self.Status.COMPLETED

    @property
    def is_disputed(self) -> bool:
        return self.status == self.Status.DISPUTED or self.has_dispute

    @property
    def is_cancelled(self) -> bool:
        return self.status == self.Status.CANCELLED

    @property
    def selected_offer(self):
        try:
            return (
                self.offers.select_related("employee")
                .filter(Q(status=Offer.Status.SELECTED) | Q(status="selected"))
                .first()
            )
        except Exception:
            return None

    def ensure_offers_window(self, force: bool = False) -> None:
        days = getattr(settings, "OFFERS_WINDOW_DAYS", 5)
        if force or not self.offers_window_ends_at:
            base = self.created_at or timezone.now()
            self.offers_window_ends_at = base + timedelta(days=days)

    def flag_agreement_overdue_if_needed(self) -> bool:
        if (
            self.status == self.Status.AGREEMENT_PENDING
            and self.agreement_overdue
            and not self.sla_agreement_overdue
        ):
            self.sla_agreement_overdue = True
            self.save(update_fields=["sla_agreement_overdue", "updated_at"])
            return True
        return False

    @transaction.atomic
    def mark_offer_selected_now(self, employee):
        if not employee or getattr(employee, "role", None) != "employee":
            raise ValidationError("لا يمكن الإسناد إلا لمستخدم بدور 'employee'.")

        now = timezone.now()
        self.assigned_employee = employee
        self.status = self.Status.OFFER_SELECTED
        self.selected_at = now
        self.agreement_due_at = now + timedelta(days=3)
        self.sla_agreement_overdue = False
        self.ensure_offers_window()
        self.save(
            update_fields=[
                "assigned_employee",
                "status",
                "selected_at",
                "agreement_due_at",
                "sla_agreement_overdue",
                "offers_window_ends_at",
                "updated_at",
            ]
        )

    @transaction.atomic
    def transition_to_agreement_pending(self):
        if self.status != self.Status.OFFER_SELECTED:
            raise ValidationError("لا يمكن الانتقال إلى AGREEMENT_PENDING إلا من حالة OFFER_SELECTED.")
        self.status = self.Status.AGREEMENT_PENDING
        self.save(update_fields=["status", "updated_at"])

    @transaction.atomic
    def accept_agreement_and_wait_payment(self):
        if self.status != self.Status.AGREEMENT_PENDING:
            raise ValidationError("لا يمكن الانتقال إلى AWAITING_PAYMENT_CONFIRMATION إلا من حالة AGREEMENT_PENDING.")
        self.status = self.Status.AWAITING_PAYMENT_CONFIRMATION
        self.save(update_fields=["status", "updated_at"])

    @transaction.atomic
    def start_in_progress(self):
        return self.accept_agreement_and_wait_payment()

    @transaction.atomic
    def mark_paid_and_start(self):
        if self.status != self.Status.AWAITING_PAYMENT_CONFIRMATION:
            raise ValidationError("لا يمكن البدء بالتنفيذ إلا من حالة AWAITING_PAYMENT_CONFIRMATION.")
        self.status = self.Status.IN_PROGRESS
        self.save(update_fields=["status", "updated_at"])

    @transaction.atomic
    def mark_completed(self):
        if self.status not in (self.Status.IN_PROGRESS, self.Status.DISPUTED):
            raise ValidationError("يمكن الإكمال فقط من حالات التنفيذ أو النزاع (بعد الحل).")
        self.status = self.Status.COMPLETED
        self.save(update_fields=["status", "updated_at"])

    @transaction.atomic
    def admin_cancel(self):
        self.assigned_employee = None
        self.status = self.Status.CANCELLED
        self.selected_at = None
        self.agreement_due_at = None
        self.sla_agreement_overdue = False
        self.save(
            update_fields=[
                "assigned_employee",
                "status",
                "selected_at",
                "agreement_due_at",
                "sla_agreement_overdue",
                "updated_at",
            ]
        )

    @transaction.atomic
    def reset_to_new(self):
        try:
            Offer.objects.filter(request=self).exclude(
                status=getattr(Offer.Status, "REJECTED", "rejected")
            ).update(status=getattr(Offer.Status, "REJECTED", "rejected"))
        except Exception:
            pass

        self.assigned_employee = None
        self.status = self.Status.NEW
        self.selected_at = None
        self.agreement_due_at = None
        self.sla_agreement_overdue = False
        days = getattr(settings, "OFFERS_WINDOW_DAYS", 5)
        self.offers_window_ends_at = timezone.now() + timedelta(days=days)
        self.save(
            update_fields=[
                "assigned_employee",
                "status",
                "selected_at",
                "agreement_due_at",
                "sla_agreement_overdue",
                "offers_window_ends_at",
                "updated_at",
            ]
        )

    @transaction.atomic
    def reassign_to(self, employee):
        if not employee or getattr(employee, "role", None) != "employee":
            raise ValidationError("لا يمكن الإسناد إلا لمستخدم بدور 'employee'.")
        self.assigned_employee = employee
        self.save(update_fields=["assigned_employee", "updated_at"])

    @transaction.atomic
    def open_dispute(self):
        self.status = self.Status.DISPUTED
        self.has_dispute = True
        self.save(update_fields=["status", "has_dispute", "updated_at"])

        try:
            from django.contrib.auth import get_user_model
            from notifications.utils import create_notification

            UserModel = get_user_model()
            finance_users = UserModel.objects.filter(role="finance", is_active=True)
            admin_users = UserModel.objects.filter(role="admin", is_active=True)

            for user in list(finance_users) + list(admin_users):
                create_notification(
                    recipient=user,
                    title=f"نزاع جديد على الطلب #{self.pk}",
                    body=f"تم فتح نزاع جديد على الطلب '{self.title}'.",
                    url=self.get_absolute_url(),
                    actor=self.client if hasattr(self, "client") else None,
                    target=self,
                )
        except Exception:
            pass

    @transaction.atomic
    def close_dispute(self, resume_status: Optional[str] = None):
        self.has_dispute = False
        if resume_status:
            self.status = resume_status
        else:
            self.status = self.Status.AGREEMENT_PENDING if self.selected_at else self.Status.NEW
        self.save(update_fields=["status", "has_dispute", "updated_at"])

    def get_absolute_url(self) -> str:
        try:
            return reverse("marketplace:request_detail", args=[self.pk])
        except Exception:
            return f"/marketplace/r/{self.pk}/"

    def __str__(self) -> str:
        return f"[{self.pk}] {self.title} — {self.get_status_display()}"

    class Meta:
        indexes = [
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["client"]),
            models.Index(fields=["assigned_employee"]),
            models.Index(fields=["offers_window_ends_at"]),
            models.Index(fields=["agreement_due_at"]),
        ]
        constraints = [
            models.CheckConstraint(
                check=Q(estimated_duration_days__gt=0),
                name="request_duration_days_gt_0",
            ),
            models.CheckConstraint(
                check=Q(estimated_price__gte=0),
                name="request_estimated_price_gte_0",
            ),
        ]
        verbose_name = "طلب"
        verbose_name_plural = "طلبات"


class Offer(models.Model):
    """
    سياسة المال المعتمدة (مصدر الحقيقة: finance.services.pricing):
      - P = proposed_price
      - fee = P × fee%
      - net_emp = P − fee
      - vat = P × vat%
      - client_total = P + vat
    """

    class Status(models.TextChoices):
        PENDING = "pending", "قيد المراجعة"
        SELECTED = "selected", "العرض المختار"
        REJECTED = "rejected", "مرفوض"
        WITHDRAWN = "withdrawn", "مسحوب"

    request = models.ForeignKey(
        "marketplace.Request",
        related_name="offers",
        on_delete=models.CASCADE,
        verbose_name="الطلب",
    )
    employee = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        related_name="offers",
        on_delete=models.CASCADE,
        verbose_name="الموظف",
    )

    proposed_duration_days = models.PositiveIntegerField("المدة المقترحة (أيام)")
    proposed_price = models.DecimalField("السعر المقترح P", max_digits=12, decimal_places=2)
    note = models.TextField("ملاحظة", blank=True)

    status = models.CharField(
        "الحالة",
        max_length=20,
        choices=Status.choices,
        default=Status.PENDING,
        db_index=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    client_total_amount_cache = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        null=True,
        blank=True,
        editable=False,
        verbose_name="إجمالي العميل (مخزن)",
    )

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["request"],
                condition=Q(status="selected"),
                name="uq_request_single_selected_offer",
            ),
            models.UniqueConstraint(
                fields=["request", "employee"],
                condition=~Q(status="withdrawn"),
                name="uq_active_offer_per_employee_per_request",
            ),
            models.CheckConstraint(
                check=Q(proposed_duration_days__gt=0),
                name="offer_duration_days_gt_0",
            ),
            models.CheckConstraint(
                check=Q(proposed_price__gte=0),
                name="offer_price_gte_0",
            ),
        ]
        verbose_name = "عرض"
        verbose_name_plural = "عروض"

    @cached_property
    def _raw_rates(self) -> tuple[Decimal, Decimal]:
        """
        يرجع (fee_raw, vat_raw) كما هي من الإعدادات (للعرض فقط).
        ممكن تكون 10 أو 0.10 حسب ما هو مخزن.
        """
        try:
            from finance.models import FinanceSettings
            fee, vat = FinanceSettings.current_rates()
        except Exception:
            fee, vat = 0, 0
        return Decimal(str(fee or 0)), Decimal(str(vat or 0))

    @property
    def platform_fee_percent(self) -> Decimal:
        fee_raw, _ = self._raw_rates
        return fee_raw.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    @property
    def vat_percent(self) -> Decimal:
        _, vat_raw = self._raw_rates
        return vat_raw.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    @cached_property
    def _rates(self) -> tuple[Decimal, Decimal]:
        """
        يرجع (fee_percent, vat_percent) كنسب عشرية للاستخدام في الحساب.
        """
        fee_raw, vat_raw = self._raw_rates
        return _normalize_percent(fee_raw), _normalize_percent(vat_raw)

    @cached_property
    def _breakdown(self):
        from finance.services.pricing import compute_breakdown
        fee_rate, vat_rate = self._rates
        return compute_breakdown(
            self.proposed_price_q,
            fee_percent=fee_rate,
            vat_rate=vat_rate,
        )

    @property
    def proposed_price_q(self) -> Decimal:
        return Decimal(self.proposed_price or 0).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    @property
    def platform_fee_amount(self) -> Decimal:
        return Decimal(self._breakdown.platform_fee_value).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )

    @property
    def net_for_employee(self) -> Decimal:
        return Decimal(self._breakdown.tech_payout).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )

    @property
    def vat_amount(self) -> Decimal:
        return Decimal(self._breakdown.vat_amount).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )

    @property
    def client_total_amount(self) -> Decimal:
        if self.client_total_amount_cache is not None:
            return Decimal(self.client_total_amount_cache).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        return Decimal(self._breakdown.client_total).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )

    def as_financial_dict(self) -> dict:
        return {
            "proposed_price": self.proposed_price_q,
            "employee_net": self.net_for_employee,
            "platform_fee": self.platform_fee_amount,
            "vat_amount": self.vat_amount,
            "client_total": self.client_total_amount,
        }

    def save(self, *args, skip_clean: bool = False, **kwargs):
        if not skip_clean:
            self.full_clean()
        try:
            self.client_total_amount_cache = self.client_total_amount
        except Exception:
            self.client_total_amount_cache = None
        return super().save(*args, **kwargs)

    def can_view(self, user) -> bool:
        if not getattr(user, "is_authenticated", False):
            return False
        if getattr(user, "is_superuser", False) or getattr(user, "is_staff", False):
            return True
        if getattr(user, "role", "") in ("admin", "manager", "finance"):
            return True
        return user.id in (self.request.client_id, self.employee_id)

    def can_select(self, user) -> bool:
        return (
            getattr(user, "is_authenticated", False)
            and user.id == self.request.client_id
            and self.status == self.Status.PENDING
            and self.request.status == Request.Status.NEW
            and self.request.offers_window_active
        )

    def can_reject(self, user) -> bool:
        return (
            getattr(user, "is_authenticated", False)
            and user.id == self.request.client_id
            and self.status == self.Status.PENDING
        )

    def clean(self):
        if self.proposed_duration_days <= 0:
            raise ValidationError("المدة المقترحة يجب أن تكون أكبر من صفر.")
        if self.proposed_price < 0:
            raise ValidationError("السعر المقترح لا يمكن أن يكون سالبًا.")

        req: Request = getattr(self, "request", None)
        if req:
            req.ensure_offers_window()
            if (
                req.status == Request.Status.NEW
                and req.offers_window_ends_at
                and timezone.now() > req.offers_window_ends_at
                and self.status != self.Status.WITHDRAWN
            ):
                raise ValidationError("انتهت نافذة استقبال العروض لهذا الطلب.")

        if self.note:
            self.note = strip_tags(self.note).strip()

    def __str__(self):
        return f"Offer#{self.pk} R{self.request_id} by {self.employee_id}"


class Note(models.Model):
    request = models.ForeignKey(Request, on_delete=models.CASCADE, related_name="notes", verbose_name="الطلب")
    author = models.ForeignKey(User, on_delete=models.CASCADE, verbose_name="الكاتب")
    text = models.TextField("نص الملاحظة")
    parent = models.ForeignKey(
        "self", on_delete=models.CASCADE, null=True, blank=True, related_name="replies"
    )
    is_internal = models.BooleanField("رؤية مقيدة (داخلي)", default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        verbose_name = "ملاحظة"
        verbose_name_plural = "ملاحظات"

    def clean(self):
        if self.text:
            self.text = strip_tags(self.text).strip()

    def __str__(self):
        return f"Note#{self.pk} R{self.request_id} by {self.author_id}"


class ServiceRequest(Request):
    class Meta:
        proxy = True
        verbose_name = "طلب"
        verbose_name_plural = "طلبات"

    @property
    def in_offers_window(self) -> bool:
        days = getattr(settings, "OFFERS_WINDOW_DAYS", 5)
        if not self.created_at:
            return False
        limit = self.created_at + timedelta(days=days)
        return timezone.now() < limit and self.status == Request.Status.NEW
