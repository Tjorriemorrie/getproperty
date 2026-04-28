from django.db import migrations, models

PLACEHOLDER = 'contact agent for street address'


def null_placeholder_address(apps, schema_editor):
    Listing = apps.get_model('main', 'Listing')
    Listing.objects.filter(address__iexact=PLACEHOLDER).update(address=None)
    Listing.objects.filter(address='').update(address=None)


def restore_placeholder_address(apps, schema_editor):
    Listing = apps.get_model('main', 'Listing')
    Listing.objects.filter(address__isnull=True).update(address='')


class Migration(migrations.Migration):
    dependencies = [
        ('main', '0004_listing_erf_size_m2_listing_floor_size_m2'),
    ]

    operations = [
        migrations.AlterField(
            model_name='listing',
            name='address',
            field=models.CharField(blank=True, max_length=500, null=True),
        ),
        migrations.RunPython(null_placeholder_address, restore_placeholder_address),
    ]
