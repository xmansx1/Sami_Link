# marketplace/views.py
from __future__ import annotations
from datetime import timedelta

from core.permissions import require_role
from django.utils import timezone
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.core.exceptions import PermissionDenied
from django.core.mail import send_mail
from django.db import IntegrityError, transaction
from django.db.models import Q, Prefetch
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST
from django.views.generic import CreateView, DetailView, ListView

from notifications.utils import create_notification
from .forms import RequestCreateForm, OfferCreateForm, OfferForm, AdminReassignForm
from .models import Request, Offer, Note


# ======================
# Mixins للصلاحيات
# ======================
class ClientOnlyMixin(UserPassesTestMixin):
    def test_func(self):
        u = self.request.user
        return u.is_authenticated and getattr(u, "role", None) == "client"


class EmployeeOnlyMixin(UserPassesTestMixin):
    def test_func(self):
        u = self.request.user
        return u.is_authenticated and getattr(u, "role", None) == "employee"


# ======================
# أدوات إشعار
# ======================
def _send_email_safely(subject: str, body: str, to_email: str | None):
    try:
        if getattr(settings, "DEFAULT_FROM_EMAIL", None) and to_email:
            send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [to_email], fail_silently=True)
    except Exception:
        pass


def _notify(recipient, title: str, body: str = ""):
    try:
        create_notification(recipient=recipient, title=title, body=body, url="")
    except Exception:
        pass
    _send_email_safely(title, body, getattr(recipient, "email", None))


def _notify_link(recipient, title: str, body: str = "", url: str = "", actor=None, target=None):
    try:
        create_notification(recipient=recipient, title=title, body=body, url=url or "", actor=actor, target=target)
    except Exception:
        pass
    _send_email_safely(title, body, getattr(recipient, "email", None))


def _notify_new_offer(off: Offer):
    _notify_link(
        recipient=off.request.client,
        title="عرض جديد على طلبك",
        body=f"قدّم {off.employee} عرضًا بقيمة {off.proposed_price} لمدة {off.proposed_duration_days} يوم.",
        url=reverse("marketplace:request_detail", args=[off.request_id]),
        actor=off.employee,
        target=off.request,
    )


def _notify_offer_selected(off: Offer):
    _notify_link(
        recipient=off.employee,
        title="تم اختيار عرضك",
        body=f"تم اختيار عرضك لطلب [{off.request_id}] {off.request.title}.",
        url=reverse("marketplace:request_detail", args=[off.request_id]),
        actor=off.request.client,
        target=off.request,
    )


# ======================
# صلاحيات وأدوات مساعدة
# ======================
def _is_admin(u) -> bool:
    return u.is_authenticated and (getattr(u, "role", None) == "admin" or getattr(u, "is_staff", False))


def _can_manage_request(user, req) -> bool:
    if not user.is_authenticated:
        return False
    if _is_admin(user):
        return True
    return getattr(req, "assigned_employee_id", None) == user.id


def _can_open_dispute(user, req) -> tuple[bool, str]:
    if not user.is_authenticated:
        return False, "anonymous"
    if user.id == getattr(req, "client_id", None):
        return True, "client"
    if getattr(req, "assigned_employee_id", None) == user.id:
        return True, "employee"
    if _is_admin(user):
        return True, "admin"
    return False, "forbidden"


def _status_field_name(req) -> str | None:
    if hasattr(req, "status"):
        return "status"
    if hasattr(req, "state"):
        return "state"
    return None


def _status_vals(*names):
    """
    يعيد قائمة بالقيم الصحيحة للحالات المطلوبة سواء لديك Enum Request.Status
    أو كانت الحالة نصية (lowercase).
    """
    out: list[str] = []
    for n in names:
        if hasattr(Request, "Status") and hasattr(Request.Status, n):
            out.append(getattr(Request.Status, n))
        else:
            out.append(n.lower())
    return out


def _fallback_after_forbidden(user):
    """توجيه مناسب حسب الدور عند نقص الصلاحية."""
    try:
        role = getattr(user, "role", "") or ""
        if role == "employee":
            return reverse("marketplace:my_tasks")
        if role == "client":
            return reverse("marketplace:my_requests")
        return reverse("marketplace:request_list")
    except Exception:
        return "/"


