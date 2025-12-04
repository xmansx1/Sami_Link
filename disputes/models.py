# disputes/models.py
from __future__ import annotations

from django.conf import settings
from django.db import models, transaction
from django.db.models import Q
from django.db.models.signals import post_save, post_delete
from django.dispatch import receiver
from django.utils import timezone

# نفترض أن نموذج الطلب في app: marketplace واسمه Request
# إن كان لديك اسم/مسار مختلف عدّله هنا:
from marketplace.models import Request


class Dispute(models.Model):
    """
    نموذج النزاع المرتبط بطلب. يجمّد الصرف/التحصيل تلقائيًا أثناء فتح النزاع
    وحتى الحسم. نزاع واحد فقط يمكن أن يكون مفتوحًا/قيد المراجعة لكل طلب.
    """

    # حالات النزاع
    class Status(models.TextChoices):
        OPEN = "open", "مفتوح"
        IN_REVIEW = "in_review", "قيد المراجعة"
        RESOLVED = "resolved", "محسوم"
        CANCELED = "canceled", "ملغى"

    # من فتح النزاع
    class OpenerRole(models.TextChoices):
        CLIENT = "client", "عميل"
        EMPLOYEE = "employee", "موظف"
        ADMIN = "admin", "إداري"

    # العلاقات الأساسية
    request = models.ForeignKey(
        Request,
        on_delete=models.CASCADE,
        related_name="disputes",
        verbose_name="الطلب",
    )
    # اختياري: لو تريد ربط النزاع بمرحلة (Milestone) بدون اعتماد صريح على agreements
    milestone_id = models.IntegerField(null=True, blank=True, verbose_name="مرحلة مرتبطة (ID)")

    opened_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="opened_disputes",
        verbose_name="فُتح بواسطة",
    )
    opener_role = models.CharField(max_length=16, choices=OpenerRole.choices, verbose_name="دور مُنشئ النزاع")

    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.OPEN,
        verbose_name="الحالة",
    )

    title = models.CharField(max_length=200, verbose_name="العنوان")
    reason = models.TextField(verbose_name="السبب")               # سبب مختصر/واضح
    details = models.TextField(blank=True, verbose_name="تفاصيل") # تفاصيل إضافية

    resolved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        null=True,
        blank=True,
        on_delete=models.SET_NULL,
        related_name="resolved_disputes",
        verbose_name="حُل بواسطة",
    )
    resolved_note = models.TextField(blank=True, verbose_name="ملاحظة الحسم")

    opened_at = models.DateTimeField(default=timezone.now, verbose_name="تاريخ الفتح")
    resolved_at = models.DateTimeField(null=True, blank=True, verbose_name="تاريخ الحسم")

    class Meta:
        ordering = ["-opened_at"]
        indexes = [
            models.Index(fields=["request", "status"]),
            models.Index(fields=["opened_at"]),
        ]
        constraints = [
            # نزاع واحد فقط (OPEN/IN_REVIEW) لكل طلب — نستخدم القيم النصية لتجنّب تحذير Pylance
            models.UniqueConstraint(
                fields=["request"],
                condition=Q(status__in=["open", "in_review"]),
                name="uniq_open_or_review_dispute_per_request",
            ),
        ]

        verbose_name = "نزاع"
        verbose_name_plural = "نزاعات"

    # ---------- خصائص/مساعدات ----------
    def __str__(self) -> str:
        return f"Dispute #{self.pk} on Request #{self.request_id} [{self.status}]"

    @property
    def is_active(self) -> bool:
        """النزاع فعّال إذا كان مفتوحًا أو قيد المراجعة."""
        return self.status in {self.Status.OPEN, self.Status.IN_REVIEW}


