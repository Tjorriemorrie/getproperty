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
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import imagehash
from botasaurus.request import Request, request
from botasaurus.soupify import soupify
from bs4 import BeautifulSoup
from django.core.files.base import ContentFile
from django.db import transaction
from django.db.models.functions import Coalesce
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
RECHECK_FRACTION = 0.01
RECHECK_PROGRESS_EVERY = 50
# A removed listing redirects to its suburb's list page, which says e.g. "the listing with
# ref <strong>T4534103</strong> is no longer available" (the ref is wrapped in markup).
REMOVED_MARKER_RE = re.compile(
    r'listing with ref\s*(?:<[^>]*>\s*)*T\d+\s*(?:<[^>]*>\s*)*is no longer available',
    re.IGNORECASE,
)
# HTTP statuses that confirm a listing is gone; any other 4xx/5xx is treated as a failed fetch.
GONE_STATUSES = (404, 410)
HTTP_ERROR_MIN = 400
RESULT_COUNT_RE = re.compile(r'(\d[\d \xa0]*)\s*properties', re.IGNORECASE)


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


def _keep_base_query(url: str, base_url: str) -> str:
    """Carry the start URL's query (e.g. the date sort) onto a pagination link.

    The site's next-page links are a bare ``?page=N``: following them as-is drops
    ``sorttype``/``sortorder``, so pages 2+ come back in a different order and a walk
    silently skips some listings and repeats others.
    """
    parts = urlsplit(url)
    query = {**dict(parse_qsl(urlsplit(base_url).query)), **dict(parse_qsl(parts.query))}
    return urlunsplit(parts._replace(query=urlencode(query)))


def _parse_result_count(html: str) -> int | None:
    """Return the search's stated total (the "118 properties" heading), if present."""
    m = RESULT_COUNT_RE.search(soupify(html).get_text(' '))
    return _parse_int(m.group(1)) if m else None


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


def walk_list_pages(start_url: str, max_pages: int | None = None) -> tuple[list[dict], bool]:
    """Walk every list page of a search; return ``(cards, complete)``.

    Always walks to the end rather than stopping at the first known listing: featured
    (older) cards are mixed in among the newest ones, so an early stop skips new listings
    that sit after them. ``complete`` is True only when the walk ran out of pages on its
    own (not cut short by ``max_pages`` or an empty page), i.e. ``cards`` is the full set
    of live listings for the search.
    """
    logger.info('📄 Walking list pages: start_url=%s, max_pages=%s', start_url, max_pages)
    url = start_url
    pages = 0
    stated: int | None = None
    cards_out: list[dict] = []
    seen: set[str] = set()
    complete = False

    while True:
        pages += 1
        logger.info('📄 Fetching list page %s: %s', pages, url)
        html = _fetch(url)
        if pages == 1:
            stated = _parse_result_count(html)
        cards = _parse_list_page(html)
        if not cards:
            logger.warning('⚠️ No cards parsed, walk incomplete: page=%s, url=%s', pages, url)
            break
        for c in cards:
            if c['listing_id'] not in seen:
                seen.add(c['listing_id'])
                cards_out.append(c)

        next_url = _find_next_page(url, html)
        next_url = _keep_base_query(next_url, start_url) if next_url else None
        if not next_url or next_url == url:
            complete = True
            break
        if max_pages and pages >= max_pages:
            break
        url = next_url

    if complete and stated is not None and len(cards_out) < stated:
        logger.warning(
            '⚠️ Walk found fewer cards than the site states: start_url=%s, cards=%s, stated=%s',
            start_url,
            len(cards_out),
            stated,
        )
    logger.info(
        '📄 Walked list pages: start_url=%s, pages=%s, cards=%s, stated=%s, complete=%s',
        start_url,
        pages,
        len(cards_out),
        stated,
        complete,
    )
    return cards_out, complete


def _save_listing(detail: dict) -> Listing:
    images = detail.pop('images', [])
    with transaction.atomic():
        defaults = {k: v for k, v in detail.items() if v is not None}
        # A new listing with levies starts out flagged as a complex; after that
        # the flag is only changed by hand, so updates never touch it.
        listing, created = Listing.objects.update_or_create(
            listing_id=detail['listing_id'],
            defaults=defaults,
            create_defaults={**defaults, 'is_complex': bool(defaults.get('levies'))},
        )
        if created and listing.listed_at is None:
            listing.listed_at = timezone.now()
            listing.save(update_fields=['listed_at'])
    logger.info(
        '💾 Saved listing: listing_id=%s, created=%s, is_complex=%s, images=%s',
        listing.listing_id,
        created,
        listing.is_complex,
        len(images),
    )
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


