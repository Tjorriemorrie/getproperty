# getproperty

Django-based scraper for South African property listings.

## Setup

```bash
uv sync
uv run python manage.py migrate
```

Create a `.env` with `SECRET_KEY`, `DEBUG`, `DEVELOPER` (see `getproperty/settings.py`).

## Scrape

```bash
uv run python manage.py scrape_privateproperty [--max-pages N] [--all]
```

Each run covers every `PP_*` search URL in `main/constants.py` (one per suburb):

1. **Walk**: fetches every list page and gathers all live listing IDs, then fetches detail pages and photos for listings not yet stored. Listings go in the `Listing` / `ListingImage` models; photos land under `media/listings/YYYY/MM/`.
2. **Reconcile**: stored active listings that are on no list page are re-fetched, and `removed_at` is set only if the detail page confirms the listing is gone. Removed listings that show up on a list page again are restored. Runs only when every walk reached its last page.
3. **Rotation**: re-fetches the least recently checked 1% of active listings to pick up price, detail and photo changes.

Options:

- `--max-pages N`: cap list pages per URL. This makes the walk incomplete, so the reconcile is skipped.
- `--all`: re-fetch details for every listed card, not just new ones. Skips the rotation.

### privateproperty.co.za findings

- Search results are server-rendered HTML with 20 cards per page; page 1 shows the total as "N properties".
- The next-page links are a bare `?page=N` that drops `sorttype=Date&sortorder=Descending`. Followed as-is, pages 2+ come back in a different order and a walk misses ~8% of listings. The walker copies the start URL's query onto every page link.
- Featured (older) cards (`a.featured-listing`) are mixed into page 1 among the newest ones, so stopping at the first known listing skipped new listings (106 live listings had never been scraped by 2026-11-19). The walk therefore always goes to the last page.
- A removed listing's URL redirects (HTTP 200) to its suburb's list page with `?archiveId=…`, which says "the listing with ref `<strong>T…</strong>` is no longer available".

## Browse

```bash
uv run python manage.py runserver
```

Open `http://127.0.0.1:8000/`:

- **Home** (`/`) — all listings with filters (search, type, in a complex, complex name, price range, liked, floor size, beds/baths/garages), sorting, and three layouts (cards, list, compact). No pagination. Removed listings that match the filters are listed last in their own **Removed** section, most recently removed first whatever the sort. Filters are remembered in a cookie; `?reset=1` clears them.
- **Detail** (`/listing/<listing_id>/`) — full details plus a Swiper.js photo gallery with thumbnails.

### Complexes

Most listings with a levy are in a sectional-title complex. The scraped data never includes the complex name, so you enter it by hand:

- Each detail page has a vertical **In a complex** switch (knob up = yes) in the stats grid. Switched on, the **Levies** tile (amount, or — if the listing gives none) and the **Complex** name tile show on the same row; switched off, both are hidden (the name is kept). It saves on click and is shared across linked duplicates.
- New listings that list levies start switched on; after that the scraper never changes it. The `0015` migration switched on every listing with levies, and the old R 1 levy placeholders became an empty levy on a listing that is switched on.
- Type the name into the **Complex** tile. It suggests names already in use and saves when you pick a suggestion, press Enter or click away. Clear the box to remove the name.
- Names are tidied on save: extra spaces are removed, and if the same name already exists in different capitals, the existing spelling is used.
- Linked duplicates share one complex name; it is copied to every listing in the group and kept if a listing is unlinked later.
- On the home page, the **In a complex** filter (Any / Yes / No) uses the same switch; it replaced the old Levies filter. The **Complex** filter lists every name with a count. **— Not set** finds listings that still need a name (combine with **In a complex: Yes** for a to-do list). Cards show the complex name before the address.

Styling uses Tailwind CSS via CDN and Google Fonts (Fraunces + Inter); no build step.
