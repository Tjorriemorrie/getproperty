import logging
from dataclasses import dataclass, field
from urllib.parse import urlencode

from django.db import transaction
from django.db.models import Count, F, Q
from django.http import HttpResponseBadRequest, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from main.models import Listing, ListingGroup, ListingImage

logger = logging.getLogger(__name__)

FILTER_COOKIE = 'listing_filters'
FILTER_COOKIE_MAX_AGE = 60 * 60 * 24 * 365

SORT_OPTIONS = {
    'newest': ('-listed_at', 'Newest first'),
    'oldest': ('listed_at', 'Oldest first'),
    'price_asc': ('price', 'Price: low to high'),
    'price_desc': ('-price', 'Price: high to low'),
    'bedrooms_desc': ('-bedrooms', 'Most bedrooms'),
    'floor_size_asc': ('floor_size_m2', 'Floor size: small to large'),
}

# Complex facet value meaning "no complex name entered yet" (stored as '').
COMPLEX_NOT_SET = '__none__'

# Conditions shared by the In-a-complex/Liked radio filters and their counts.
# is_complex is kept identical across a group, so the shown primary's own value
# stands for the whole group.
COMPLEX_YES_Q = Q(is_complex=True)
COMPLEX_NO_Q = Q(is_complex=False)
LIKED_YES_Q = Q(group__isnull=True, is_liked=True) | Q(group__is_liked=True)
LIKED_NO_Q = Q(group__isnull=True, is_liked=False) | Q(group__is_liked=False)

# Percentile range facets: (skip key, model column, min param, max param, rounding step).
# Order matters: price is snapped before size within each pass.
RANGE_FACETS = (
    ('price', 'price', 'price_min', 'price_max', 50000),
    ('size', 'floor_size_m2', 'size_min', 'size_max', 10),
)
RANGE_SNAP_MAX_PASSES = 3

# Checkbox facets: (template context key, model column, URL param / skip key, filters attr).
CHECKBOX_FACETS = (
    ('type_facet', 'property_type', 'property_type', 'types'),
    ('bed_facet', 'bedrooms', 'beds', 'beds'),
    ('bath_facet', 'bathrooms', 'baths', 'baths'),
    ('garage_facet', 'garages', 'garages', 'garages'),
    ('complex_facet', 'complex_name', 'complex', 'complexes'),
)

# Sorts redone in Python after group siblings adjust price (None values last).
PYTHON_SORT_KEYS = {
    'price_asc': lambda li: (li.price is None, li.price or 0),
    'price_desc': lambda li: (li.price is None, -(li.price or 0)),
    'floor_size_asc': lambda li: (li.floor_size_m2 is None, li.floor_size_m2 or 0),
}

# A group needs at least this many listings.
MIN_GROUP_SIZE = 2


def _parse_float(value):
    try:
        return float(value) if value not in (None, '') else None
    except (TypeError, ValueError):
        return None


def _fmt_num(value):
    """Format a facet number (1.0 -> '1', 1.5 -> '1.5') consistently for URL and display."""
    if value is None:
        return None
    f = float(value)
    return str(int(f)) if f.is_integer() else str(f)


def _normalize_complex_name(raw, targets):
    """Tidy a typed complex name and fold it onto an existing spelling.

    Collapses whitespace, and if a listing outside `targets` (the listings about
    to be saved) already uses the same name in a different case, returns that
    stored spelling so the facet doesn't split "Blue Gum" and "blue gum".
    Excluding `targets` lets you fix the casing of a name only they use.
    """
    name = ' '.join((raw or '').split())[:200]
    if not name:
        logger.info('🏢 Normalised complex name to empty (clear)')
        return ''
    existing = (
        Listing.objects.filter(complex_name__iexact=name)
        .exclude(pk__in=targets.values('pk'))
        .order_by()
        .values_list('complex_name', flat=True)
        .first()
    )
    if existing and existing != name:
        logger.info(
            f'🏢 Folded complex name onto existing spelling: typed={name!r}, existing={existing!r}'
        )
        return existing
    logger.info(f'🏢 Normalised complex name: name={name!r}, existing_match={bool(existing)}')
    return name


def _percentile_thresholds(values, step):
    """Return (6 interior percentile thresholds, top-of-range value) for `values`.

    `values` must be sorted. Thresholds are rounded to `step` and de-duplicated,
    so fewer than 6 may come back.
    """
    if not values:
        return [], 0
    n = len(values)
    seen = set()
    thresholds = []
    for i in range(6):
        # Percentiles at (i+1)/7 — excludes 0 and 100.
        idx = int(round((i + 1) * (n - 1) / 7))
        rounded = int(round(float(values[idx]) / step) * step) or step
        if rounded in seen:
            continue
        seen.add(rounded)
        thresholds.append(rounded)

    max_value = int(round(float(values[-1]) / step) * step) or thresholds[-1] if thresholds else 0
    if thresholds and max_value <= thresholds[-1]:
        max_value = thresholds[-1] + step
    return thresholds, max_value


