from __future__ import annotations

import logging
import re
import time
from urllib.parse import urlencode

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.db.models import Q, Count
from django.http import Http404, HttpResponseBadRequest, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views.generic import DetailView, ListView, CreateView, UpdateView, DeleteView
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.urls import reverse_lazy

from marketplace.models import Request

from .models import EmployeeProfile, PortfolioItem
from .forms import PortfolioItemForm

logger = logging.getLogger(__name__)

# ===== سياسات قابلة للضبط =====
ALLOW_PUBLIC_EMPLOYEES_LIST = getattr(settings, "ALLOW_PUBLIC_EMPLOYEES_LIST", True)
HIDE_CONTACTS_DURING_OFFERS = getattr(settings, "HIDE_CONTACTS_DURING_OFFERS", True)
WHATSAPP_REDIRECT_ENABLED = getattr(settings, "WHATSAPP_REDIRECT_ENABLED", True)
WHATSAPP_RATE_LIMIT_PER_MIN = int(getattr(settings, "WHATSAPP_RATE_LIMIT_PER_MIN", 5))
WHATSAPP_MIN_SECONDS_BETWEEN_CLICKS = int(getattr(settings, "WHATSAPP_MIN_SECONDS_BETWEEN_CLICKS", 5))


def _is_e164(phone: str) -> bool:
    if not phone:
        return False
    return bool(re.fullmatch(r"\+?\d{8,15}", phone))


def _mask_phone(phone: str) -> str:
    if not phone:
        return ""
    p = phone.replace("+", "")
    if len(p) <= 4:
        return "****"
    return f"{p[:2]}****{p[-2:]}"


@method_decorator(login_required, name="dispatch")
class EmployeeListView(ListView):
    """
    قائمة التقنيين — تتطلب دخولًا (لتتبّع الجودة ومنع إساءة الاستخدام).
    تدعم q و sort=recent|rating و ترقيم الصفحات.
    """
    model = EmployeeProfile
    template_name = "profiles/employees_list.html"
    context_object_name = "employees"
    paginate_by = 12

    def dispatch(self, request, *args, **kwargs):
        if not ALLOW_PUBLIC_EMPLOYEES_LIST:
            raise Http404("القائمة غير مفعّلة حالياً.")
        return super().dispatch(request, *args, **kwargs)

    def get_queryset(self):
        qs = (
            EmployeeProfile.objects.select_related("user")
            .only(
                "id",
                "slug",
                "user__id",
                "user__name",
                "user__role",
                "user__is_active",
                "title",
                "city",
                "specialty",
                "skills",
                "rating",
                "reviews_count",
                "public_visible",
                "updated_at",
            )
            .filter(public_visible=True, user__is_active=True, user__role="employee")
            .annotate(
                real_completed_jobs_count=Count(
                    "user__assigned_requests",
                    filter=Q(user__assigned_requests__status="completed")
                )
            )
            .order_by("-rating", "-updated_at")
        )

        q = (self.request.GET.get("q") or "").strip()
        if q:
            q = q[:80]
            qs = qs.filter(
                Q(user__name__icontains=q)
                | Q(user__email__icontains=q)
                | Q(specialty__icontains=q)
                | Q(skills__icontains=q)
                | Q(title__icontains=q)
                | Q(city__icontains=q)
            )

        sort = (self.request.GET.get("sort") or "").strip()
        if sort == "recent":
            qs = qs.order_by("-updated_at", "-rating")
        elif sort == "rating":
            qs = qs.order_by("-rating", "-updated_at")

        return qs

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        ctx["q"] = (self.request.GET.get("q") or "").strip()
        ctx["sort"] = (self.request.GET.get("sort") or "").strip()
        return ctx