# نافذة العروض: 5 أيام افتراضيًا (قابلة للتهيئة في settings.OFFERS_WINDOW_DAYS)
OFFERS_WINDOW_DAYS = int(getattr(settings, "OFFERS_WINDOW_DAYS", 5))


def _in_offers_window(req: Request) -> bool:
    """
    يتحقق إن كان الطلب ما زال ضمن نافذة استقبال العروض.
    أولًا يستخدم حقل offers_window_ends_at إن وُجد، وإلا يُحسب من created_at.
    """
    now = timezone.now()
    # حقل صريح إن وُجد
    end = getattr(req, "offers_window_ends_at", None)
    if end:
        try:
            return now <= end
        except Exception:
            pass
    # حساب من created_at
    created_at = getattr(req, "created_at", None)
    if created_at:
        try:
            return now <= (created_at + timedelta(days=OFFERS_WINDOW_DAYS))
        except Exception:
            return False
    return False


def _mask_value(_: str) -> str:
    return "— مخفي أثناء فترة العروض —"


# ======================
# قوائم الطلبات
# ======================
class RequestListView(LoginRequiredMixin, ListView):
    template_name = "marketplace/request_list.html"
    context_object_name = "items"
    paginate_by = 20

    def get_queryset(self):
        user = self.request.user
        qs = (
            Request.objects
            .select_related("client", "assigned_employee")
            .prefetch_related(Prefetch("offers", queryset=Offer.objects.only("id", "status", "employee_id")))
        )
        status = (self.request.GET.get("status") or "").strip()
        q = (self.request.GET.get("q") or "").strip()

        if not _is_admin(user):
            role = getattr(user, "role", None)
            if role == "client":
                qs = qs.filter(client=user)
            elif role == "employee":
                qs = qs.filter(Q(assigned_employee=user))
            else:
                qs = qs.none()

        if status:
            qs = qs.filter(Q(status=status) | Q(state=status))

        if q:
            qs = qs.filter(
                Q(title__icontains=q) |
                Q(details__icontains=q) |
                Q(client__name__icontains=q)
            )

        return qs.order_by("-updated_at", "-id")


class MyAssignedRequestsView(LoginRequiredMixin, ListView):
    """
    الطلبات المعيّنة لي (للموظف). المدير/الستاف يشوفون الكل للمتابعة.
    """
    template_name = "marketplace/my_assigned.html"
    context_object_name = "items"
    paginate_by = 20

    def get_queryset(self):
        user = self.request.user
        qs = (
            Request.objects
            .select_related("client", "assigned_employee")
            .prefetch_related(Prefetch("offers", queryset=Offer.objects.only("id", "status", "employee_id")))
        )
        status = (self.request.GET.get("status") or "").strip()
        q = (self.request.GET.get("q") or "").strip()

        if not _is_admin(user):
            qs = qs.filter(assigned_employee=user)

        if status:
            qs = qs.filter(Q(status=status) | Q(state=status))

        if q:
            qs = qs.filter(
                Q(title__icontains=q) |
                Q(details__icontains=q) |
                Q(client__name__icontains=q)
            )

        # إخفاء المكتملة/الملغاة/المغلقة نهائيًا من “مهامي”
        done_like = _status_vals("COMPLETED", "CANCELED", "CLOSED")
        qs = qs.exclude(status__in=done_like)

        # إخفاء النزاعات (المجمّدة) إن رغبت
        if hasattr(Request, "Status") and hasattr(Request.Status, "DISPUTED"):
            qs = qs.exclude(status=Request.Status.DISPUTED)
        else:
            qs = qs.exclude(status="disputed")

        if hasattr(Request, "is_frozen"):
            qs = qs.filter(Q(is_frozen=False) | Q(is_frozen__isnull=True))

        return qs.order_by("-updated_at", "-id")