def _snap_range(min_v, max_v, thresholds, max_value):
    """Re-point a selected range onto the *current* threshold set.

    Range thresholds are percentiles of the filtered result set, so applying any
    other filter shifts them. Without snapping, the bounds still in the URL stop
    matching any rendered row and the facet looks unselected while it is in fact
    still narrowing the results. Snapping picks the nearest equivalent option, so
    the selection stays visible and the filter keeps applying at that value.

    Returns the (min, max) pair of exactly one rendered option: a "≥" row, a "≤"
    row, or an "=" band (the pair either side of a row).
    """
    if not thresholds or (min_v is None and max_v is None):
        return min_v, max_v
    if max_v is None:
        return min(thresholds, key=lambda t: abs(t - min_v)), None
    if min_v is None:
        return None, min(thresholds, key=lambda t: abs(t - max_v))

    # Both bounds set — an "=" band. Snap to the band that shifted the least.
    def _band(i):
        return (
            thresholds[i - 1] if i else 0,
            thresholds[i + 1] if i + 1 < len(thresholds) else max_value,
        )

    best = min(
        range(len(thresholds)),
        key=lambda i: abs(_band(i)[0] - min_v) + abs(_band(i)[1] - max_v),
    )
    return _band(best)


def _parse_floats(values):
    return [float(v) for v in values if _parse_float(v) is not None]


def _facet_value(raw):
    """Facet values as strings: text as-is, numbers via ``_fmt_num`` (1.0 -> '1')."""
    return raw if isinstance(raw, str) else _fmt_num(raw)


@dataclass
class ListingFilters:
    """List-page filter state parsed from the query string.

    The range bounds are mutable on purpose: ``_resolve_ranges`` re-snaps them
    onto the current percentile thresholds, and everything downstream (filtering,
    hidden inputs, facet links, the saved cookie) uses the snapped values.
    """

    q: str = ''
    types: list[str] = field(default_factory=list)
    beds: list[float] = field(default_factory=list)
    baths: list[float] = field(default_factory=list)
    garages: list[float] = field(default_factory=list)
    complexes: list[str] = field(default_factory=list)
    is_complex: str = ''
    is_liked: str = ''
    price_min: float | None = None
    price_max: float | None = None
    size_min: float | None = None
    size_max: float | None = None
    sort: str = 'newest'

    @classmethod
    def from_query(cls, params):
        """Build filters from ``request.GET``, dropping unparseable numbers."""
        return cls(
            q=params.get('q', '').strip(),
            types=params.getlist('property_type'),
            beds=_parse_floats(params.getlist('beds')),
            baths=_parse_floats(params.getlist('baths')),
            garages=_parse_floats(params.getlist('garages')),
            complexes=[c for c in params.getlist('complex') if c],
            is_complex=params.get('is_complex', ''),
            is_liked=params.get('is_liked', ''),
            price_min=_parse_float(params.get('price_min')),
            price_max=_parse_float(params.get('price_max')),
            size_min=_parse_float(params.get('size_min')),
            size_max=_parse_float(params.get('size_max')),
            sort=params.get('sort', 'newest'),
        )

    @property
    def bounds(self):
        """Current range bounds keyed by URL parameter (price_min, price_max, ...)."""
        return {
            param: getattr(self, param)
            for _key, _column, min_param, max_param, _step in RANGE_FACETS
            for param in (min_param, max_param)
        }

    def active_count(self):
        """Number of applied filters, shown in the sidebar's "Clear (n)" link."""
        multi = self.types + self.beds + self.baths + self.garages + self.complexes
        single = (self.q, self.is_complex, self.is_liked)
        return (
            len(multi)
            + sum(1 for v in single if v)
            + sum(1 for v in self.bounds.values() if v is not None)
        )


def _search_q(text):
    """Free-text search over title, headline, address and description."""
    if not text:
        return None
    return (
        Q(title__icontains=text)
        | Q(headline__icontains=text)
        | Q(address__icontains=text)
        | Q(description__icontains=text)
    )


def _complex_q(complexes):
    """Match any selected complex; ``COMPLEX_NOT_SET`` matches listings without one.

    complex_name is kept in sync across a group, so the shown primary's own
    column is authoritative.
    """
    if not complexes:
        return None
    cond = Q(complex_name__in=[c for c in complexes if c != COMPLEX_NOT_SET])
    if COMPLEX_NOT_SET in complexes:
        cond |= Q(complex_name='')
    return cond


