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
uv run python manage.py scrape_privateproperty
```

Walks `PRIVATE_PROPERTY_URL` (defined in `main/constants.py`) newest-first, stops at the first already-saved listing, then fetches each new listing's detail page and photo gallery concurrently. Listings are stored in the `Listing` / `ListingImage` models; photos land under `media/listings/YYYY/MM/`.

Options:

- `--url URL` — override start URL.
- `--max-pages N` — cap list pages traversed.

## Browse

```bash
uv run python manage.py runserver
```

Open `http://127.0.0.1:8000/`:

- **Home** (`/`) — all listings with filters (search, type, levies, complex, price range, liked, floor size, beds/baths/garages), sorting, and three layouts (cards, list, compact). No pagination. Filters are remembered in a cookie; `?reset=1` clears them.
- **Detail** (`/listing/<listing_id>/`) — full details plus a Swiper.js photo gallery with thumbnails.

### Complexes

Most listings with a levy are in a sectional-title complex. The scraped data never includes the complex name, so you enter it by hand:

- On a detail page with levies (the R 1 placeholder counts), type the name into the **Complex** tile next to Levies. It suggests names already in use and saves when you pick a suggestion, press Enter or click away. Clear the box to remove the name.
- Names are tidied on save: extra spaces are removed, and if the same name already exists in different capitals, the existing spelling is used.
- Linked duplicates share one complex name; it is copied to every listing in the group and kept if a listing is unlinked later.
- On the home page, the **Complex** filter lists every name with a count. **— Not set** finds listings that still need a name (combine with **With levies** for a to-do list). Cards show the complex name before the address.

Styling uses Tailwind CSS via CDN and Google Fonts (Fraunces + Inter); no build step.