def _existing_image_index(listing: Listing):
    """Return (source URLs, content hashes, phash -> (file_size, pk)) for saved images.

    The phash map keeps the largest saved copy of each visually identical image.
    """
    existing_urls = set(listing.images.values_list('source_url', flat=True))
    existing_hashes = set(
        listing.images.exclude(image_hash='').values_list('image_hash', flat=True)
    )
    phash_best: dict[str, tuple[int, int | None]] = {}
    for pk, ph, fs in listing.images.exclude(phash='').values_list('pk', 'phash', 'file_size'):
        if ph not in phash_best or fs > phash_best[ph][0]:
            phash_best[ph] = (fs, pk)
    logger.info(
        '🖼️ Indexed saved images for %s: urls=%s, hashes=%s, phashes=%s',
        listing.listing_id,
        len(existing_urls),
        len(existing_hashes),
        len(phash_best),
    )
    return existing_urls, existing_hashes, phash_best


def _resolve_visual_duplicate(phash: str, file_size: int, phash_best: dict) -> str:
    """Keep the larger copy of a visually identical image.

    Returns 'skip' when a saved copy is at least as large, 'replaced' after
    deleting a smaller saved copy, and 'new' when there is no visual match (or
    the matched row is already gone).
    """
    if not phash or phash not in phash_best:
        return 'new'
    existing_size, existing_pk = phash_best[phash]
    if file_size <= existing_size:
        return 'skip'
    try:
        old_img = ListingImage.objects.get(pk=existing_pk)
    except ListingImage.DoesNotExist:
        logger.warning(
            '🖼️ Smaller duplicate already gone, saving new copy: phash=%s, pk=%s, size=%s',
            phash,
            existing_pk,
            file_size,
        )
        return 'new'
    old_img.file.delete(save=False)
    old_img.delete()
    logger.info(
        '🖼️ Replaced smaller duplicate: pk=%s, old_size=%s, new_size=%s',
        existing_pk,
        existing_size,
        file_size,
    )
    return 'replaced'


def _download_listing_images(listing: Listing, image_urls: list[str]) -> int:
    if not image_urls:
        logger.info('🖼️ No image URLs for %s', listing.listing_id)
        return 0
    existing_urls, existing_hashes, phash_best = _existing_image_index(listing)

    jobs = [
        {'listing_id': listing.listing_id, 'url': u, 'position': i}
        for i, u in enumerate(image_urls)
        if u not in existing_urls
    ]
    if not jobs:
        logger.info('🖼️ All %s images already saved for %s', len(image_urls), listing.listing_id)
        return 0

    logger.info('🖼️ Downloading %s images for %s', len(jobs), listing.listing_id)
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
        outcome = _resolve_visual_duplicate(phash, file_size, phash_best)
        if outcome == 'skip':
            skipped += 1
            continue
        replaced += outcome == 'replaced'

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
    logger.info(
        '🖼️ Saved %s/%s images for %s: skipped_duplicates=%s, replaced_smaller=%s',
        saved,
        len(jobs),
        listing.listing_id,
        skipped,
        replaced,
    )
    return saved


@request(cache=False, output=None, create_error_logs=False, raise_exception=False)
def _fetch_recheck(req: Request, data: dict) -> dict | None:
    idx = data.get('index')
    total = data.get('total')
    tag = f'[recheck {idx}/{total}]' if idx and total else '[recheck]'
    started = time_mod.monotonic()
    logger.info('🔁 %s fetching %s', tag, data['listing_id'])
    try:
        resp = req.get(data['url'], timeout=60)
    except Exception as exc:
        logger.warning('⚠️ %s fetch failed %s: %s', tag, data['url'], exc)
        return None

    status = resp.status_code
    text = resp.text
    if status in GONE_STATUSES or REMOVED_MARKER_RE.search(text):
        logger.info(
            '🔁 %s %s no longer available — marking removed: status=%s',
            tag,
            data['listing_id'],
            status,
        )
        return {'listing_id': data['listing_id'], 'url': data['url'], 'removed': True}
    if status >= HTTP_ERROR_MIN:
        logger.warning(
            '⚠️ %s HTTP error, listing left untouched: listing_id=%s, status=%s, url=%s',
            tag,
            data['listing_id'],
            status,
            data['url'],
        )
        return None

    result = _parse_detail_html(text, data)
    elapsed = time_mod.monotonic() - started
    logger.info(
        '🔁 %s parsed %s in %.1fs — %s | %s | %s imgs',
        tag,
        data['listing_id'],
        elapsed,
        result.get('price_text') or '?',
        (result.get('title') or '')[:60],
        len(result.get('images') or []),
    )
    return result


