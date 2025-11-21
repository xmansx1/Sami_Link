from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from django import forms
from django.core.exceptions import ValidationError
from django.forms import BaseInlineFormSet, inlineformset_factory
from django.utils.html import strip_tags
from django.utils.text import Truncator

from .models import Agreement, Milestone, AgreementClause

# ========================= أدوات مساعدة =========================
def _clean_text_simple(v: str | None, max_len: int | None = None) -> str:
    if v is None:
        return ""
    v = strip_tags(v or "")
    v = " ".join(v.split())
    if max_len:
        v = Truncator(v).chars(max_len)
    return v

def _to_money(value) -> Decimal:
    try:
        d = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise forms.ValidationError("قيمة رقمية غير صالحة.")
    return d.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

# ========================= نماذج الاتفاقية =========================
class AgreementForm(forms.ModelForm):
    class Meta:
        model = Agreement
        fields = ["title", "text", "duration_days", "total_amount"]
        labels = {
            "title": "عنوان الاتفاقية",
            "text": "نص الاتفاقية",
            "duration_days": "المدة (أيام)",
            "total_amount": "الإجمالي (ريال)",
        }
        widgets = {
            "title": forms.TextInput(attrs={"class": "input", "placeholder": "عنوان واضح"}),
            "text": forms.Textarea(attrs={"class": "input", "rows": 6, "placeholder": "نص الاتفاقية (اختياري)"}),
            "duration_days": forms.NumberInput(attrs={"class": "input", "min": 1}),
            "total_amount": forms.NumberInput(attrs={"class": "input", "step": "0.01", "min": 0}),
        }

    def clean_title(self) -> str:
        title = _clean_text_simple(self.cleaned_data.get("title", ""), max_len=200)
        if not title:
            raise forms.ValidationError("يرجى إدخال عنوان الاتفاقية.")
        return title

    def clean_text(self) -> str:
        return _clean_text_simple(self.cleaned_data.get("text", ""), max_len=10000)

    def clean_duration_days(self) -> int:
        v = self.cleaned_data.get("duration_days")
        if v is None:
            raise forms.ValidationError("المدة إجبارية.")
        if v < 1 or v > 3650:
            raise forms.ValidationError("المدة يجب أن تكون بين 1 و 3650 يومًا.")
        return v

    def clean_total_amount(self) -> Decimal:
        v = self.cleaned_data.get("total_amount")
        amt = _to_money(v)
        if amt < Decimal("0.00"):
            raise forms.ValidationError("الإجمالي يجب أن يكون صفرًا أو أكبر.")
        if amt > Decimal("100000000.00"):
            raise forms.ValidationError("الإجمالي كبير جدًا.")
        return amt


class AgreementEditForm(forms.ModelForm):
    class Meta:
        model = Agreement
        fields = ["title", "text", "duration_days"]
        labels = {"title": "عنوان الاتفاقية", "text": "نص الاتفاقية", "duration_days": "المدة (أيام)"}
        widgets = {
            "title": forms.TextInput(attrs={"class": "input", "placeholder": "عنوان واضح"}),
            "text": forms.Textarea(attrs={"class": "input", "rows": 6, "placeholder": "نص الاتفاقية (اختياري)"}),
            "duration_days": forms.NumberInput(attrs={"class": "input", "min": 1}),
        }

    def clean_title(self) -> str:
        title = _clean_text_simple(self.cleaned_data.get("title", ""), max_len=200)
        if not title:
            raise forms.ValidationError("يرجى إدخال عنوان الاتفاقية.")
        return title

    def clean_text(self) -> str:
        return _clean_text_simple(self.cleaned_data.get("text", ""), max_len=10000)

# ========================= مراحل الاتفاقية (بدون مبالغ) =========================
class MilestoneForm(forms.ModelForm):
    class Meta:
        model = Milestone
        fields = ["title", "due_days", "order"]
        widgets = {
            "title": forms.TextInput(attrs={"dir": "rtl", "placeholder": "مثال: تسليم النسخة الأولية"}),
            "due_days": forms.NumberInput(attrs={"min": 1}),
        }

    def clean(self):
        cleaned = super().clean()
        # لا مبالغ في هذه المرحلة، نضمن صفر
        self.instance.amount = Decimal("0.00")
        # ترتيب أدنى شيء 1 (سنعيد ترقيمها لاحقًا كمان)
        if cleaned.get("order") is None or cleaned.get("order") < 1:
            cleaned["order"] = 1
        return cleaned


