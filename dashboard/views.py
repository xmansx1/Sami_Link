from __future__ import annotations

import logging
from datetime import date, timedelta
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Q, Count, Sum, Value as V, DecimalField
from django.db.models.functions import Coalesce
from django.shortcuts import render, redirect
from django.urls import reverse
from django.utils.dateparse import parse_date

logger = logging.getLogger(__name__)

User = get_user_model()

# ====================== نماذج اختيارية (قد لا تكون مثبتة) ======================

try:
    from marketplace.models import Request, Offer  # type: ignore
except Exception:  # pragma: no cover
    Request = None  # type: ignore
    Offer = None  # type: ignore

try:
    from agreements.models import Agreement  # type: ignore
except Exception:  # pragma: no cover
    Agreement = None  # type: ignore

try:
    from finance.models import Invoice  # type: ignore
except Exception:  # pragma: no cover
    Invoice = None  # type: ignore

try:
    from disputes.models import Dispute  # type: ignore
except Exception:  # pragma: no cover
    Dispute = None  # type: ignore

try:
    from website.models import SiteSetting, ContactMessage
except Exception:
    SiteSetting = None
    ContactMessage = None


# ====================== أمان وصلاحيات ======================


def _is_admin(user) -> bool:
    """
    تحقّق من كون المستخدم مديرًا/مالية/سوبر يوزر (RBAC مبسّط).
    يسمح للأدوار:
    - superuser / staff
    - role in [admin, gm, manager, finance]
    - أو وجود العلم is_system_admin إن وجد.
    """
    if not getattr(user, "is_authenticated", False):
        return False

    # سوبر / ستاف يمر دائمًا
    if getattr(user, "is_superuser", False) or getattr(user, "is_staff", False):
        return True

    # أدوار من حقل role (مدير عام / أدمن / مالية ...)
    role = getattr(user, "role", "") or ""
    if role in ["admin", "gm", "manager", "finance"]:
        return True

    # فلاغ إضافي (لو موجود عندك)
    return bool(getattr(user, "is_system_admin", False))


def _require_admin(request) -> bool:
    """إظهار رسالة ومنع الوصول إن لم يكن المستخدم مديرًا."""
    if not _is_admin(request.user):
        messages.error(request, "ليس لديك صلاحية الوصول لهذه الصفحة.")
        return False
    return True


# ====================== أدوات مساعدة عامة ======================


def _paginate(request, qs, per_page: int = 20):
    """مساعد لترقيم الصفحات بشكل موحّد."""
    paginator = Paginator(qs, per_page)
    page_number = request.GET.get("page") or 1
    return paginator.get_page(page_number)


def _safe_parse_date(value: str | None):
    """محاولة تحويل قيمة نصية إلى تاريخ بدون كسر."""
    if not value:
        return None
    try:
        return parse_date(value)
    except Exception:
        return None


def _daterange(request):
    """
    فلاتر زمنية موحّدة: ?start=YYYY-MM-DD&end=YYYY-MM-DD
    افتراضي: آخر 30 يومًا، مع تصحيح start<=end.
    """
    today = date.today()
    d_to = _safe_parse_date(request.GET.get("end")) or today
    d_from = _safe_parse_date(request.GET.get("start")) or (d_to - timedelta(days=30))
    if d_from > d_to:
        d_from, d_to = d_to, d_from
    return d_from, d_to


def _safe_reverse(name: str, **kwargs):
    """
    إرجاع رابط عكسي إن وُجد، وإلا None بدون رفع استثناء.
    مفيد للوحات الإدارة حتى لا تنكسر الصفحة إن غاب مسار.
    """
    try:
        return reverse(name, kwargs=kwargs) if kwargs else reverse(name)
    except Exception:
        return None


def _money(value):
    """تطبيع القيم المالية إلى Decimal آمن."""
    return value if value is not None else Decimal("0.00")


def _model_has_field(model, field: str) -> bool:
    """التحقق من وجود حقل في نموذج معيّن."""
    if not model:
        return False
    try:
        model._meta.get_field(field)  # type: ignore[attr-defined]
        return True
    except Exception:
        return False


