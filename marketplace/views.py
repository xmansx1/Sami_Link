from .forms import OfferCancelForm, OfferExtensionForm, ReviewForm
from .models import Review
from django.contrib.auth.decorators import login_required
# ======================
# إلغاء العرض
# ======================
@login_required
def offer_cancel(request, offer_id):
    offer = get_object_or_404(Offer, pk=offer_id)
    user = request.user
    if not offer.can_cancel(user):
        messages.error(request, "غير مصرح لك بإلغاء هذا العرض.")
        return redirect("marketplace:request_detail", pk=offer.request.pk)
    if request.method == "POST":
        form = OfferCancelForm(request.POST, instance=offer)
        if form.is_valid():
            req = offer.request
            offer.delete()  # حذف العرض فعليًا
            # إعادة الطلب لحالة جديد وإلغاء الإسناد
            from .models import Status
            req.status = Status.NEW
            req.assigned_employee = None
            req.save(update_fields=["status", "assigned_employee"])
            # إشعار العميل
            try:
                create_notification(
                    recipient=req.client,
                    title="تم إلغاء العرض",
                    body=f"قام الموظف {user} بإلغاء عرضه على طلبك #{req.pk} لسبب: {form.cleaned_data.get('modification_reason', '')}",
                    url=reverse("marketplace:request_detail", args=[req.pk]),
                    actor=user,
                    target=req,
                )
            except Exception:
                pass
            messages.success(request, "تم إلغاء العرض بنجاح، وعاد الطلب لحالة جديد.")
            return redirect("marketplace:request_detail", pk=req.pk)
        else:
            messages.error(request, "يرجى تصحيح الأخطاء في النموذج.")
    else:
        form = OfferCancelForm(instance=offer)
    return render(request, "marketplace/offer_cancel.html", {"form": form, "offer": offer})

# ======================
# طلب تمديد العرض
# ======================
@login_required
def offer_extend(request, offer_id):
    offer = get_object_or_404(Offer, pk=offer_id)
    user = request.user
    if not offer.can_extend(user):
        messages.error(request, "غير مصرح لك بطلب تمديد لهذا العرض.")
        return redirect("marketplace:request_detail", pk=offer.request.pk)
    if request.method == "POST":
        form = OfferExtensionForm(request.POST, instance=offer)
        if form.is_valid():
            offer = form.save(commit=False)
            # يمكن وضع حالة خاصة للتمديد إذا رغبت
            offer.save()
            # إشعار العميل
            try:
                create_notification(
                    recipient=offer.request.client,
                    title="طلب تمديد مدة المشروع",
                    body=f"قام الموظف {user} بطلب تمديد مدة المشروع لعدد {offer.extension_requested_days} يوم. السبب: {offer.extension_reason}",
                    url=reverse("marketplace:request_detail", args=[offer.request.pk]),
                    actor=user,
                    target=offer.request,
                )
            except Exception:
                pass
            messages.success(request, "تم إرسال طلب التمديد بنجاح.")
            return redirect("marketplace:request_detail", pk=offer.request.pk)
        else:
            messages.error(request, "يرجى تصحيح الأخطاء في النموذج.")
    else:
        form = OfferExtensionForm(instance=offer)
    return render(request, "marketplace/offer_extend.html", {"form": form, "offer": offer})
from django.contrib.auth.decorators import login_required
from .forms import OfferEditForm
# ======================
# تعديل العرض من الموظف
# ======================
@login_required
def edit_offer(request, offer_id):
    offer = get_object_or_404(Offer, pk=offer_id)
    user = request.user
    # السماح فقط للموظف صاحب العرض أو الإدارة
    if not (user.is_authenticated and (user == offer.employee or getattr(user, "is_staff", False))):
        messages.error(request, "غير مصرح لك بتعديل هذا العرض.")
        return redirect("marketplace:request_detail", pk=offer.request.pk)

    if request.method == "POST":
        form = OfferEditForm(request.POST, instance=offer)
        if form.is_valid():
            offer = form.save(commit=False)
            from .models import Status
            offer.status = Status.WAITING_CLIENT_APPROVAL
            offer.save()
            # إشعار العميل بالتعديل
            try:
                create_notification(
                    recipient=offer.request.client,
                    title="تم تعديل العرض",
                    body=f"قام الموظف {user} بتعديل العرض على طلبك #{offer.request.pk}. يرجى مراجعة التعديلات والموافقة عليها.",
                    url=reverse("marketplace:offer_detail", args=[offer.pk]),
                    actor=user,
                    target=offer.request,
                )
            except Exception:
                pass
            messages.success(request, "تم تعديل العرض بنجاح. بانتظار موافقة العميل.")
            return redirect("marketplace:request_detail", pk=offer.request.pk)
        else:
            messages.error(request, "يرجى تصحيح الأخطاء في النموذج.")
    else:
        form = OfferEditForm(instance=offer)

    return render(request, "marketplace/edit_offer.html", {"form": form, "offer": offer})
