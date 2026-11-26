"""Scrape privateproperty.co.za listings, then reconcile removals against the live list pages."""

import logging

from django.core.management.base import BaseCommand

from main import constants
from main.privateproperty import recheck_oldest, reconcile_with_live, scrape

logger = logging.getLogger(__name__)

URLS = [getattr(constants, name) for name in dir(constants) if name.startswith('PP_')]


class Command(BaseCommand):
    help = 'Scrape privateproperty.co.za listings from all PP_* URLs in main.constants.'

    def add_arguments(self, parser):
        """Register --max-pages and --all."""
        parser.add_argument(
            '--max-pages',
            type=int,
            default=None,
            help='Maximum list pages to traverse per URL (skips the removal reconcile).',
        )
        parser.add_argument(
            '--all',
            action='store_true',
            dest='full',
            help='Re-fetch the details of every listing on every page, not just new ones.',
        )

    def handle(self, *args, **options):
        """Scrape every PP_* URL, reconcile removals, then rotate-recheck unless --all."""
        logger.info('🚀 Starting scrape: sources=%s, options=%s', len(URLS), options)
        total_new = 0
        total_images = 0
        live_ids: set[str] = set()
        all_complete = True
        for url in URLS:
            self.stdout.write(f'Scraping {url}')
            result = scrape(start_url=url, max_pages=options['max_pages'], full=options['full'])
            total_new += result['new_listings']
            total_images += result['images']
            live_ids |= result['live_ids']
            all_complete &= result['complete']
            self.stdout.write(
                f'  -> new listings: {result["new_listings"]}, images: {result["images"]}, '
                f'live: {len(result["live_ids"])}, complete walk: {result["complete"]}'
            )

        summary = f'Done. Total new listings: {total_new}'
        if all_complete:
            self.stdout.write(f'Reconciling stored listings against {len(live_ids)} live listings')
            rec = reconcile_with_live(live_ids)
            total_images += rec['images']
            summary += f', removed: {rec["removed"]}, restored: {rec["restored"]}'
        else:
            logger.warning('⚠️ Skipping reconcile, incomplete list walk: live=%s', len(live_ids))
            self.stdout.write(self.style.WARNING('Skipping reconcile: a list walk was incomplete'))

        if not options['full']:
            self.stdout.write('Rechecking oldest-checked active listings')
            rot = recheck_oldest()
            total_images += rot['images']
            summary += f', rotation rechecked: {rot["rechecked"]} (removed {rot["removed"]})'

        summary += f', images: {total_images}'
        logger.info('🚀 Scrape finished: %s', summary)
        self.stdout.write(self.style.SUCCESS(summary))