def _pick_field(model, candidates: list[str]) -> str | None:
    """اختر أول حقل موجود من قائمة مرشّحة (مثلاً created_at / created...)."""
    for f in candidates:
        if _model_has_field(model, f):
            return f
    return None


def _only_fields(model, base: list[str]) -> list[str]:
    """إرجاع قائمة حقول موجودة فعليًا لاستخدامها مع .only()."""
    return [f for f in base if f and _model_has_field(model, f)]


# ====================== لوحة المدير ======================


@login_required
def admin_dashboard(request):
    """
    لوحة تحكم المدير العام للمنصّة:
    - مؤشرات المستخدمين
    - مؤشرات الفواتير (إجمالي / مدفوع / غير مدفوع / ضريبة / عمولة / مجمّد)
    - آخر الفواتير / الطلبات / الاتفاقيات / النزاعات
    - روابط سريعة موحّدة للقوالب.
    """
    if not _require_admin(request):
        return redirect("website:home")

    today = date.today()

    # روابط آمنة (حتى لو غاب مسار لا تنكسر اللوحة)
    urls = {
        "requests": _safe_reverse("dashboard:all_requests")
                    or _safe_reverse("dashboard:requests_list")
                    or _safe_reverse("marketplace:request_list"),
        "invoices": _safe_reverse("finance:invoice_list") or _safe_reverse("finance:home"),
        "agreements": _safe_reverse("agreements:list"),
        "offers": _safe_reverse("marketplace:offers_list"),
        "disputes": _safe_reverse("disputes:list"),
        "clients": _safe_reverse("dashboard:clients_list"),
        "employees": _safe_reverse("dashboard:employees_list"),
        "users": _safe_reverse("dashboard:employees_list")
                 or _safe_reverse("accounts:user_list"),
    }

    # إجراءات سريعة (تُعرض أعلى اللوحة في القالب)
    quick = [
        {"label": "جميع الطلبات", "url": urls["requests"], "icon": "fa-list"},
        {"label": "الفواتير", "url": urls["invoices"], "icon": "fa-receipt"},
        {"label": "الاتفاقيات", "url": urls["agreements"], "icon": "fa-file-signature"},
        {"label": "العروض", "url": urls["offers"], "icon": "fa-handshake"},
        {"label": "النزاعات", "url": urls["disputes"], "icon": "fa-scale-balanced"},
        {"label": "العملاء", "url": urls["clients"], "icon": "fa-user-group"},
        {"label": "الموظفون", "url": urls["employees"], "icon": "fa-users-gear"},
    ]
    # استبعاد العناصر التي لا تملك رابطًا فعليًا
    quick = [q for q in quick if q["url"]]

    # إعدادات الموقع
    site_setting_id = None
    if SiteSetting:
        s = SiteSetting.objects.first()
        if not s:
            s = SiteSetting.objects.create()
        site_setting_id = s.id

    ctx: dict[str, object] = {
        "today": today,
        "from": None,
        "to": None,
        "urls": urls,
        "quick": quick,
        "ops_alerts": [],
        "site_setting_id": site_setting_id,
    }

    # ---- المستخدمون
    try:
        users_qs = User.objects.all().only(
            *_only_fields(User, ["id", "email", "name", "date_joined", "is_staff", "role"])
        )
        ctx["users_total"] = users_qs.count()
        ctx["users_staff"] = users_qs.filter(is_staff=True).count()
        ctx["role_counts"] = (
            list(
                users_qs.values("role")
                .annotate(c=Count("id"))
                .order_by("-c")
            )
            if _model_has_field(User, "role")
            else []
        )
    except Exception as e:
        logger.exception("Users stats error: %s", e)
        ctx["users_total"] = 0
        ctx["users_staff"] = 0
        ctx["role_counts"] = []

    # ---- الفواتير
    ctx["inv_totals"] = {
        "total": Decimal("0.00"),
        "paid": Decimal("0.00"),
        "unpaid": Decimal("0.00"),
        "vat_total": Decimal("0.00"),
        "platform_fee_total": Decimal("0.00"),
        "disputed_total": Decimal("0.00"),
    }
    ctx["inv_recent"] = []
    ctx["overdue_count"] = 0

    try:
        if Invoice is not None and Request is not None:
            from finance.views import compute_agreement_totals  # استدعاء دالة الحساب الرسمية

            invs = Invoice.objects.all().select_related("agreement", "agreement__request")

            paid_val = getattr(getattr(Invoice, "Status", None), "PAID", "paid")
            unpaid_val = getattr(getattr(Invoice, "Status", None), "UNPAID", "unpaid")
            disputed_val = getattr(getattr(Request, "Status", None), "DISPUTED", "disputed")

            total = Decimal("0.00")
            paid_total = Decimal("0.00")
            unpaid_total = Decimal("0.00")
            vat_total = Decimal("0.00")
            fee_total = Decimal("0.00")
            disputed_total = Decimal("0.00")

            for inv in invs:
                # FIX: نعتمد على inv.amount باعتباره الإجمالي (Grand Total) لتجنب ازدواجية الضريبة
                client_total = _money(getattr(inv, "amount", None))
                
                vat_percent = _money(getattr(inv, "vat_percent", 0))
                fee_percent = _money(getattr(inv, "platform_fee_percent", 0))
                
                vat_amount = Decimal("0.00")
                fee_amount = Decimal("0.00")
                P = Decimal("0.00")

                if client_total > 0:
                    # P = G / (1+V)
                    P = client_total / (Decimal("1") + vat_percent)
                    vat_amount = client_total - P
                    fee_amount = P * fee_percent
                
                # إذا كانت القيمة صفرية، نحاول الحساب من الاتفاقية (fallback)
                elif getattr(inv, "agreement", None):
                    ag = getattr(inv, "agreement", None)
                    try:
                        breakdown = compute_agreement_totals(ag)
                        client_total = _money(breakdown.get("grand_total"))
                        vat_amount = _money(breakdown.get("vat_amount"))
                        fee_amount = _money(breakdown.get("platform_fee"))
                        P = _money(breakdown.get("P"))
                    except Exception:
                        pass

                total += client_total
                vat_total += vat_amount
                fee_total += fee_amount

                if getattr(inv, "status", None) == paid_val:
                    paid_total += client_total
                elif getattr(inv, "status", None) == unpaid_val:
                    unpaid_total += client_total

                # مبالغ مجمّدة في النزاعات: نعتمد صافي الموظف
                ag = getattr(inv, "agreement", None)
                if ag and getattr(getattr(ag, "request", None), "status", None) == disputed_val:
                    net_emp = P - fee_amount
                    if net_emp < 0:
                        net_emp = Decimal("0.00")
                    disputed_total += net_emp

            ctx["inv_totals"] = {
                "total": total,
                "paid": paid_total,
                "unpaid": unpaid_total,
                "vat_total": vat_total,
                "platform_fee_total": fee_total,
                "disputed_total": disputed_total,
            }

            issued_field = _pick_field(Invoice, ["issued_at", "created_at", "created"])
            if issued_field:
                invs = invs.order_by(f"-{issued_field}")
            else:
                invs = invs.order_by("-id")

            ctx["inv_recent"] = list(invs[:10])

            # فواتير متأخرة (أقدم من 3 أيام وغير مدفوعة)
            if issued_field:
                overdue = invs.filter(
                    status=unpaid_val,
                    **{issued_field + "__lt": today - timedelta(days=3)},
                ).count()
                ctx["overdue_count"] = overdue
                if overdue:
                    ctx["ops_alerts"].append(f"هناك {overdue} فاتورة متأخرة عن السداد.")
    except Exception as e:
        logger.exception("Invoices stats error: %s", e)

    # ---- الاتفاقيات + العروض + الطلبات
    ctx["agreements_total"] = 0
    ctx["agreements_recent"] = []
    ctx["offers_total"] = 0
    ctx["req_total"] = 0
    ctx["req_recent"] = []

    try:
        if Agreement is not None:
            ag_qs = Agreement.objects.select_related("request", "employee").all()
            created_field = _pick_field(Agreement, ["created_at", "approved_at", "created"])
            ag_qs = ag_qs.order_by(f"-{created_field}" if created_field else "-id")
            ctx["agreements_total"] = ag_qs.count()
            ctx["agreements_recent"] = list(ag_qs[:5])

        if Offer is not None:
            ctx["offers_total"] = Offer.objects.count()

        if Request is not None:
            req_qs = Request.objects.select_related("client").all()
            created_field = _pick_field(Request, ["created_at", "submitted_at", "created"])
            req_qs = req_qs.order_by(f"-{created_field}" if created_field else "-id")
            ctx["req_total"] = req_qs.count()
            ctx["req_recent"] = list(req_qs[:5])

            # --- الطلبات المتأخرة (Delayed Requests) ---
            # الطلب قيد التنفيذ + يوجد اتفاقية + (تاريخ البدء + المدة) < اليوم
            delayed_requests = []
            in_progress_reqs = req_qs.filter(status="in_progress", agreement__isnull=False)
            
            for r in in_progress_reqs:
                ag = r.agreement
                if ag.started_at and ag.duration_days:
                    deadline = ag.started_at + timedelta(days=ag.duration_days)
                    if deadline < today:
                        # حساب أيام التأخير
                        overdue_days = (today - deadline).days
                        # إضافة خاصية مؤقتة للعرض
                        r.overdue_days = overdue_days
                        delayed_requests.append(r)
            
            ctx["delayed_requests"] = delayed_requests
            if delayed_requests:
                ctx["ops_alerts"].append(f"هناك {len(delayed_requests)} مشروع متأخر عن التسليم.")

    except Exception as e:
        logger.exception("Agreements/Offers/Requests stats error: %s", e)

    # ---- النزاعات
    ctx["disputes_recent"] = []
    try:
        if Dispute is not None:
            d_qs = Dispute.objects.all()
            opened_field = _pick_field(Dispute, ["opened_at", "created_at", "created"])
            d_qs = d_qs.order_by(f"-{opened_field}" if opened_field else "-id")
            ctx["disputes_recent"] = list(d_qs[:10])
    except Exception as e:
        logger.exception("Disputes stats error: %s", e)

    return render(request, "dashboard/admin_dashboard.html", ctx)