# ======================
# إنشاء الطلب + “طلباتي”
# ======================
class RequestCreateView(LoginRequiredMixin, ClientOnlyMixin, CreateView):
    template_name = "marketplace/request_create.html"
    model = Request
    form_class = RequestCreateForm

    def form_valid(self, form):
        form.instance.client = self.request.user
        self.object = form.save()
        messages.success(self.request, "تم إنشاء الطلب بنجاح.")
        try:
            _notify_link(
                recipient=self.request.user,
                title="تم إنشاء طلبك",
                body=f"تم إنشاء الطلب #{self.object.pk}: {self.object.title}",
                url=reverse("marketplace:request_detail", args=[self.object.pk]),
                actor=self.request.user,
                target=self.object,
            )
        except Exception:
            pass
        return redirect("marketplace:request_detail", pk=self.object.pk)

    def form_invalid(self, form):
        messages.error(self.request, "لم يتم إنشاء الطلب. الرجاء تصحيح الأخطاء.")
        return super().form_invalid(form)


class MyRequestsListView(LoginRequiredMixin, ClientOnlyMixin, ListView):
    template_name = "marketplace/my_requests.html"
    context_object_name = "requests"
    paginate_by = 10

    def get_queryset(self):
        return (
            Request.objects
            .filter(client=self.request.user)
            .select_related("client", "assigned_employee")
            .order_by("-created_at")
        )


class NewRequestsForEmployeesView(LoginRequiredMixin, EmployeeOnlyMixin, ListView):
    template_name = "marketplace/new_requests.html"
    context_object_name = "requests"
    paginate_by = 10

    def get_queryset(self):
        return (
            Request.objects
            .filter(status=Request.Status.NEW, assigned_employee__isnull=True)
            .select_related("client")
            .prefetch_related(Prefetch("offers", queryset=Offer.objects.only("id", "status", "employee_id")))
            .order_by("-created_at")
        )

    def get_context_data(self, **kwargs):
        """
        نمرّر offered_request_ids إلى القالب حتى يعمل شرط زر “تقديم عرض” بدون TemplateSyntaxError.
        """
        ctx = super().get_context_data(**kwargs)
        u = self.request.user
        offered_ids = list(
            Offer.objects
            .filter(employee=u, status=Offer.Status.PENDING)
            .values_list("request_id", flat=True)
        )
        ctx["offered_request_ids"] = offered_ids
        return ctx


