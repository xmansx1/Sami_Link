from django.contrib import admin
from .models import SiteSetting, ContactMessage

@admin.register(ContactMessage)
class ContactMessageAdmin(admin.ModelAdmin):
    list_display = ('name', 'email', 'phone', 'subject', 'created_at', 'is_read')
    list_filter = ('is_read', 'created_at')
    search_fields = ('name', 'email', 'phone', 'subject', 'message')
    readonly_fields = ('created_at',)
    list_editable = ('is_read',)

@admin.register(SiteSetting)
class SiteSettingAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        # Only allow adding if no instance exists
        if SiteSetting.objects.exists():
            return False
        return True

    def has_delete_permission(self, request, obj=None):
        # Prevent deleting the settings
        return False
