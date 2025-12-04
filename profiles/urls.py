from __future__ import annotations
from . import views

from django.urls import path
from django.views.generic.base import RedirectView

from .views import EmployeeListView, EmployeeDetailView, whatsapp_redirect

app_name = "profiles"

urlpatterns = [
    # القوائم
    path("", EmployeeListView.as_view(), name="employees_list"),
    path("techs/", RedirectView.as_view(pattern_name="profiles:employees_list", permanent=True), name="techs_list"),
    path("employees/", RedirectView.as_view(pattern_name="profiles:employees_list", permanent=True), name="employees_list_alias"),
    
    # ✅ التصحيح: دمج المسارين في مسار واحد يدعم pk و slug
    path('employees/<int:pk>/', EmployeeDetailView.as_view(), name='employee_detail_by_pk'),
    path('employees/<slug:slug>/', EmployeeDetailView.as_view(), name='employee_detail_by_slug'),
    
    # ✅ مسار رئيسي واحد
    path('employee/<int:pk>/', EmployeeDetailView.as_view(), name='employee_detail'),
    
    # تحويل واتساب الآمن
    path("w/emp/<int:user_id>/", whatsapp_redirect, name="whatsapp_redirect"),

    # إدارة معرض الأعمال
    path('portfolio/', views.PortfolioListView.as_view(), name='portfolio_list'),
    path('portfolio/add/', views.PortfolioCreateView.as_view(), name='portfolio_add'),
    path('portfolio/<int:pk>/edit/', views.PortfolioUpdateView.as_view(), name='portfolio_edit'),
    path('portfolio/<int:pk>/delete/', views.PortfolioDeleteView.as_view(), name='portfolio_delete'),
]