from decimal import Decimal, ROUND_HALF_UP

import logging
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.core.exceptions import PermissionDenied, ValidationError
from django.core.mail import send_mail
from django.db import IntegrityError, transaction
from django.db.models import Prefetch, Q
from django.http import HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from django.views.generic import CreateView, DetailView, ListView

from core.permissions import require_role
from finance.models import FinanceSettings, Invoice
from notifications.utils import create_notification
from .forms import AdminReassignForm, OfferCreateForm, OfferForm, RequestCreateForm
from .models import Note, Offer, Request, Status, Comment

logger = logging.getLogger(__name__)

# ======================
# Mixins للصلاحيات
# ======================
class ClientOnlyMixin(UserPassesTestMixin):
    def test_func(self):
        u = self.request.user
        if not u.is_authenticated:
            return False

        # ✅ السماح للإداري (أدمن/مالية/مدير عام/ستاف) بالدخول بدل 403
        role = (getattr(u, "role", "") or "").lower()
        is_admin_like = (
            getattr(u, "is_staff", False)
            or getattr(u, "is_superuser", False)
            or role in {"admin", "manager", "gm", "finance"}
        )
        if is_admin_like:
            return True

        # المستخدم العادي: عميل فقط
        return role == "client"


class EmployeeOnlyMixin(UserPassesTestMixin):
    def test_func(self):
        u = self.request.user
        if not u.is_authenticated:
            return False

        # ✅ السماح للإداري بالدخول بدون 403
        role = (getattr(u, "role", "") or "").lower()
        is_admin_like = (
            getattr(u, "is_staff", False)
            or getattr(u, "is_superuser", False)
            or role in {"admin", "manager", "gm", "finance"}
        )
        if is_admin_like:
            return True

        # المستخدم العادي: موظف فقط
        return role == "employee"


# ======================
# أدوات إشعار وبريد
# ======================
def _send_email_safely(subject: str, body: str, to_email: str | None):
    try:
        if getattr(settings, "DEFAULT_FROM_EMAIL", None) and to_email:
            send_mail(subject, body, settings.DEFAULT_FROM_EMAIL, [to_email], fail_silently=True)
    except Exception:
        # لا نُسقط العملية في حال فشل البريد
        pass

def _normalize_percent(value) -> Decimal:
    """
    يحوّل القيمة إلى نسبة عشرية موحّدة:
    - 10  -> 0.10
    - 15  -> 0.15
    - 0.10 -> 0.10
    """
    if value is None:
        return Decimal("0")
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    if value > 1:
        value = value / Decimal("100")
    return value.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)


