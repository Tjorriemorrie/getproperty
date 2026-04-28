"""Scrape privateproperty.co.za listings (newest-first, stops at first known listing)."""

import logging

from django.core.management.base import BaseCommand

from main import constants
from main.privateproperty import scrape

logger = logging.getLogger(__name__)

URLS = [getattr(constants, name) for name in dir(constants) if name.startswith('PP_')]


class Command(BaseCommand):
    help = 'Scrape privateproperty.co.za listings from all PP_* URLs in main.constants.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--max-pages', type=int, default=None, help='Maximum list pages to traverse per URL.'
        )
        parser.add_argument(
            '--all',
            action='store_true',
            dest='full',
            help='Scrape every listing on every page (do not stop at first known listing).',
        )

    def handle(self, *args, **options):
        total_new = 0
        total_images = 0
        total_rechecked = 0
        total_removed = 0
        for url in URLS:
            self.stdout.write(f'Scraping {url}')
            result = scrape(
                start_url=url,
                max_pages=options['max_pages'],
                full=options['full'],
            )
            total_new += result['new_listings']
            total_images += result['images']
            total_rechecked += result.get('rechecked', 0)
            total_removed += result.get('removed', 0)
            extras = ''
            if 'rechecked' in result:
                extras = f', rechecked: {result["rechecked"]}, removed: {result["removed"]}'
            self.stdout.write(
                f'  -> new listings: {result["new_listings"]}, images: {result["images"]}{extras}'
            )
        summary = f'Done. Total new listings: {total_new}, images: {total_images}'
        if not options['full']:
            summary += f', rechecked: {total_rechecked}, removed: {total_removed}'
        self.stdout.write(self.style.SUCCESS(summary))
