# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Maintenance

Always keep this file up to date. Whenever you learn or introduce something a future Claude Code session would need to be productive — new commands, dependencies, architecture, conventions, gotchas — update the relevant section here in the same change.

Also keep `README.md` up to date as the project develops. Unlike this file (which targets Claude Code), the README targets human readers: project purpose, setup/install steps, how to run, and any other information relevant for someone new to the repo. Update it in the same change whenever user-facing behavior, setup, or commands change.

## Project

Property scraper. Python 3.13.1, managed with `uv` (see `pyproject.toml` + `.python-version`, `.venv/` in-tree). Django project `getproperty/` with a single app `main/` registered in `INSTALLED_APPS`.

A public-facing browse UI lives in the same app: `main/urls.py` registers `listing_list` (home, `/`) and `listing_detail` (`/listing/<listing_id>/`) in `main/views.py`, rendered by templates in `main/templates/main/` (`base.html`, `listing_list.html`, `listing_detail.html`, plus `_card_grid/list/compact.html` partials). The project `getproperty/urls.py` `include`s `main.urls` at `/` and serves `MEDIA_URL` in `DEBUG`. Frontend is Tailwind CDN + Google Fonts (Fraunces display, Inter body), Alpine.js for the layout toggle, and Swiper.js for the detail gallery — no build step.

Duplicate listings from different agents can be linked via `main.models.ListingGroup`. Listings have a nullable `group` FK; the group has a `primary` FK to the Listing whose details represent the group on list pages. The list view filters to `Q(group__isnull=True) | Q(group__primary_id=F('id'))` so only one row per group is shown (facet counts follow). Users toggle selection via a link icon on each card (persisted in `localStorage` via Alpine) and POST to `main:mark_similar`, which creates or merges groups. The detail page shows a numbered switcher of group siblings when present.

Free-text `notes` live on both `ListingGroup` and `Listing`. The detail page's `set_notes` endpoint (debounced 2s autosave via Alpine/fetch) writes to `group.notes` when the listing is grouped (so all duplicates share one note) and to `listing.notes` otherwise. The list view sets `l.display_notes` by the same rule and cards render it under the price/address block.

Scrapers live in `main/<source>.py` (e.g. `main/privateproperty.py`) and are invoked by a matching management command in `main/management/commands/scrape_<source>.py`. Listing/photo data is stored in `main.models.Listing` and `main.models.ListingImage`; downloaded photos land under `MEDIA_ROOT` (`media/listings/YYYY/MM/`). Scrapers use botasaurus's `@request` decorator with `parallel=N` for concurrent detail/image fetches, and walk list pages newest-first stopping when they hit an already-saved `listing_id`.

After a non-`--all` scrape, `recheck_oldest()` re-fetches the oldest-checked active listings (5%, rounded up — ordered by `rechecked_at` ASC nulls-first, then `first_seen_at`). If the response contains the "listing with ref T… is no longer available" marker, the listing is marked `removed=True`; otherwise its fields are overwritten and **all** existing `ListingImage` rows are deleted and re-downloaded from scratch. `Listing.removed`, `Listing.rechecked_at`, and `ListingGroup.removed` track this. Group cascade: a `ListingGroup` is auto-marked removed iff every member is removed — recomputed in the recheck loop and on `mark_similar` / `unlink_similar`. Card templates render a "Removed" badge on the image when `listing.removed` or `group.removed` is true.

## Commands

- Scrape privateproperty.co.za: `uv run python manage.py scrape_privateproperty [--url URL] [--max-pages N]`
- Run dev server: `uv run python manage.py runserver`
- Migrate: `uv run python manage.py migrate`
- Django management: `uv run python manage.py <cmd>`
- Add dependency: `uv add <pkg>`
- Sync env from `pyproject.toml`: `uv sync`

## Conventions

- Never pin dependency versions in `pyproject.toml` (no `==`, `>=`, `~=`, etc.) unless the user has explicitly pinned them. After `uv add`, strip any version specifier it inserts so entries read as bare names (e.g. `"django"`, not `"django>=6.0.4"`). If the user has set a pin, preserve it.

## Pre-commit

`pre-commit` is a dev dependency (see `pyproject.toml`) with hooks defined in `.pre-commit-config.yaml` (ruff, plus core sanity/format checks).

Run on changed files: `uv run pre-commit run --files <path> [<path> ...]`. A `PostToolUse` hook in `.claude/settings.local.json` runs this automatically after every Write/Edit/MultiEdit, so you should not need to invoke it manually — but if a run reports failures (auto-fixes or errors), re-run it until it passes before considering a change complete.

No tests or build step are configured yet. If you add one, update this file.

Never write any tests.