# ======================
# تفاصيل الطلب + تقديم عرض داخل الصفحة
# ======================
class RequestDetailView(LoginRequiredMixin, DetailView):
    model = Request
    template_name = "marketplace/request_detail.html"
    context_object_name = "req"

    def get_queryset(self):
        return (
            Request.objects
            .select_related("client", "assigned_employee")
            .prefetch_related(
                Prefetch("offers", queryset=Offer.objects.select_related("employee")),
                Prefetch("notes", queryset=Note.objects.select_related("author")),
            )
        )

    def _is_admin_like(self, u) -> bool:
        role = getattr(u, "role", "") or ""
        return bool(u.is_superuser or u.is_staff or role in {"admin", "manager", "gm", "finance"})

    def _can_view(self, u, req: Request) -> bool:
        if u.id in {req.client_id, getattr(req, "assigned_employee_id", None)}:
            return True
        if self._is_admin_like(u):
            return True
        return False

    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        if not self._can_view(request.user, self.object):
            messages.error(request, "ليس لديك صلاحية لعرض هذا الطلب.")
            return redirect(_fallback_after_forbidden(request.user))
        context = self.get_context_data(object=self.object)
        return self.render_to_response(context)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        req: Request = ctx["req"]
        u = self.request.user

        # ------------ إخفاء بيانات العميل أثناء نافذة العروض ------------
        current_status = (getattr(req, "status", "") or getattr(req, "state", "") or "").lower()
        new_like = set(_status_vals("NEW", "OPEN", "OFFERING"))
        has_selected_offer = bool(getattr(req, "selected_offer", None))
        has_agreement = bool(getattr(req, "agreement", None))
        is_employee = getattr(u, "role", None) == "employee"
        is_assigned = getattr(req, "assigned_employee_id", None) == getattr(u, "id", None)

        should_redact = (
            is_employee
            and not is_assigned
            and current_status in new_like
            and not has_selected_offer
            and not has_agreement
            and _in_offers_window(req)
        )

        # تمهيد قيم آمنة للعرض في القالب (لا نمرّر بيانات حساسة إن كان should_redact=True)
        client_obj = getattr(req, "client", None)
        client_name = getattr(getattr(client_obj, "profile", None), "name", None) or getattr(client_obj, "name", "")
        client_email = getattr(client_obj, "email", "")
        client_phone = getattr(client_obj, "phone", "")
        client_address = getattr(client_obj, "address", "")

        ctx["REDACT_CONTACTS"] = should_redact
        ctx["client_name_safe"] = _mask_value(client_name) if should_redact else client_name
        ctx["client_email_safe"] = _mask_value(client_email) if should_redact else client_email
        ctx["client_phone_safe"] = _mask_value(client_phone) if should_redact else client_phone
        ctx["client_address_safe"] = _mask_value(client_address) if should_redact else client_address

        # ------------ تقديم عرض (موظف فقط وعلى NEW وغير مُسنَّد) ------------
        ctx["can_offer"] = False
        ctx["my_offer"] = None
        ctx["offer_form"] = None
        if (
            u.is_authenticated
            and getattr(u, "role", None) == "employee"
            and req.status == Request.Status.NEW
            and req.assigned_employee_id is None
        ):
            my_offer = req.offers.filter(employee=u, status=Offer.Status.PENDING).first()
            ctx["my_offer"] = my_offer
            ctx["can_offer"] = my_offer is None
            if my_offer is None:
                ctx["offer_form"] = OfferCreateForm()

        # إنشاء/فتح الاتفاقية (بعد اختيار العرض)
        ctx["can_create_agreement"] = False
        if u.is_authenticated and getattr(u, "role", None) == "employee":
            selected = getattr(req, "selected_offer", None)
            if selected and (req.assigned_employee_id == u.id or selected.employee_id == u.id):
                if req.status == Request.Status.OFFER_SELECTED or hasattr(req, "agreement"):
                    ctx["can_create_agreement"] = True

        # نزاع/تغيير حالة
        ok_dispute, _role = _can_open_dispute(u, req)
        ctx["can_open_dispute"] = ok_dispute
        ctx["can_change_state"] = _can_manage_request(u, req)
        ctx["allowed_state_actions"] = {
            "to_awaiting_review": True,
            "to_in_progress": True,
            "to_completed": True,
            "cancel": True,
        }
        return ctx

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        req: Request = self.object
        u = request.user

        if not self._can_view(u, req):
            messages.error(request, "ليس لديك صلاحية لتنفيذ هذا الإجراء.")
            return redirect(_fallback_after_forbidden(u))

        if not (u.is_authenticated and getattr(u, "role", None) == "employee"):
            messages.error(request, "غير مصرح بتقديم عرض على هذا الطلب.")
            return redirect("marketplace:request_detail", pk=req.pk)

        if req.status != Request.Status.NEW or req.assigned_employee_id is not None:
            messages.warning(request, "لا يمكن تقديم عروض لهذا الطلب في حالته الحالية.")
            return redirect("marketplace:request_detail", pk=req.pk)

        if req.offers.filter(employee=u, status=Offer.Status.PENDING).exists():
            messages.info(request, "قدّمت عرضًا مسبقًا لهذا الطلب.")
            return redirect("marketplace:request_detail", pk=req.pk)

        form = OfferCreateForm(request.POST or None)
        form.instance.request = req
        form.instance.employee = u

        if not form.is_valid():
            messages.error(request, "لم يتم إرسال العرض. الرجاء تصحيح الأخطاء.")
            context = self.get_context_data(object=req)
            context["offer_form"] = form
            return self.render_to_response(context)

        try:
            form.save()
        except IntegrityError:
            messages.warning(request, "لديك عرض مسبق لهذا الطلب.")
            return redirect("marketplace:request_detail", pk=req.pk)

        # إشعار العميل
        try:
            off = req.offers.filter(employee=u).order_by("-id").first()
            if off:
                _notify_new_offer(off)
            else:
                _notify_link(
                    recipient=req.client,
                    title="عرض جديد على طلبك",
                    body=f"قدّم {u} عرضًا على طلبك #{req.pk}.",
                    url=reverse("marketplace:request_detail", args=[req.pk]),
                    actor=u,
                    target=req,
                )
        except Exception:
            pass

        messages.success(request, "تم تقديم العرض بنجاح.")
        return redirect("marketplace:request_detail", pk=req.pk)