# ====================== جميع الطلبات (قالب مخصص) ======================


@login_required
def all_requests_view(request):
    """
    عرض جميع الطلبات في جدول بسيط مع بحث نصّي عام.
    يستخدم القالب: dashboard/all_requests.html
    """
    if not _require_admin(request):
        return redirect("website:home")

    if Request is None:
        messages.warning(request, "تطبيق الطلبات غير متاح.")
        return render(
            request,
            "dashboard/all_requests.html",
            {"page_obj": None, "q": "", "today": date.today()},
        )

    q = (request.GET.get("q") or "").strip()

    req_qs = Request.objects.select_related("client").all()

    if q:
        # بحث مبدئي بالعنوان فقط (القالب بسيط)
        req_qs = req_qs.filter(title__icontains=q)

    created_field = _pick_field(Request, ["created_at", "submitted_at", "created"])
    if created_field:
        req_qs = req_qs.order_by(f"-{created_field}")
    else:
        req_qs = req_qs.order_by("-id")

    page_obj = _paginate(request, req_qs, per_page=30)
    ctx = {
        "page_obj": page_obj,
        "q": q,
        "today": date.today(),
    }
    return render(request, "dashboard/all_requests.html", ctx)


# ====================== إدارة الموظفين ======================


@login_required
def employees_list(request):
    """
    قائمة الموظفين (role=employee/tech) مع بحث بالاسم/الإيميل.
    يستخدم القالب: dashboard/employees.html
    """
    if not _require_admin(request):
        return redirect("website:home")

    q = (request.GET.get("q") or "").strip()
    qs = User.objects.all()

    # قصر النتائج على الموظفين فقط إن وجد حقل role
    if _model_has_field(User, "role"):
        qs = qs.filter(role__in=["employee", "tech"])

    if q:
        filters = Q(username__icontains=q) | Q(email__icontains=q)
        if _model_has_field(User, "first_name"):
            filters |= Q(first_name__icontains=q)
        if _model_has_field(User, "last_name"):
            filters |= Q(last_name__icontains=q)
        if _model_has_field(User, "name"):
            filters |= Q(name__icontains=q)
        qs = qs.filter(filters)

    fields = _only_fields(User, ["id", "email", "name", "date_joined"])
    if not fields:
        fields = ["id"]

    order = "-date_joined" if _model_has_field(User, "date_joined") else "-id"
    qs = qs.only(*fields).order_by(order)

    page_obj = _paginate(request, qs, per_page=25)
    ctx = {
        "page_obj": page_obj,
        "q": q,
        "today": date.today(),
    }
    return render(request, "dashboard/employees.html", ctx)