def _replace_listing_images(listing: Listing, image_urls: list[str]) -> int:
    """Replace the listing's images with freshly downloaded copies.

    Pre-fetches all blobs first; only deletes the existing set once at least one
    new image has been fetched successfully. This prevents a broken/empty
    recheck response from wiping good data.
    """
    if not image_urls:
        return 0
    jobs = [
        {'listing_id': listing.listing_id, 'url': u, 'position': i}
        for i, u in enumerate(image_urls)
    ]
    logger.info('pre-fetching %s replacement images for %s', len(jobs), listing.listing_id)
    results = _fetch_image(jobs, parallel=IMAGE_CONCURRENCY)
    fetched = [r for r in results if r and r.get('content')]
    if not fetched:
        logger.warning(
            'no images downloaded for %s — keeping existing images intact',
            listing.listing_id,
        )
        return 0

    for img in listing.images.all():
        img.file.delete(save=False)
        img.delete()

    saved = 0
    seen_hashes: set[str] = set()
    phash_best: dict[str, tuple[int, int]] = {}
    for r in fetched:
        content_hash = hashlib.md5(r['content']).hexdigest()  # noqa: S324
        if content_hash in seen_hashes:
            continue
        seen_hashes.add(content_hash)
        file_size = len(r['content'])
        phash = _compute_phash(r['content'])
        if phash and phash in phash_best:
            existing_size, existing_pk = phash_best[phash]
            if file_size <= existing_size:
                continue
            try:
                old_img = ListingImage.objects.get(pk=existing_pk)
                old_img.file.delete(save=False)
                old_img.delete()
            except ListingImage.DoesNotExist:
                pass
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
    logger.info('saved %s/%s replacement images for %s', saved, len(jobs), listing.listing_id)
    return saved


def _mark_removed(listing: Listing, now) -> None:
    if listing.removed_at is None:
        listing.removed_at = now
    listing.rechecked_at = now
    listing.save(update_fields=['removed_at', 'rechecked_at'])
    if listing.group_id:
        group = ListingGroup.objects.filter(pk=listing.group_id).first()
        if group:
            group.refresh_removed_at()


def _apply_recheck_result(d: dict, replace_images: bool = True) -> tuple[str, int]:
    """Persist a single recheck outcome; return ``(outcome, images_saved)``.

    ``outcome`` is ``'removed'``, ``'refreshed'``, ``'restored'`` (was removed, live
    again) or ``'unknown'`` (no such listing in the DB). Only overwrites listing
    fields/images when the parse clearly indicates the listing is still available
    (title or price text or headline AND ≥1 image). Otherwise marks the listing removed
    without touching existing data. With ``replace_images=False`` a live listing's photos
    are topped up incrementally (only unseen URLs are downloaded) instead of re-fetching
    the whole set.
    """
    listing = Listing.objects.filter(listing_id=d['listing_id']).first()
    if listing is None:
        logger.warning('⚠️ Recheck result for unknown listing: listing_id=%s', d['listing_id'])
        return 'unknown', 0

    now = timezone.now()
    if d.get('removed'):
        _mark_removed(listing, now)
        logger.info('🔁 Recheck marked listing removed: listing_id=%s', listing.listing_id)
        return 'removed', 0

    images = d.pop('images', [])
    has_text = bool(d.get('title') or d.get('price_text') or d.get('headline'))
    if not has_text or not images:
        _mark_removed(listing, now)
        logger.warning(
            '⚠️ Recheck parse missing key fields, marked removed: listing_id=%s, text=%s, images=%s',
            listing.listing_id,
            has_text,
            len(images),
        )
        return 'removed', 0

    was_removed = listing.removed_at is not None
    update = {k: v for k, v in d.items() if k not in ('index', 'total') and v is not None}
    update['removed_at'] = None
    update['rechecked_at'] = now
    with transaction.atomic():
        for k, v in update.items():
            setattr(listing, k, v)
        listing.save()
    if replace_images:
        saved = _replace_listing_images(listing, images)
    else:
        saved = _download_listing_images(listing, images)
    if listing.group_id:
        group = ListingGroup.objects.filter(pk=listing.group_id).first()
        if group:
            group.refresh_removed_at()
    outcome = 'restored' if was_removed else 'refreshed'
    logger.info(
        '🔁 Recheck %s listing: listing_id=%s, images_saved=%s, replace_images=%s',
        outcome,
        listing.listing_id,
        saved,
        replace_images,
    )
    return outcome, saved