class DisputeMessage(models.Model):
    """
    رسائل المحادثة داخل النزاع (بين العميل، الموظف، والإدارة).
    """
    dispute = models.ForeignKey(
        Dispute,
        on_delete=models.CASCADE,
        related_name="messages",
        verbose_name="النزاع"
    )
    sender = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="dispute_messages",
        verbose_name="المرسل"
    )
    content = models.TextField(verbose_name="نص الرسالة")
    attachment = models.FileField(
        upload_to="disputes/attachments/%Y/%m/",
        null=True,
        blank=True,
        verbose_name="مرفق"
    )
    created_at = models.DateTimeField(auto_now_add=True, verbose_name="وقت الإرسال")
    is_internal = models.BooleanField(
        default=False,
        verbose_name="ملاحظة داخلية (للإدارة فقط)"
    )

    class Meta:
        ordering = ["created_at"]
        verbose_name = "رسالة نزاع"
        verbose_name_plural = "رسائل النزاع"

    def __str__(self):
        return f"Msg #{self.pk} by {self.sender} on Dispute #{self.dispute_id}"

    # ---------- إجراءات حالة عالية المستوى ----------
    def mark_in_review(self, by_user=None, note: str = "") -> None:
        """تحويل النزاع إلى قيد المراجعة."""
        self.status = self.Status.IN_REVIEW
        if note:
            self.resolved_note = (self.resolved_note + "\n" + note).strip() if self.resolved_note else note
        self._save_status_only()

    def resolve(self, by_user=None, note: str = "") -> None:
        """حسم النزاع."""
        self.status = self.Status.RESOLVED
        self.resolved_at = timezone.now()
        if by_user:
            self.resolved_by = by_user
        if note:
            self.resolved_note = (self.resolved_note + "\n" + note).strip() if self.resolved_note else note
        self._save_status_only()

    def cancel(self, by_user=None, note: str = "") -> None:
        """إلغاء النزاع (مثلاً بطلب أطرافه قبل الحسم)."""
        self.status = self.Status.CANCELED
        self.resolved_at = timezone.now()
        if by_user:
            self.resolved_by = by_user
        if note:
            self.resolved_note = (self.resolved_note + "\n" + note).strip() if self.resolved_note else note
        self._save_status_only()

    def _save_status_only(self) -> None:
        """حفظ آمن وتحديث حقول الحالة/التواريخ فقط."""
        updates = ["status"]
        if "resolved_at" in self.get_deferred_fields() or getattr(self, "resolved_at", None):
            updates.append("resolved_at")
        if "resolved_by" in self.get_deferred_fields() or getattr(self, "resolved_by_id", None):
            updates.append("resolved_by")
        if "resolved_note" in self.get_deferred_fields() or getattr(self, "resolved_note", ""):
            updates.append("resolved_note")
        if hasattr(self, "opened_at"):
            # لا نغيّر opened_at هنا
            pass
        self.save(update_fields=list(dict.fromkeys(updates)))  # إزالة التكرارات بحفظ الترتيب

    # ---------- تحقّقات خفيفة ----------
    def clean(self):
        # يمكن إضافة تحقق من تناسق opener_role مع خصائص opened_by.role إن أردت
        super().clean()


# =========================
# Signals: تجميد/فكّ التجميد
# =========================

def _freeze_request(req: Request) -> None:
    """
    يفعّل تجميد الطلب ماليًا ويضع حالته إلى DISPUTED إن وُجدت هذه الحالة.
    لا يحذف أي معلومات أخرى.
    """
    needs_save = False
    now = timezone.now()

    # علم التجميد
    if hasattr(req, "is_frozen"):
        if not getattr(req, "is_frozen", False):
            req.is_frozen = True
            needs_save = True

    # تحويل حالة الطلب إلى DISPUTED إن كانت مدعومة
    disputed_val = getattr(getattr(Request, "Status", None), "DISPUTED", None)
    if disputed_val:
        if getattr(req, "status", None) != disputed_val:
            req.status = disputed_val
            needs_save = True

    if hasattr(req, "updated_at"):
        req.updated_at = now
        needs_save = True

    if needs_save:
        req.save(update_fields=[f for f in ["is_frozen", "status", "updated_at"] if hasattr(req, f)])


def _unfreeze_request_if_no_active_disputes(req: Request) -> None:
    """
    يفك التجميد إذا لم يعد هناك نزاعات فعّالة على الطلب.
    لا يغيّر الحالة إلى ما قبل النزاع؛ فقط يزيل العلم is_frozen.
    """
    if not hasattr(req, "is_frozen"):
        return
    if req.disputes.filter(status__in=[Dispute.Status.OPEN, Dispute.Status.IN_REVIEW]).exists():
        return
    if getattr(req, "is_frozen", False):
        req.is_frozen = False
        if hasattr(req, "updated_at"):
            req.updated_at = timezone.now()
            req.save(update_fields=["is_frozen", "updated_at"])
        else:
            req.save(update_fields=["is_frozen"])


@receiver(post_save, sender=Dispute)
def _disputes_on_save(sender, instance: Dispute, created: bool, **kwargs):
    """
    عند إنشاء نزاع أو تغيّر حالته:
    - إذا كان فعّالاً ⇒ جمّد الطلب و(إن أمكن) اجعله DISPUTED.
    - إذا لم يعد فعّالاً ⇒ فك التجميد لو لم تبقَ نزاعات فعّالة أخرى.
    نستخدم on_commit لتفادي سباقات الحفظ.
    """
    def _apply():
        req = instance.request
        if instance.is_active:
            _freeze_request(req)
        else:
            _unfreeze_request_if_no_active_disputes(req)

    try:
        transaction.on_commit(_apply)
    except Exception:
        # في حال عدم وجود معاملة، نفّذ مباشرة
        _apply()


@receiver(post_delete, sender=Dispute)
def _disputes_on_delete(sender, instance: Dispute, **kwargs):
    """
    عند حذف النزاع: فك التجميد إذا لم تبقَ نزاعات فعّالة على الطلب.
    """
    def _apply():
        req = instance.request
        _unfreeze_request_if_no_active_disputes(req)

    try:
        transaction.on_commit(_apply)
    except Exception:
        _apply()
