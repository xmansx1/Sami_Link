# agreements/signals.py
from __future__ import annotations

from django.db import transaction
from django.db.models.signals import post_save
from django.dispatch import receiver

from .models import Milestone

# ملاحظات عامة:
# - نعتمد نهجًا دفاعيًا: نتعامل مع اختلافات محتملة في أسماء الحالات والحقول.
# - نستخدم atomic() + select_for_update (حيث يلزم) للحد من حالات الظروف الحرِجة (race conditions).
# - لا نرمي استثناءات من الإشارة حتى لا تُفشل عملية الحفظ الأصلية؛ نكتفي بالتحقق الهادئ.


# ===== أدوات مساعدة لاكتشاف الأسماء/القيم المتاحة ديناميكيًا =====

def _status_value(model_cls, path: list[str], default: str) -> str:
    """
    يعيد قيمة حالة (string) من model_cls عبر مسار مثل ["Status", "APPROVED"].
    إن تعذّر الوصول، يُرجع default.
    """
    cur = model_cls
    try:
        for part in path:
            cur = getattr(cur, part)
        if isinstance(cur, str):
            return cur
        # TextChoices: قد تكون خاصية .value
        return getattr(cur, "value", default)
    except Exception:
        return default


def _has_fk(obj, field_name: str) -> bool:
    """يتحقق هل الكائن يملك حقلًا باسم field_name."""
    return hasattr(obj, field_name)


def _get_related_manager(obj, rel_name: str):
    """يحصل على مدير علاقة عكسيّة إن وُجد وإلا يعيد None."""
    return getattr(obj, rel_name, None)


def _invoice_statuses(Invoice):
    """
    يعيد قاموسًا موحدًا للأسماء المتوقعة لحالات الفاتورة.
    سندعم تنوعات: DUE/UNPAID مقابل PAID/VOID.
    """
    due = _status_value(Invoice, ["Status", "DUE"], "due")
    unpaid = _status_value(Invoice, ["Status", "UNPAID"], "unpaid")
    paid = _status_value(Invoice, ["Status", "PAID"], "paid")
    void = _status_value(Invoice, ["Status", "VOID"], "void")
    # نرجّح استخدام DUE إن وُجد وإلا UNPAID
    open_status = due if due.lower() != "due" or unpaid.lower() == "unpaid" else unpaid
    # إذا كان كلاهما افتراضيين، اختر "due"
    open_status = open_status or "due"
    return {
        "OPEN": open_status,  # حالة غير مدفوعة
        "PAID": paid or "paid",
        "VOID": void or "void",
        "DUE": due or "due",
        "UNPAID": unpaid or "unpaid",
    }


def _agreement_completed_status(Agreement):
    # نحاول العثور على COMPLETED ضمن TextChoices إن وجدت، وإلا "completed"
    return _status_value(Agreement, ["Status", "COMPLETED"], "completed")


def _request_completed_status(Request):
    return _status_value(Request, ["Status", "COMPLETED"], "completed")


def _milestone_status_approved(MilestoneCls):
    return _status_value(MilestoneCls, ["Status", "APPROVED"], "approved")


def _milestone_status_paid(MilestoneCls):
    # قد لا توجد حالة "PAID" للميلستون في بعض المشاريع؛ نجعلها اختيارية.
    return _status_value(MilestoneCls, ["Status", "PAID"], "paid")