def _calculate_financials_from_net(net_amount, platform_fee_percent, vat_percent):
    """
    net_amount = صافي الموظف (proposed_price)
    platform_fee_percent, vat_percent = النِّسب كما هي من الإعدادات (10 أو 0.10)
    """
    net = Decimal(str(net_amount)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    platform_fee_percent = _normalize_percent(platform_fee_percent)
    vat_percent = _normalize_percent(vat_percent)

    # عمولة المنصّة
    platform_fee = (net * platform_fee_percent).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    # المجموع قبل الضريبة
    subtotal = (net + platform_fee).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    # الضريبة على المجموع
    vat_amount = (subtotal * vat_percent).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    # الإجمالي الذي يدفعه العميل
    client_total = (subtotal + vat_amount).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    return net, platform_fee, vat_amount, client_total


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


# --- helpers: admin check ---
def _is_admin(user) -> bool:
    """
    صلاحيات المدير: is_staff أو role ∈ {admin/manager/gm/finance} أو superuser.
    """
    if not getattr(user, "is_authenticated", False):
        return False
    role = (getattr(user, "role", "") or "").lower()
    return bool(
        getattr(user, "is_staff", False)
        or getattr(user, "is_superuser", False)
        or role in {"admin", "manager", "gm", "finance"}
    )


# ======================
# صلاحيات وأدوات مساعدة
# ======================
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
    from .models import Status
    for n in names:
        if hasattr(Status, n):
            out.append(getattr(Status, n))
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
    end = getattr(req, "offers_window_ends_at", None)
    if end:
        try:
            return now <= end
        except Exception:
            return False
    created_at = getattr(req, "created_at", None)
    if created_at:
        try:
            return now <= (created_at + timedelta(days=OFFERS_WINDOW_DAYS))
        except Exception:
            return False
    return False


def _mask_value(_: str) -> str:
    return "— مخفي أثناء فترة العروض —"


def _is_new_unassigned(req: Request) -> bool:
    """
    يتحقّق أن الطلب في حالة استقبال عروض (NEW/OPEN/OFFERING) وغير مُسنّد،
    وذلك كي نسمح للموظفين بمشاهدة التفاصيل مع إخفاء معلومات العميل.
    """
    status_val = (getattr(req, "status", "") or getattr(req, "state", "") or "").lower()
    new_like = set(_status_vals("NEW", "OPEN", "OFFERING"))
    return (status_val in new_like) and getattr(req, "assigned_employee_id", None) is None


def _notify_new_offer(off: Offer):
    """
    إشعار العميل بعرض جديد مع توضيح:
    - صافي الموظف (proposed_price)
    - رسوم المنصّة المتوقعة
    - الضريبة
    - المبلغ الإجمالي المتوقع على العميل
    """
    try:
        # جلب النِّسب الحالية من إعدادات المالية
        try:
            fee_percent, vat_percent = FinanceSettings.current_rates()
        except Exception:
            fee_percent, vat_percent = (Decimal("0"), Decimal("0"))

        net, platform_fee, vat_amount, client_total = _calculate_financials_from_net(
            net_amount=off.proposed_price,
            platform_fee_percent=fee_percent,
            vat_percent=vat_percent,
        )

        body_lines = [
            f"قدّم {off.employee} عرضًا على طلبك #{off.request_id} - {off.request.title}.",
            "",
            f"• صافي حصة الموظف (بدون رسوم/ضريبة): {net} ريال",
            f"• رسوم المنصّة المتوقعة: {platform_fee} ريال",
            f"• ضريبة القيمة المضافة: {vat_amount} ريال",
            f"• المبلغ الإجمالي المتوقع عليك: {client_total} ريال",
        ]
        if getattr(off, "proposed_duration_days", None):
            body_lines.append(f"• مدة التنفيذ المقترحة: {off.proposed_duration_days} يوم")

        body = "\n".join(body_lines)

        _notify_link(
            recipient=off.request.client,
            title="عرض جديد على طلبك",
            body=body,
            url=reverse("marketplace:request_detail", args=[off.request_id]),
            actor=off.employee,
            target=off.request,
        )
    except Exception:
        # لو صار أي خطأ في الحساب، نرجع للإشعار البسيط القديم
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
# قوائم الطلبات
# ======================
class RequestListView(LoginRequiredMixin, ListView):
    template_name = "marketplace/request_list.html"
    context_object_name = "items"
    paginate_by = 20

    def get_queryset(self):
        user = self.request.user
        qs = (
            Request.objects.select_related("client", "assigned_employee")
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
            qs = qs.filter(Q(title__icontains=q) | Q(details__icontains=q) | Q(client__name__icontains=q))

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
            Request.objects.select_related("client", "assigned_employee")
            .prefetch_related(Prefetch("offers", queryset=Offer.objects.only("id", "status", "employee_id")))
        )
        status = (self.request.GET.get("status") or "").strip()
        q = (self.request.GET.get("q") or "").strip()

        if not _is_admin(user):
            qs = qs.filter(assigned_employee=user)

        if status:
            qs = qs.filter(Q(status=status) | Q(state=status))

        if q:
            qs = qs.filter(Q(title__icontains=q) | Q(details__icontains=q) | Q(client__name__icontains=q))

        # إخفاء المكتملة/الملغاة/المغلقة نهائيًا من “مهامي”
        done_like = _status_vals("COMPLETED", "CANCELED", "CLOSED")
        qs = qs.exclude(status__in=done_like)

        # إخفاء النزاعات (المجمّدة) إن رغبت
        if hasattr(Status, "DISPUTED"):
            qs = qs.exclude(status=Status.DISPUTED)
        else:
            qs = qs.exclude(status="disputed")

        if hasattr(Request, "is_frozen"):
            qs = qs.filter(Q(is_frozen=False) | Q(is_frozen__isnull=True))

        return qs.order_by("-updated_at", "-id")


# ======================
# إنشاء الطلب + “طلباتي”
# ======================
class RequestCreateView(LoginRequiredMixin, CreateView):
    template_name = "marketplace/request_create.html"
    model = Request
    form_class = RequestCreateForm

    def form_valid(self, form):
        form.instance.client = self.request.user
        self.object = form.save()

        # حفظ المرفقات إن وجدت
        files = self.request.FILES.getlist('attachments')
        if files:
            try:
                from uploads.models import RequestFile
                for f in files:
                    RequestFile.objects.create(request=self.object, file=f)
            except Exception:
                pass

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
            Request.objects.filter(client=self.request.user)
            .select_related("client", "assigned_employee")
            .order_by("-created_at")
        )