# ======================
# العروض: إنشاء/اختيار/رفض
# ======================
class OfferCreateView(LoginRequiredMixin, EmployeeOnlyMixin, CreateView):
    template_name = "marketplace/offer_create.html"
    model = Offer
    form_class = OfferCreateForm

    def dispatch(self, request, *args, **kwargs):
        self.req_obj = get_object_or_404(
            Request.objects.select_related("client"),
            pk=kwargs.get("request_id"),
            status=Request.Status.NEW,
            assigned_employee__isnull=True,
        )
        if Offer.objects.filter(request=self.req_obj, employee=request.user, status=Offer.Status.PENDING).exists():
            messages.warning(request, "قدّمت عرضًا مسبقًا لهذا الطلب.")
            return redirect("marketplace:request_detail", pk=self.req_obj.pk)
        return super().dispatch(request, *args, **kwargs)

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        form.instance.request = self.req_obj
        form.instance.employee = self.request.user
        return form

    def form_valid(self, form):
        try:
            response = super().form_valid(form)
            messages.success(self.request, "تم تقديم العرض.")
            try:
                _notify_new_offer(self.object)
            except Exception:
                pass
            return response
        except IntegrityError:
            messages.warning(self.request, "لديك عرض مسبق لهذا الطلب.")
            return redirect("marketplace:request_detail", pk=self.req_obj.pk)

    def get_success_url(self):
        return reverse("marketplace:request_detail", args=[self.req_obj.pk])


@require_role("client")
@transaction.atomic
def offer_select(request, offer_id):
    # اختيار العرض من العميل أو الإدارة (الديكوريتر يسمح للستاف/admin أيضًا)
    off = get_object_or_404(
        Offer.objects.select_related("request").select_for_update(),
        pk=offer_id
    )
    req = off.request

    # تحقق صاحب الطلب
    if req.client != request.user and not getattr(request.user, "is_staff", False):
        return HttpResponseForbidden("غير مسموح")

    # منع الاختيار أثناء النزاع
    if getattr(req, "is_frozen", False) or str(getattr(req, "status", "")).lower() == "disputed":
        messages.error(request, "لا يمكن اختيار عرض: الطلب في حالة نزاع.")
        return redirect("marketplace:request_detail", pk=req.pk)

    # صلاحية/وضع العرض
    if hasattr(off, "can_select") and not off.can_select(request.user):
        return HttpResponseForbidden("لا يمكن اختيار هذا العرض")
    if getattr(off, "status", None) != getattr(Offer.Status, "PENDING", "pending"):
        messages.info(request, "لا يمكن اختيار عرض غير معلّق.")
        return redirect("marketplace:request_detail", pk=req.pk)

    # ارفض بقية العروض
    Offer.objects.filter(request=req).exclude(pk=off.pk).update(status=getattr(Offer.Status, "REJECTED", "rejected"))

    # اختر العرض
    off.status = getattr(Offer.Status, "SELECTED", "selected")
    off.save(update_fields=["status"])

    # إسناد الطلب وتحديث حالته
    req.assigned_employee = off.employee
    req.status = getattr(Request.Status, "OFFER_SELECTED", "offer_selected")
    update_fields = ["assigned_employee", "status"]
    if hasattr(req, "updated_at"):
        req.updated_at = timezone.now()
        update_fields.append("updated_at")
    req.save(update_fields=update_fields)

    try:
        _notify_offer_selected(off)
    except Exception:
        pass

    messages.success(request, "تم اختيار العرض وإسناد الطلب")
    return redirect("marketplace:request_detail", pk=req.pk)


