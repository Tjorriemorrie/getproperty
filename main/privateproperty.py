"""Scraper for privateproperty.co.za listings using botasaurus."""

from __future__ import annotations

import hashlib
import io
import json
import logging
import math
import re
import time as time_mod
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, time
from datetime import timezone as dt_timezone
from urllib.parse import urljoin

import imagehash
from botasaurus.request import Request, request
from botasaurus.soupify import soupify
from bs4 import BeautifulSoup
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models import F
from django.utils import timezone
from PIL import Image

from main.constants import PP_SVILLE
from main.models import Listing, ListingGroup, ListingImage

logger = logging.getLogger(__name__)

BASE_URL = 'https://www.privateproperty.co.za'
LISTING_ID_RE = re.compile(r'/(T\d+)(?:[/?#]|$)')
INT_RE = re.compile(r'[\d\s]+')
DETAIL_CONCURRENCY = 10
IMAGE_CONCURRENCY = 10
RECHECK_FRACTION = 0.05
RECHECK_MAX = 5
REMOVED_MARKER_RE = re.compile(r'listing with ref\s+T\d+\s+is no longer available', re.IGNORECASE)


def _extract_listing_id(url: str) -> str | None:
    m = LISTING_ID_RE.search(url)
    return m.group(1) if m else None


def _parse_int(text: str) -> int | None:
    if not text:
        return None
    digits = ''.join(ch for ch in text if ch.isdigit())
    return int(digits) if digits else None


def _parse_float(text: str) -> float | None:
    if not text:
        return None
    m = re.search(r'\d+(?:\.\d+)?', text)
    return float(m.group(0)) if m else None


@request(cache=False, output=None, create_error_logs=False, raise_exception=True)
def _fetch(req: Request, url: str) -> str:
    resp = req.get(url, timeout=60)
    return resp.text


def _parse_list_page(html: str) -> list[dict]:
    soup: BeautifulSoup = soupify(html)
    cards: list[dict] = []
    seen: set[str] = set()

    for card in soup.select('a.listing-result[href], a.featured-listing[href]'):
        href = urljoin(BASE_URL, card.get('href', ''))
        lid = _extract_listing_id(href)
        if not lid or lid in seen:
            continue
        wishlist = card.select_one('[data-listing-id]')
        internal_id = wishlist.get('data-listing-id') if wishlist else ''
        seen.add(lid)
        cards.append({'listing_id': lid, 'url': href, 'internal_id': internal_id})

    return cards


def _find_next_page(current_url: str, html: str) -> str | None:
    soup = soupify(html)
    nxt = (
        soup.select_one('link[rel="next"]')
        or soup.select_one('a[rel="next"]')
        or soup.select_one('a.paging__btn--next:not(.paging__btn--disabled)')
        or soup.select_one('a.next')
    )
    if nxt and nxt.get('href'):
        href = urljoin(BASE_URL, nxt['href'])
        if href != current_url:
            return href
    return None


PP_IMAGE_RE = re.compile(r'(https://images\.pp\.co\.za/listing/\d+/[^/]+)/\d+/\d+/')


def _clean(text: str) -> str:
    return text.replace('\xa0', ' ').strip() if text else ''


def _name_minus_value(full: str, value: str) -> str:
    if value and value in full:
        return full.rsplit(value, 1)[0].strip()
    return full.strip()


def _extract_property_details(soup: BeautifulSoup) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in soup.select('.property-details__list-item'):
        span = item.select_one('.property-details__name-value')
        if not span:
            continue
        value_el = span.select_one('.property-details__value')
        value = _clean(value_el.get_text(' ', strip=True)) if value_el else ''
        full = _clean(span.get_text(' ', strip=True))
        name = _name_minus_value(full, value)
        if name:
            out[name] = value
    return out


def _extract_property_features(soup: BeautifulSoup) -> dict[str, bool | float | str]:
    out: dict[str, bool | float | str] = {}
    for item in soup.select('.property-features__list-item'):
        span = item.select_one('.property-features__name-value')
        if not span:
            continue
        boxed = span.select_one('.property-features__value--boxed')
        check = span.select_one('.property-features__list-icon-check')
        full = _clean(span.get_text(' ', strip=True))
        if boxed:
            val_text = _clean(boxed.get_text(' ', strip=True))
            name = _name_minus_value(full, val_text)
            num = _parse_float(val_text)
            out[name] = num if num is not None else val_text
        else:
            out[full] = bool(check)
    return out


def _extract_jsonld_residence(soup: BeautifulSoup) -> dict:
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or '')
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict) and data.get('@type') == 'Residence':
            return data
    return {}