# ====================== إدارة العملاء ======================


@login_required
def clients_list(request):
    """
    قائمة العملاء (role=client) مع بحث بالاسم/الإيميل.
    يستخدم القالب: dashboard/clients.html
    """
    if not _require_admin(request):
        return redirect("website:home")

    q = (request.GET.get("q") or "").strip()
    qs = User.objects.all()

    if _model_has_field(User, "role"):
        qs = qs.filter(role="client")

    if q:
        filters = Q(username__icontains=q) | Q(email__icontains=q)
        if _model_has_field(User, "first_name"):
            filters |= Q(first_name__icontains=q)
        if _model_has_field(User, "last_name"):
            filters |= Q(last_name__icontains=q)
        if _model_has_field(User, "name"):
            filters |= Q(name__icontains=q)
        qs = qs.filter(filters)

    fields = _only_fields(User, ["id", "email", "name", "date_joined"])
    if not fields:
        fields = ["id"]

    order = "-date_joined" if _model_has_field(User, "date_joined") else "-id"
    qs = qs.only(*fields).order_by(order)

    page_obj = _paginate(request, qs, per_page=25)
    ctx = {
        "page_obj": page_obj,
        "q": q,
        "today": date.today(),
    }
    return render(request, "dashboard/clients.html", ctx)


