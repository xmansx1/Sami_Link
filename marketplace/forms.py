from __future__ import annotations
from django import forms
from .models import Request, Offer, Note, Review
# نموذج إلغاء العرض
class OfferCancelForm(forms.ModelForm):
    class Meta:
        model = Offer
        fields = ["modification_reason"]
        labels = {"modification_reason": "سبب الإلغاء"}
        widgets = {
            "modification_reason": forms.Textarea(attrs={"rows": 2, "class": "input", "placeholder": "سبب الإلغاء (إجباري)"}),
        }

    def clean_modification_reason(self):
        reason = _clean_text(self.cleaned_data.get("modification_reason"))
        if not reason:
            raise forms.ValidationError("يرجى كتابة سبب الإلغاء.")
        return reason

# نموذج طلب تمديد العرض
class OfferExtensionForm(forms.ModelForm):
    class Meta:
        model = Offer
        fields = ["extension_requested_days", "extension_reason"]
        labels = {
            "extension_requested_days": "عدد أيام التمديد المطلوبة",
            "extension_reason": "سبب طلب التمديد",
        }
        widgets = {
            "extension_requested_days": forms.NumberInput(attrs={"min": 1, "class": "input", "placeholder": "عدد الأيام"}),
            "extension_reason": forms.Textarea(attrs={"rows": 2, "class": "input", "placeholder": "سبب التمديد (إجباري)"}),
        }

    def clean_extension_requested_days(self):
        days = self.cleaned_data.get("extension_requested_days")
        if not days or days < 1:
            raise forms.ValidationError("يرجى إدخال عدد أيام التمديد (رقم موجب).")
        return days

    def clean_extension_reason(self):
        reason = _clean_text(self.cleaned_data.get("extension_reason"))
        if not reason:
            raise forms.ValidationError("يرجى كتابة سبب التمديد.")
        return reason
# marketplace/forms.py
from django import forms
from .models import Request, Offer, Note
class OfferEditForm(forms.ModelForm):
    class Meta:
        model = Offer
        fields = [
            "modified_price",
            "modified_duration_days",
            "modification_reason",
            "note",
        ]
        widgets = {
            "modified_price": forms.NumberInput(
                attrs={"min": 1, "step": "0.5", "class": "input", "placeholder": "السعر بعد التعديل"}
            ),
            "modified_duration_days": forms.NumberInput(
                attrs={"min": 1, "class": "input", "placeholder": "المدة بعد التعديل بالأيام"}
            ),
            "modification_reason": forms.Textarea(
                attrs={"rows": 2, "class": "input", "placeholder": "سبب التعديل"}
            ),
            "note": forms.Textarea(
                attrs={"rows": 2, "class": "input", "placeholder": "ملاحظات إضافية (اختياري)"}
            ),
        }
# marketplace/forms.py
from django import forms
from django.contrib.auth import get_user_model
from django.utils.html import strip_tags
from django.utils.text import Truncator

from .models import Request, Offer, Note

User = get_user_model()


# ---------------------------------------------
# أدوات تنظيف نصوص آمنة
# ---------------------------------------------
def _clean_text(v: str | None) -> str:
    """
    تنظيف بسيط وآمن للنصوص:
    - إزالة HTML لمنع XSS في الحقول النصية.
    - تقليم المسافات الزائدة وطيّها.
    - يعيد سلسلة فارغة عند None.
    """
    if v is None:
        return ""
    v = strip_tags(v)
    v = v.strip()
    return v


# ---------------------------------------------
# نموذج إنشاء طلب (العميل)
# ---------------------------------------------
class MultipleFileInput(forms.ClearableFileInput):
    allow_multiple_selected = True

