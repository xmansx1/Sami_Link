from __future__ import annotations

from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator, MaxValueValidator
from django.db import models, transaction
from django.db.models import Q
from django.urls import reverse
from django.utils import timezone
from django.utils.functional import cached_property
from django.utils.html import strip_tags

User = settings.AUTH_USER_MODEL

# تعريف الحالات بشكل مشترك بين الطلبات والعروض
class Status(models.TextChoices):
    NEW = "new", "طلب جديد"
    OFFER_SELECTED = "offer_selected", "تم اختيار عرض"
    AGREEMENT_PENDING = "agreement_pending", "اتفاقية بانتظار الموافقة"
    AWAITING_PAYMENT_CONFIRMATION = "awaiting_payment_confirmation", "تم قبول الاتفاقية وبانتظار تأكيد الدفع"
    IN_PROGRESS = "in_progress", "قيد التنفيذ"
    COMPLETED = "completed", "مكتمل"
    DISPUTED = "disputed", "نزاع"
    CANCELLED = "cancelled", "ملغى"
    # حالات خاصة بالعروض:
    REJECTED = "rejected", "مرفوض"
    MODIFIED = "modified", "معدل بانتظار موافقة العميل"
    WAITING_CLIENT_APPROVAL = "waiting_client_approval", "بانتظار موافقة العميل"
    SELECTED = "selected", "تم اختيار العرض"
    WITHDRAWN = "withdrawn", "منسحب/ملغي من الموظف"


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


    # الحقول الأساسية المطلوبة للفورم والقوالب
    title = models.CharField(
        max_length=255,
        verbose_name="عنوان الطلب",
        help_text="عنوان مختصر للطلب."
    )
    details = models.TextField(
        verbose_name="تفاصيل الطلب",
        help_text="وصف مفصل للطلب."
    )
    estimated_duration_days = models.PositiveIntegerField(
        verbose_name="المدة التقديرية (أيام)",
        help_text="عدد الأيام المتوقع لإنجاز الطلب."
    )
    estimated_price = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        verbose_name="السعر التقديري",
        help_text="المبلغ المتوقع للطلب."
    )
    links = models.TextField(
        blank=True,
        null=True,
        verbose_name="روابط مرتبطة",
        help_text="روابط أو مراجع إضافية (اختياري)."
    )
    agreement_due_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        verbose_name="تاريخ استحقاق الاتفاقية",
        help_text="تاريخ ووقت استحقاق الاتفاقية لهذا الطلب.",
    )
    offers_window_ends_at = models.DateTimeField(
        null=True,
        blank=True,
        db_index=True,
        verbose_name="نهاية فترة استقبال العروض",
        help_text="تاريخ ووقت انتهاء فترة استقبال العروض لهذا الطلب.",
    )
    client = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="requests",
        verbose_name="العميل",
        db_index=True,
    )
    assigned_employee = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        related_name="assigned_requests",
        verbose_name="الموظف المسند",
        null=True,
        blank=True,
        db_index=True,
    )
    status = models.CharField(
        max_length=32,
        choices=Status.choices,
        default=Status.NEW,
        db_index=True,
        verbose_name="الحالة",
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
    request = models.ForeignKey('Request', on_delete=models.CASCADE, related_name='offers', verbose_name='الطلب')
    employee = models.ForeignKey(User, on_delete=models.CASCADE, related_name='offers', verbose_name='الموظف')
    status = models.CharField(
        max_length=32,
        choices=Status.choices,
        default=Status.NEW,
        db_index=True,
        verbose_name="الحالة",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    proposed_price = models.DecimalField("السعر المقترح", max_digits=12, decimal_places=2, blank=True, null=True)
    proposed_duration_days = models.PositiveIntegerField("المدة المقترحة (أيام)", blank=True, null=True)
    modification_reason = models.TextField("سبب التعديل/الإلغاء", blank=True, null=True)
    extension_requested_days = models.PositiveIntegerField("عدد أيام التمديد المطلوبة", blank=True, null=True)
    extension_reason = models.TextField("سبب طلب التمديد", blank=True, null=True)
    modified_price = models.DecimalField("السعر بعد التعديل", max_digits=12, decimal_places=2, blank=True, null=True)
    modified_duration_days = models.PositiveIntegerField("المدة بعد التعديل بالأيام", blank=True, null=True)
    note = models.TextField("ملاحظات إضافية (اختياري)", blank=True, null=True)

    @property
    def breakdown(self):
        from finance.services.pricing import breakdown_for_offer
        return breakdown_for_offer(self)

    @property
    def client_total_amount(self):
        return self.breakdown.client_total

    def can_cancel(self, user) -> bool:
        """
        يمنع الإلغاء إذا كانت الاتفاقية مقبولة.
        """
        if not getattr(user, "is_authenticated", False):
            return False
        if self.status in [Status.REJECTED, Status.WITHDRAWN]:
            return False
        # منع الإلغاء إذا كانت الاتفاقية مقبولة
        agreement = getattr(self.request, "agreement", None)
        if agreement and getattr(agreement, "status", None) == "accepted":
            return False
        if user == self.employee or getattr(user, "is_staff", False) or getattr(user, "role", "") == "admin":
            return True
        return False

    def can_extend(self, user) -> bool:
        """
        يسمح للموظف صاحب العرض أو الإدارة بطلب تمديد إذا كان العرض مختارًا أو قيد التنفيذ.
        """
        if not getattr(user, "is_authenticated", False):
            return False
        if self.status not in [Status.SELECTED, Status.MODIFIED, Status.WAITING_CLIENT_APPROVAL]:
            return False
        if user == self.employee or getattr(user, "is_staff", False) or getattr(user, "role", "") == "admin":
            return True
        return False





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


class Comment(models.Model):
    request = models.ForeignKey(Request, on_delete=models.CASCADE, related_name="comments", verbose_name="الطلب")
    author = models.ForeignKey(User, on_delete=models.CASCADE, verbose_name="الكاتب")
    content = models.TextField("المحتوى")
    file = models.FileField(upload_to="comments_files/", blank=True, null=True, verbose_name="ملف مرفق")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]
        verbose_name = "تعليق"
        verbose_name_plural = "تعليقات"

    def __str__(self):
        return f"Comment by {self.author} on {self.request}"


class Review(models.Model):
    request = models.OneToOneField(Request, on_delete=models.CASCADE, related_name="review", verbose_name="الطلب")
    reviewer = models.ForeignKey(User, on_delete=models.CASCADE, related_name="reviews_given", verbose_name="المقيم")
    reviewee = models.ForeignKey(User, on_delete=models.CASCADE, related_name="reviews_received", verbose_name="المقيم عليه")
    rating = models.PositiveSmallIntegerField(
        "التقييم",
        validators=[MinValueValidator(0), MaxValueValidator(5)],
        help_text="من 0 إلى 5"
    )
    comment = models.TextField("التعليق", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "تقييم"
        verbose_name_plural = "التقييمات"

    def __str__(self):
        return f"Review {self.rating}/5 for {self.reviewee} by {self.reviewer}"

