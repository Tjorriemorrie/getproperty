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

- **Home** (`/`) — all listings with filters (search, city, suburb, type, price range, min beds/baths), sorting, and three layouts (cards, list, compact). No pagination.
- **Detail** (`/listing/<listing_id>/`) — full details plus a Swiper.js photo gallery with thumbnails.

Styling uses Tailwind CSS via CDN and Google Fonts (Fraunces + Inter); no build step.