def _range_q(column, low, high):
    """Inclusive bounds on `column`, either side optional; None when unbounded."""
    cond = Q()
    if low is not None:
        cond &= Q(**{f'{column}__gte': low})
    if high is not None:
        cond &= Q(**{f'{column}__lte': high})
    return cond or None


def _filter_conditions(filters):
    """Map each filter's skip key to the Q it applies (None when inactive)."""
    conds = {
        'q': _search_q(filters.q),
        'is_complex': {'yes': COMPLEX_YES_Q, 'no': COMPLEX_NO_Q}.get(filters.is_complex),
        'complex': _complex_q(filters.complexes),
        'is_liked': {'yes': LIKED_YES_Q, 'no': LIKED_NO_Q}.get(filters.is_liked),
        'price': _range_q('price', filters.price_min, filters.price_max),
        'size': _range_q('floor_size_m2', filters.size_min, filters.size_max),
    }
    for key, column, values in (
        ('property_type', 'property_type', filters.types),
        ('beds', 'bedrooms', filters.beds),
        ('baths', 'bathrooms', filters.baths),
        ('garages', 'garages', filters.garages),
    ):
        conds[key] = Q(**{f'{column}__in': values}) if values else None
    return conds


def _apply_filters(qs, filters, skip=None):
    """Apply every active filter except `skip`, so a facet can count its own options."""
    for key, cond in _filter_conditions(filters).items():
        if cond is not None and key != skip:
            qs = qs.filter(cond)
    return qs


def _resolve_ranges(base, filters):
    """Re-snap the range bounds in `filters`; return {key: (values, thresholds, max_value)}.

    Range thresholds are percentiles of the filtered set, so any other filter
    shifts them and the bounds from the URL must be re-snapped onto the new
    thresholds to stay applied *and* visibly selected. Each range's thresholds
    also depend on the other's bounds, so iterate to a fixed point — a pass that
    snaps nothing means every facet was built from the final bounds. Settles on
    the first pass unless a bound actually moved. Mutates `filters`.
    """
    ranges = {}
    passes = 0
    while passes < RANGE_SNAP_MAX_PASSES:
        passes += 1
        before = filters.bounds
        for key, column, min_param, max_param, step in RANGE_FACETS:
            values = sorted(
                _apply_filters(base, filters, skip=key)
                .filter(**{f'{column}__isnull': False, f'{column}__gt': 0})
                .values_list(column, flat=True)
            )
            thresholds, max_value = _percentile_thresholds(values, step=step)
            low, high = _snap_range(
                getattr(filters, min_param), getattr(filters, max_param), thresholds, max_value
            )
            setattr(filters, min_param, low)
            setattr(filters, max_param, high)
            ranges[key] = (values, thresholds, max_value)
        if filters.bounds == before:
            break
    logger.info(
        f'🏠 Resolved range facets: passes={passes}, bounds={filters.bounds}, '
        f'price_values={len(ranges["price"][0])}, size_values={len(ranges["size"][0])}'
    )
    return ranges


def _percentile_facet(params, filters, list_url, spec, data):
    """Build the ≤/=/≥ selector rows for one range facet; return (rows, clear_href).

    `filters` holds the already-snapped bounds and `data` the thresholds they were
    snapped onto, which is what guarantees the applied range still lights up a row.
    """
    _key, _column, min_param, max_param, _step = spec
    values, thresholds, max_value = data
    if not values:
        return [], None
    current_min = _fmt_num(getattr(filters, min_param)) or ''
    current_max = _fmt_num(getattr(filters, max_param)) or ''
    bounds = filters.bounds
    # Carry the *snapped* bounds of the other range facet, not the stale ones
    # still sitting in the URL, so clicking a row here doesn't resurrect them.
    base_params = [(k, v) for k, vals in params.lists() if k not in bounds for v in vals] + [
        (k, _fmt_num(v))
        for k, v in bounds.items()
        if k not in (min_param, max_param) and v is not None
    ]

    def _href(new_min, new_max):
        query = base_params + [(p, v) for p, v in ((min_param, new_min), (max_param, new_max)) if v]
        return '?' + urlencode(query) if query else list_url

    rows = []
    for i, rounded in enumerate(thresholds):
        prev_v = thresholds[i - 1] if i > 0 else 0
        next_v = thresholds[i + 1] if i + 1 < len(thresholds) else max_value
        v_str, prev_str, next_str = str(rounded), str(prev_v), str(next_v)
        gte_selected = current_min == v_str
        lte_selected = current_max == v_str
        eq_selected = current_min == prev_str and current_max == next_str
        rows.append(
            {
                'value': rounded,
                'display': f'{rounded:,}'.replace(',', ' '),
                'gte_count': sum(1 for v in values if v >= rounded),
                'lte_count': sum(1 for v in values if v <= rounded),
                'eq_count': sum(1 for v in values if prev_v <= v <= next_v),
                'gte_selected': gte_selected,
                'lte_selected': lte_selected,
                'eq_selected': eq_selected,
                'gte_href': _href('' if gte_selected else v_str, ''),
                'lte_href': _href('', '' if lte_selected else v_str),
                'eq_href': _href('', '') if eq_selected else _href(prev_str, next_str),
            }
        )
    clear_href = _href('', '') if current_min or current_max else None
    return rows, clear_href


