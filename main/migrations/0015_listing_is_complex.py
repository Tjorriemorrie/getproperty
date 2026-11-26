import logging

from django.db import migrations, models

logger = logging.getLogger(__name__)

# The old "+ Set placeholder" button stored levies=1 to mark a complex.
LEVY_PLACEHOLDER = 1


def mark_complexes(apps, schema_editor):
    """Flag listings with levies as complexes, then drop the R 1 placeholders."""
    listing = apps.get_model('main', 'Listing')
    flagged = listing.objects.filter(levies__gt=0).update(is_complex=True)
    # Duplicates share one flag (see views.set_is_complex): one flagged member flags all.
    group_ids = (
        listing.objects.filter(is_complex=True, group__isnull=False)
        .values_list('group_id', flat=True)
        .distinct()
    )
    synced = listing.objects.filter(group_id__in=list(group_ids), is_complex=False).update(
        is_complex=True
    )
    cleared = listing.objects.filter(levies=LEVY_PLACEHOLDER).update(levies=None)
    logger.info(
        f'🏢 Migrated complexes: flagged_is_complex={flagged}, '
        f'group_siblings_synced={synced}, placeholders_cleared={cleared}'
    )


def restore_placeholders(apps, schema_editor):
    """Reverse: give complexes without a levy the R 1 placeholder again.

    Group siblings that were flagged only by the sync get a placeholder too.
    """
    listing = apps.get_model('main', 'Listing')
    restored = listing.objects.filter(is_complex=True, levies__isnull=True).update(
        levies=LEVY_PLACEHOLDER
    )
    logger.info(f'🏢 Reverted complexes: placeholders_restored={restored}')


class Migration(migrations.Migration):
    dependencies = [
        ('main', '0014_listing_complex_name'),
    ]

    operations = [
        migrations.AddField(
            model_name='listing',
            name='is_complex',
            field=models.BooleanField(db_index=True, default=False),
        ),
        migrations.RunPython(mark_complexes, restore_placeholders),
    ]