def _parse_listing_date(text: str):
    for fmt in ('%d %b %Y', '%d %B %Y'):
        try:
            d = datetime.strptime(text, fmt).date()
            return datetime.combine(d, time.min, tzinfo=dt_timezone.utc)
        except ValueError:
            continue
    return None


def _decode_obfuscated_scripts(soup: BeautifulSoup) -> list:
    """Decode obfuscated ``JSON.parse(arr.map($=>arr[$]).join(''))`` in inline scripts."""
    results: list = []
    decoder = json.JSONDecoder()
    for script in soup.select('script:not([src])'):
        text = script.string or ''
        if 'JSON.parse' not in text:
            continue
        m = re.search(
            r'JSON\.parse\((\w+)\.map\(\$\s*=>\s*(\w+)\[\$\]\)\.join\([\'"][\'\"]\)\)',
            text,
        )
        if not m:
            continue
        idx_var, str_var = m.group(1), m.group(2)
        str_pos = re.search(rf'const\s+{re.escape(str_var)}\s*=\s*', text)
        idx_pos = re.search(rf'const\s+{re.escape(idx_var)}\s*=\s*', text)
        if not str_pos or not idx_pos:
            continue
        try:
            strings, _ = decoder.raw_decode(text, str_pos.end())
            indices, _ = decoder.raw_decode(text, idx_pos.end())
            json_str = ''.join(strings[i] for i in indices)
            data = json.loads(json_str)
            results.append(data)
        except (json.JSONDecodeError, ValueError, IndexError, TypeError):
            continue
    return results


def _collect_pp_image_urls(data) -> list[str]:
    """Recursively find privateproperty image URLs in decoded JSON data."""
    urls: list[str] = []
    if isinstance(data, str):
        if 'images.pp.co.za/listing/' in data:
            urls.append(data)
    elif isinstance(data, list):
        for item in data:
            urls.extend(_collect_pp_image_urls(item))
    elif isinstance(data, dict):
        for v in data.values():
            urls.extend(_collect_pp_image_urls(v))
    return urls


def _extract_images(soup: BeautifulSoup) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()

    def _add(raw_url: str) -> None:
        m = PP_IMAGE_RE.match(raw_url)
        full = f'{m.group(1)}/1600/1066/contain/jpegorpng' if m else raw_url
        if full not in seen:
            seen.add(full)
            urls.append(full)

    # Primary: decode obfuscated JS data which contains ALL images
    for data in _decode_obfuscated_scripts(soup):
        for raw_url in _collect_pp_image_urls(data):
            _add(raw_url)

    # Fallback: extract from HTML img tags (only visible subset)
    if not urls:
        for img in soup.select('.media-container__image, .details-page-photogrid__photo'):
            src = img.get('src') or img.get('data-src')
            if not src or 'images.pp.co.za' not in src:
                continue
            _add(src)

    return urls