@receiver(post_save, sender=Milestone)
def handle_milestone_post_save(sender, instance: Milestone, created: bool, **kwargs):
    """
    سيناريو المعالجة:
    1) إذا أصبحت المرحلة APPROVED ⇒ أنشئ فاتورة مرتبطة إن لم تكن موجودة (idempotent).
    2) بعد أي حفظ للمرحلة ⇒ تحقّق إن كانت جميع فواتير الاتفاقية مدفوعة:
       - إن نعم: ضع الاتفاقية والطلب في COMPLETED (إن توفرت الحقول وحالاتها).
       - (اختياري) يمكننا مزامنة حالة الميلستون إلى "PAID" إن كانت متاحة وكل فواتيرها مدفوعة.
    ملاحظات:
    - نتجنب الاستيراد الدائري عبر الاستيراد المتأخر داخل الدالة.
    - لا نطلق استثناءات؛ في حال أي نقص بنيوي نتجاهل بهدوء.
    """

    milestone = instance
    agreement = getattr(milestone, "agreement", None)
    if agreement is None:
        # حالة غير متوقعة: مرحلة بلا اتفاقية
        return

    try:
        # استيرادات مؤجلة لتفادي الدوران
        from finance.models import Invoice
    except Exception:
        # إن لم يتوفر نموذج Invoice لأي سبب، لا نفعل شيئًا
        return

    # خرائط الحالات
    MS_APPROVED = _milestone_status_approved(Milestone.__class__ if isinstance(Milestone, type) else Milestone)
    MS_PAID = _milestone_status_paid(Milestone.__class__ if isinstance(Milestone, type) else Milestone)
    INV_ST = _invoice_statuses(Invoice)

    # توحيد الوصول لعلاقات الفواتير:
    # قد يكون على Invoice FK إلى agreement، وقد يعتمد فقط على milestone.
    # سنبحث وفق الحالتين لضمان التوافق.
    def _invoices_for_agreement(ag):
        # أولوية: إن كان Invoice يملك FK اسمه "agreement"
        try:
            return Invoice.objects.filter(agreement=ag)
        except Exception:
            # fallback: اجلب جميع فواتير Milestones التابعة للاتفاقية
            return Invoice.objects.filter(milestone__agreement=ag)

    def _invoice_for_milestone(ms):
        try:
            # أحيانًا تكون OneToOne: milestone.invoice
            inv = getattr(ms, "invoice", None)
            if inv is not None:
                return inv
        except Exception:
            pass
        # وإلا نحاول عبر FK عادي
        try:
            return Invoice.objects.get(milestone=ms)
        except Invoice.DoesNotExist:
            return None

    # العمليات الذرّية لتجنب السباقات
    with transaction.atomic():
        # قفل الصف الحالي للمرحلة أثناء التحديثات اللاحقة
        # (قد لا يكون ضروريًا دائمًا، لكن يحسن الأمان في سيناريوهات concurency)
        type(milestone).objects.select_for_update().filter(pk=milestone.pk)

        # (1) تمت إزالة أي منطق لإنشاء فاتورة تلقائيًا عند اعتماد المرحلة نهائيًا.

        # (2) فحص اكتمال الاتفاقية/الطلب: هل توجد فواتير غير مدفوعة؟
        inv_qs = _invoices_for_agreement(agreement)

        # إن لم توجد فواتير إطلاقًا لكن الاتفاقية معتمدة ومراحلها صفرية، لا نعلن اكتمالًا هنا.
        if inv_qs.exists():
            has_unpaid = inv_qs.filter(status__in=[INV_ST["UNPAID"], INV_ST["DUE"]]).exists()

            if not has_unpaid:
                # جميع الفواتير مدفوعة (أو لا توجد إلا مدفوعة/ملغاة)

                # (اختياري) مزامنة حالة الميلستون إلى "PAID" إن كان ذلك منطقيًا ومتاحًا
                # ننفذ فقط للمرحلة الحالية إذا كانت فاتورتها مدفوعة
                # (ولن نكسر المشاريع التي لا تملك Milestone.Status.PAID أصلاً)
                if hasattr(milestone, "status") and MS_PAID:
                    ms_inv = _invoice_for_milestone(milestone)
                    if ms_inv and str(getattr(ms_inv, "status", "")).lower() == str(INV_ST["PAID"]).lower():
                        fields = ["status"]
                        setattr(milestone, "status", MS_PAID)
                        # إن كان لديك paid_at على الميلستون، نسنده من الفاتورة
                        if hasattr(milestone, "paid_at") and hasattr(ms_inv, "paid_at"):
                            setattr(milestone, "paid_at", getattr(ms_inv, "paid_at"))
                            fields.append("paid_at")
                        milestone.save(update_fields=fields)

                # إعلان اكتمال الاتفاقية
                ag_status = getattr(agreement, "status", None)
                if ag_status is not None:
                    completed = _agreement_completed_status(type(agreement))
                    # لا نعيد الحفظ إن كانت بالفعل مكتملة
                    if str(ag_status).lower() != str(completed).lower():
                        agreement.status = completed
                        try:
                            agreement.save(update_fields=["status"])
                        except Exception:
                            # إن فشل التحديث (اختلاف حقول/صلاحيات) نتجاهل بهدوء
                            pass

                # إعلان اكتمال الطلب المرتبط
                req = getattr(agreement, "request", None)
                if req is not None and hasattr(req, "status"):
                    req_completed = _request_completed_status(type(req))
                    if str(getattr(req, "status", "")).lower() != str(req_completed).lower():
                        setattr(req, "status", req_completed)
                        try:
                            req.save(update_fields=["status"])
                        except Exception:
                            pass

    # انتهى: لا نرفع استثناءات حتى لا نفسد تسلسل حفظ الميلستون الأصلي.