@method_decorator(login_required, name="dispatch")
class EmployeeDetailView(DetailView):
    """
    عرض بروفايل موظف:
    - يُظهر الهاتف مقنّعًا فقط.
    - يعرض ملخص المشاريع المكتملة عبر المنصة.
    - رابط واتساب عبر endpoint وسيط مع RBAC/Rate-limit إن كانت السياسة تسمح.
    """
    model = EmployeeProfile
    template_name = "profiles/employee_detail.html"
    context_object_name = "emp"

    def get_object(self, queryset=None):
        """
        الحصول على object مع معالجة مرنة للشروط
        """
        # الحصول على الـ object بالطريقة العادية أولاً
        if queryset is None:
            queryset = self.get_queryset()
        
        pk = self.kwargs.get('pk')
        slug = self.kwargs.get('slug')
        
        if pk:
            obj = get_object_or_404(queryset, pk=pk)
        elif slug:
            obj = get_object_or_404(queryset, slug=slug)
        else:
            raise Http404("لم يتم توفير معرف للبروفايل")
        
        # التحقق من الشروط بمرونة
        if not self.is_profile_accessible(obj):
            raise Http404("هذا البروفايل غير متاح.")
        
        return obj

    def is_profile_accessible(self, obj):
        """
        التحقق من إمكانية الوصول للبروفايل بشروط مرنة
        """
        # إذا كان المستخدم الحالي هو صاحب البروفايل، اسمح بالوصول دائماً
        if self.request.user == obj.user:
            return True
        
        # التحقق من الشروط الأساسية
        if not obj.user.is_active:
            return False
        
        # إذا كان public_visible موجود، نتحقق منه
        if hasattr(obj, 'public_visible') and not obj.public_visible:
            return False
        
        # إذا كان role موجود، نتحقق منه
        if hasattr(obj.user, 'role') and obj.user.role != "employee":
            return False
            
        return True

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        emp = ctx["emp"]

        # رقم الجوال المقنّع
        phone_e164 = getattr(emp.user, "phone", None) or getattr(emp, "phone", None)
        ctx["masked_phone"] = self._mask_phone(phone_e164) if self._is_e164(phone_e164) else ""

        # رابط الواتساب (عبر proxy) إذا سمحت السياسة
        # استخدام قيم افتراضية إذا لم تكن الثوابت موجودة
        allow_contact_link = getattr(self, 'WHATSAPP_REDIRECT_ENABLED', True) and not getattr(self, 'HIDE_CONTACTS_DURING_OFFERS', False)
        ctx["wa_redirect_url"] = (
            reverse("profiles:whatsapp_redirect", args=[emp.user_id]) if allow_contact_link else None
        )

        # المشاريع المكتملة لهذا الموظف
        try:
            base_qs = Request.objects.filter(
                assigned_employee=emp.user,
                status='completed'
            )
            # العدد الكلي
            total_count = base_qs.count()
            ctx["completed_requests_count"] = total_count
            
            # آخر 10 مشاريع للعرض
            completed_qs = (
                base_qs.select_related("client")
                .order_by("-updated_at", "-created_at")[:10]
            )
            ctx["completed_requests"] = completed_qs
            
        except Exception as e:
            # في حالة وجود أي خطأ، نعطي قيم افتراضية
            ctx["completed_requests"] = []
            ctx["completed_requests_count"] = 0
            print(f"Error loading completed requests: {e}")

        return ctx

    def _is_e164(self, phone):
        """التحقق من تنسيق رقم الهاتف"""
        if not phone:
            return False
        # تحقق بسيط من تنسيق الهاتف
        return phone.startswith('+') and len(phone) > 8

    def _mask_phone(self, phone):
        """إخفاء رقم الهاتف"""
        if not phone or len(phone) < 4:
            return "****"
        return f"****{phone[-4:]}"