def _parse_detail_html(html: str, data: dict) -> dict:
    soup: BeautifulSoup = soupify(html)

    def sel_text(sel: str) -> str:
        el = soup.select_one(sel)
        return _clean(el.get_text(' ', strip=True)) if el else ''

    title = sel_text('h1.listing-details__title')
    price_text = sel_text('.listing-price-display__price')
    address = sel_text('.listing-details__address #address-show-map') or sel_text(
        '.listing-details__address'
    )
    if not address or address.strip().lower() == 'contact agent for street address':
        address = None
    headline = sel_text('.listing-description__headline')

    desc_el = soup.select_one('.listing-description__text')
    if desc_el:
        paras = [_clean(p.get_text(' ', strip=True)) for p in desc_el.select('p')]
        description = '\n\n'.join(p for p in paras if p) or _clean(
            desc_el.get_text('\n', strip=True)
        )
    else:
        description = ''

    details = _extract_property_details(soup)
    features = _extract_property_features(soup)
    images = _extract_images(soup)
    ld = _extract_jsonld_residence(soup)

    bedrooms = (
        features.get('Bedrooms') if isinstance(features.get('Bedrooms'), (int, float)) else None
    )
    bathrooms = (
        features.get('Bathrooms') if isinstance(features.get('Bathrooms'), (int, float)) else None
    )
    garages = (
        features.get('Garage parking')
        if isinstance(features.get('Garage parking'), (int, float))
        else None
    )
    parking = (
        features.get('Open parking')
        if isinstance(features.get('Open parking'), (int, float))
        else None
    )

    erf = details.get('Land size', '')
    floor = details.get('Floor size', '')
    property_type = details.get('Property type', '')
    rates = _parse_int(details.get('Rates and taxes', ''))
    levies = _parse_int(details.get('Levies', ''))
    listed_at = _parse_listing_date(details.get('Listing date', ''))

    geo = ld.get('geo') if isinstance(ld, dict) else None
    latitude = geo.get('latitude') if isinstance(geo, dict) else None
    longitude = geo.get('longitude') if isinstance(geo, dict) else None
    addr = ld.get('address') if isinstance(ld, dict) else None
    suburb = city = province = ''
    if isinstance(addr, dict):
        locality = addr.get('addressLocality', '')
        if ',' in locality:
            suburb, city = (s.strip() for s in locality.split(',', 1))
        else:
            suburb = locality
        province = addr.get('addressRegion', '') or ''

    agent_el = soup.select_one('[class*="agent-name" i], [class*="AgentName" i]')
    agency_el = soup.select_one('[class*="agency" i], [class*="Agency" i]')

    return {
        'listing_id': data['listing_id'],
        'url': data['url'],
        'title': title[:500],
        'headline': headline[:500],
        'price_text': price_text[:100],
        'price': _parse_int(price_text),
        'address': address[:500] if address else None,
        'suburb': suburb[:200],
        'city': city[:200],
        'province': province[:200],
        'latitude': latitude,
        'longitude': longitude,
        'property_type': property_type[:100],
        'description': description,
        'bedrooms': bedrooms,
        'bathrooms': bathrooms,
        'garages': garages,
        'parking_spaces': parking,
        'erf_size': erf[:100],
        'floor_size': floor[:100],
        'rates_and_taxes': rates,
        'levies': levies,
        'listed_at': listed_at,
        'details': details,
        'features': features,
        'agent_name': (agent_el.get_text(' ', strip=True) if agent_el else '')[:200],
        'agency': (agency_el.get_text(' ', strip=True) if agency_el else '')[:200],
        'images': images,
    }


@request(cache=False, output=None, create_error_logs=False, raise_exception=False)
def _fetch_detail(req: Request, data: dict) -> dict | None:
    idx = data.get('index')
    total = data.get('total')
    tag = f'[{idx}/{total}]' if idx and total else ''
    started = time_mod.monotonic()
    logger.info('%s fetching detail %s', tag, data['listing_id'])
    try:
        resp = req.get(data['url'], timeout=60)
    except Exception as exc:
        logger.warning('%s detail fetch failed %s: %s', tag, data['url'], exc)
        return None

    result = _parse_detail_html(resp.text, data)
    elapsed = time_mod.monotonic() - started
    logger.info(
        '%s parsed %s in %.1fs — %s | %s | %s imgs',
        tag,
        data['listing_id'],
        elapsed,
        result.get('price_text') or '?',
        (result.get('title') or '')[:60],
        len(result.get('images') or []),
    )
    return result


@request(cache=False, output=None, create_error_logs=False, raise_exception=False)
def _fetch_image(req: Request, data: dict) -> dict | None:
    try:
        resp = req.get(data['url'], timeout=120)
        return {
            'listing_id': data['listing_id'],
            'url': data['url'],
            'position': data['position'],
            'content': resp.content,
        }
    except Exception as exc:
        logger.warning('image fetch failed %s: %s', data['url'], exc)
        return None


def collect_new_listing_cards(
    start_url: str, max_pages: int | None = None, full: bool = False
) -> list[dict]:
    """Walk list pages from newest and return cards not yet stored (unless ``full``)."""
    existing = set(Listing.objects.values_list('listing_id', flat=True))
    url: str | None = start_url
    pages = 0
    cards_out: list[dict] = []
    seen: set[str] = set()

    while url:
        pages += 1
        logger.info('fetching list page %s: %s', pages, url)
        html = _fetch(url)
        cards = _parse_list_page(html)
        if not cards:
            logger.warning('no cards parsed on %s', url)
            break

        hit_existing = False
        for c in cards:
            if c['listing_id'] in seen:
                continue
            if not full and c['listing_id'] in existing:
                logger.info('hit existing listing %s — stopping', c['listing_id'])
                hit_existing = True
                break
            seen.add(c['listing_id'])
            cards_out.append(c)

        if hit_existing:
            break
        if max_pages and pages >= max_pages:
            break

        next_url = _find_next_page(url, html)
        if not next_url or next_url == url:
            break
        url = next_url

    return cards_out


