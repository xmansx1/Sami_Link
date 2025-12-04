from django.db import models
from marketplace.models import Request
import os

class RequestFile(models.Model):
    request = models.ForeignKey(Request, on_delete=models.CASCADE, related_name="files", verbose_name="الطلب")
    file = models.FileField(upload_to="request_files/", verbose_name="الملف")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "ملف طلب"
        verbose_name_plural = "ملفات الطلبات"

    def __str__(self):
        return f"File #{self.id} for Request #{self.request_id}"

    @property
    def filename(self):
        return os.path.basename(self.file.name)