class NewRequestsForEmployeesView(LoginRequiredMixin, EmployeeOnlyMixin, ListView):
    template_name = "marketplace/new_requests.html"
    context_object_name = "requests"
    paginate_by = 10

    def get_queryset(self):
        return (
            Request.objects.filter(status=Status.NEW, assigned_employee__isnull=True)
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
            Offer.objects.filter(employee=u, status="pending").values_list("request_id", flat=True)
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
        return Request.objects.select_related("client", "assigned_employee").prefetch_related(
            Prefetch("offers", queryset=Offer.objects.select_related("employee")),
            Prefetch("notes", queryset=Note.objects.select_related("author")),
            Prefetch("comments", queryset=Comment.objects.select_related("author")),
        )

    # عرض التفاصيل
    def get(self, request, *args, **kwargs):
        self.object = self.get_object()
        if not self._can_view(request.user, self.object):
            messages.error(request, "ليس لديك صلاحية لعرض هذا الطلب.")
            return redirect(_fallback_after_forbidden(request.user))
        context = self.get_context_data(object=self.object)
        return self.render_to_response(context)

    # صلاحية العرض: العميل، الموظف المسند، الإدارة
    def _can_view(self, user, req) -> bool:
        if not user.is_authenticated:
            return False
        if _is_admin(user):
            return True
        if user.id == getattr(req, "client_id", None):
            return True
        if getattr(req, "assigned_employee_id", None) == user.id:
            return True
        # السماح للموظف برؤية الطلب الجديد غير المُسنَّد لكن مع إخفاء بيانات العميل
        if getattr(user, "role", None) == "employee" and _is_new_unassigned(req):
            return True
        # السماح للموظف الذي لديه عرض (حتى لو لم يُسنّد له الطلب) برؤية الطلب مع إخفاء بيانات العميل
        if getattr(user, "role", None) == "employee":
            if req.offers.filter(employee=user).exists():
                return True
        return False


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

        # تمرير selected_offer دائمًا
        ctx["selected_offer"] = getattr(req, "selected_offer", None)

        # تمرير my_offer دائمًا (إذا كان المستخدم موظفًا)
        my_offer = None
        if u.is_authenticated and getattr(u, "role", None) == "employee":
            my_offer = req.offers.filter(employee=u).order_by("-id").first()
        ctx["my_offer"] = my_offer

        # ------------ تقديم عرض (موظف فقط وعلى NEW وغير مُسنَّد) ------------
        ctx["can_offer"] = False
        ctx["offer_form"] = None
        from .models import Status
        if (
            u.is_authenticated
            and getattr(u, "role", None) == "employee"
            and getattr(req, "status", None) == getattr(Status, "NEW", "new")
            and getattr(req, "assigned_employee_id", None) is None
        ):
            from .models import Status
            pending_offer = req.offers.filter(employee=u, status="pending").first()
            ctx["can_offer"] = pending_offer is None
            if pending_offer is None:
                ctx["offer_form"] = OfferCreateForm()

        # إنشاء/فتح الاتفاقية (بعد اختيار العرض)
        ctx["can_create_agreement"] = False
        if u.is_authenticated:
            # 1. Check if user is the assigned employee
            is_assigned = getattr(req, "assigned_employee_id", None) == u.id
            
            # 2. Check if user is the owner of the selected offer (fallback)
            selected = getattr(req, "selected_offer", None)
            is_offer_owner = selected and getattr(selected, "employee_id", None) == u.id
            
            if is_assigned or is_offer_owner:
                # التحقق من الحالة: تم اختيار العرض أو وجود اتفاقية
                status_ok = str(getattr(req, "status", "")) == "offer_selected"
                
                # Check for agreement safely
                has_agreement = False
                try:
                    has_agreement = req.agreement is not None
                except Exception:
                    pass
                
                if status_ok or has_agreement:
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
        # تمرير المبلغ الإجمالي للعميل في العرض المختار (إن وجد)
        selected_offer = ctx["selected_offer"]
        if selected_offer:
            try:
                ctx["selected_offer_client_total"] = selected_offer.client_total_amount
                selected_offer.client_total = selected_offer.client_total_amount
            except Exception:
                ctx["selected_offer_client_total"] = None
                selected_offer.client_total = None

        # التقييم (للعميل فقط، عند اكتمال الطلب، ولم يقم بالتقييم بعد)
        ctx["can_review"] = False
        ctx["review_form"] = None
        if (
            u.is_authenticated
            and getattr(req, "status", None) == "completed"
            and getattr(req, "client_id", None) == u.id
        ):
            try:
                if not hasattr(req, "review"):
                    ctx["can_review"] = True
                    ctx["review_form"] = ReviewForm()
            except Review.DoesNotExist:
                ctx["can_review"] = True
                ctx["review_form"] = ReviewForm()

        return ctx

    # إرسال عرض من نفس صفحة التفاصيل (للموظف)
    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        req: Request = self.object
        u = request.user

        if not self._can_view(u, req):
            messages.error(request, "ليس لديك صلاحية لتنفيذ هذا الإجراء.")
            return redirect(_fallback_after_forbidden(u))

        # معالجة التقييم
        if "submit_review" in request.POST:
            if (
                getattr(req, "status", None) == "completed"
                and getattr(req, "client_id", None) == u.id
                and not hasattr(req, "review")
            ):
                form = ReviewForm(request.POST)
                if form.is_valid():
                    review = form.save(commit=False)
                    review.request = req
                    review.reviewer = u
                    review.reviewee = req.assigned_employee
                    review.save()
                    messages.success(request, "تم إرسال تقييمك بنجاح.")
                    return redirect("marketplace:request_detail", pk=req.pk)
                else:
                    messages.error(request, "يرجى تصحيح الأخطاء في نموذج التقييم.")
                    context = self.get_context_data(object=req)
                    context["can_review"] = True
                    context["review_form"] = form
                    return self.render_to_response(context)

        if not (u.is_authenticated and getattr(u, "role", None) == "employee"):
            messages.error(request, "غير مصرح بتقديم عرض على هذا الطلب.")
            return redirect("marketplace:request_detail", pk=req.pk)

        if getattr(req, "status", None) != getattr(Status, "NEW", "new") or getattr(req, "assigned_employee_id", None) is not None:
            messages.warning(request, "لا يمكن تقديم عروض لهذا الطلب في حالته الحالية.")
            return redirect("marketplace:request_detail", pk=req.pk)

        # فرض قيد “عرض واحد فعّال لكل (request, employee)” + نافذة العروض
        from .models import Status
        if req.offers.filter(employee=u).exclude(status=getattr(Status, "WITHDRAWN", "withdrawn")).exists():
            messages.info(request, "لديك عرض فعّال مسبقًا لهذا الطلب.")
            return redirect("marketplace:request_detail", pk=req.pk)

        if not _in_offers_window(req):
            messages.error(request, "انتهت نافذة استقبال العروض لهذا الطلب.")
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
        except ValidationError as e:
            messages.error(request, f"لم يتم إنشاء العرض: {e}")
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


# === تفاصيل الطلب عبر short_code ===
class RequestDetailByCodeView(RequestDetailView):
    pk_url_kwarg = "short_code"

    def get_object(self, queryset=None):
        qs = self.get_queryset()
        return get_object_or_404(qs, short_code__iexact=self.kwargs["short_code"])


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
            status=Status.NEW,
            assigned_employee__isnull=True,
        )
        # عرض واحد فعّال لكل موظف على ذات الطلب
        if Offer.objects.filter(request=self.req_obj, employee=request.user).exclude(status=Status.WITHDRAWN).exists():
            messages.warning(request, "قدّمت عرضًا مسبقًا لهذا الطلب.")
            return redirect("marketplace:request_detail", pk=self.req_obj.pk)
        # التحقّق من النافذة
        if not _in_offers_window(self.req_obj):
            messages.error(request, "انتهت نافذة استقبال العروض لهذا الطلب.")
            return redirect("marketplace:request_detail", pk=self.req_obj.pk)
        return super().dispatch(request, *args, **kwargs)

    def get_form(self, form_class=None):
        form = super().get_form(form_class)
        form.instance.request = self.req_obj
        form.instance.employee = self.request.user
        return form

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # ضمان تمرير القيم دائمًا
        fee, vat = 0, 0
        try:
            fee, vat = FinanceSettings.current_rates()
        except Exception:
            pass
        context.setdefault("platform_fee_percent", float(fee))
        context.setdefault("vat_percent", float(vat))
        # إذا كان هناك form.instance، عيّن القيم الافتراضية داخله أيضًا
        form = context.get("form")
        if form and hasattr(form, "instance"):
            if not getattr(form.instance, "platform_fee_percent", None):
                form.instance.platform_fee_percent = float(fee)
            if not getattr(form.instance, "vat_percent", None):
                form.instance.vat_percent = float(vat)
        return context

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
        except ValidationError as e:
            messages.error(self.request, f"لم يتم إنشاء العرض: {e}")
            return redirect("marketplace:request_detail", pk=self.req_obj.pk)

    def get_success_url(self):
        return reverse("marketplace:request_detail", args=[self.req_obj.pk])


