# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Maintenance

Always keep this file up to date. Whenever you learn or introduce something a future Claude Code session would need to be productive — new commands, dependencies, architecture, conventions, gotchas — update the relevant section here in the same change.

Also keep `README.md` up to date as the project develops. Unlike this file (which targets Claude Code), the README targets human readers: project purpose, setup/install steps, how to run, and any other information relevant for someone new to the repo. Update it in the same change whenever user-facing behavior, setup, or commands change.

## Project

Property scraper. Python 3.13.1, managed with `uv` (see `pyproject.toml` + `.python-version`, `.venv/` in-tree). Django project `getproperty/` with a single app `main/` registered in `INSTALLED_APPS`. Scraping logic has not been written yet.

## Commands

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
