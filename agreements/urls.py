from __future__ import annotations
# ========================= طلب تمديد المهلة للاتفاقية =========================
# (يجب أن يكون بعد الاستيرادات)
import inspect
from typing import Callable, Any
from django.urls import path
from . import views



app_name = "agreements"

# =========================================================
# أدوات مساعدة: تمرير pk أو agreement_id تلقائيًا حسب توقيع الدالة
# =========================================================
def _wrap_pk(view_attr: str) -> Callable[..., Any]:
    """
    يلفّ دالة من views باسم view_attr بحيث:
    - إن كانت الدالة تقبل 'agreement_id' نمرّر agreement_id=pk
    - إن كانت تقبل 'pk' نمرّر pk=pk
    - وإلا نحاول الاستدعاء بالـ agreement_id ثم pk.
    """
    view_func = getattr(views, view_attr, None)
    if view_func is None:
        # دالة غير موجودة، أرجع دالة ترمي خطأ واضحًا
        def _missing(*args, **kwargs):
            raise AttributeError(f"views.{view_attr} غير موجودة")
        return _missing

    def _wrapped(request, pk: int, **kwargs):
        try:
            params = inspect.signature(view_func).parameters
        except (TypeError, ValueError):
            # في حال الدالة مغلّفة بديكوريتر وفُقد التوقيع
            try:
                return view_func(request, agreement_id=pk, **kwargs)
            except TypeError:
                return view_func(request, pk=pk, **kwargs)

        if "agreement_id" in params:
            return view_func(request, agreement_id=pk, **kwargs)
        if "pk" in params:
            return view_func(request, pk=pk, **kwargs)

        # آخر محاولة: جرّب بالـ agreement_id ثم pk
        try:
            return view_func(request, agreement_id=pk, **kwargs)
        except TypeError:
            return view_func(request, pk=pk, **kwargs)

    return _wrapped


# =========================================================
# مسارات حسب الطلب (by-request) — تعتمد request_id
# =========================================================
urlpatterns = [
    # فتح/إنشاء اتفاقية انطلاقًا من الطلب
    path("by-request/<int:request_id>/open/", views.open_by_request, name="open_by_request"),
    # موافقة/رفض العميل على اتفاقية الطلب
    path("by-request/<int:request_id>/accept/", views.accept_by_request, name="accept_by_request"),
    path("by-request/<int:request_id>/reject/", views.reject_by_request, name="reject_by_request"),
    # طلب تمديد المهلة للاتفاقية
    path("<int:pk>/request-extension/", views.request_extension, name="request_extension"),
    # موافقة أو رفض العميل على طلب التمديد
    path("<int:pk>/approve-extension/", views.approve_extension, name="approve_extension"),
    path("<int:pk>/reject-extension/", views.reject_extension, name="reject_extension"),
]

# توافق خلفي: بعض القوالب القديمة كانت تمرّر pk بدل request_id
urlpatterns += [
    path("by-request/pk/<int:pk>/open/", lambda req, pk: views.open_by_request(req, request_id=pk), name="open_by_request_pk"),
    path("by-request/pk/<int:pk>/accept/", lambda req, pk: views.accept_by_request(req, request_id=pk), name="accept_by_request_pk"),
    path("by-request/pk/<int:pk>/reject/", lambda req, pk: views.reject_by_request(req, request_id=pk), name="reject_by_request_pk"),
]

# =========================================================
# مسارات الاتفاقية حسب المعرّف (agreement) — تدعم pk أو agreement_id
# =========================================================
if hasattr(views, "detail"):
    urlpatterns.append(path("<int:pk>/", _wrap_pk("detail"), name="detail"))


if hasattr(views, "edit"):
    urlpatterns.append(path("<int:pk>/edit/", _wrap_pk("edit"), name="edit"))

# إضافة مسار رفض الاتفاقية عبر pk (POST)
if hasattr(views, "reject"):
    urlpatterns.append(path("<int:pk>/reject/", _wrap_pk("reject"), name="reject"))

# دعم finalize_clauses أو finalize أيهما متوفر
_finalize_attr = "finalize_clauses" if hasattr(views, "finalize_clauses") else ("finalize" if hasattr(views, "finalize") else None)
if _finalize_attr:
    urlpatterns.append(path("<int:pk>/finalize-clauses/", _wrap_pk(_finalize_attr), name="finalize_clauses"))

# =========================================================
# مسارات المراحل (Milestones)
# =========================================================
# النمط المعتمد: يتضمن agreement_id ثم رقم المرحلة
if hasattr(views, "milestone_deliver"):
    urlpatterns.append(
        path("<int:agreement_id>/milestones/<int:milestone_id>/deliver/", views.milestone_deliver, name="milestone_deliver")
    )
if hasattr(views, "milestone_approve"):
    urlpatterns.append(
        path("<int:agreement_id>/milestones/<int:milestone_id>/approve/", views.milestone_approve, name="milestone_approve")
    )
if hasattr(views, "milestone_reject"):
    urlpatterns.append(
        path("<int:agreement_id>/milestones/<int:milestone_id>/reject/", views.milestone_reject, name="milestone_reject")
    )
if hasattr(views, "milestone_review"):
    urlpatterns.append(
        path("<int:agreement_id>/milestones/<int:milestone_id>/review/", views.milestone_review, name="milestone_review")
    )

# توافق خلفي (اختياري): مسارات قصيرة تعتمد milestone_id فقط — بأسماء مختلفة حتى لا تتعارض مع الأسماء المعتمدة
if hasattr(views, "milestone_deliver"):
    urlpatterns.append(
        path("milestone/<int:milestone_id>/deliver/", views.milestone_deliver, name="milestone_deliver_short")
    )
if hasattr(views, "milestone_approve"):
    urlpatterns.append(
        path("milestone/<int:milestone_id>/approve/", views.milestone_approve, name="milestone_approve_short")
    )
if hasattr(views, "milestone_reject"):
    urlpatterns.append(
        path("milestone/<int:milestone_id>/reject/", views.milestone_reject, name="milestone_reject_short")
    )
if hasattr(views, "milestone_review"):
    urlpatterns.append(
        path("milestone/<int:milestone_id>/review/", views.milestone_review, name="milestone_review_short")
    )
