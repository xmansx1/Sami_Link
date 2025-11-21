from decimal import Decimal
from django.test import TestCase
from marketplace.models import Offer
from finance.models import FinanceSettings

class OfferFinancialsTest(TestCase):
    def setUp(self):
        # إعداد نسب المنصة والضريبة مع pk=1 (تحديث إذا كان موجودًا)
        obj, _ = FinanceSettings.objects.get_or_create(pk=1, defaults={
            'platform_fee_percent': Decimal('0.10'),
            'vat_rate': Decimal('0.15'),
        })
        obj.platform_fee_percent = Decimal('0.10')
        obj.vat_rate = Decimal('0.15')
        obj.save()
        # حذف كاش إعدادات المالية في Offer إذا كان موجودًا
        from marketplace.models import Offer
        if hasattr(Offer, '_finance_settings'):
            try:
                del Offer._finance_settings
            except Exception:
                pass

    def test_calculate_financials_from_net(self):
        from finance.utils import calculate_financials_from_net
        proposed_price = Decimal('1000.00')
        platform_fee_percent = Decimal('0.10')
        vat_percent = Decimal('0.15')
        result = calculate_financials_from_net(
            net_amount=proposed_price,
            platform_fee_percent=platform_fee_percent,
            vat_rate=vat_percent,
        )
        self.assertEqual(result['platform_fee'], Decimal('100.00'))
        self.assertEqual(result['vat_amount'], Decimal('150.00'))
        self.assertEqual(result['net_for_employee'], Decimal('1000.00') - Decimal('100.00'))
        self.assertEqual(result['client_total'], Decimal('1000.00') + Decimal('100.00') + Decimal('150.00'))

    def test_offer_financials(self):
        from marketplace.models import Request
        from django.contrib.auth import get_user_model
        User = get_user_model()
        client = User.objects.create(email='client@test.com', name='Client')
        employee = User.objects.create(email='employee@test.com', name='Employee')
        req = Request.objects.create(title='طلب اختبار', client=client, estimated_duration_days=1, estimated_price=Decimal('1000.00'))
        proposed_price = Decimal('1000.00')
        offer = Offer.objects.create(request=req, employee=employee, proposed_duration_days=1, proposed_price=proposed_price)
        offer = Offer.objects.get(pk=offer.pk)  # إعادة التحميل من قاعدة البيانات
        # إعادة تهيئة الكاش يدويًا
        from finance.models import FinanceSettings
        offer._finance_settings = FinanceSettings.get_solo()
        # يجب أن تكون عمولة المنصة = 1000 * 0.10 = 100
        self.assertEqual(offer.platform_fee_amount, Decimal('100.00'))
        # يجب أن تكون الضريبة = 1000 * 0.15 = 150
        self.assertEqual(offer.vat_amount, Decimal('150.00'))
        # صافي الموظف = 1000 - 100 = 900
        self.assertEqual(offer.net_for_employee, Decimal('900.00'))
        # الإجمالي للعميل = 1000 + 100 + 150 = 1250
        self.assertEqual(offer.client_total_amount, Decimal('1250.00'))
