# marketplace/urls.py
from django.urls import path
from django.shortcuts import redirect
from django.views.generic import RedirectView
from . import views
from .views import RequestDetailView, RequestDetailByCodeView

app_name = "marketplace"

# ======================
# Aliases / Redirects (توافق لمسارات قديمة)
# ======================
def request_list_alias(request):
    """حاليًا تحيل إلى 'طلباتي' للعميل."""
    return redirect("marketplace:my_requests")

def offer_create_legacy_cbv_redirect(request, request_id: int):
    """o/<int:request_id>/new/ → r/<int:request_id>/offer/new/"""
    return redirect("marketplace:offer_create", request_id=request_id)

def offer_select_legacy_cbv_redirect(request, offer_id: int):
    """o/<int:offer_id>/select/ → offers/<int:offer_id>/select/"""
    return redirect("marketplace:offer_select", offer_id=offer_id)

def request_detail_fallback(request, ref: str):
    """ref رقمي → pk، غير ذلك → short_code."""
    if ref.isdigit():
        return redirect("marketplace:request_detail", pk=int(ref))
    return redirect("marketplace:request_detail_by_code", short_code=ref)

urlpatterns = [
    # ======================
    # الطلبات (Requests)
    # ======================
    path("r/new/", views.RequestCreateView.as_view(), name="request_create"),
    path("r/mine/", views.MyRequestsListView.as_view(), name="my_requests"),
    path("r/new-requests/", views.NewRequestsForEmployeesView.as_view(), name="new_requests"),
    path("r/assigned/", views.MyAssignedRequestsView.as_view(), name="assigned_requests"),

    # “مهامي”
    path("tasks/", views.my_tasks, name="my_tasks"),

    # تفاصيل الطلب (pk / short_code / fallback)
    path("r/<int:pk>/", RequestDetailView.as_view(), name="request_detail"),
    path("r/code/<slug:short_code>/", RequestDetailByCodeView.as_view(), name="request_detail_by_code"),
    path("r/ref/<slug:ref>/", request_detail_fallback, name="request_detail_ref"),

    # قائمة افتراضية (تحويل)
    path("r/", request_list_alias, name="request_list"),

    # ملاحظات على الطلب
    path("r/<int:pk>/notes/add/", views.request_add_note, name="request_add_note"),
    path("r/<int:pk>/comment/add/", views.add_comment, name="add_comment"),

    # تغيير حالة الطلب/إلغاء
    path("r/<int:pk>/status/change/", views.request_change_state, name="request_change_status"),
    path("r/<int:pk>/state/change/", views.request_change_state, name="request_change_state"),  # توافق
    path("r/<int:pk>/state/cancel/", views.request_cancel, name="request_cancel"),

    # ======================
    # العروض (Offers)
    # ======================
    path("r/<int:request_id>/offer/new/", views.OfferCreateView.as_view(), name="offer_create"),
    path("offers/<int:offer_id>/", views.offer_detail, name="offer_detail"),
    path("offers/<int:offer_id>/select/", views.offer_select, name="offer_select"),
    path("offers/<int:offer_id>/reject/", views.offer_reject, name="offer_reject"),

    path("offers/<int:offer_id>/edit/", views.edit_offer, name="offer_edit"),
    path("offers/<int:offer_id>/cancel/", views.offer_cancel, name="offer_cancel"),
    path("offers/<int:offer_id>/extend/", views.offer_extend, name="offer_extend"),

    # توافق لمسارات قديمة
    path("o/<int:request_id>/new/", offer_create_legacy_cbv_redirect, name="offer_create_cbv"),
    path("o/<int:offer_id>/select/", offer_select_legacy_cbv_redirect, name="offer_select_cbvstyle"),

    # ======================
    # إجراءات المدير (ضرورية للقالب)
    # ======================
    path("admin/request/<int:pk>/reassign/", views.admin_request_reassign, name="admin_request_reassign"),
    path("admin/request/<int:pk>/delete/", views.admin_request_delete, name="admin_request_delete"),
    # الاسم الأساسي:
    path("admin/request/<int:pk>/reset/", views.admin_request_reset_to_new, name="request_reset_to_new"),
    # alias بالاسم الذي يستخدمه القالب:
    path(
        "admin/request/<int:pk>/reset-to-new/",
        RedirectView.as_view(pattern_name="marketplace:request_reset_to_new", permanent=False),
        name="admin_request_reset_to_new",
    ),

    # ======================
    # نزاعات
    # ======================
    path("disputed/", views.disputed_tasks, name="disputed_tasks"),

    # قائمة جميع الطلبات الإدارية
    path("all-requests/", views.all_requests_admin, name="all_requests_admin"),
]
