"""Membership number tracker: FastAPI + SQLite, single-page UI."""
from __future__ import annotations

import csv
import io
import os
import re
import secrets
import sqlite3
from contextlib import asynccontextmanager, contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field, field_validator

DB_PATH = Path(os.getenv("DB_PATH", "./data/memberships.db"))
APP_USER = os.getenv("APP_USER") or "admin"
APP_PASSWORD = os.getenv("APP_PASSWORD", "")
STATIC_DIR = Path(__file__).parent / "static"

CATEGORIES = ["airline", "hotel", "car rental", "rail", "club", "retail", "other"]
FIELDS = ["person_id", "program", "category", "number", "tier", "alliance",
          "expires", "url", "notes", "color", "pinned"]
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


# ---------- database ----------
@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect() as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute(
            """CREATE TABLE IF NOT EXISTS memberships (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                program TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'other',
                number TEXT NOT NULL,
                holder TEXT NOT NULL DEFAULT '',
                tier TEXT NOT NULL DEFAULT '',
                alliance TEXT NOT NULL DEFAULT '',
                expires TEXT NOT NULL DEFAULT '',
                url TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                color TEXT NOT NULL DEFAULT '#16233B',
                pinned INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        db.execute(
            """CREATE TABLE IF NOT EXISTS people (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE COLLATE NOCASE,
                created_at TEXT NOT NULL
            )"""
        )
        cols = {r["name"] for r in db.execute("PRAGMA table_info(memberships)")}
        if "person_id" not in cols:
            db.execute("ALTER TABLE memberships ADD COLUMN person_id INTEGER REFERENCES people(id)")
        # One-time migration: turn old free-text "holder" values into people.
        for (holder,) in db.execute(
            "SELECT DISTINCT holder FROM memberships WHERE person_id IS NULL AND holder != ''"
        ).fetchall():
            pid = get_or_create_person(db, holder)
            db.execute(
                "UPDATE memberships SET person_id=?, holder='' WHERE person_id IS NULL AND holder=?",
                (pid, holder),
            )


def get_or_create_person(db: sqlite3.Connection, name: str) -> int:
    row = db.execute("SELECT id FROM people WHERE name=?", (name,)).fetchone()
    if row:
        return row["id"]
    return db.execute(
        "INSERT INTO people (name, created_at) VALUES (?, ?)", (name, now())
    ).lastrowid


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d.pop("holder", None)
    d["pinned"] = bool(d["pinned"])
    return d


MEMBERSHIP_SELECT = (
    "SELECT m.*, p.name AS person_name FROM memberships m "
    "LEFT JOIN people p ON p.id = m.person_id "
)


# ---------- models ----------
class PersonIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)

    @field_validator("name", mode="before")
    @classmethod
    def strip_name(cls, v):
        return v.strip() if isinstance(v, str) else v


class MembershipIn(BaseModel):
    person_id: Optional[int] = None
    program: str = Field(min_length=1, max_length=120)
    category: str = "other"
    number: str = Field(min_length=1, max_length=120)
    tier: str = Field("", max_length=80)
    alliance: str = Field("", max_length=80)
    expires: str = ""
    url: str = Field("", max_length=500)
    notes: str = Field("", max_length=2000)
    color: str = "#16233B"
    pinned: bool = False

    @field_validator("*", mode="before")
    @classmethod
    def strip_strings(cls, v, info):
        if info.field_name == "person_id":
            return v if v not in ("", None) else None
        return v.strip() if isinstance(v, str) else ("" if v is None else v)

    @field_validator("category")
    @classmethod
    def valid_category(cls, v: str) -> str:
        v = v.lower()
        return v if v in CATEGORIES else "other"

    @field_validator("expires")
    @classmethod
    def valid_date(cls, v: str) -> str:
        if v:
            try:
                date.fromisoformat(v)
            except ValueError:
                raise ValueError("expires must be a date in YYYY-MM-DD format")
        return v

    @field_validator("url")
    @classmethod
    def valid_url(cls, v: str) -> str:
        if v and not v.lower().startswith(("http://", "https://")):
            v = "https://" + v
        return v

    @field_validator("color")
    @classmethod
    def valid_color(cls, v: str) -> str:
        return v if COLOR_RE.match(v) else "#16233B"


class MembershipImport(MembershipIn):
    """Backup format: people are referenced by name so files move between installs."""
    person: str = Field("", max_length=80)
    holder: str = Field("", max_length=120)  # accepted from pre-people backups


# ---------- auth ----------
security = HTTPBasic(auto_error=False)


def require_auth(creds: Optional[HTTPBasicCredentials] = Depends(security)) -> None:
    """Basic auth is enforced only when APP_PASSWORD is set."""
    if not APP_PASSWORD:
        return
    ok = (
        creds is not None
        and secrets.compare_digest(creds.username.encode(), APP_USER.encode())
        and secrets.compare_digest(creds.password.encode(), APP_PASSWORD.encode())
    )
    if not ok:
        raise HTTPException(
            status_code=401,
            detail="Sign in to view memberships.",
            headers={"WWW-Authenticate": 'Basic realm="Memberships"'},
        )


# ---------- app ----------
@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="Membership Tracker", lifespan=lifespan,
              docs_url=None, redoc_url=None, openapi_url=None)
protected = APIRouter(dependencies=[Depends(require_auth)])


@app.get("/health", include_in_schema=False)
def health():
    return {"status": "ok"}


@protected.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")


@protected.get("/api/memberships")
def list_memberships():
    with connect() as db:
        rows = db.execute(
            MEMBERSHIP_SELECT + "ORDER BY m.pinned DESC, m.program COLLATE NOCASE"
        ).fetchall()
    return [row_to_dict(r) for r in rows]


def check_person(db: sqlite3.Connection, person_id: Optional[int]) -> None:
    if person_id is not None and not db.execute(
        "SELECT 1 FROM people WHERE id=?", (person_id,)
    ).fetchone():
        raise HTTPException(400, "That person no longer exists. Pick someone else.")


def insert(db: sqlite3.Connection, m: MembershipIn) -> int:
    data = m.model_dump(include=set(FIELDS))
    ts = now()
    cur = db.execute(
        f"INSERT INTO memberships ({', '.join(FIELDS)}, created_at, updated_at) "
        f"VALUES ({', '.join('?' for _ in FIELDS)}, ?, ?)",
        [data[f] for f in FIELDS] + [ts, ts],
    )
    return cur.lastrowid


@protected.post("/api/memberships", status_code=201)
def create_membership(m: MembershipIn):
    with connect() as db:
        check_person(db, m.person_id)
        new_id = insert(db, m)
        row = db.execute(MEMBERSHIP_SELECT + "WHERE m.id=?", (new_id,)).fetchone()
    return row_to_dict(row)


@protected.put("/api/memberships/{item_id}")
def update_membership(item_id: int, m: MembershipIn):
    data = m.model_dump()
    with connect() as db:
        check_person(db, m.person_id)
        cur = db.execute(
            f"UPDATE memberships SET {', '.join(f + '=?' for f in FIELDS)}, updated_at=? "
            "WHERE id=?",
            [data[f] for f in FIELDS] + [now(), item_id],
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "That membership no longer exists.")
        row = db.execute(MEMBERSHIP_SELECT + "WHERE m.id=?", (item_id,)).fetchone()
    return row_to_dict(row)


@protected.delete("/api/memberships/{item_id}", status_code=204)
def delete_membership(item_id: int):
    with connect() as db:
        cur = db.execute("DELETE FROM memberships WHERE id=?", (item_id,))
        if cur.rowcount == 0:
            raise HTTPException(404, "That membership no longer exists.")
    return Response(status_code=204)


@protected.get("/api/export")
def export_memberships():
    with connect() as db:
        rows = db.execute(MEMBERSHIP_SELECT + "ORDER BY m.id").fetchall()
        people = [r["name"] for r in db.execute("SELECT name FROM people ORDER BY id")]
    items = []
    for r in rows:
        d = row_to_dict(r)
        item = {f: d[f] for f in FIELDS if f != "person_id"}
        item["person"] = d["person_name"] or ""
        items.append(item)
    payload = {"exported_at": now(), "people": people, "memberships": items}
    stamp = datetime.now().strftime("%Y-%m-%d")
    return JSONResponse(
        payload,
        headers={"Content-Disposition": f'attachment; filename="memberships-{stamp}.json"'},
    )


@protected.get("/api/export.csv")
def export_csv():
    """One row per program, one column per person; cells hold membership numbers."""
    with connect() as db:
        people = db.execute("SELECT id, name FROM people ORDER BY name COLLATE NOCASE").fetchall()
        rows = db.execute(
            "SELECT program, person_id, number FROM memberships "
            "ORDER BY program COLLATE NOCASE, id"
        ).fetchall()

    columns = [(p["id"], p["name"]) for p in people]
    if any(r["person_id"] is None for r in rows):
        columns.append((None, "Unassigned"))

    table: dict[str, dict] = {}  # program key (case-insensitive) -> {"name", person_id -> [numbers]}
    for r in rows:
        entry = table.setdefault(r["program"].casefold(), {"name": r["program"], "cells": {}})
        entry["cells"].setdefault(r["person_id"], []).append(r["number"])

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Program"] + [name for _, name in columns])
    for entry in table.values():
        writer.writerow(
            [entry["name"]] + [" / ".join(entry["cells"].get(pid, [])) for pid, _ in columns]
        )

    stamp = datetime.now().strftime("%Y-%m-%d")
    return Response(
        "\ufeff" + buf.getvalue(),  # BOM so Excel reads UTF-8 names correctly
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="memberships-{stamp}.csv"'},
    )


class ImportPayload(BaseModel):
    people: list[str] = []
    memberships: list[MembershipImport] = []


@protected.post("/api/import")
def import_memberships(payload: ImportPayload):
    """Adds every item as a new entry; people are matched by name or created."""
    with connect() as db:
        for name in payload.people:
            if name.strip():
                get_or_create_person(db, name.strip()[:80])
        for m in payload.memberships:
            name = (m.person or m.holder).strip()[:80]
            m.person_id = get_or_create_person(db, name) if name else None
            insert(db, m)
    return {"imported": len(payload.memberships)}


# ---------- people ----------
@protected.get("/api/people")
def list_people():
    with connect() as db:
        rows = db.execute(
            "SELECT p.id, p.name, COUNT(m.id) AS count FROM people p "
            "LEFT JOIN memberships m ON m.person_id = p.id "
            "GROUP BY p.id ORDER BY p.name COLLATE NOCASE"
        ).fetchall()
    return [dict(r) for r in rows]


@protected.post("/api/people", status_code=201)
def create_person(p: PersonIn):
    with connect() as db:
        if db.execute("SELECT 1 FROM people WHERE name=?", (p.name,)).fetchone():
            raise HTTPException(409, f"{p.name} is already in your people list.")
        pid = get_or_create_person(db, p.name)
    return {"id": pid, "name": p.name, "count": 0}


@protected.put("/api/people/{person_id}")
def rename_person(person_id: int, p: PersonIn):
    with connect() as db:
        clash = db.execute(
            "SELECT 1 FROM people WHERE name=? AND id!=?", (p.name, person_id)
        ).fetchone()
        if clash:
            raise HTTPException(409, f"{p.name} is already in your people list.")
        cur = db.execute("UPDATE people SET name=? WHERE id=?", (p.name, person_id))
        if cur.rowcount == 0:
            raise HTTPException(404, "That person no longer exists.")
    return {"id": person_id, "name": p.name}


@protected.delete("/api/people/{person_id}")
def delete_person(person_id: int):
    """Removes the person and every membership saved under them."""
    with connect() as db:
        removed = db.execute(
            "DELETE FROM memberships WHERE person_id=?", (person_id,)
        ).rowcount
        cur = db.execute("DELETE FROM people WHERE id=?", (person_id,))
        if cur.rowcount == 0:
            raise HTTPException(404, "That person no longer exists.")
    return {"deleted_memberships": removed}


app.include_router(protected)
