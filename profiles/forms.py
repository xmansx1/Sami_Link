from django import forms
from .models import PortfolioItem, EmployeeProfile

class EmployeeProfileForm(forms.ModelForm):
    class Meta:
        model = EmployeeProfile
        fields = ['title', 'specialty', 'city', 'skills', 'bio', 'photo']
        widgets = {
            'title': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'مثال: مطور واجهات أمامية'}),
            'specialty': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'مثال: تطوير ويب'}),
            'city': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'الرياض'}),
            'skills': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Python, Django, React...'}),
            'bio': forms.Textarea(attrs={'class': 'form-control', 'rows': 4, 'placeholder': 'نبذة مختصرة عن خبراتك...'}),
            'photo': forms.FileInput(attrs={'class': 'form-control'}),
        }
        labels = {
            'title': 'المسمى الوظيفي',
            'specialty': 'التخصص',
            'city': 'المدينة',
            'skills': 'المهارات',
            'bio': 'نبذة عني',
            'photo': 'الصورة الشخصية',
        }

class PortfolioItemForm(forms.ModelForm):
    class Meta:
        model = PortfolioItem
        fields = ['title', 'description', 'tags', 'link', 'image', 'attachment', 'is_public', 'sort_order']
        widgets = {
            'title': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'عنوان العمل'}),
            'description': forms.Textarea(attrs={'class': 'form-control', 'rows': 4, 'placeholder': 'وصف تفصيلي للعمل...'}),
            'tags': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'مثال: تصميم, شعار, فوتوشوب (مفصولة بفواصل)'}),
            'link': forms.URLInput(attrs={'class': 'form-control', 'placeholder': 'https://example.com'}),
            'image': forms.FileInput(attrs={'class': 'form-control'}),
            'attachment': forms.FileInput(attrs={'class': 'form-control'}),
            'is_public': forms.CheckboxInput(attrs={'class': 'form-check-input'}),
            'sort_order': forms.NumberInput(attrs={'class': 'form-control'}),
        }
        labels = {
            'title': 'عنوان العمل',
            'description': 'الوصف',
            'tags': 'الوسوم',
            'link': 'رابط خارجي',
            'image': 'صورة العرض',
            'attachment': 'ملف مرفق',
            'is_public': 'عرض للعامة',
            'sort_order': 'الترتيب',
        }