@login_required
@transaction.atomic
def offer_reject(request, offer_id):
    if request.method != "POST":
        return HttpResponseForbidden("غير مسموح")
    off = get_object_or_404(Offer.objects.select_related("request"), pk=offer_id)
    if not off.can_reject(request.user):
        return HttpResponseForbidden("غير مسموح")

    off.status = Offer.Status.REJECTED
    off.save(update_fields=["status"])
    try:
        _notify_link(
            recipient=off.employee,
            title="تم رفض عرضك",
            body=f"تم رفض عرضك على الطلب #{off.request_id}.",
            url=reverse("marketplace:offer_detail", args=[off.pk]) if hasattr(off, "get_absolute_url") else "",
            actor=request.user,
            target=off.request,
        )
    except Exception:
        pass

    messages.info(request, "تم رفض العرض.")
    return redirect(off.request.get_absolute_url())


# ======================
# ملاحظات الطلب
# ======================
@login_required
@require_POST
def request_add_note(request, pk: int):
    req = get_object_or_404(Request, pk=pk)

    user = request.user
    role = getattr(user, 'role', None)
    allowed = (
        user.id == req.client_id
        or user.id == getattr(req, 'assigned_employee_id', None)
        or role == 'admin'
        or user.is_staff
    )
    if not allowed:
        messages.error(request, "غير مصرح بإضافة ملاحظة على هذا الطلب.")
        return redirect('marketplace:request_detail', pk=req.id)

    text = (request.POST.get('text') or '').strip()
    if len(text) < 2:
        messages.error(request, "الرجاء إدخال ملاحظة صالحة (على الأقل حرفان).")
        return redirect('marketplace:request_detail', pk=req.id)

    Note.objects.create(request=req, author=user, text=text)
    messages.success(request, "تم حفظ الملاحظة.")

    try:
        url = reverse("marketplace:request_detail", args=[req.pk])
        if user.id == req.client_id:
            target_user = getattr(req, "assigned_employee", None) or (getattr(req, "selected_offer", None) and req.selected_offer.employee)
            if target_user:
                _notify_link(
                    recipient=target_user,
                    title="ملاحظة جديدة على الطلب",
                    body=f"أضاف العميل ملاحظة على الطلب #{req.pk}.",
                    url=url,
                    actor=user,
                    target=req,
                )
        else:
            _notify_link(
                recipient=req.client,
                title="ملاحظة جديدة على طلبك",
                body=f"أضيفت ملاحظة على طلبك #{req.pk}.",
                url=url,
                actor=user,
                target=req,
            )
    except Exception:
        pass

    return redirect('marketplace:request_detail', pk=req.id)


# ======================
# تغيير حالة الطلب + إلغاء
# ======================
@login_required
@require_POST
def request_change_state(request, pk: int):
    req = get_object_or_404(Request, pk=pk)
    user = request.user

    if not _can_manage_request(user, req):
        raise PermissionDenied

    new_state = (request.POST.get("state") or "").strip()
    allowed_states = {"in_progress", "awaiting_review", "awaiting_payment", "completed", "cancelled"}
    if new_state not in allowed_states:
        messages.error(request, "حالة غير مسموح بها.")
        return redirect(req.get_absolute_url())

    current = getattr(req, "status", getattr(req, "state", "")) or ""
    allowed_transitions = {
        "in_progress": {"awaiting_review", "cancelled"},
        "awaiting_review": {"in_progress", "awaiting_payment"},
        "awaiting_payment": {"completed", "cancelled"},
        "completed": set(),
    }

    is_admin_user = _is_admin(user)
    if new_state == "cancelled" and is_admin_user:
        pass
    else:
        if current not in allowed_transitions or new_state not in allowed_transitions[current]:
            messages.error(request, "لا يُسمح بالانتقال المطلوب من الحالة الحالية.")
            return redirect(req.get_absolute_url())

    field = _status_field_name(req)
    if not field:
        messages.error(request, "حقل الحالة غير معرّف على هذا الطلب.")
        return redirect(req.get_absolute_url())

    setattr(req, field, new_state)
    try:
        if hasattr(req, "updated_at"):
            req.save(update_fields=[field, "updated_at"])
        else:
            req.save(update_fields=[field])
        messages.success(request, f"تم تحديث حالة الطلب إلى: {new_state}.")
    except Exception as e:
        messages.error(request, f"تعذر تحديث الحالة: {e}")

    return redirect(req.get_absolute_url())