@require_role("client")
@transaction.atomic
def offer_select(request, offer_id):
    """
    اختيار عرض من العميل (أو الإدارة عبر الديكوريتر).
    الشروط:
      - الطلب ليس نزاعًا/مجمّدًا
      - العرض PENDING وعلى نفس الطلب
      - الطلب NEW وغير مُسنَّد
    """
    off = get_object_or_404(
        Offer.objects.select_related("request", "employee").select_for_update(),
        pk=offer_id,
    )
    req = off.request

    # تأكيد المالك أو ستاف (الديكوريتر يسمح ستاف/Admin)
    if req.client_id != request.user.id and not getattr(request.user, "is_staff", False):
        return HttpResponseForbidden("غير مسموح")

    # منع الاختيار أثناء النزاع/التجميد
    if getattr(req, "is_frozen", False) or str(getattr(req, "status", "")).lower() == "disputed":
        messages.error(request, "لا يمكن اختيار عرض: الطلب في حالة نزاع.")
        return redirect("marketplace:request_detail", pk=req.pk)

    # لا تعتمد على can_select إن كان متشددًا؛ نطبّق شروطنا بوضوح
    from .models import Status
    # السماح بالحالات: pending, new, waiting_client_approval, modified
    allowed_statuses = ["pending", "new", Status.NEW, Status.WAITING_CLIENT_APPROVAL, Status.MODIFIED]
    
    if getattr(off, "status", None) not in allowed_statuses:
        messages.info(request, f"لا يمكن اختيار عرض بحالة: {off.get_status_display()}")
        return redirect("marketplace:request_detail", pk=req.pk)


    # إذا كان العرض معدل أو بانتظار موافقة العميل، اسمح بالاختيار حتى لو لم يكن الطلب NEW بشرط عدم وجود عرض مختار مسبقًا
    if getattr(off, "status", None) in [Status.WAITING_CLIENT_APPROVAL, Status.MODIFIED]:
        # لا تسمح إذا كان هناك عرض مختار بالفعل
        if Offer.objects.filter(request=req, status=Status.SELECTED).exclude(pk=off.pk).exists():
            messages.error(request, "تم اختيار عرض آخر بالفعل.")
            return redirect("marketplace:request_detail", pk=req.pk)
    else:
        # الطلب يجب أن يكون NEW وغير مُسنَّد
        if getattr(req, "status", None) != getattr(Status, "NEW", "new") or getattr(req, "assigned_employee_id", None) is not None:
            messages.error(request, "لا يمكن اختيار عرض في هذه الحالة.")
            return redirect("marketplace:request_detail", pk=req.pk)

    # ارفض بقية العروض
    from .models import Status
    Offer.objects.filter(request=req).exclude(pk=off.pk).update(status=getattr(Status, "REJECTED", "rejected"))

    # اختر العرض
    from .models import Status
    off.status = getattr(Status, "SELECTED", "selected")
    off.save(update_fields=["status"])

    # إسناد الطلب + تحديث حالته
    req.assigned_employee = off.employee
    req.status = getattr(Status, "OFFER_SELECTED", "offer_selected")
    update_fields = ["assigned_employee", "status"]
    if hasattr(req, "updated_at"):
        req.updated_at = timezone.now()
        update_fields.append("updated_at")
    req.save(update_fields=update_fields)

    try:
        _notify_offer_selected(off)
    except Exception:
        pass

    messages.success(request, "تم اختيار العرض وإسناد الطلب.")
    return redirect("marketplace:request_detail", pk=req.pk)