def _recheck_rows(rows: list[dict], reason: str, replace_images: bool = True) -> dict:
    """Re-fetch the given listings (``listing_id`` + ``url``) concurrently and apply each result."""
    stats = {'rechecked': 0, 'removed': 0, 'restored': 0, 'failed': 0, 'images': 0}
    if not rows:
        logger.info('🔁 Nothing to recheck: reason=%s', reason)
        return stats

    total = len(rows)
    logger.info('🔁 Rechecking listings: reason=%s, count=%s', reason, total)
    jobs = [{**r, 'index': i + 1, 'total': total} for i, r in enumerate(rows)]
    done = 0
    with ThreadPoolExecutor(max_workers=DETAIL_CONCURRENCY) as pool:
        futures = [pool.submit(_fetch_recheck, j) for j in jobs]
        for fut in as_completed(futures):
            done += 1
            d = fut.result()
            if d:
                outcome, saved = _apply_recheck_result(d, replace_images=replace_images)
                stats['rechecked'] += 1
                stats['images'] += saved
                if outcome in ('removed', 'restored'):
                    stats[outcome] += 1
            else:
                stats['failed'] += 1
            if done % RECHECK_PROGRESS_EVERY == 0:
                logger.info('🔁 Recheck progress: reason=%s, done=%s/%s', reason, done, total)

    logger.info('🔁 Recheck done: reason=%s, total=%s, %s', reason, total, stats)
    return stats


def recheck_oldest(fraction: float = RECHECK_FRACTION) -> dict:
    """Re-fetch the least recently checked active listings to refresh their data.

    Picks ``ceil(active_total * fraction)`` listings ordered by when they were last
    looked at (``rechecked_at``, else ``first_seen_at``), so the whole set rotates.
    Ordering never-rechecked listings first instead starves the rotation: new listings
    arrive faster than the 1% budget, so an already-rechecked listing never comes up again.
    """
    qs = Listing.objects.filter(removed_at__isnull=True)
    total_active = qs.count()
    n = math.ceil(total_active * fraction)
    rows = list(
        qs.order_by(Coalesce('rechecked_at', 'first_seen_at'), 'first_seen_at').values(
            'listing_id', 'url'
        )[:n]
    )
    logger.info('🔁 Selected oldest-checked: selected=%s, active=%s', len(rows), total_active)
    stats = _recheck_rows(rows, reason='rotation')
    logger.info('🔁 Rotation recheck completed: active=%s, %s', total_active, stats)
    return stats


def reconcile_with_live(live_ids: set[str]) -> dict:
    """Recheck listings whose stored status disagrees with a complete list-page walk.

    ``live_ids`` must be every listing currently on the site's list pages (all sources,
    all pages). Active listings missing from it are re-fetched to confirm the removal on
    their detail page; removed listings present in it are re-fetched so they get restored.
    """
    gone = [
        r
        for r in Listing.objects.filter(removed_at__isnull=True).values('listing_id', 'url')
        if r['listing_id'] not in live_ids
    ]
    back = [
        r
        for r in Listing.objects.filter(removed_at__isnull=False).values('listing_id', 'url')
        if r['listing_id'] in live_ids
    ]
    logger.info(
        '🔁 Reconciling with list pages: live=%s, active_not_listed=%s, removed_but_listed=%s',
        len(live_ids),
        len(gone),
        len(back),
    )
    # Incremental photos: a few live listings sit outside the searched suburbs and come up
    # here on every run, so a full re-download each time would be pure churn.
    stats = _recheck_rows(gone + back, reason='reconcile', replace_images=False)
    logger.info('🔁 Reconcile completed: live=%s, %s', len(live_ids), stats)
    return stats


def scrape(start_url: str | None = None, max_pages: int | None = None, full: bool = False) -> dict:
    """Scrape new listings (every card when ``full``) and their images; return a summary.

    The summary's ``live_ids``/``complete`` describe the list-page walk, for
    :func:`reconcile_with_live`.
    """
    start_url = start_url or PP_SVILLE

    all_cards, complete = walk_list_pages(start_url, max_pages=max_pages)
    live_ids = {c['listing_id'] for c in all_cards}
    if full:
        cards = all_cards
    else:
        existing = set(Listing.objects.values_list('listing_id', flat=True))
        cards = [c for c in all_cards if c['listing_id'] not in existing]
    total = len(cards)
    logger.info(
        '📄 Found listing cards to process: to_fetch=%s, live=%s, full=%s',
        total,
        len(live_ids),
        full,
    )

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

    logger.info(
        '📄 Scrape completed: start_url=%s, saved=%s/%s, images=%s, live=%s, complete=%s',
        start_url,
        new_count,
        total,
        image_count,
        len(live_ids),
        complete,
    )
    return {
        'new_listings': new_count,
        'images': image_count,
        'live_ids': live_ids,
        'complete': complete,
    }