def _save_listing(detail: dict) -> Listing:
    images = detail.pop('images', [])
    with transaction.atomic():
        defaults = {k: v for k, v in detail.items() if v is not None}
        listing, created = Listing.objects.update_or_create(
            listing_id=detail['listing_id'],
            defaults=defaults,
        )
        if created and listing.listed_at is None:
            listing.listed_at = timezone.now()
            listing.save(update_fields=['listed_at'])
    return listing, images


def _filename_for(url: str, position: int) -> str:
    tail = url.split('?', maxsplit=1)[0].rsplit('/', 1)[-1] or f'img_{position}.jpg'
    if '.' not in tail:
        tail = f'{tail}.jpg'
    return f'{position:03d}_{tail}'


def _compute_phash(content: bytes) -> str:
    """Compute a perceptual hash for image bytes."""
    try:
        img = Image.open(io.BytesIO(content))
        return str(imagehash.phash(img))
    except Exception:
        return ''


def _download_listing_images(listing: Listing, image_urls: list[str]) -> int:
    if not image_urls:
        return 0
    existing_urls = set(listing.images.values_list('source_url', flat=True))
    existing_hashes = set(
        listing.images.exclude(image_hash='').values_list('image_hash', flat=True)
    )
    # Map phash -> (file_size, ListingImage pk) for existing images
    phash_best: dict[str, tuple[int, int]] = {}
    for pk, ph, fs in listing.images.exclude(phash='').values_list('pk', 'phash', 'file_size'):
        if ph not in phash_best or fs > phash_best[ph][0]:
            phash_best[ph] = (fs, pk)

    jobs = [
        {'listing_id': listing.listing_id, 'url': u, 'position': i}
        for i, u in enumerate(image_urls)
        if u not in existing_urls
    ]
    if not jobs:
        return 0

    logger.info('downloading %s images for %s', len(jobs), listing.listing_id)
    results = _fetch_image(jobs, parallel=IMAGE_CONCURRENCY)
    saved = 0
    skipped = 0
    replaced = 0
    for r in results:
        if not r or not r.get('content'):
            continue
        content_hash = hashlib.md5(r['content']).hexdigest()  # noqa: S324
        if content_hash in existing_hashes:
            skipped += 1
            continue
        existing_hashes.add(content_hash)

        file_size = len(r['content'])
        phash = _compute_phash(r['content'])

        # If a visually identical image exists, keep the larger (higher quality) one
        if phash and phash in phash_best:
            existing_size, existing_pk = phash_best[phash]
            if file_size <= existing_size:
                skipped += 1
                continue
            # New image is larger — replace the existing one
            try:
                old_img = ListingImage.objects.get(pk=existing_pk)
                old_img.file.delete(save=False)
                old_img.delete()
                replaced += 1
            except ListingImage.DoesNotExist:
                pass

        if phash:
            phash_best[phash] = (file_size, None)  # pk filled after save

        img = ListingImage(
            listing=listing,
            source_url=r['url'],
            position=r['position'],
            image_hash=content_hash,
            phash=phash,
            file_size=file_size,
        )
        img.file.save(_filename_for(r['url'], r['position']), ContentFile(r['content']), save=True)
        if phash:
            phash_best[phash] = (file_size, img.pk)
        saved += 1
    if skipped:
        logger.info('skipped %s duplicate images for %s', skipped, listing.listing_id)
    if replaced:
        logger.info('replaced %s lower-quality images for %s', replaced, listing.listing_id)
    logger.info('saved %s/%s images for %s', saved, len(jobs), listing.listing_id)
    return saved


@request(cache=False, output=None, create_error_logs=False, raise_exception=False)
def _fetch_recheck(req: Request, data: dict) -> dict | None:
    idx = data.get('index')
    total = data.get('total')
    tag = f'[recheck {idx}/{total}]' if idx and total else '[recheck]'
    started = time_mod.monotonic()
    logger.info('%s fetching %s', tag, data['listing_id'])
    try:
        resp = req.get(data['url'], timeout=60)
    except Exception as exc:
        logger.warning('%s fetch failed %s: %s', tag, data['url'], exc)
        return None

    text = resp.text
    if REMOVED_MARKER_RE.search(text):
        logger.info('%s %s no longer available — marking removed', tag, data['listing_id'])
        return {'listing_id': data['listing_id'], 'url': data['url'], 'removed': True}

    result = _parse_detail_html(text, data)
    elapsed = time_mod.monotonic() - started
    logger.info(
        '%s parsed %s in %.1fs — %s | %s | %s imgs',
        tag,
        data['listing_id'],
        elapsed,
        result.get('price_text') or '?',
        (result.get('title') or '')[:60],
        len(result.get('images') or []),
    )
    return result