# ====================== إدارة الطلبات (متقدمة) ======================


@login_required
def requests_list(request):
    """
    إدارة الطلبات مع فلاتر (بحث + حالة + نطاق زمني).
    يستخدم القالب: dashboard/requests.html
    """
    if not _require_admin(request):
        return redirect("website:home")

    if Request is None:
        messages.warning(request, "تطبيق الطلبات غير متاح.")
        return render(
            request,
            "dashboard/requests.html",
            {"page_obj": None, "q": "", "state": "", "today": date.today()},
        )

    q = (request.GET.get("q") or "").strip()
    wanted_state = (request.GET.get("state") or "").strip()
    d_from, d_to = _daterange(request)

    qs = Request.objects.select_related("client").all()

    if q:
        # البحث في العنوان واسم العميل فقط (الحقل name في client)
        qs = qs.filter(
            Q(title__icontains=q) |
            Q(client__name__icontains=q)
        )

    req_state = _pick_field(Request, ["state", "status", "phase"])
    if wanted_state and req_state:
        qs = qs.filter(**{req_state: wanted_state})

    req_created = _pick_field(Request, ["created_at", "created", "submitted_at"])
    if req_created and d_from and d_to:
        qs = qs.filter(
            **{
                f"{req_created}__date__gte": d_from,
                f"{req_created}__date__lte": d_to,
            }
        )

    qs = qs.order_by(f"-{req_created}" if req_created else "-id")

    page_obj = _paginate(request, qs, per_page=25)

    # تمرير جميع الحالات المتاحة
    state_choices = []
    if hasattr(Request, "Status"):
        state_choices = list(Request.Status.choices)
    elif hasattr(Request, "status") and hasattr(Request, "_meta"):
        field = Request._meta.get_field("status")
        if hasattr(field, "choices"):
            state_choices = list(field.choices)

    ctx = {
        "page_obj": page_obj,
        "q": q,
        "state": wanted_state,
        "start": d_from,
        "end": d_to,
        "today": date.today(),
        "state_choices": state_choices,
    }
    return render(request, "dashboard/requests.html", ctx)


