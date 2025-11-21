from marketplace.models import Offer
from django.core.management.base import BaseCommand

class Command(BaseCommand):
    help = 'Recalculate and save all offers to ensure client_total_amount is always available.'

    def handle(self, *args, **options):
        offers = Offer.objects.all()
        updated = 0
        for offer in offers:
            # Force recalculation by saving the offer (if any logic in save or properties)
            offer.save()
            updated += 1
        self.stdout.write(self.style.SUCCESS(f'Successfully updated {updated} offers.'))
