# disputes/admin.py
from __future__ import annotations

from typing import Iterable, Optional
from django.contrib import admin, messages
from django.db import transaction
from django.http import HttpRequest
from django.utils import timezone

from .models import Dispute, DisputeMessage

class DisputeMessageInline(admin.TabularInline):
    model = DisputeMessage
    extra = 0
    readonly_fields = ["created_at"]
    fields = ["sender", "content", "attachment", "is_internal", "created_at"]

@admin.register(Dispute)
class DisputeAdmin(admin.ModelAdmin):
    inlines = [DisputeMessageInline]
    """
    إدارة النزاعات وفق التحليل:
    - عند فتح نزاع: يُجمَّد الطلب/الصرف (إن وُجد حقل مناسب على الطلب).
    - عند الحل/الإغلاق: يُفك التجميد تلقائيًا (إلا إذا كان هناك سبب يمنع).
    - إجراءات سريعة: حلّ، إغلاق، إعادة فتح، تجميد/فك تجميد.
    - تحسين الأداء عبر select_related، والتحقق الدفاعي من الحقول غير الموجودة.
    """

    # أعمدة العرض
    list_display = (
        "id",
        "request_link",
        "status_badge",
        "opener_role",
        "opened_by",
        "opened_at",
        "resolved_by",
        "resolved_at",
        "is_request_frozen",
    )
    list_filter = ("status", "opener_role", ("opened_at", admin.DateFieldListFilter), ("resolved_at", admin.DateFieldListFilter))
    search_fields = (
        "title",
        "reason",
        "details",
        "request__title",
        "request__short_code",
        "request__id",
        "opened_by__name",
        "opened_by__email",
    )
    autocomplete_fields = ("opened_by", "resolved_by", "request")
    date_hierarchy = "opened_at"
    ordering = ("-opened_at", "-id")
    list_per_page = 50
    list_select_related = ("request", "opened_by", "resolved_by")

    # حقول للقراءة فقط (مع منطق تمييزي)
    readonly_fields = ("opened_at", "resolved_at")
    fieldsets = (
        ("الأساسي", {"fields": ("request", "status", "title", "reason")}),
        ("التفاصيل", {"fields": ("details",)}),
        ("الافتتاح", {"fields": ("opener_role", "opened_by", "opened_at")}),
        ("الإغلاق", {"fields": ("resolved_by", "resolved_at")}),
    )

    # ======= تحسينات العرض =======
    def request_link(self, obj: Dispute):
        req = getattr(obj, "request", None)
        if not req:
            return "-"
        # إن وُجد get_absolute_url استخدمه، وإلا أعرض المعرّف فقط
        try:
            url = req.get_absolute_url()
            return admin.utils.format_html('<a href="{}" target="_blank">#{}</a> — {}', url, req.pk, getattr(req, "title", ""))
        except Exception:
            return f"#{getattr(req, 'pk', '—')} — {getattr(req, 'title', '')}"
    request_link.short_description = "الطلب"

    def status_badge(self, obj: Dispute):
        val = getattr(obj, "status", "") or ""
        return f"{val}"
    status_badge.short_description = "الحالة"

    def is_request_frozen(self, obj: Dispute) -> str:
        req = getattr(obj, "request", None)
        if not req:
            return "—"
        # نحاول قراءة أي مؤشر تجميد معروف
        for attr in ("is_frozen", "frozen", "finance_hold"):
            if hasattr(req, attr):
                return "نعم" if bool(getattr(req, attr)) else "لا"
        return "غير معروف"
    is_request_frozen.short_description = "الطلب مُجمّد؟"

    # ======= صلاحيات الكتابة =======
    def get_readonly_fields(self, request: HttpRequest, obj: Optional[Dispute] = None) -> Iterable[str]:
        base = list(super().get_readonly_fields(request, obj))
        # المشرف/الستاف يستطيع تعديل الحالة، البقية قراءة فقط
        is_admin_like = bool(request.user.is_superuser or request.user.is_staff or getattr(request.user, "role", "") in {"admin", "manager", "gm", "finance"})
        if not is_admin_like:
            return base + ["status", "opener_role", "opened_by", "resolved_by", "request", "title", "reason", "details"]
        return base

    # ======= إجراءات سريعة =======
    actions = (
        "action_mark_resolved",
        "action_mark_closed",
        "action_reopen",
        "action_freeze_request",
        "action_unfreeze_request",
    )

    def _set_request_freeze(self, req, freeze: bool) -> bool:
        """
        يحاول تفعيل/إلغاء التجميد على الطلب، مع التحمّل الدفاعي لأسماء خصائص مختلفة.
        يُرجع True إذا نجح تحديث أي علم معروف، وإلا False.
        """
        updated = False
        now = timezone.now()
        possible_flags = ("is_frozen", "frozen", "finance_hold")
        for attr in possible_flags:
            if hasattr(req, attr):
                try:
                    setattr(req, attr, bool(freeze))
                    update_fields = [attr]
                    if hasattr(req, "updated_at"):
                        req.updated_at = now
                        update_fields.append("updated_at")
                    req.save(update_fields=update_fields)
                    updated = True
                    break
                except Exception:
                    # نواصل تجربة حقل آخر إن فشل الحفظ
                    continue
        return updated

    def _maybe_touch_disputed_status(self, req) -> None:
        """
        يضبط حالة الطلب إلى DISPUTED عند التجميد، أو يعيدها لوضع سابق عند فك التجميد (لا يفرض).
        """
        try:
            DISPUTED = getattr(getattr(req, "Status", None), "DISPUTED", "disputed")
            if hasattr(req, "status"):
                req.status = DISPUTED
                update_fields = ["status"]
                if hasattr(req, "updated_at"):
                    req.updated_at = timezone.now()
                    update_fields.append("updated_at")
                req.save(update_fields=update_fields)
        except Exception:
            pass

    @transaction.atomic
    def action_mark_resolved(self, request: HttpRequest, queryset):
        """
        حلّ النزاع: يضبط resolved_by/at + status='resolved'.
        يفك التجميد إن أمكن (لكن لا يُغيّر حالة الطلب إن كان هناك نزاعات أخرى مفتوحة).
        """
        user = request.user
        now = timezone.now()
        changed = 0
        for d in queryset.select_for_update().select_related("request"):
            try:
                d.status = "resolved"
                if hasattr(d, "resolved_by"):
                    d.resolved_by = user
                if hasattr(d, "resolved_at"):
                    d.resolved_at = now
                d.save(update_fields=[f for f in ("status", "resolved_by", "resolved_at") if hasattr(d, f)])

                # إذا لم تعد هناك نزاعات مفتوحة لنفس الطلب، نفك تجميده
                req = getattr(d, "request", None)
                if req:
                    open_exists = Dispute.objects.filter(request=req, status__in=("open", "pending")).exists()
                    if not open_exists:
                        self._set_request_freeze(req, False)
                changed += 1
            except Exception:
                transaction.set_rollback(True)
                messages.error(request, f"تعذّر حلّ النزاع #{getattr(d, 'id', '—')}.")
        if changed:
            messages.success(request, f"تم حلّ {changed} نزاع/نزاعات.")
    action_mark_resolved.short_description = "وَسْم المحدّد كمحلول"

    @transaction.atomic
    def action_mark_closed(self, request: HttpRequest, queryset):
        """
        إغلاق النزاع إداريًا: status='closed' + resolved_by/at.
        لا يغيّر القرار المالي إلا إن كانت السياسة تقضي بذلك لاحقًا.
        """
        user = request.user
        now = timezone.now()
        changed = 0
        for d in queryset.select_for_update().select_related("request"):
            try:
                d.status = "closed"
                if hasattr(d, "resolved_by"):
                    d.resolved_by = user
                if hasattr(d, "resolved_at"):
                    d.resolved_at = now
                d.save(update_fields=[f for f in ("status", "resolved_by", "resolved_at") if hasattr(d, f)])
                changed += 1
            except Exception:
                transaction.set_rollback(True)
                messages.error(request, f"تعذّر إغلاق النزاع #{getattr(d, 'id', '—')}.")
        if changed:
            messages.success(request, f"تم إغلاق {changed} نزاع/نزاعات.")
    action_mark_closed.short_description = "إغلاق النزاع (إداريًا)"

    @transaction.atomic
    def action_reopen(self, request: HttpRequest, queryset):
        """
        إعادة فتح نزاع مغلق/محلول: status='open' + تفريغ resolved_by/at.
        يفعِّل تجميد الطلب من جديد.
        """
        now = timezone.now()
        changed = 0
        for d in queryset.select_for_update().select_related("request"):
            try:
                d.status = "open"
                if hasattr(d, "resolved_by"):
                    d.resolved_by = None
                if hasattr(d, "resolved_at"):
                    d.resolved_at = None
                d.save(update_fields=[f for f in ("status", "resolved_by", "resolved_at") if hasattr(d, f)])

                req = getattr(d, "request", None)
                if req:
                    self._set_request_freeze(req, True)
                    self._maybe_touch_disputed_status(req)
                changed += 1
            except Exception:
                transaction.set_rollback(True)
                messages.error(request, f"تعذّر إعادة فتح النزاع #{getattr(d, 'id', '—')}.")
        if changed:
            messages.success(request, f"تمت إعادة فتح {changed} نزاع/نزاعات.")
    action_reopen.short_description = "إعادة فتح النزاع"

    @transaction.atomic
    def action_freeze_request(self, request: HttpRequest, queryset):
        """
        تجميد الطلبات المرتبطة بالنزاعات المحددة (Finance Hold).
        """
        changed = 0
        for d in queryset.select_related("request"):
            req = getattr(d, "request", None)
            if not req:
                continue
            try:
                if self._set_request_freeze(req, True):
                    self._maybe_touch_disputed_status(req)
                    changed += 1
            except Exception:
                transaction.set_rollback(True)
                messages.error(request, f"تعذّر تجميد طلب النزاع #{getattr(d, 'id', '—')}.")
        if changed:
            messages.success(request, f"تم تجميد {changed} طلب/طلبات.")
    action_freeze_request.short_description = "تجميد الطلب المرتبط"

    @transaction.atomic
    def action_unfreeze_request(self, request: HttpRequest, queryset):
        """
        فكّ تجميد الطلبات المرتبطة (إن لم توجد نزاعات مفتوحة أخرى على نفس الطلب).
        """
        changed = 0
        for d in queryset.select_related("request"):
            req = getattr(d, "request", None)
            if not req:
                continue
            try:
                open_exists = Dispute.objects.filter(request=req, status__in=("open", "pending")).exists()
                if open_exists:
                    messages.warning(
                        request,
                        f"لا يمكن فك التجميد لطلب النزاع #{getattr(d, 'id', '—')} لوجود نزاعات مفتوحة أخرى.",
                    )
                    continue
                if self._set_request_freeze(req, False):
                    changed += 1
            except Exception:
                transaction.set_rollback(True)
                messages.error(request, f"تعذّر فكّ التجميد لطلب النزاع #{getattr(d, 'id', '—')}.")
        if changed:
            messages.success(request, f"تم فكّ تجميد {changed} طلب/طلبات.")
    action_unfreeze_request.short_description = "فكّ تجميد الطلب المرتبط"