# ====================== إدارة النزاعات ======================


@login_required
def disputes_list(request):
    """
    إدارة النزاعات - تم نقلها إلى تطبيق disputes.
    """
    if not _require_admin(request):
        return redirect("website:home")
    
    # إعادة توجيه مع الحفاظ على المعاملات
    query_string = request.META.get("QUERY_STRING", "")
    url = reverse("disputes:list")
    if query_string:
        url = f"{url}?{query_string}"
    return redirect(url)


@login_required
def contact_messages_list(request):
    if not _is_admin(request.user):
        messages.error(request, "غير مصرح لك بالدخول لهذه الصفحة.")
        return redirect("website:home")

    if not ContactMessage:
        messages.warning(request, "نموذج الرسائل غير متوفر.")
        return redirect("dashboard:admin_dashboard")

    # --- Handle Actions (POST) ---
    if request.method == "POST":
        action = request.POST.get("action")
        msg_id = request.POST.get("msg_id")
        selected_ids = request.POST.getlist("selected_ids")

        if action == "delete" and msg_id:
            ContactMessage.objects.filter(id=msg_id).delete()
            messages.success(request, "تم حذف الرسالة بنجاح.")
        
        elif action == "mark_read" and msg_id:
            ContactMessage.objects.filter(id=msg_id).update(is_read=True)
            messages.success(request, "تم تحديد الرسالة كمقروءة.")

        elif action == "mark_unread" and msg_id:
            ContactMessage.objects.filter(id=msg_id).update(is_read=False)
            messages.success(request, "تم تحديد الرسالة كغير مقروءة.")

        elif action == "bulk_delete" and selected_ids:
            count, _ = ContactMessage.objects.filter(id__in=selected_ids).delete()
            messages.success(request, f"تم حذف {count} رسالة بنجاح.")

        elif action == "bulk_read" and selected_ids:
            updated = ContactMessage.objects.filter(id__in=selected_ids).update(is_read=True)
            messages.success(request, f"تم تحديد {updated} رسالة كمقروءة.")

        elif action == "bulk_unread" and selected_ids:
            updated = ContactMessage.objects.filter(id__in=selected_ids).update(is_read=False)
            messages.success(request, f"تم تحديد {updated} رسالة كغير مقروءة.")
        
        return redirect(request.get_full_path())

    # --- Filtering & Search ---
    msgs_qs = ContactMessage.objects.all().order_by("-created_at")
    
    # Search
    q = request.GET.get("q", "").strip()
    if q:
        msgs_qs = msgs_qs.filter(
            Q(name__icontains=q) | 
            Q(email__icontains=q) | 
            Q(subject__icontains=q) |
            Q(message__icontains=q)
        )

    # Status Filter
    status_filter = request.GET.get("status", "all")
    if status_filter == "read":
        msgs_qs = msgs_qs.filter(is_read=True)
    elif status_filter == "unread":
        msgs_qs = msgs_qs.filter(is_read=False)

    # Counts for tabs
    all_count = ContactMessage.objects.count()
    unread_count = ContactMessage.objects.filter(is_read=False).count()
    read_count = ContactMessage.objects.filter(is_read=True).count()

    # Pagination
    paginator = Paginator(msgs_qs, 20)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    context = {
        "page_obj": page_obj,
        "q": q,
        "status_filter": status_filter,
        "all_count": all_count,
        "unread_count": unread_count,
        "read_count": read_count,
    }
    return render(request, "dashboard/contact_messages.html", context)
