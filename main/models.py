import re

from django.db import models


def parse_size_m2(value):
    """Extract an integer m² value from strings like '1 235 m²' or '93m2'."""
    if not value:
        return None
    digits = re.sub(r'[^\d]', '', str(value).split('m')[0])
    return int(digits) if digits else None


class ListingGroup(models.Model):
    """Groups duplicate listings from different agents. The primary listing's details
    are used to represent the group on list pages.
    """

    primary = models.ForeignKey(
        'Listing', on_delete=models.SET_NULL, null=True, blank=True, related_name='+'
    )
    notes = models.TextField(blank=True, default='')
    removed = models.BooleanField(default=False, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f'Group {self.pk} ({self.listings.count()} listings)'


class Listing(models.Model):
    listing_id = models.CharField(max_length=50, unique=True, db_index=True)
    url = models.URLField(max_length=500)

    title = models.CharField(max_length=500, blank=True)
    price_text = models.CharField(max_length=100, blank=True)
    price = models.BigIntegerField(null=True, blank=True)

    address = models.CharField(max_length=500, blank=True, null=True)
    suburb = models.CharField(max_length=200, blank=True)
    city = models.CharField(max_length=200, blank=True)
    province = models.CharField(max_length=200, blank=True)
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)

    property_type = models.CharField(max_length=100, blank=True)
    bedrooms = models.FloatField(null=True, blank=True)
    bathrooms = models.FloatField(null=True, blank=True)
    garages = models.FloatField(null=True, blank=True)
    parking_spaces = models.FloatField(null=True, blank=True)
    erf_size = models.CharField(max_length=100, blank=True)
    erf_size_m2 = models.IntegerField(null=True, blank=True, db_index=True)
    floor_size = models.CharField(max_length=100, blank=True)
    floor_size_m2 = models.IntegerField(null=True, blank=True, db_index=True)

    rates_and_taxes = models.IntegerField(null=True, blank=True)
    levies = models.IntegerField(null=True, blank=True)

    headline = models.CharField(max_length=500, blank=True)
    description = models.TextField(blank=True)

    details = models.JSONField(default=dict, blank=True)
    features = models.JSONField(default=dict, blank=True)

    agent_name = models.CharField(max_length=200, blank=True)
    agency = models.CharField(max_length=200, blank=True)

    notes = models.TextField(blank=True, default='')

    group = models.ForeignKey(
        ListingGroup, on_delete=models.SET_NULL, null=True, blank=True, related_name='listings'
    )

    listed_at = models.DateTimeField(null=True, blank=True)
    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_scraped_at = models.DateTimeField(auto_now=True)
    rechecked_at = models.DateTimeField(null=True, blank=True, db_index=True)
    removed = models.BooleanField(default=False, db_index=True)

    class Meta:
        ordering = ['-listed_at']

    def save(self, *args, **kwargs):
        self.floor_size_m2 = parse_size_m2(self.floor_size)
        self.erf_size_m2 = parse_size_m2(self.erf_size)
        super().save(*args, **kwargs)

    def __str__(self):
        return f'{self.listing_id} — {self.title or self.url}'


class ListingImage(models.Model):
    listing = models.ForeignKey(Listing, related_name='images', on_delete=models.CASCADE)
    source_url = models.URLField(max_length=1000)
    file = models.FileField(upload_to='listings/%Y/%m/')
    position = models.PositiveIntegerField(default=0)
    image_hash = models.CharField(max_length=32, blank=True, default='', db_index=True)
    phash = models.CharField(max_length=16, blank=True, default='', db_index=True)
    file_size = models.PositiveIntegerField(default=0)
    downloaded_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('listing', 'source_url')
        ordering = ['position']

    def __str__(self):
        return f'{self.listing.listing_id} #{self.position}'
