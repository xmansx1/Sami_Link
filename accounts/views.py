# accounts/views.py
from __future__ import annotations

import logging
from typing import Any

from django.conf import settings
from django.contrib import messages
from django.contrib.auth import login, logout
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib.auth.views import PasswordResetView, PasswordResetConfirmView
from django.http import HttpRequest, HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse_lazy
from django.utils.http import url_has_allowed_host_and_scheme
from django.utils.translation import gettext_lazy as _
from django.views.generic import FormView, CreateView, TemplateView, UpdateView

from .forms import LoginForm, RegisterForm, ProfileUpdateForm
from profiles.forms import EmployeeProfileForm
from profiles.models import EmployeeProfile

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------
# دالة توجيه آمنة: تسمح بـ ?next= فقط إذا كان ضمن نفس المضيف وبحسب HTTPS
# ---------------------------------------------------------------------
def _safe_next(request: HttpRequest, fallback_url: str) -> str:
    next_url = request.GET.get("next") or request.POST.get("next")
    if next_url and url_has_allowed_host_and_scheme(
        url=next_url,
        allowed_hosts={request.get_host()},
        require_https=getattr(settings, "SECURE_SSL_REDIRECT", False),
    ):
        return next_url
    return fallback_url


class LoginPageView(FormView):
    """
    صفحة تسجيل دخول بالبريد/كلمة مرور عبر LoginForm.
    - عند نجاح الدخول: يوجّه إلى ?next= إن كان آمنًا وإلا للصفحة الرئيسية.
    - عند فشل الدخول: رسالة واضحة وتسجيل الحدث.
    """
    template_name = "accounts/login.html"
    form_class = LoginForm
    success_url = reverse_lazy("website:home")

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        if request.user.is_authenticated:
            # المستخدم مسجل دخول بالفعل — نوجّه مباشرة
            return redirect(_safe_next(request, self.get_success_url()))
        return super().dispatch(request, *args, **kwargs)

    def form_valid(self, form: LoginForm) -> HttpResponse:
        # LoginForm يجب أن يضع المستخدم المصادق عليه في cleaned_data["user"]
        user = form.cleaned_data["user"]
        login(self.request, user)
        messages.success(self.request, _("تم تسجيل الدخول بنجاح."))
        logger.info("User %s logged in successfully", user.pk)
        return redirect(_safe_next(self.request, self.get_success_url()))

    def form_invalid(self, form: LoginForm) -> HttpResponse:
        messages.error(self.request, _("تعذّر تسجيل الدخول. تأكد من البريد وكلمة المرور."))
        logger.warning("Login failed: %s", form.errors.as_json())
        return super().form_invalid(form)


class LogoutView(LoginRequiredMixin, TemplateView):
    """
    تسجيل الخروج وإظهار رسالة مناسبة ثم التوجيه لصفحة الدخول.
    """
    template_name = "accounts/logout.html"

    def get(self, request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
        uid = getattr(request.user, "pk", None)
        logout(request)
        messages.info(request, _("تم تسجيل الخروج."))
        logger.info("User %s logged out", uid)
        return redirect("accounts:login")


class RegisterView(CreateView):
    """
    إنشاء حساب جديد عبر RegisterForm.
    - رسائل نجاح/فشل واضحة.
    """
    template_name = "accounts/register.html"
    form_class = RegisterForm
    success_url = reverse_lazy("accounts:login")

    def form_valid(self, form: RegisterForm) -> HttpResponse:
        resp = super().form_valid(form)
        messages.success(self.request, _("تم إنشاء الحساب. يمكنك تسجيل الدخول الآن."))
        logger.info("New user registered: %s", getattr(self.object, "pk", None))
        return resp

    def form_invalid(self, form: RegisterForm) -> HttpResponse:
        messages.error(self.request, _("تعذّر إنشاء الحساب. يرجى تصحيح الأخطاء بالأسفل."))
        logger.warning("Registration failed: %s", form.errors.as_json())
        return super().form_invalid(form)


class ProfileView(LoginRequiredMixin, TemplateView):
    """
    عرض الملف الشخصي للمستخدم الحالي.
    """
    template_name = "accounts/profile.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        user = self.request.user
        # فقط للعميل
        if getattr(user, "role", None) == "client":
            from django.db import models
            from marketplace.models import Request
            from finance.models import Invoice
            # عدد الطلبات
            requests_qs = Request.objects.filter(client=user)
            context["client_requests_count"] = requests_qs.count()
            # إجمالي المدفوع
            paid_invoices = Invoice.objects.filter(agreement__request__client=user, status=getattr(Invoice.Status, "PAID", "paid"))
            context["client_paid_total"] = paid_invoices.aggregate(total=models.Sum("amount"))['total'] or 0
            # آخر 3 طلبات وحالتها
            last_requests = requests_qs.order_by("-created_at")[:3]
            context["client_last_requests"] = last_requests
        return context


class ProfileEditView(LoginRequiredMixin, UpdateView):
    """
    تعديل الملف الشخصي للمستخدم الحالي.
    - يستخدم ProfileUpdateForm.
    - يدعم تعديل بيانات الموظف إذا كان المستخدم موظفاً.
    """
    template_name = "accounts/profile_edit.html"
    form_class = ProfileUpdateForm
    success_url = reverse_lazy("accounts:profile")

    def get_object(self):
        return self.request.user

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        if getattr(self.request.user, 'role', None) == 'employee':
            profile, _ = EmployeeProfile.objects.get_or_create(user=self.request.user)
            if 'employee_form' not in context:
                context['employee_form'] = EmployeeProfileForm(instance=profile)
        return context

    def post(self, request, *args, **kwargs):
        self.object = self.get_object()
        form = self.get_form()
        
        employee_form = None
        if getattr(request.user, 'role', None) == 'employee':
            profile, _ = EmployeeProfile.objects.get_or_create(user=request.user)
            employee_form = EmployeeProfileForm(request.POST, request.FILES, instance=profile)

        if form.is_valid() and (employee_form is None or employee_form.is_valid()):
            return self.form_valid(form, employee_form)
        else:
            return self.form_invalid(form, employee_form)

    def form_valid(self, form, employee_form=None):
        if employee_form:
            employee_form.save()
        messages.success(self.request, _("تم تحديث ملفك الشخصي."))
        logger.info("Profile updated for user %s", self.request.user.pk)
        return super().form_valid(form)

    def form_invalid(self, form, employee_form=None):
        messages.error(self.request, _("تعذّر تحديث الملف الشخصي. يرجى مراجعة الحقول."))
        logger.warning("Profile update failed for user %s: %s",
                       getattr(self.request.user, "pk", None), form.errors.as_json())
        return self.render_to_response(self.get_context_data(form=form, employee_form=employee_form))


# -------------------------------
# استعادة كلمة المرور (اختياري)
# -------------------------------
class ResetPasswordView(PasswordResetView):
    """
    إرسال رابط استعادة كلمة المرور إلى بريد المستخدم.
    """
    template_name = "accounts/password_reset_form.html"
    email_template_name = "accounts/password_reset_email.txt"
    subject_template_name = "accounts/password_reset_subject.txt"
    success_url = reverse_lazy("accounts:password_reset_done")


class ResetPasswordConfirmView(PasswordResetConfirmView):
    """
    تأكيد تعيين كلمة مرور جديدة بعد فتح الرابط.
    """
    template_name = "accounts/password_reset_confirm.html"
    success_url = reverse_lazy("accounts:password_reset_complete")
