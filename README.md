# Membership Tracker

A small self-hosted app for loyalty and membership numbers (airlines, hotels, rail, car rental, clubs, retail) for you and anyone else you keep numbers for. One login edits everything; each membership belongs to a person, and you can view everyone at once or one person at a time. Tap to copy a number, track tier and expiry (expiring within 60 days is highlighted), pin favorites, export/import JSON backups.

Stack: FastAPI + SQLite, single-page vanilla JS UI. Data lives in `/data/memberships.db` inside the container (a named Docker volume).

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `APP_PASSWORD` | empty | Turns on HTTP basic auth when set. Leave empty only on a trusted LAN. |
| `APP_USER` | `admin` | Username for basic auth. |
| `DB_PATH` | `/data/memberships.db` | SQLite file location. |

Put `APP_USER` / `APP_PASSWORD` in a `.env` file next to `docker-compose.yml` (see `.env.example`). `.env` is git-ignored.

## Run locally without Docker

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8080
```

## Run with Docker (build locally)

```bash
docker compose up -d --build
# open http://localhost:8765
```

## Publish to GitHub and pull on another machine

Pushing to `main` triggers `.github/workflows/docker-publish.yml`, which builds an amd64 + arm64 image and pushes it to `ghcr.io/<your-username>/membership-tracker`.

On the other machine you only need `docker-compose.yml` (and `.env`):

```bash
docker compose pull
docker compose up -d
```

To update later: push to `main`, wait for the Action to finish, then `docker compose pull && docker compose up -d` again.

## Upgrading from the single-person version

Nothing to do. On first start the app creates the people table and turns any old "Member name" values into people automatically. Old backup files still import; their member names become people.

## Spreadsheet export

**Export spreadsheet (CSV)** gives one row per program and one column per person, with each cell holding that person's number: `Program, Daniel, Mum, ...`. If someone has two numbers for the same program, they're joined with ` / `. This file is read-only output; use **Export backup** (JSON) for restoring.

## Backups

Use **Export backup** in the UI, or copy the database out of the volume:

```bash
docker cp membership-tracker:/data/memberships.db ./memberships-backup.db
```