@login_required
@transaction.atomic
def offer_reject(request, offer_id):
    if request.method != "POST":
        return HttpResponseForbidden("غير مسموح")
    off = get_object_or_404(Offer.objects.select_related("request"), pk=offer_id)
    if hasattr(off, "can_reject") and not off.can_reject(request.user):
        return HttpResponseForbidden("غير مسموح")

    from .models import Status
    off.status = Status.REJECTED
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
    allowed = (
        user.id == req.client_id
        or user.id == getattr(req, "assigned_employee_id", None)
        or _is_admin(user)
    )
    if not allowed:
        messages.error(request, "غير مصرح بإضافة ملاحظة على هذا الطلب.")
        return redirect("marketplace:request_detail", pk=req.id)

    text = (request.POST.get("text") or "").strip()
    if len(text) < 2:
        messages.error(request, "الرجاء إدخال ملاحظة صالحة (على الأقل حرفان).")
        return redirect("marketplace:request_detail", pk=req.id)

    Note.objects.create(request=req, author=user, text=text)
    messages.success(request, "تم حفظ الملاحظة.")

    try:
        url = reverse("marketplace:request_detail", args=[req.pk])
        if user.id == req.client_id:
            target_user = getattr(req, "assigned_employee", None) or (
                getattr(req, "selected_offer", None) and req.selected_offer.employee
            )
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

    return redirect("marketplace:request_detail", pk=req.id)