def _build_facets(base, filters):
    """Checkbox facet rows and the In-a-complex/Liked radio counts, keyed for the template.

    Each facet is counted over the set filtered by everything except itself.
    """
    facets = {}
    for ctx_key, column, param, attr in CHECKBOX_FACETS:
        selected = {_facet_value(v) for v in getattr(filters, attr)}
        rows = (
            _apply_filters(base, filters, skip=param)
            .values(column)
            .annotate(n=Count('id'))
            .order_by(column)
        )
        items = []
        for row in rows:
            if row[column] in (None, ''):
                continue
            value = _facet_value(row[column])
            items.append((value, value, row['n'], value in selected))
        facets[ctx_key] = items

    # "Not set" leads the list so "In a complex: Yes + Not set" is an easy to-do view.
    not_set = _apply_filters(base, filters, skip='complex').filter(complex_name='').count()
    if not_set:
        facets['complex_facet'].insert(
            0, (COMPLEX_NOT_SET, '— Not set', not_set, COMPLEX_NOT_SET in filters.complexes)
        )

    complex_qs = _apply_filters(base, filters, skip='is_complex')
    facets['complex_counts'] = {
        'any': complex_qs.count(),
        'yes': complex_qs.filter(COMPLEX_YES_Q).count(),
        'no': complex_qs.filter(COMPLEX_NO_Q).count(),
    }
    liked_qs = _apply_filters(base, filters, skip='is_liked')
    facets['liked_counts'] = {
        'any': liked_qs.count(),
        'yes': liked_qs.filter(LIKED_YES_Q).count(),
        'no': liked_qs.exclude(LIKED_YES_Q).count(),
    }
    option_counts = ', '.join(f'{key}={len(facets[key])}' for key, *_ in CHECKBOX_FACETS)
    logger.info(
        f'🏠 Built list facets: {option_counts}, '
        f'complex_yes={facets["complex_counts"]["yes"]}, '
        f'complex_no={facets["complex_counts"]["no"]}'
    )
    return facets


def _display_image(listing):
    """Card thumbnail: the group's chosen image, else the listing's, else its first photo."""
    if listing.group_id and listing.group.main_image_id:
        return listing.group.main_image
    if listing.main_image_id:
        return listing.main_image
    images = list(listing.images.all())
    return images[0] if images else None


def _merge_sibling_display(listing):
    """Surface sibling data on a group's shown listing.

    Fills a missing address, takes the cheapest sibling price, and once the
    group is removed records its active span (earliest sibling start → removal).
    """
    group = listing.group
    siblings = list(group.listings.all())
    if not listing.address:
        listing.address = next((s.address for s in siblings if s.address), listing.address)
    priced = [s for s in siblings if s.price]
    if priced:
        cheapest = min(priced, key=lambda s: s.price)
        if listing.price is None or cheapest.price < listing.price:
            listing.price = cheapest.price
            listing.price_text = cheapest.price_text
    if group.removed_at:
        starts = [d for d in (s.listed_at or s.first_seen_at for s in siblings) if d]
        if starts:
            listing.display_removed_start = min(starts)
            listing.display_removed_end = group.removed_at


def _decorate_listings(listings):
    """Set the ``display_*`` attributes the card templates read.

    Each value comes from the group when the listing is grouped, else from the
    listing itself.
    """
    grouped = 0
    for listing in listings:
        group = listing.group if listing.group_id else None
        listing.display_notes = group.notes if group else listing.notes
        listing.display_is_liked = group.is_liked if group else listing.is_liked
        # A grouped listing only counts as removed once every sibling is removed,
        # which is exactly what ListingGroup.refresh_removed_at() tracks.
        listing.display_removed_at = group.removed_at if group else listing.removed_at
        listing.display_removed_start = None
        listing.display_removed_end = None
        listing.display_image = _display_image(listing)
        if group:
            _merge_sibling_display(listing)
            grouped += 1
    logger.info(f'🏠 Decorated listings for display: listings={len(listings)}, grouped={grouped}')


