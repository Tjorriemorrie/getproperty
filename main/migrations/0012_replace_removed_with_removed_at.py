from django.db import migrations, models
from django.utils import timezone


def copy_removed_to_removed_at(apps, schema_editor):
    Listing = apps.get_model('main', 'Listing')
    ListingGroup = apps.get_model('main', 'ListingGroup')
    now = timezone.now()
    for listing in Listing.objects.filter(removed=True):
        listing.removed_at = listing.rechecked_at or now
        listing.save(update_fields=['removed_at'])
    for group in ListingGroup.objects.filter(removed=True):
        latest = group.listings.exclude(removed_at=None).order_by('-removed_at').first()
        group.removed_at = latest.removed_at if latest else now
        group.save(update_fields=['removed_at'])


class Migration(migrations.Migration):
    dependencies = [
        ('main', '0011_set_is_liked_default_true'),
    ]

    operations = [
        migrations.AddField(
            model_name='listing',
            name='removed_at',
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AddField(
            model_name='listinggroup',
            name='removed_at',
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.RunPython(copy_removed_to_removed_at, migrations.RunPython.noop),
        migrations.RemoveField(model_name='listing', name='removed'),
        migrations.RemoveField(model_name='listinggroup', name='removed'),
    ]
