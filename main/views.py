from urllib.parse import urlencode

from django.db import transaction
from django.db.models import Count, F, Q
from django.http import HttpResponseBadRequest, HttpResponseRedirect, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from main.models import Listing, ListingGroup

FILTER_COOKIE = 'listing_filters'
FILTER_COOKIE_MAX_AGE = 60 * 60 * 24 * 365

SORT_OPTIONS = {
    'newest': ('-listed_at', 'Newest first'),
    'oldest': ('listed_at', 'Oldest first'),
    'price_asc': ('price', 'Price: low to high'),
    'price_desc': ('-price', 'Price: high to low'),
    'bedrooms_desc': ('-bedrooms', 'Most bedrooms'),
}


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


def listing_list(request):
    """Home page: show all listings with facet filters, sorting, and layout toggles.

    Filters are persisted in a cookie so they survive navigation to detail pages
    and back. Explicit clearing uses `?reset=1`.
    """
    list_url = reverse('main:listing_list')
    if 'reset' in request.GET:
        response = HttpResponseRedirect(list_url)
        response.delete_cookie(FILTER_COOKIE)
        return response
    if not request.GET:
        saved = request.COOKIES.get(FILTER_COOKIE)
        if saved:
            return HttpResponseRedirect(f'{list_url}?{saved}')

    q = request.GET.get('q', '').strip()
    types = request.GET.getlist('property_type')
    beds = request.GET.getlist('beds')
    baths = request.GET.getlist('baths')
    garages = request.GET.getlist('garages')
    has_levies = request.GET.get('has_levies', '')
    price_min = _parse_float(request.GET.get('price_min'))
    price_max = _parse_float(request.GET.get('price_max'))
    size_min = _parse_float(request.GET.get('size_min'))
    size_max = _parse_float(request.GET.get('size_max'))
    sort = request.GET.get('sort', 'newest')

    beds_f = [float(b) for b in beds if _parse_float(b) is not None]
    baths_f = [float(b) for b in baths if _parse_float(b) is not None]
    garages_f = [float(g) for g in garages if _parse_float(g) is not None]

    # Only show one listing per group: solo listings, or the group's primary.
    base = Listing.objects.filter(Q(group__isnull=True) | Q(group__primary_id=F('id')))

    def apply_filters(qs, skip=None):
        if q:
            qs = qs.filter(
                Q(title__icontains=q)
                | Q(headline__icontains=q)
                | Q(address__icontains=q)
                | Q(description__icontains=q)
            )
        if skip != 'property_type' and types:
            qs = qs.filter(property_type__in=types)
        if skip != 'beds' and beds_f:
            qs = qs.filter(bedrooms__in=beds_f)
        if skip != 'baths' and baths_f:
            qs = qs.filter(bathrooms__in=baths_f)
        if skip != 'garages' and garages_f:
            qs = qs.filter(garages__in=garages_f)
        if skip != 'has_levies':
            if has_levies == 'yes':
                qs = qs.filter(levies__isnull=False, levies__gt=0)
            elif has_levies == 'no':
                qs = qs.filter(Q(levies__isnull=True) | Q(levies=0))
        if skip != 'price':
            if price_min is not None:
                qs = qs.filter(price__gte=price_min)
            if price_max is not None:
                qs = qs.filter(price__lte=price_max)
        if skip != 'size':
            if size_min is not None:
                qs = qs.filter(floor_size_m2__gte=size_min)
            if size_max is not None:
                qs = qs.filter(floor_size_m2__lte=size_max)
        return qs

    def facet(field, skip_key, selected_strs):
        """Return [(value_str, label, count, is_selected), ...] for a faceted field."""
        qs = apply_filters(base, skip=skip_key)
        rows = qs.values(field).annotate(n=Count('id')).order_by(field)
        out = []
        for row in rows:
            raw = row[field]
            if raw in (None, ''):
                continue
            value = _fmt_num(raw) if not isinstance(raw, str) else raw
            label = value
            out.append((value, label, row['n'], value in selected_strs))
        return out

    type_facet = facet('property_type', 'property_type', set(types))
    bed_facet = facet('bedrooms', 'beds', {_fmt_num(b) for b in beds_f})
    bath_facet = facet('bathrooms', 'baths', {_fmt_num(b) for b in baths_f})
    garage_facet = facet('garages', 'garages', {_fmt_num(g) for g in garages_f})

    def percentile_facet(values, min_param, max_param, step):
        """Build 6 interior percentile thresholds with ≤/≥ selectors."""
        rows = []
        clear_href = None
        if not values:
            return rows, clear_href
        n = len(values)
        current_min_str = request.GET.get(min_param, '')
        current_max_str = request.GET.get(max_param, '')
        base_params = [
            (k, v)
            for k, vals in request.GET.lists()
            if k not in (min_param, max_param)
            for v in vals
        ]

        def _href(new_min, new_max):
            params = list(base_params)
            if new_min:
                params.append((min_param, new_min))
            if new_max:
                params.append((max_param, new_max))
            return '?' + urlencode(params) if params else list_url

        if current_min_str or current_max_str:
            clear_href = _href('', '')

        seen = set()
        thresholds = []
        for i in range(6):
            # Percentiles at (i+1)/7 — excludes 0 and 100.
            idx = int(round((i + 1) * (n - 1) / 7))
            raw = float(values[idx])
            rounded = int(round(raw / step) * step) or step
            if rounded in seen:
                continue
            seen.add(rounded)
            thresholds.append(rounded)

        max_value = (
            int(round(float(values[-1]) / step) * step) or thresholds[-1] if thresholds else 0
        )
        if thresholds and max_value <= thresholds[-1]:
            max_value = thresholds[-1] + step

        for i, rounded in enumerate(thresholds):
            prev_v = thresholds[i - 1] if i > 0 else 0
            next_v = thresholds[i + 1] if i + 1 < len(thresholds) else max_value
            gte_count = sum(1 for v in values if v >= rounded)
            lte_count = sum(1 for v in values if v <= rounded)
            eq_count = sum(1 for v in values if prev_v <= v <= next_v)
            v_str = str(rounded)
            prev_str = str(prev_v)
            next_str = str(next_v)
            gte_selected = current_min_str == v_str
            lte_selected = current_max_str == v_str
            eq_selected = current_min_str == prev_str and current_max_str == next_str
            rows.append(
                {
                    'value': rounded,
                    'display': f'{rounded:,}'.replace(',', ' '),
                    'gte_count': gte_count,
                    'lte_count': lte_count,
                    'eq_count': eq_count,
                    'gte_selected': gte_selected,
                    'lte_selected': lte_selected,
                    'eq_selected': eq_selected,
                    'gte_href': _href('' if gte_selected else v_str, ''),
                    'lte_href': _href('', '' if lte_selected else v_str),
                    'eq_href': _href('', '') if eq_selected else _href(prev_str, next_str),
                }
            )
        return rows, clear_href

    price_values = sorted(
        apply_filters(base, skip='price')
        .filter(price__isnull=False, price__gt=0)
        .values_list('price', flat=True)
    )
    price_facet, price_clear_href = percentile_facet(
        price_values, 'price_min', 'price_max', step=50000
    )

    size_values = sorted(
        apply_filters(base, skip='size')
        .filter(floor_size_m2__isnull=False, floor_size_m2__gt=0)
        .values_list('floor_size_m2', flat=True)
    )
    size_facet, size_clear_href = percentile_facet(size_values, 'size_min', 'size_max', step=10)

    levies_qs = apply_filters(base, skip='has_levies')
    levies_counts = {
        'any': levies_qs.count(),
        'yes': levies_qs.filter(levies__isnull=False, levies__gt=0).count(),
        'no': levies_qs.filter(Q(levies__isnull=True) | Q(levies=0)).count(),
    }

    sort_field, _label = SORT_OPTIONS.get(sort, SORT_OPTIONS['newest'])
    listings_qs = (
        apply_filters(base)
        .prefetch_related('images', 'group__listings')
        .order_by(sort_field, '-listed_at')
    )
    total = listings_qs.count()
    # For grouped listings, surface any sibling's address and the lowest sibling price.
    listings = list(listings_qs)
    for l in listings:
        l.display_notes = l.group.notes if l.group_id else l.notes
        if not l.group_id:
            continue
        siblings = list(l.group.listings.all())
        if not l.address:
            for s in siblings:
                if s.address:
                    l.address = s.address
                    break
        priced = [s for s in siblings if s.price]
        if priced:
            cheapest = min(priced, key=lambda s: s.price)
            if l.price is None or cheapest.price < l.price:
                l.price = cheapest.price
                l.price_text = cheapest.price_text

    if sort == 'price_asc':
        listings.sort(key=lambda l: (l.price is None, l.price or 0))
    elif sort == 'price_desc':
        listings.sort(key=lambda l: (l.price is None, -(l.price or 0)))

    context = {
        'listings': listings,
        'total': total,
        'type_facet': type_facet,
        'bed_facet': bed_facet,
        'bath_facet': bath_facet,
        'garage_facet': garage_facet,
        'price_facet': price_facet,
        'price_clear_href': price_clear_href,
        'size_facet': size_facet,
        'size_clear_href': size_clear_href,
        'levies_counts': levies_counts,
        'sort_options': [(k, v[1]) for k, v in SORT_OPTIONS.items()],
        'filters': {
            'q': q,
            'price_min': request.GET.get('price_min', ''),
            'price_max': request.GET.get('price_max', ''),
            'size_min': request.GET.get('size_min', ''),
            'size_max': request.GET.get('size_max', ''),
            'has_levies': has_levies,
            'sort': sort,
        },
        'active_count': (
            len(types)
            + len(beds)
            + len(baths)
            + len(garages)
            + (1 if has_levies else 0)
            + (1 if price_min is not None else 0)
            + (1 if price_max is not None else 0)
            + (1 if size_min is not None else 0)
            + (1 if size_max is not None else 0)
            + (1 if q else 0)
        ),
    }
    response = render(request, 'main/listing_list.html', context)
    response.set_cookie(
        FILTER_COOKIE,
        request.GET.urlencode(),
        max_age=FILTER_COOKIE_MAX_AGE,
        samesite='Lax',
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
    return render(
        request,
        'main/listing_detail.html',
        {
            'listing': listing,
            'group_siblings': group_siblings,
            'is_primary': is_primary,
            'anchor_listing_id': anchor_listing_id,
            'property_types': property_types,
            'notes_value': notes_value,
        },
    )


@require_POST
def mark_similar(request):
    """Mark a set of listings as similar: merge/create a ListingGroup."""
    listing_ids = request.POST.getlist('listing_id')
    if len(listing_ids) < 2:
        return HttpResponseBadRequest('Select at least two listings.')

    listings = list(Listing.objects.filter(listing_id__in=listing_ids))
    if len(listings) < 2:
        return HttpResponseBadRequest('Listings not found.')

    with transaction.atomic():
        existing_groups = {l.group_id for l in listings if l.group_id}
        if existing_groups:
            group_id = min(existing_groups)
            group = ListingGroup.objects.get(pk=group_id)
            other_group_ids = existing_groups - {group_id}
            absorbed = list(ListingGroup.objects.filter(pk__in=other_group_ids))
        else:
            group = ListingGroup.objects.create()
            absorbed = []

        # Combine notes from the target group, any absorbed groups, and any
        # solo listings being added. De-duplicate while preserving order.
        combined = []
        seen = set()

        def _add(text):
            t = (text or '').strip()
            if t and t not in seen:
                seen.add(t)
                combined.append(t)

        _add(group.notes)
        for g in absorbed:
            _add(g.notes)
        for l in listings:
            if not l.group_id:
                _add(l.notes)

        if absorbed:
            Listing.objects.filter(group_id__in=[g.pk for g in absorbed]).update(group=group)
            ListingGroup.objects.filter(pk__in=[g.pk for g in absorbed]).delete()

        Listing.objects.filter(pk__in=[l.pk for l in listings]).update(group=group, notes='')

        merged_notes = '\n\n'.join(combined)
        if merged_notes != group.notes:
            group.notes = merged_notes
            group.save(update_fields=['notes'])

        primary = group.listings.order_by('listed_at', 'first_seen_at').first()
        if primary and group.primary_id != primary.pk:
            group.primary = primary
            group.save(update_fields=['primary'])

        all_removed = all(l.removed for l in group.listings.all())
        if group.removed != all_removed:
            group.removed = all_removed
            group.save(update_fields=['removed'])

    next_url = request.POST.get('next') or ''
    if not url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()}):
        next_url = reverse('main:listing_list')
    return HttpResponseRedirect(next_url)


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
            all_removed = all(l.removed for l in remaining)
            if group.removed != all_removed:
                group.removed = all_removed
                group.save(update_fields=['removed'])

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
def set_levy_placeholder(request, listing_id):
    """Set levies to 1 so the listing no longer filters under 'no levies'."""
    listing = get_object_or_404(Listing, listing_id=listing_id)
    if not listing.levies:
        listing.levies = 1
        listing.save(update_fields=['levies'])
    return redirect('main:listing_detail', listing_id=listing_id)


@require_POST
def set_notes(request, listing_id):
    """Save free-text notes. Stored on the group when the listing is grouped,
    so all duplicates share the same note; otherwise on the listing itself.
    """
    listing = get_object_or_404(Listing, listing_id=listing_id)
    notes = request.POST.get('notes', '')
    if listing.group_id:
        listing.group.notes = notes
        listing.group.save(update_fields=['notes'])
    else:
        listing.notes = notes
        listing.save(update_fields=['notes'])
    return JsonResponse({'saved': True})


@require_POST
def set_primary(request, listing_id):
    """Promote a listing to be the primary representation of its group."""
    listing = get_object_or_404(Listing, listing_id=listing_id)
    if listing.group_id:
        listing.group.primary = listing
        listing.group.save(update_fields=['primary'])
    return redirect('main:listing_detail', listing_id=listing_id)