def _replace_listing_images(listing: Listing, image_urls: list[str]) -> int:
    """Delete every existing image for the listing and download all from scratch."""
    for img in listing.images.all():
        img.file.delete(save=False)
        img.delete()
    return _download_listing_images(listing, image_urls)


def _refresh_group_removed(group_id: int) -> None:
    """Recompute group.removed: True iff every listing in the group is removed."""
    group = ListingGroup.objects.filter(pk=group_id).first()
    if group is None:
        return
    listings = list(group.listings.all())
    if not listings:
        return
    all_removed = all(listing.removed for listing in listings)
    if group.removed != all_removed:
        group.removed = all_removed
        group.save(update_fields=['removed'])


def _apply_recheck_result(d: dict) -> int:
    """Persist a single recheck outcome. Returns number of images saved."""
    listing = Listing.objects.filter(listing_id=d['listing_id']).first()
    if listing is None:
        return 0

    now = timezone.now()
    if d.get('removed'):
        listing.removed = True
        listing.rechecked_at = now
        listing.save(update_fields=['removed', 'rechecked_at'])
        if listing.group_id:
            _refresh_group_removed(listing.group_id)
        return 0

    images = d.pop('images', [])
    update = {k: v for k, v in d.items() if k not in ('index', 'total') and v is not None}
    update['removed'] = False
    update['rechecked_at'] = now
    with transaction.atomic():
        for k, v in update.items():
            setattr(listing, k, v)
        listing.save()
    saved = _replace_listing_images(listing, images)
    if listing.group_id:
        _refresh_group_removed(listing.group_id)
    return saved


def recheck_oldest(fraction: float = RECHECK_FRACTION) -> dict:
    """Re-fetch the oldest-checked active listings to detect removals and refresh data.

    Picks ``ceil(active_total * fraction)`` listings ordered by ``rechecked_at`` ASC
    (nulls first) then ``first_seen_at`` ASC.
    """
    qs = Listing.objects.filter(removed=False)
    total_active = qs.count()
    if total_active == 0:
        return {'rechecked': 0, 'removed': 0, 'images': 0}

    n = min(RECHECK_MAX, math.ceil(total_active * fraction))
    rows = list(
        qs.order_by(F('rechecked_at').asc(nulls_first=True), 'first_seen_at').values(
            'listing_id', 'url'
        )[:n]
    )
    if not rows:
        return {'rechecked': 0, 'removed': 0, 'images': 0}

    logger.info('rechecking %s/%s active listings', len(rows), total_active)
    jobs = [{**r, 'index': i + 1, 'total': len(rows)} for i, r in enumerate(rows)]

    rechecked = 0
    removed = 0
    image_count = 0
    with ThreadPoolExecutor(max_workers=DETAIL_CONCURRENCY) as pool:
        futures = [pool.submit(_fetch_recheck, j) for j in jobs]
        for fut in as_completed(futures):
            d = fut.result()
            if not d:
                continue
            saved = _apply_recheck_result(d)
            image_count += saved
            rechecked += 1
            if d.get('removed'):
                removed += 1

    logger.info(
        'recheck done — rechecked=%s removed=%s images=%s',
        rechecked,
        removed,
        image_count,
    )
    return {'rechecked': rechecked, 'removed': removed, 'images': image_count}


def scrape(start_url: str | None = None, max_pages: int | None = None, full: bool = False) -> dict:
    """Scrape new listings and their images; return a summary dict."""
    start_url = start_url or PP_SVILLE

    cards = collect_new_listing_cards(start_url, max_pages=max_pages, full=full)
    total = len(cards)
    logger.info('found %s listing cards to process', total)

    new_count = 0
    image_count = 0
    if cards:
        jobs = [{**c, 'index': i + 1, 'total': total} for i, c in enumerate(cards)]
        with ThreadPoolExecutor(max_workers=DETAIL_CONCURRENCY) as pool:
            futures = [pool.submit(_fetch_detail, j) for j in jobs]
            for fut in as_completed(futures):
                d = fut.result()
                if not d:
                    continue
                listing, image_urls = _save_listing(d)
                new_count += 1
                saved = _download_listing_images(listing, image_urls)
                image_count += saved
                logger.info(
                    'progress %s/%s saved — listing=%s images=%s (total images=%s)',
                    new_count,
                    total,
                    listing.listing_id,
                    saved,
                    image_count,
                )

    result = {'new_listings': new_count, 'images': image_count}
    if not full:
        recheck = recheck_oldest()
        result['rechecked'] = recheck['rechecked']
        result['removed'] = recheck['removed']
        result['images'] += recheck['images']

    return result