@login_required
@require_POST
def request_cancel(request, pk: int):
    req = get_object_or_404(Request, pk=pk)
    user = request.user

    is_admin_user = _is_admin(user)
    is_assigned = getattr(req, "assigned_employee_id", None) == user.id
    if not (is_admin_user or is_assigned):
        raise PermissionDenied

    reason = (request.POST.get("reason") or "").strip()
    if len(reason) < 3:
        messages.error(request, "سبب الإلغاء قصير جدًا.")
        return redirect(req.get_absolute_url())

    field = _status_field_name(req)
    if not field:
        messages.error(request, "حقل الحالة غير معرّف على هذا الطلب.")
        return redirect(req.get_absolute_url())

    setattr(req, field, "cancelled")
    try:
        if hasattr(req, "updated_at"):
            req.save(update_fields=[field, "updated_at"])
        else:
            req.save(update_fields=[field])
        messages.warning(request, f"تم إلغاء الطلب. السبب: {reason}")
    except Exception as e:
        messages.error(request, f"تعذر إلغاء الطلب: {e}")

    return redirect(req.get_absolute_url())


# ======================
# “مهامي” + “قيد النزاع”
# ======================
class MyTasksView(LoginRequiredMixin, ListView):
    model = Request
    template_name = "marketplace/my_tasks.html"
    context_object_name = "requests"
    paginate_by = 20

    def get_queryset(self):
        u = self.request.user
        qs = (
            Request.objects
            .select_related("client", "assigned_employee")
            .filter(assigned_employee=u)
        )

        done_like = _status_vals("COMPLETED", "CANCELED", "CLOSED")
        qs = qs.exclude(status__in=done_like)

        if hasattr(Request, "Status") and hasattr(Request.Status, "DISPUTED"):
            qs = qs.exclude(status=Request.Status.DISPUTED)
        else:
            qs = qs.exclude(status="disputed")

        if hasattr(Request, "is_frozen"):
            qs = qs.filter(Q(is_frozen=False) | Q(is_frozen__isnull=True))

        return qs.order_by("-updated_at", "-id")


@login_required
def my_tasks(request):
    u = request.user
    qs = (
        Request.objects
        .select_related("client", "assigned_employee")
        .filter(assigned_employee=u)
    )

    done_like = _status_vals("COMPLETED", "CANCELED", "CLOSED")
    qs = qs.exclude(status__in=done_like)

    if hasattr(Request, "Status") and hasattr(Request.Status, "DISPUTED"):
        qs = qs.exclude(status=Request.Status.DISPUTED)
    else:
        qs = qs.exclude(status="disputed")

    if hasattr(Request, "is_frozen"):
        qs = qs.filter(Q(is_frozen=False) | Q(is_frozen__isnull=True))

    qs = qs.order_by("-updated_at", "-id")
    return render(request, "marketplace/my_tasks.html", {"requests": qs})


@login_required
def disputed_tasks(request):
    u = request.user

    disputed_q = Q(status="disputed")
    if hasattr(Request, "Status") and hasattr(Request.Status, "DISPUTED"):
        disputed_q = Q(status=Request.Status.DISPUTED) | Q(status__iexact="disputed")

    qs = (
        Request.objects
        .select_related("client", "assigned_employee")
        .filter(assigned_employee=u)
        .filter(disputed_q)
    )

    if hasattr(Request, "is_frozen"):
        qs = qs.filter(is_frozen=True)

    qs = qs.order_by("-updated_at", "-id")
    return render(request, "marketplace/disputed_tasks.html", {"requests": qs})


