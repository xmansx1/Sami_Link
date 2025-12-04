from django.core.management.base import BaseCommand
from django.utils import timezone
from django.contrib.auth import get_user_model
from marketplace.models import Request
from notifications.models import Notification
from datetime import timedelta

User = get_user_model()

class Command(BaseCommand):
    help = 'Checks for overdue projects and notifies admins'

    def handle(self, *args, **options):
        today = timezone.now().date()
        
        # Find in-progress requests with agreements
        requests = Request.objects.filter(
            status='in_progress', 
            agreement__isnull=False
        ).select_related('agreement', 'assigned_employee', 'client')

        overdue_count = 0
        
        # Get admin users to notify
        # Assuming admins have is_superuser=True or role='admin'/'manager'
        admins = User.objects.filter(
            is_active=True
        ).filter(
            is_superuser=True
        ) | User.objects.filter(role__in=['admin', 'manager'])
        
        admins = admins.distinct()

        for req in requests:
            ag = req.agreement
            if not ag.started_at or not ag.duration_days:
                continue
                
            deadline = ag.started_at + timedelta(days=ag.duration_days)
            
            if deadline < today:
                overdue_days = (today - deadline).days
                overdue_count += 1
                
                self.stdout.write(self.style.WARNING(f"Request #{req.id} is overdue by {overdue_days} days"))
                
                # Create notification for admins
                title = f"⚠️ مشروع متأخر: {req.title}"
                body = f"المشروع #{req.id} تجاوز موعد التسليم بـ {overdue_days} يوم. الموظف: {req.assigned_employee}"
                url = f"/marketplace/request/{req.id}/" # Adjust URL as needed
                
                for admin in admins:
                    # Check if notification already exists for today to avoid spam
                    exists = Notification.objects.filter(
                        recipient=admin,
                        title=title,
                        created_at__date=today
                    ).exists()
                    
                    if not exists:
                        Notification.objects.create(
                            recipient=admin,
                            title=title,
                            body=body,
                            url=url,
                            # content_object=req # If using GenericForeignKey
                        )
                        self.stdout.write(f"  -> Notified {admin.email}")

        self.stdout.write(self.style.SUCCESS(f"Check complete. Found {overdue_count} overdue projects."))
