# dashboard/urls.py
from django.urls import path
from . import views

app_name = "dashboard"

urlpatterns = [
    # لوحة المدير (الصفحة الرئيسية للوحة التحكم)
    path("", views.admin_dashboard, name="admin_dashboard"),

    # إدارة المستخدمين
    path("employees/", views.employees_list, name="employees_list"),
    path("clients/", views.clients_list, name="clients_list"),

    # إدارة الطلبات
    path("requests/", views.requests_list, name="requests_list"),

    # جميع الطلبات (قالب مخصص)
    path("all-requests/", views.all_requests_view, name="all_requests"),

    # إدارة النزاعات
    path("disputes/", views.disputes_list, name="disputes_list"),

    # رسائل التواصل
    path("messages/", views.contact_messages_list, name="contact_messages_list"),
]