class RequestCreateForm(forms.ModelForm):
    attachments = forms.FileField(
        required=False,
        widget=MultipleFileInput(attrs={"class": "file-input"}),
        label="مرفقات (اختياري)",
        help_text="يمكنك تحديد أكثر من ملف."
    )

    class Meta:
        model = Request
        fields = ["title", "details", "estimated_duration_days", "estimated_price", "links"]
        labels = {
            "title": "العنوان",
            "details": "التفاصيل",
            "estimated_duration_days": "المدة التقديرية (أيام)",
            "estimated_price": "السعر التقريبي",
            "links": "روابط (اختياري)",
        }
        widgets = {
            "title": forms.TextInput(attrs={"class": "input", "placeholder": "عنوان الطلب", "maxlength": 160}),
            "details": forms.Textarea(attrs={"class": "input", "rows": 5, "placeholder": "وصف مختصر للعمل المطلوب"}),
            "estimated_duration_days": forms.NumberInput(attrs={"class": "input", "min": 1}),
            "estimated_price": forms.NumberInput(attrs={"class": "input", "step": "0.01", "min": 0}),
            "links": forms.Textarea(attrs={"class": "input", "rows": 2, "placeholder": "روابط (اختياري)", "maxlength": 2000}),
        }
        help_texts = {
            "links": "ضع سطرًا لكل رابط إن وُجد (GitHub, Drive, Figma…).",
        }

    def clean_title(self) -> str:
        title = _clean_text(self.cleaned_data.get("title"))
        if not title:
            raise forms.ValidationError("يرجى إدخال عنوان الطلب.")
        return Truncator(title).chars(160)

    def clean_details(self) -> str:
        details = _clean_text(self.cleaned_data.get("details"))
        return Truncator(details).chars(5000)

    def clean_estimated_duration_days(self) -> int:
        days = self.cleaned_data.get("estimated_duration_days")
        if not days or days < 1:
            raise forms.ValidationError("المدة التقديرية يجب أن تكون رقمًا موجبًا.")
        return days

    def clean_estimated_price(self):
        price = self.cleaned_data.get("estimated_price")
        if price is None or price < 0:
            raise forms.ValidationError("السعر التقريبي يجب أن يكون صفرًا أو رقمًا موجبًا.")
        return price

    def clean_links(self) -> str:
        links = _clean_text(self.cleaned_data.get("links"))
        return Truncator(links).chars(2000)


# ---------------------------------------------
# نموذج تقديم عرض (الموظف)
# ---------------------------------------------
class OfferCreateForm(forms.ModelForm):
    """
    - يعتمد على ضبط instance.request و instance.employee من الـ view.
    - بديلًا عن ذلك، يقبل kwargs: request=..., employee=... ليضبطهما آمنًا.
    - يمنع تقديم أكثر من عرض (PENDING) لنفس (request, employee).
    """
    class Meta:
        model = Offer
        fields = ["note", "proposed_duration_days", "proposed_price"]
        labels = {
            "note": "تفاصيل العرض",
            "proposed_duration_days": "المدة المقترحة (أيام)",
            "proposed_price": "السعر المقترح",
        }
        widgets = {
            "note": forms.Textarea(attrs={"class": "input", "rows": 4, "placeholder": "تفاصيل عرضك"}),
            "proposed_duration_days": forms.NumberInput(attrs={"class": "input", "min": 1}),
            "proposed_price": forms.NumberInput(attrs={"class": "input", "step": "0.01", "min": 0}),
        }

    # دعم تمرير request/employee عبر kwargs للراحة
    def __init__(self, *args, **kwargs):
        req = kwargs.pop("request_obj", None)  # لتفادي ظل اسم request الخاص بـ HttpRequest
        emp = kwargs.pop("employee_obj", None)
        super().__init__(*args, **kwargs)
        if req is not None:
            self.instance.request = req
        if emp is not None:
            self.instance.employee = emp

    def clean_note(self) -> str:
        text = _clean_text(self.cleaned_data.get("note"))
        if not text:
            raise forms.ValidationError("يرجى كتابة تفاصيل العرض.")
        return Truncator(text).chars(5000)

    def clean_proposed_duration_days(self) -> int:
        days = self.cleaned_data.get("proposed_duration_days")
        if not days or days < 1:
            raise forms.ValidationError("المدة المقترحة يجب أن تكون رقمًا موجبًا.")
        return days

    def clean_proposed_price(self):
        price = self.cleaned_data.get("proposed_price")
        if price is None or price < 0:
            raise forms.ValidationError("السعر المقترح يجب أن يكون صفرًا أو رقمًا موجبًا.")
        return price

    def clean(self):
        """
        منع الموظف من تقديم أكثر من عرض (PENDING) لنفس الطلب.
        يعتمد على ضبط instance.request و instance.employee قبل الاستدعاء.
        """
        cleaned = super().clean()
        req = getattr(self.instance, "request", None)
        emp = getattr(self.instance, "employee", None)
        if req and emp:
            # نسمح بعرض واحد PENDING؛ العروض المرفوضة/الملغية لا تمنع تقديم عرض جديد
            from .models import Status
            exists = Offer.objects.filter(
                request=req,
                employee=emp,
                status=getattr(Status, "PENDING", "pending"),
            ).exists()
            if exists:
                raise forms.ValidationError("لا يمكنك تقديم أكثر من عرض واحد قيد الانتظار لهذا الطلب.")
        return cleaned


