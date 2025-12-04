from django.db import models
from django.core.exceptions import ValidationError

class SiteSetting(models.Model):
    # Contact Section
    contact_title = models.CharField(max_length=200, default="تواصل مع فريق سامي لينك", verbose_name="عنوان قسم التواصل")
    contact_description = models.TextField(default="للاستفسارات، الشراكات، أو التجربة الأولى للمنصة: يسعدنا تواصلك معنا عبر البريد الإلكتروني أو الواتساب.", verbose_name="وصف قسم التواصل")
    
    email = models.EmailField(default="support@samilink.sa", verbose_name="البريد الإلكتروني الرسمي")
    whatsapp = models.CharField(max_length=50, default="0000 000 50 966+", verbose_name="واتساب خدمة العملاء")
    working_hours = models.CharField(max_length=200, default="الأحد - الخميس: 8 ص - 5 م", verbose_name="أوقات العمل")
    
    # Partnership Card (Left Side)
    partnership_card_badge = models.CharField(max_length=100, default="جاهزون لمرافقتك 🚀", verbose_name="شارة البطاقة")
    partnership_card_title = models.CharField(max_length=200, default="من أول طلب حتى التسليم", verbose_name="عنوان البطاقة")
    partnership_card_description = models.TextField(default="يمكن تهيئة المنصة للاستخدام الداخلي في شركتك أو فريقك، مع صلاحيات مالية وإدارية متقدمة وتقارير متابعة مخصصة بحسب احتياج الإدارة.", verbose_name="وصف البطاقة")
    partnership_button_text = models.CharField(max_length=100, default="طلب شراكة 👋", verbose_name="نص الزر")
    partnership_button_url = models.CharField(max_length=200, default="#", verbose_name="رابط الزر")

    class Meta:
        verbose_name = "إعدادات الموقع والصفحة الرئيسية"
        verbose_name_plural = "إعدادات الموقع والصفحة الرئيسية"

    def __str__(self):
        return "إعدادات الموقع (تعديل البيانات)"

    def save(self, *args, **kwargs):
        if not self.pk and SiteSetting.objects.exists():
            # If you want to ensure only one object exists, you can raise an error
            # or just update the existing one. Here we prevent creating a new one if one exists.
            raise ValidationError('There can be only one SiteSetting instance')
        return super(SiteSetting, self).save(*args, **kwargs)


class ContactMessage(models.Model):
    name = models.CharField("الاسم", max_length=100)
    email = models.EmailField("البريد الإلكتروني")
    phone = models.CharField("رقم الجوال", max_length=20, blank=True, null=True)
    subject = models.CharField("الموضوع", max_length=200)
    message = models.TextField("الرسالة")
    created_at = models.DateTimeField("تاريخ الإرسال", auto_now_add=True)
    is_read = models.BooleanField("تمت القراءة", default=False)

    class Meta:
        verbose_name = "رسالة تواصل"
        verbose_name_plural = "رسائل التواصل"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.name} - {self.subject}"