def listing_list(request):
    """Home page: show all listings with facet filters, sorting, and layout toggles.

    Filters are persisted in a cookie so they survive navigation to detail pages
    and back. Explicit clearing uses `?reset=1`.
    """
    list_url = reverse('main:listing_list')
    if 'reset' in request.GET:
        response = HttpResponseRedirect(list_url)
        response.delete_cookie(FILTER_COOKIE)
        logger.info('🏠 Cleared saved listing filters, redirecting to list')
        return response
    if not request.GET:
        saved = request.COOKIES.get(FILTER_COOKIE)
        if saved:
            logger.info('🏠 Restoring saved listing filters from cookie')
            return HttpResponseRedirect(f'{list_url}?{saved}')

    filters = ListingFilters.from_query(request.GET)
    # Only show one listing per group: solo listings, or the group's primary.
    base = Listing.objects.filter(Q(group__isnull=True) | Q(group__primary_id=F('id')))

    # Ranges are resolved before anything else is counted or filtered, because
    # everything downstream must use the snapped bounds.
    ranges = _resolve_ranges(base, filters)
    price_spec, size_spec = RANGE_FACETS
    price_facet, price_clear_href = _percentile_facet(
        request.GET, filters, list_url, price_spec, ranges['price']
    )
    size_facet, size_clear_href = _percentile_facet(
        request.GET, filters, list_url, size_spec, ranges['size']
    )
    facets = _build_facets(base, filters)

    sort_field, _label = SORT_OPTIONS.get(filters.sort, SORT_OPTIONS['newest'])
    listings_qs = (
        _apply_filters(base, filters)
        .select_related('main_image', 'group__main_image')
        .prefetch_related('images', 'group__listings')
        .order_by(sort_field, '-listed_at')
    )
    total = listings_qs.count()
    listings = list(listings_qs)
    _decorate_listings(listings)
    # Group siblings can lower a card's price, so these sorts are redone in Python.
    if filters.sort in PYTHON_SORT_KEYS:
        listings.sort(key=PYTHON_SORT_KEYS[filters.sort])

    # Removed listings go into their own section after every active one (POA
    # included). Active ones keep the chosen sort; removed ones are always most
    # recently removed first (the stable sort keeps the chosen order on ties).
    active_listings = [li for li in listings if not li.display_removed_at]
    removed_listings = sorted(
        (li for li in listings if li.display_removed_at),
        key=lambda li: li.display_removed_at,
        reverse=True,
    )

    # Snapped values, so the hidden inputs resubmit the bounds that are actually
    # highlighted rather than the stale ones from the URL.
    snapped_bounds = {k: _fmt_num(v) or '' for k, v in filters.bounds.items()}
    context = {
        'listings': listings,
        'active_listings': active_listings,
        'removed_listings': removed_listings,
        'total': total,
        **facets,
        'price_facet': price_facet,
        'price_clear_href': price_clear_href,
        'size_facet': size_facet,
        'size_clear_href': size_clear_href,
        'sort_options': [(k, v[1]) for k, v in SORT_OPTIONS.items()],
        'filters': {
            'q': filters.q,
            **snapped_bounds,
            'is_complex': filters.is_complex,
            'is_liked': filters.is_liked,
            'sort': filters.sort,
        },
        'active_count': filters.active_count(),
    }
    response = render(request, 'main/listing_list.html', context)
    # Persist the snapped bounds, so returning via the cookie restores the range
    # that was actually shown as selected.
    saved_params = request.GET.copy()
    for key, value in snapped_bounds.items():
        saved_params.setlist(key, [value] if value else [])
    response.set_cookie(
        FILTER_COOKIE,
        saved_params.urlencode(),
        max_age=FILTER_COOKIE_MAX_AGE,
        samesite='Lax',
    )
    logger.info(
        f'🏠 Rendered listing list: total={total}, active={len(active_listings)}, '
        f'removed={len(removed_listings)}, sort={filters.sort}, '
        f'filters={context["active_count"]}, complexes_selected={len(filters.complexes)}, '
        f'complex_options={len(facets["complex_facet"])}'
    )
    return response