# ======================
# إجراءات المدير
# ======================
@login_required
@user_passes_test(_is_admin)
@require_POST
def admin_request_reset_to_new(request, pk: int):
    req = get_object_or_404(Request, pk=pk)
    old_assignee = getattr(req, "assigned_employee", None)
    try:
        req.reset_to_new()
        messages.success(request, "تمت إعادة الطلب كجديد بنجاح، وأصبحت العروض السابقة مرفوضة للأرشفة.")
        try:
            url = reverse("marketplace:request_detail", args=[pk])
            _notify_link(
                recipient=req.client,
                title="أُعيد طلبك كجديد",
                body=f"تمت إعادة الطلب #{pk} كجديد وتمت أرشفة العروض السابقة.",
                url=url,
                actor=request.user,
                target=req,
            )
            if old_assignee:
                _notify_link(
                    recipient=old_assignee,
                    title="إلغاء إسناد طلب",
                    body=f"تم إلغاء إسناد الطلب #{pk} بعد إعادته كجديد.",
                    url=url,
                    actor=request.user,
                    target=req,
                )
        except Exception:
            pass
    except Exception as e:
        messages.error(request, f"تعذّر إعادة الطلب: {e}")
    return redirect(req.get_absolute_url())


@login_required
@user_passes_test(_is_admin)
@require_POST
def admin_request_delete(request, pk: int):
    req = get_object_or_404(Request, pk=pk)
    title = f"[{req.pk}] {req.title}"
    client = req.client
    old_assignee = getattr(req, "assigned_employee", None)
    try:
        req.delete()
        messages.success(request, f"تم شطب الطلب نهائيًا: {title}")
        try:
            _notify(client, "تم شطب طلبك", f"تم شطب الطلب {title} نهائيًا.")
            if old_assignee:
                _notify(old_assignee, "تم شطب طلب مُسند", f"تم شطب الطلب {title} الذي كان مُسندًا إليك.")
        except Exception:
            pass
        return redirect("marketplace:request_list")
    except Exception as e:
        messages.error(request, f"تعذّر الحذف: {e}")
        return redirect(req.get_absolute_url())


@login_required
@user_passes_test(_is_admin)
def admin_request_reassign(request, pk: int):
    req = get_object_or_404(Request, pk=pk)
    if request.method == "POST":
        form = AdminReassignForm(request.POST)
        if form.is_valid():
            employee = form.cleaned_data["employee"]
            old_assignee = getattr(req, "assigned_employee", None)
            try:
                req.reassign_to(employee)
                messages.success(request, f"تمت إعادة إسناد الطلب إلى: {employee}")
                try:
                    url = reverse("marketplace:request_detail", args=[pk])
                    _notify_link(
                        recipient=employee,
                        title="أُسند إليك طلب",
                        body=f"تم إسناد الطلب #{pk}: {req.title}",
                        url=url,
                        actor=request.user,
                        target=req,
                    )
                    _notify_link(
                        recipient=req.client,
                        title="تحديث على طلبك",
                        body=f"تم إسناد طلبك #{pk} إلى {employee}.",
                        url=url,
                        actor=request.user,
                        target=req,
                    )
                    if old_assignee and old_assignee.id != employee.id:
                        _notify_link(
                            recipient=old_assignee,
                            title="إلغاء إسناد طلب",
                            body=f"تم سحب الطلب #{pk} من إسنادك وإسناده إلى {employee}.",
                            url=url,
                            actor=request.user,
                            target=req,
                        )
                except Exception:
                    pass
                return redirect(req.get_absolute_url())
            except Exception as e:
                messages.error(request, f"فشل إعادة الإسناد: {e}")
    else:
        form = AdminReassignForm()
    return render(request, "marketplace/admin_reassign.html", {"req": req, "form": form})


@login_required
def offer_detail(request, offer_id):
    off = get_object_or_404(
        Offer.objects.select_related("request", "employee", "request__client"),
        pk=offer_id,
    )
    # احترم منطق الصلاحيات إن كان موجودًا في الموديل
    if hasattr(off, "can_view"):
        if not off.can_view(request.user):
            return HttpResponseForbidden("غير مسموح")
    else:
        # بديل آمن: المالك/الموظف المُسنَد/ستاف
        u = request.user
        allowed = (
            getattr(off.request, "client_id", None) == u.id
            or getattr(off, "employee_id", None) == u.id
            or u.is_staff
            or getattr(u, "role", "") == "admin"
        )
        if not allowed:
            return HttpResponseForbidden("غير مسموح")

    return render(request, "marketplace/offer_detail.html", {"off": off, "req": off.request})