# ---------------------------------------------
# ملاحظات على الطلب (نقاش رسمي)
# ---------------------------------------------
class NoteForm(forms.ModelForm):
    class Meta:
        model = Note
        fields = ["text", "is_internal"]
        labels = {"text": "الملاحظة", "is_internal": "رؤية مقيدة (داخلي)"}
        widgets = {
            "text": forms.Textarea(attrs={"class": "input", "rows": 3, "placeholder": "أضف ملاحظة"}),
        }

    def clean_text(self) -> str:
        text = _clean_text(self.cleaned_data.get("text"))
        if not text:
            raise forms.ValidationError("يرجى كتابة نص الملاحظة.")
        return Truncator(text).chars(5000)


# ---------------------------------------------
# نموذج عرض بديل (موجود عندك سابقًا) – أبقيناه متوافقًا
# ---------------------------------------------
class OfferForm(forms.ModelForm):
    class Meta:
        model = Offer
        fields = ["proposed_duration_days", "proposed_price", "note"]
        widgets = {
            "proposed_duration_days": forms.NumberInput(
                attrs={"min": 1, "class": "input", "placeholder": "المدة بالأيام"}
            ),
            "proposed_price": forms.NumberInput(
                attrs={"min": 1, "step": "0.5", "class": "input", "placeholder": "المبلغ بالريال"}
            ),
            "note": forms.Textarea(
                attrs={"rows": 3, "class": "input", "placeholder": "تفاصيل العرض (اختياري)"}
            ),
        }

    def clean_proposed_duration_days(self):
        v = self.cleaned_data["proposed_duration_days"]
        if v < 1 or v > 365:
            raise forms.ValidationError("المدة يجب أن تكون بين 1 و 365 يومًا.")
        return v

    def clean_proposed_price(self):
        v = self.cleaned_data["proposed_price"]
        if v is None or v <= 0:
            raise forms.ValidationError("المبلغ يجب أن يكون أكبر من صفر.")
        return v


# ---------------------------------------------
# نموذج إداري: إعادة إسناد موظف للطلب
# ---------------------------------------------
class AdminReassignForm(forms.Form):
    """اختيار موظف لإعادة الإسناد (admin-only)."""
    employee = forms.ModelChoiceField(
        queryset=User.objects.none(),  # نضبطه في __init__ لضمان فلترة صحيحة
        label="الموظف الجديد",
        widget=forms.Select(attrs={"class": "input"}),
        help_text="يظهر فقط المستخدمون بدور 'employee' وفعّالون.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["employee"].queryset = User.objects.filter(
            role="employee", is_active=True
        ).order_by("name", "email")


class ReviewForm(forms.ModelForm):
    class Meta:
        model = Review
        fields = ["rating", "comment"]
        widgets = {
            "rating": forms.NumberInput(attrs={"min": 0, "max": 5, "class": "input", "placeholder": "التقييم من 0 إلى 5"}),
            "comment": forms.Textarea(attrs={"rows": 3, "class": "input", "placeholder": "اكتب تعليقك هنا..."}),
        }