# ======================
# تغيير حالة الطلب + إلغاء
# ======================
@login_required
@require_POST
@transaction.atomic
def request_change_state(request, pk: int):
    # نستخدم select_for_update لتفادي تعارضات التحديث
    req = get_object_or_404(Request.objects.select_for_update(), pk=pk)
    user = request.user

    # صلاحية الإدارة/الموظف المُسند
    if not _can_manage_request(user, req):
        raise PermissionDenied

    # الحالة المطلوبة (تطبيع)
    new_state = (request.POST.get("state") or "").strip().lower()
    # تم إضافة "status" كاسم بديل للحقل في الفورم
    if not new_state:
        new_state = (request.POST.get("status") or "").strip().lower()

    allowed_states = {"in_progress", "awaiting_review", "awaiting_payment", "completed", "cancelled"}
    if new_state not in allowed_states:
        messages.error(request, "حالة غير مسموح بها.")
        return redirect(req.get_absolute_url())

    # الحالة الحالية (تطبيع)
    current = str(getattr(req, "status", getattr(req, "state", "")) or "").strip().lower()


    # السماح للمدير بتغيير الحالة من 'نزاع' لأي حالة أخرى
    is_admin_user = _is_admin(user)
    if current == "disputed" and is_admin_user:
        # إزالة علم النزاع عند تغيير الحالة
        if hasattr(req, "has_dispute"):
            req.has_dispute = False
    else:
        # إذا كان المستخدم مديراً، يسمح له بتغيير الحالة من أي حالة لأي حالة
        if not is_admin_user:
            # الانتقالات المسموحة لغير المدير
            allowed_transitions = {
                "in_progress": {"awaiting_review", "cancelled"},
                "awaiting_review": {"in_progress", "awaiting_payment"},
                "awaiting_payment": {"completed", "cancelled"},
                "completed": set(),
                "cancelled": set(),
            }
            # منع انتقال غير منطقي (إلا الإلغاء)
            if new_state != "cancelled":
                if current not in allowed_transitions or new_state not in allowed_transitions[current]:
                    messages.error(request, "لا يُسمح بالانتقال المطلوب من الحالة الحالية.")
                    return redirect(req.get_absolute_url())

    # تحديد اسم حقل الحالة الفعلي (status أو state)
    field = _status_field_name(req)
    if not field:
        messages.error(request, "حقل الحالة غير معرّف على هذا الطلب.")
        return redirect(req.get_absolute_url())

    # =========================================================
    # شرط الدفع قبل التحويل إلى قيد التنفيذ
    # =========================================================
    if new_state == "in_progress":
        agreement = getattr(req, "agreement", None)
        # إذا المدير يحاول تغيير الحالة من نزاع، تجاوز شرط الفاتورة
        if not (current == "disputed" and is_admin_user):
            # لا يوجد اتفاقية => لا يوجد فاتورة => لا بدء تنفيذ
            if not agreement:
                messages.error(request, "لا يمكن بدء التنفيذ قبل وجود اتفاقية وفاتورة مدفوعة.")
                return redirect(req.get_absolute_url())

            invoice_paid = False

            # ✅ المصدر الرسمي الجديد إن وُجد
            if hasattr(agreement, "invoices_all_paid"):
                try:
                    invoice_paid = bool(agreement.invoices_all_paid)
                except Exception:
                    invoice_paid = False
            else:
                # fallback لو لم تُحدَّث Agreement بعد
                try:
                    invoices_mgr = getattr(agreement, "invoices", None)
                    if invoices_mgr is not None:
                        qs = invoices_mgr.all()
                        if qs.exists():
                            from finance.models import Invoice as InvoiceModel
                            paid_val = getattr(getattr(InvoiceModel, "Status", None), "PAID", "paid")
                            invoice_paid = not qs.exclude(status=paid_val).exists()
                    else:
                        inv = getattr(agreement, "invoice", None)
                        if inv is not None:
                            status_val = str(getattr(inv, "status", "") or "").lower()
                            paid_val = str(getattr(getattr(inv.__class__, "Status", None), "PAID", "paid") or "").lower()
                            invoice_paid = status_val == paid_val
                except Exception:
                    invoice_paid = False

            if not invoice_paid:
                messages.error(request, "لا يمكن تحويل الطلب إلى قيد التنفيذ إلا بعد سداد جميع فواتير الاتفاقية.")
                return redirect(req.get_absolute_url())

            # (اختياري) تثبيت تاريخ البدء وتزامن حالة الطلب من داخل الاتفاقية
            try:
                if hasattr(agreement, "mark_started"):
                    agreement.mark_started(save=True)
                if hasattr(agreement, "sync_request_state"):
                    # لا نحفظ الطلب هنا حتى لا يحدث تعارض، لأننا سنحفظه بعد قليل
                    agreement.sync_request_state(save_request=False)
            except Exception:
                pass

    # تحديث الحالة على الطلب
    setattr(req, field, new_state)

    try:
        update_fields = [field]

        # تحديث updated_at إن كان موجودًا
        if hasattr(req, "updated_at"):
            req.updated_at = timezone.now()
            update_fields.append("updated_at")

        req.save(update_fields=update_fields)
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
        qs = Request.objects.select_related("client", "assigned_employee").filter(assigned_employee=u)

        done_like = _status_vals("COMPLETED", "CANCELED", "CLOSED")
        qs = qs.exclude(status__in=done_like)

        if hasattr(Status, "DISPUTED"):
            qs = qs.exclude(status=Status.DISPUTED)
        else:
            qs = qs.exclude(status="disputed")

        if hasattr(Request, "is_frozen"):
            qs = qs.filter(Q(is_frozen=False) | Q(is_frozen__isnull=True))

        return qs.order_by("-updated_at", "-id")