def listing_detail(request, listing_id):
    """Detail page for a single listing, including gallery and all fields."""
    listing = get_object_or_404(Listing.objects.prefetch_related('images'), listing_id=listing_id)
    group_siblings = []
    is_primary = False
    anchor_listing_id = listing.listing_id
    if listing.group_id:
        group_siblings = list(
            listing.group.listings.order_by('listed_at', 'first_seen_at').values(
                'listing_id', 'agency', 'agent_name'
            )
        )
        is_primary = listing.group.primary_id == listing.pk
        if listing.group.primary_id:
            anchor_listing_id = listing.group.primary.listing_id
    property_types = sorted(
        t
        for t in Listing.objects.order_by().values_list('property_type', flat=True).distinct()
        if t
    )
    notes_value = listing.group.notes if listing.group_id else listing.notes
    is_liked_value = listing.group.is_liked if listing.group_id else listing.is_liked
    main_image_id = (
        listing.group.main_image_id if listing.group_id else listing.main_image_id
    ) or 0
    complex_names = list(
        Listing.objects.exclude(complex_name='')
        .order_by('complex_name')
        .values_list('complex_name', flat=True)
        .distinct()
    )
    active_start = listing.listed_at or listing.first_seen_at
    active_end = listing.removed_at or timezone.now()
    logger.info(
        f'🏠 Rendered listing detail: listing_id={listing.listing_id}, '
        f'group_id={listing.group_id}, siblings={len(group_siblings)}, '
        f'is_complex={listing.is_complex}, complex_name={listing.complex_name!r}, '
        f'complex_options={len(complex_names)}'
    )
    return render(
        request,
        'main/listing_detail.html',
        {
            'listing': listing,
            'group_siblings': group_siblings,
            'is_primary': is_primary,
            'anchor_listing_id': anchor_listing_id,
            'property_types': property_types,
            'complex_names': complex_names,
            'notes_value': notes_value,
            'is_liked_value': is_liked_value,
            'main_image_id': main_image_id,
            'active_start': active_start,
            'active_end': active_end,
        },
    )


def _target_group(listings):
    """Return (group to merge into, other groups it absorbs).

    Merges into the lowest-id existing group among `listings`, or a new group
    when none of them is grouped yet.
    """
    existing = {li.group_id for li in listings if li.group_id}
    if not existing:
        group = ListingGroup.objects.create()
        logger.info(f'🔗 Created new group for merge: group_id={group.pk}')
        return group, []
    group = ListingGroup.objects.get(pk=min(existing))
    absorbed = list(ListingGroup.objects.filter(pk__in=existing - {group.pk}))
    logger.info(f'🔗 Merging into existing group: group_id={group.pk}, absorbed={len(absorbed)}')
    return group, absorbed


def _merged_group_state(group, absorbed, listings):
    """Return (notes, is_liked) combined from everything being merged.

    Sources are the target group, absorbed groups, and any solo listings being
    added. Notes are de-duplicated in order. Disliked dominates: any source with
    is_liked=False makes the merged group disliked.
    """
    solo = [li for li in listings if not li.group_id]
    notes = []
    for text in (group.notes, *(g.notes for g in absorbed), *(li.notes for li in solo)):
        stripped = (text or '').strip()
        if stripped and stripped not in notes:
            notes.append(stripped)
    liked = group.is_liked and all(g.is_liked for g in absorbed) and all(li.is_liked for li in solo)
    logger.info(
        f'🔗 Combined group state: group_id={group.pk}, note_blocks={len(notes)}, '
        f'solo_listings={len(solo)}, is_liked={liked}'
    )
    return '\n\n'.join(notes), liked


@require_POST
def mark_similar(request):
    """Mark a set of listings as similar: merge/create a ListingGroup."""
    listing_ids = request.POST.getlist('listing_id')
    if len(listing_ids) < MIN_GROUP_SIZE:
        logger.warning(
            f'🔗 Rejected mark_similar: too few listings selected, listing_ids={listing_ids}'
        )
        return HttpResponseBadRequest('Select at least two listings.')

    listings = list(Listing.objects.filter(listing_id__in=listing_ids))
    if len(listings) < MIN_GROUP_SIZE:
        logger.warning(
            f'🔗 Rejected mark_similar: listings not found, requested={len(listing_ids)}, '
            f'found={len(listings)}, listing_ids={listing_ids[:5]}'
        )
        return HttpResponseBadRequest('Listings not found.')

    with transaction.atomic():
        group, absorbed = _target_group(listings)
        group.notes, group.is_liked = _merged_group_state(group, absorbed, listings)

        if absorbed:
            Listing.objects.filter(group_id__in=[g.pk for g in absorbed]).update(group=group)
            ListingGroup.objects.filter(pk__in=[g.pk for g in absorbed]).delete()

        # Notes and liked now live on the group; clear the listings' own copies.
        Listing.objects.filter(pk__in=[li.pk for li in listings]).update(
            group=group, notes='', is_liked=False
        )

        # The earliest-listed member represents the group (always exists: at
        # least MIN_GROUP_SIZE listings were just moved in).
        group.primary = group.listings.order_by('listed_at', 'first_seen_at').first()
        group.save(update_fields=['notes', 'is_liked', 'primary'])

        merged_complex = _sync_group_complex_name(group)
        merged_is_complex = _sync_group_is_complex(group)
        group.refresh_removed_at()

    next_url = request.POST.get('next') or ''
    if not url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
        next_url = reverse('main:listing_list')
    if group.primary_id:
        next_url = f'{next_url}#listing-{group.primary.listing_id}'
    logger.info(
        f'🔗 Marked listings similar: group_id={group.pk}, listings={len(listings)}, '
        f'absorbed_groups={len(absorbed)}, complex_name={merged_complex!r}, '
        f'is_complex={merged_is_complex}'
    )
    return HttpResponseRedirect(next_url)