class BaseMilestoneFormSet(BaseInlineFormSet):
    def clean(self):
        super().clean()
        # اضمن أن فيه على الأقل نموذج غير محذوف وصالح
        alive = 0
        orders = set()
        for form in self.forms:
            if not form.cleaned_data:
                continue
            if form.cleaned_data.get("DELETE"):
                continue
            alive += 1
            order_val = form.cleaned_data.get("order")
            if order_val in orders:
                raise ValidationError("رجاء صحّح بيانات order المتكررة. يجب ألا تتكرر أرقام ترتيب المراحل.")
            orders.add(order_val)
        if alive < 1:
            raise ValidationError("يجب أن تحتوي الاتفاقية على مرحلة واحدة على الأقل.")
        # لا تضف أي تحقق متعلق بمجموع الأيام هنا، التحقق يتم فقط في الـ view

    def save(self, commit=True):
        """
        نحفظ، نحذف المعلّم عليها DELETE، ثم نعيد ترقيم order = 1..N.
        كما نضمن amount=0.00 للمراحل.
        """
        instances = super().save(commit=False)

        # احذف المؤشّر عليها بالحذف
        for form in self.forms:
            if form.cleaned_data and form.cleaned_data.get("DELETE") and form.instance.pk:
                form.instance.delete()

        # احفظ/عدّل الباقي
        kept = []
        for obj in instances:
            # قد يكون محذوفًا (بدون pk) أو غير صالح
            if getattr(obj, "pk", None) is None and any(
                f.cleaned_data.get("DELETE") and f.instance is obj for f in self.forms if f.cleaned_data
            ):
                continue
            # صفر المبلغ دائمًا
            obj.amount = Decimal("0.00")
            if commit:
                obj.save()
            kept.append(obj)

        # أعد جلب جميع المراحل غير المحذوفة للاتفاقية (سواء كانت قديمة أو جديدة)
        parent = self.instance  # Agreement
        all_qs = Milestone.objects.filter(agreement=parent).order_by("id")
        # رقّم 1..N بالترتيب المرغوب (هنا id، أو بدّلها حسب ترتيبك)
        order_no = 1
        for obj in all_qs:
            if obj.order != order_no:
                obj.order = order_no
                if commit:
                    obj.save(update_fields=["order"])
            order_no += 1

        self.save_m2m()
        return kept


MilestoneFormSet = inlineformset_factory(
    Agreement,
    Milestone,
    form=MilestoneForm,
    formset=BaseMilestoneFormSet,
    fields=["title", "due_days", "order"],
    extra=1,
    can_delete=True,
    validate_min=True,
    min_num=1,
)

# ========================= تثبيت بنود الاتفاقية =========================
class AgreementClauseSelectForm(forms.Form):
    clauses = forms.ModelMultipleChoiceField(
        label="اختر البنود الجاهزة",
        queryset=AgreementClause.objects.none(),
        widget=forms.CheckboxSelectMultiple,
        required=False,
        help_text="يمكن تحديد أكثر من بند. تظهر البنود المفعّلة فقط.",
    )
    custom_clauses = forms.CharField(
        label="بنود مخصّصة (اختياري)",
        required=False,
        widget=forms.Textarea(attrs={"rows": 5, "dir": "rtl", "placeholder": "اكتب كل بند في سطر مستقل"}),
        help_text="اكتب كل بند في سطر مستقل. تتم تنقية كل بند لاحقًا.",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["clauses"].queryset = AgreementClause.objects.filter(is_active=True).order_by("title")

    def cleaned_custom_lines(self) -> list[str]:
        data = self.cleaned_data.get("custom_clauses") or ""
        lines: list[str] = []
        for ln in data.splitlines():
            t = _clean_text_simple(ln, max_len=800)
            if t:
                lines.append(t)
        return lines[:50]