@login_required
def whatsapp_redirect(request, user_id: int):
    """
    Proxy آمن لإعادة التوجيه إلى WhatsApp:
    - يمنع كشف الرقم في الواجهة.
    - يطبّق سياسة الإخفاء أثناء العروض إن مفعّلة (HIDE_CONTACTS_DURING_OFFERS).
    - يطبّق Rate-limit لكل (caller→employee) + لكل IP.
    - يسجل الحدث لأغراض التدقيق.
    """
    if not WHATSAPP_REDIRECT_ENABLED:
        return HttpResponseForbidden("التواصل الخارجي عبر الواتساب غير مفعّل حالياً.")

    profile = get_object_or_404(
        EmployeeProfile.objects.select_related("user").only("user__id", "user__phone", "public_visible"),
        user_id=user_id,
        public_visible=True,
        user__is_active=True,
    )

    # سياسة: منع التواصل المباشر قبل الاتفاقية (يمكن منح صلاحية خاصة لمن يحتاج)
    if HIDE_CONTACTS_DURING_OFFERS and not (
        request.user.is_staff or request.user.has_perm("profiles.view_external_contact")
    ):
        return HttpResponseForbidden("سياسة المنصة تمنع التواصل الخارجي قبل وجود اتفاقية معتمدة.")

    phone_e164 = getattr(profile.user, "phone", None)
    if not _is_e164(phone_e164):
        return HttpResponseBadRequest("لا يتوفر رقم جوال صالح E.164 للموظف.")

    caller_id = getattr(request.user, "id", None) or "anon"
    ip = (
        request.META.get("HTTP_X_FORWARDED_FOR", "").split(",")[0].strip()
        or request.META.get("REMOTE_ADDR", "0.0.0.0")
    )

    # حد (caller→employee) بالدقيقة
    minute_bucket = timezone.now().strftime("%Y%m%d%H%M")
    key_pair = f"wa:calls:u{caller_id}:e{user_id}:{minute_bucket}"
    calls = cache.get(key_pair, 0)
    if calls >= WHATSAPP_RATE_LIMIT_PER_MIN:
        return HttpResponseForbidden("تم تجاوز الحد المسموح مؤقتًا — حاول لاحقًا.")
    cache.set(key_pair, calls + 1, 70)

    # 5 ثوانٍ بين نقرات نفس الزوج
    key_last = f"wa:last:u{caller_id}:e{user_id}"
    last_ts = cache.get(key_last, 0.0)
    now_ts = time.time()
    if now_ts - float(last_ts) < WHATSAPP_MIN_SECONDS_BETWEEN_CLICKS:
        logger.info("WA throttle (rapid-click) user=%s to employee=%s", caller_id, user_id)
    cache.set(key_last, now_ts, 300)

    # حد لكل IP
    key_ip = f"wa:ip:{ip}:{minute_bucket}"
    ip_calls = cache.get(key_ip, 0)
    if ip_calls >= (WHATSAPP_RATE_LIMIT_PER_MIN * 2):
        return HttpResponseForbidden("عدد الطلبات من هذا العنوان مرتفع — حاول لاحقًا.")
    cache.set(key_ip, ip_calls + 1, 70)

    # بناء الرابط — الرقم بلا +
    msg = (request.GET.get("msg") or "").strip()[:200]
    number = phone_e164[1:] if phone_e164.startswith("+") else phone_e164
    wa_url = f"https://wa.me/{number}"
    if msg:
        wa_url = f"{wa_url}?{urlencode({'text': msg})}"

    logger.info("WA redirect: caller=%s employee=%s ip=%s msg_len=%s", caller_id, user_id, ip, len(msg))
    return redirect(wa_url)


# ======================
# إدارة معرض الأعمال (Portfolio)
# ======================
class PortfolioListView(LoginRequiredMixin, ListView):
    model = PortfolioItem
    template_name = "profiles/portfolio_list.html"
    context_object_name = "items"

    def get_queryset(self):
        return PortfolioItem.objects.filter(owner=self.request.user)

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        # جلب الطلبات المكتملة التي أنجزها الموظف في المنصة
        # نفترض أن الحالة "completed" هي الحالة النهائية
        # يمكن تعديل الفلتر حسب تعريف الحالات في مشروعك
        context['platform_completed_requests'] = Request.objects.filter(
            assigned_employee=self.request.user,
            status='completed'  # أو استخدام Status.COMPLETED إذا كان متاحاً
        ).order_by('-updated_at')
        return context

class PortfolioCreateView(LoginRequiredMixin, CreateView):
    model = PortfolioItem
    form_class = PortfolioItemForm
    template_name = "profiles/portfolio_form.html"
    success_url = reverse_lazy('profiles:portfolio_list')

    def form_valid(self, form):
        form.instance.owner = self.request.user
        return super().form_valid(form)

class PortfolioUpdateView(LoginRequiredMixin, UserPassesTestMixin, UpdateView):
    model = PortfolioItem
    form_class = PortfolioItemForm
    template_name = "profiles/portfolio_form.html"
    success_url = reverse_lazy('profiles:portfolio_list')

    def test_func(self):
        obj = self.get_object()
        return obj.owner == self.request.user

class PortfolioDeleteView(LoginRequiredMixin, UserPassesTestMixin, DeleteView):
    model = PortfolioItem
    template_name = "profiles/portfolio_confirm_delete.html"
    success_url = reverse_lazy('profiles:portfolio_list')

    def test_func(self):
        obj = self.get_object()
        return obj.owner == self.request.user
