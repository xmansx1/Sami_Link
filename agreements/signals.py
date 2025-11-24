# agreements/signals.py
from __future__ import annotations

import logging
from typing import Optional

from django.db import transaction
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from django.utils import timezone

from .models import Milestone, Agreement

logger = logging.getLogger(__name__)


def _status_value(model_cls, name: str, default: str) -> str:
    Status = getattr(model_cls, "Status", None)
    return getattr(Status, name, default) if Status else default


@receiver(pre_save, sender=Milestone)
def _milestone_pre_save_snapshot(sender, instance: Milestone, **kwargs):
    """
    نلتقط الحالة السابقة للمرحلة حتى نعرف هل حصل transition فعلي.
    """
    instance.__old_status = None
    if instance.pk:
        old = sender.objects.only("status").filter(pk=instance.pk).first()
        if old:
            instance.__old_status = old.status


@receiver(post_save, sender=Milestone)
def handle_milestone_post_save(sender, instance: Milestone, created: bool, **kwargs):
    """
    مسؤوليات هذه الإشارة بعد التصحيح:
    - إرسال إشعار للموظف عند اعتماد/رفض المرحلة من العميل.
    - إرسال إشعار للعميل عند إنشاء مرحلة جديدة (إن كانت بانتظار مراجعته).
    - عدم تغيير حالة الطلب أو الاتفاقية هنا نهائيًا.
      (منطق الاكتمال صار في finance/signals.py بشرط: اعتماد كل المراحل + سداد الفواتير)
    """

    milestone = instance
    agreement = getattr(milestone, "agreement", None)
    if not agreement:
        return

    req = getattr(agreement, "request", None)
    employee = getattr(agreement, "employee", None)
    client = getattr(req, "client", None) if req else None

    old_status = getattr(milestone, "__old_status", None)
    new_status = getattr(milestone, "status", None)

    MS_APPROVED = _status_value(Milestone, "APPROVED", "approved")
    MS_REJECTED = _status_value(Milestone, "REJECTED", "rejected")
    MS_DELIVERED = _status_value(Milestone, "DELIVERED", "delivered")
    MS_PENDING = _status_value(Milestone, "PENDING", "pending")

    # =========================
    # 1) إشعار للعميل عند إنشاء مرحلة جديدة بانتظار المراجعة
    # =========================
    try:
        if created and client and str(new_status).lower() in {
            str(MS_PENDING).lower(),
            str(MS_DELIVERED).lower(),
        }:
            from notifications.utils import create_notification

            create_notification(
                recipient=client,
                title=f"مرحلة جديدة بانتظار موافقتك للطلب #{getattr(req, 'pk', '')}",
                body=(
                    f"تم إنشاء/تسليم مرحلة جديدة ضمن الاتفاقية للطلب "
                    f"'{getattr(req, 'title', '')}'. يرجى مراجعتها والاعتماد للمتابعة."
                ),
                url=milestone.get_absolute_url()
                if hasattr(milestone, "get_absolute_url")
                else (req.get_absolute_url() if req and hasattr(req, "get_absolute_url") else None),
                actor=employee,
                target=milestone,
            )
    except Exception:
        pass

    # =========================
    # 2) إشعار للموظف عند اعتماد/رفض مرحلة (transition فقط)
    # =========================
    try:
        if old_status and new_status != old_status:
            from notifications.utils import create_notification

            if str(new_status).lower() == str(MS_APPROVED).lower():
                if employee:
                    create_notification(
                        recipient=employee,
                        title=f"تم اعتماد المرحلة للطلب #{getattr(req, 'pk', '')}",
                        body=(
                            f"قام العميل {client} باعتماد المرحلة "
                            f"'{getattr(milestone, 'title', '')}' ضمن الاتفاقية "
                            f"للطلب '{getattr(req, 'title', '')}'."
                        ),
                        url=milestone.get_absolute_url()
                        if hasattr(milestone, "get_absolute_url")
                        else None,
                        actor=client,
                        target=milestone,
                    )

            elif str(new_status).lower() == str(MS_REJECTED).lower():
                if employee:
                    create_notification(
                        recipient=employee,
                        title=f"تم رفض المرحلة من العميل للطلب #{getattr(req, 'pk', '')}",
                        body=(
                            f"قام العميل {client} برفض المرحلة "
                            f"'{getattr(milestone, 'title', '')}' ضمن الاتفاقية "
                            f"للطلب '{getattr(req, 'title', '')}'. "
                            f"يرجى مراجعة السبب واتخاذ الإجراء المناسب."
                        ),
                        url=milestone.get_absolute_url()
                        if hasattr(milestone, "get_absolute_url")
                        else None,
                        actor=client,
                        target=milestone,
                    )
    except Exception:
        pass

    # =========================
    # 3) عدم تعديل حالة الطلب/الاتفاقية هنا
    # =========================
    return


@receiver(post_save, sender=Agreement)
def handle_agreement_created(sender, instance: Agreement, created: bool, **kwargs):
    """
    إشعار للعميل عند إنشاء اتفاقية جديدة بانتظار موافقته.
    لا نغيّر حالة الطلب هنا إطلاقًا.
    """
    if not created:
        return

    agreement = instance
    req = getattr(agreement, "request", None)
    client = getattr(req, "client", None) if req else None
    employee = getattr(agreement, "employee", None)

    AG_PENDING = _status_value(Agreement, "PENDING", "pending")
    ag_status = getattr(agreement, "status", None)

    try:
        if client and ag_status and str(ag_status).lower() == str(AG_PENDING).lower():
            from notifications.utils import create_notification

            create_notification(
                recipient=client,
                title=f"اتفاقية جديدة بانتظار موافقتك للطلب #{getattr(req, 'pk', '')}",
                body=(
                    f"تم إنشاء اتفاقية جديدة للطلب "
                    f"'{getattr(req, 'title', '')}'. يرجى مراجعتها والموافقة عليها للبدء في التنفيذ."
                ),
                url=agreement.get_absolute_url()
                if hasattr(agreement, "get_absolute_url")
                else (req.get_absolute_url() if req and hasattr(req, "get_absolute_url") else None),
                actor=employee,
                target=agreement,
            )
    except Exception:
        pass