def _sync_group_complex_name(group):
    """Give every group member the same complex name and return it.

    The primary's name wins (``mark_similar`` makes the earliest-listed member
    primary), else the first member that has one. Returns '' and changes
    nothing if no member has a name.
    """
    names = list(
        group.listings.exclude(complex_name='')
        .order_by('listed_at', 'first_seen_at')
        .values_list('complex_name', flat=True)
    )
    merged = names[0] if names else ''
    if not merged:
        logger.info(f'🔗 No complex name to sync in group: group_id={group.pk}')
        return ''
    distinct_names = set(names)
    if len(distinct_names) > 1:
        logger.warning(
            f'🔗 Conflicting complex names in group, keeping primary/first: '
            f'group_id={group.pk}, kept={merged!r}, names_count={len(distinct_names)}'
        )
    updated = group.listings.exclude(complex_name=merged).update(complex_name=merged)
    logger.info(
        f'🔗 Synced complex name across group: group_id={group.pk}, '
        f'complex_name={merged!r}, listings_updated={updated}'
    )
    return merged


def _sync_group_is_complex(group):
    """Mark every group member as a complex if any of them is; return the result.

    Duplicates are the same property, so one member toggled (or scraped with
    levies) is enough. Changes nothing when no member is a complex.
    """
    if not group.listings.filter(is_complex=True).exists():
        logger.info(f'🔗 No complex flag to sync in group: group_id={group.pk}')
        return False
    updated = group.listings.filter(is_complex=False).update(is_complex=True)
    logger.info(
        f'🔗 Synced is_complex across group: group_id={group.pk}, listings_updated={updated}'
    )
    return True


@require_POST
def unlink_similar(request, listing_id):
    """Remove a listing from its group. Deletes the group if it becomes trivial."""
    listing = get_object_or_404(Listing, listing_id=listing_id)
    group = listing.group
    if group is None:
        return redirect('main:listing_detail', listing_id=listing_id)

    with transaction.atomic():
        listing.group = None
        listing.save(update_fields=['group'])

        if group.main_image_id and group.main_image.listing_id == listing.pk:
            group.main_image = None
            group.save(update_fields=['main_image'])

        remaining = list(group.listings.order_by('listed_at', 'first_seen_at'))
        if len(remaining) <= 1:
            # Clear group from the last sibling and delete the group.
            if remaining:
                remaining[0].group = None
                remaining[0].save(update_fields=['group'])
            group.delete()
        else:
            if group.primary_id == listing.pk:
                group.primary = remaining[0]
                group.save(update_fields=['primary'])
            group.refresh_removed_at()

    return redirect('main:listing_detail', listing_id=listing_id)


@require_POST
def set_property_type(request, listing_id):
    """Update a listing's property_type from the detail page."""
    listing = get_object_or_404(Listing, listing_id=listing_id)
    new_type = (request.POST.get('property_type') or '').strip()
    if new_type:
        listing.property_type = new_type
        listing.save(update_fields=['property_type'])
    return redirect('main:listing_detail', listing_id=listing_id)


@require_POST
def set_is_complex(request, listing_id):
    """Save the "in a complex" toggle on this listing and every linked duplicate.

    Like the complex name, the whole group shares one value and each listing
    keeps it after a later unlink. Turning it off keeps the complex name.
    """
    listing = get_object_or_404(Listing, listing_id=listing_id)
    is_complex = request.POST.get('is_complex') in ('1', 'true', 'on', 'yes')
    if listing.group_id:
        targets = Listing.objects.filter(group_id=listing.group_id)
    else:
        targets = Listing.objects.filter(pk=listing.pk)
    updated = targets.exclude(is_complex=is_complex).update(is_complex=is_complex)
    logger.info(
        f'🏢 Saved is_complex: listing_id={listing_id}, group_id={listing.group_id}, '
        f'is_complex={is_complex}, listings_updated={updated}'
    )
    return JsonResponse({'saved': True, 'is_complex': is_complex})


@require_POST
def set_complex_name(request, listing_id):
    """Save the complex name on this listing and every linked duplicate.

    The whole group shares one value, and each listing keeps it after a later
    unlink. An empty value clears it.
    """
    listing = get_object_or_404(Listing, listing_id=listing_id)
    if listing.group_id:
        targets = Listing.objects.filter(group_id=listing.group_id)
    else:
        targets = Listing.objects.filter(pk=listing.pk)
    name = _normalize_complex_name(request.POST.get('complex_name'), targets)
    updated = targets.exclude(complex_name=name).update(complex_name=name)
    logger.info(
        f'🏢 Saved complex name: listing_id={listing_id}, group_id={listing.group_id}, '
        f'complex_name={name!r}, listings_updated={updated}'
    )
    return JsonResponse({'saved': True, 'complex_name': name})


