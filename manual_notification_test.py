from notifications.models import Notification
from django.contrib.auth import get_user_model

User = get_user_model()

# عدل اسم المستخدم هنا لتجربة الإشعار
username = "client_username"  # أو employee_username

try:
    user = User.objects.get(username=username)
    Notification.objects.create(
        recipient=user,
        title="إشعار تجريبي يدوي",
        body="هذا إشعار تم إنشاؤه يدويًا للتجربة.",
    )
    print(f"تم إنشاء إشعار يدوي للمستخدم {username}")
except User.DoesNotExist:
    print(f"المستخدم {username} غير موجود")