@login_required
def my_tasks(request):
    u = request.user
    qs = Request.objects.select_related("client", "assigned_employee").filter(assigned_employee=u)

    # Show all assigned tasks, regardless of status
    if hasattr(Request, "is_frozen"):
        qs = qs.filter(Q(is_frozen=False) | Q(is_frozen__isnull=True))

    qs = qs.order_by("-updated_at", "-id")
    return render(request, "marketplace/my_tasks.html", {"requests": qs})


@login_required
def disputed_tasks(request):
    u = request.user

    disputed_q = Q(status="disputed")
    if hasattr(Status, "DISPUTED"):
        disputed_q = Q(status=Status.DISPUTED) | Q(status__iexact="disputed")

    qs = (
        Request.objects.select_related("client", "assigned_employee")
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
@require_POST
@transaction.atomic
def admin_request_reset_to_new(request, pk: int):
    """
    إعادة طلب إلى حالة NEW:
    - متاح للإداريين فقط.
    - يفك الإسناد للموظف.
    - لا يعبث بالاتفاقية أو الدفعات هنا (قرار إداري مستقل).
    """
    if not _is_admin(request.user):
        messages.error(request, "غير مصرح بتنفيذ هذا الإجراء.")
        return redirect(request.META.get("HTTP_REFERER", "/"))

    obj = get_object_or_404(Request.objects.select_for_update(), pk=pk)

    NEW = getattr(Status, "NEW", "new")
    now = timezone.now()

    updates: list[str] = []
    try:
        if getattr(obj, "status", None) != NEW:
            obj.status = NEW
            updates.append("status")

        if hasattr(obj, "assigned_employee_id") and getattr(obj, "assigned_employee_id", None):
            obj.assigned_employee_id = None
            updates.append("assigned_employee_id")

        if hasattr(obj, "updated_at"):
            obj.updated_at = now
            updates.append("updated_at")

        if updates:
            obj.save(update_fields=updates)

        messages.success(request, "تمت إعادة الطلب إلى حالة جديدة (NEW) وفك الإسناد.")
        return redirect(request.META.get("HTTP_REFERER", "/"))
    except Exception as e:
        logger.exception("فشل إعادة الطلب NEW (req_id=%s): %s", pk, e)
        messages.error(request, "حدث خطأ أثناء إعادة ضبط الطلب.")
        return redirect(request.META.get("HTTP_REFERER", "/"))


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

# مثال داخل منطق إنشاء الفاتورة من عرض معيّن
from finance.utils import calculate_financials

def create_invoice_from_offer(offer):
    settings = FinanceSettings.get_solo()
    data = calculate_financials(
        net_amount=offer.proposed_price,
        platform_fee_percent=settings.platform_fee_percent,
        vat_percent=settings.vat_percent,
    )

    invoice = Invoice.objects.create(
        offer=offer,
        net_amount=data["net_for_employee"],
        platform_fee_amount=data["platform_fee"],
        vat_amount=data["vat_amount"],
        total_amount=data["client_total"],  # هذا ما يدفعه العميل
        # + أي حقول أخرى مطلوبة
    )
    return invoice


@login_required
@staff_member_required
def all_requests_admin(request):
    requests = Request.objects.select_related('client', 'assigned_employee').order_by('-created_at')
    return render(request, "marketplace/all_requests.html", {"requests": requests})


@login_required
def add_comment(request, pk):
    from .models import Request, Comment
    req = get_object_or_404(Request, pk=pk)
    
    # Check permissions: only client, assigned employee, or admin can comment
    is_client = request.user == req.client
    is_employee = request.user == req.assigned_employee
    is_admin = request.user.is_staff or getattr(request.user, 'role', '') == 'admin'
    
    if not (is_client or is_employee or is_admin):
        messages.error(request, "غير مصرح لك بإضافة تعليق على هذا الطلب.")
        return redirect("marketplace:request_detail", pk=pk)

    if request.method == "POST":
        content = request.POST.get("content")
        file = request.FILES.get("file")
        
        if content or file:
            Comment.objects.create(
                request=req,
                author=request.user,
                content=content,
                file=file
            )
            messages.success(request, "تم إضافة التعليق بنجاح.")
            
            # Notify the other party
            recipient = None
            if is_client and req.assigned_employee:
                recipient = req.assigned_employee
            elif is_employee:
                recipient = req.client
            
            if recipient:
                try:
                    from notifications.utils import create_notification
                    create_notification(
                        recipient=recipient,
                        title=f"تعليق جديد على الطلب #{req.pk}",
                        body=f"قام {request.user.get_full_name()} بإضافة تعليق جديد.",
                        url=reverse("marketplace:request_detail", args=[req.pk]),
                        actor=request.user,
                        target=req,
                    )
                except Exception:
                    pass
        else:
            messages.warning(request, "لا يمكن إضافة تعليق فارغ.")
            
    return redirect("marketplace:request_detail", pk=pk)