@require_POST
def set_liked(request, listing_id):
    """Save the liked toggle.

    Stored on the group when the listing is grouped, so all duplicates share one
    value; otherwise on the listing itself.
    """
    listing = get_object_or_404(Listing, listing_id=listing_id)
    is_liked = request.POST.get('is_liked') in ('1', 'true', 'on', 'yes')
    if listing.group_id:
        listing.group.is_liked = is_liked
        listing.group.save(update_fields=['is_liked'])
    else:
        listing.is_liked = is_liked
        listing.save(update_fields=['is_liked'])
    logger.info(
        f'👍 Saved liked toggle: listing_id={listing_id}, group_id={listing.group_id}, '
        f'is_liked={is_liked}'
    )
    return JsonResponse({'saved': True, 'is_liked': is_liked})


@require_POST
def set_notes(request, listing_id):
    """Save free-text notes.

    Stored on the group when the listing is grouped, so all duplicates share the
    same note; otherwise on the listing itself.
    """
    listing = get_object_or_404(Listing, listing_id=listing_id)
    notes = request.POST.get('notes', '')
    if listing.group_id:
        listing.group.notes = notes
        listing.group.save(update_fields=['notes'])
    else:
        listing.notes = notes
        listing.save(update_fields=['notes'])
    logger.info(
        f'📝 Saved notes: listing_id={listing_id}, group_id={listing.group_id}, chars={len(notes)}'
    )
    return JsonResponse({'saved': True})


@require_POST
def delete_listing(request, listing_id):
    """Permanently delete a single listing and its image files.

    The group is preserved as long as any sibling remains — only deleted when
    this was the sole listing. Repicks the primary and refreshes ``removed_at``
    otherwise.
    """
    listing = get_object_or_404(Listing, listing_id=listing_id)
    group = listing.group
    listing_pk = listing.pk
    group_outcome = 'none'

    with transaction.atomic():
        images = list(listing.images.all())
        for img in images:
            img.file.delete(save=False)
        listing.delete()
        logger.info(f'🗑️ Deleted listing: listing_id={listing_id}, images={len(images)}')

        if group is not None:
            remaining = list(group.listings.order_by('listed_at', 'first_seen_at'))
            if not remaining:
                group.delete()
                group_outcome = 'deleted'
            else:
                if group.primary_id in (None, listing_pk):
                    group.primary = remaining[0]
                    group.save(update_fields=['primary'])
                group.refresh_removed_at()
                group_outcome = f'kept ({len(remaining)} remaining)'

    logger.info(
        f'🗑️ Delete listing completed: listing_id={listing_id}, '
        f'group_id={group.pk if group else None}, group={group_outcome}'
    )
    return redirect('main:listing_list')


@require_POST
def set_main_image(request, listing_id):
    """Set the image used to represent this listing (or its group) on list pages.

    Any image from the listing or any sibling in the group is valid.
    """
    listing = get_object_or_404(Listing, listing_id=listing_id)
    raw_image_id = request.POST.get('image_id', '')
    try:
        image_id = int(raw_image_id)
    except ValueError:
        logger.warning(
            f'⭐ Rejected main image: invalid image_id, listing_id={listing_id}, '
            f'image_id={raw_image_id!r}'
        )
        return HttpResponseBadRequest('Invalid image_id')

    if listing.group_id:
        image = ListingImage.objects.filter(id=image_id, listing__group_id=listing.group_id).first()
    else:
        image = ListingImage.objects.filter(id=image_id, listing_id=listing.pk).first()
    if image is None:
        logger.warning(
            f'⭐ Rejected main image: not in listing or group, listing_id={listing_id}, '
            f'group_id={listing.group_id}, image_id={image_id}'
        )
        return HttpResponseBadRequest('Image not found in this listing or its group.')

    if listing.group_id:
        listing.group.main_image = image
        listing.group.save(update_fields=['main_image'])
    else:
        listing.main_image = image
        listing.save(update_fields=['main_image'])
    logger.info(
        f'⭐ Set main image: listing_id={listing_id}, group_id={listing.group_id}, '
        f'image_id={image.id}'
    )
    return JsonResponse({'saved': True, 'main_image_id': image.id})


@require_POST
def set_primary(request, listing_id):
    """Promote a listing to be the primary representation of its group."""
    listing = get_object_or_404(Listing, listing_id=listing_id)
    if listing.group_id:
        listing.group.primary = listing
        listing.group.save(update_fields=['primary'])
    return redirect('main:listing_detail', listing_id=listing_id)
