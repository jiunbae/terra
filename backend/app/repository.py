"""공개/공유 행성 분석 결과를 보존하는 SQLite 저장소."""

from __future__ import annotations

import json
import hashlib
import hmac
import secrets
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .schema import PlanetSpec

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
DB_PATH = DATA_DIR / "terra.sqlite3"


def _connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def initialize() -> None:
    with _connect() as db:
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS planets (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT NOT NULL,
                feature_type TEXT NOT NULL,
                gravity_g REAL NOT NULL,
                inhabitant_count INTEGER NOT NULL,
                spec_json TEXT NOT NULL,
                physics_json TEXT NOT NULL,
                model TEXT NOT NULL,
                cover_image_url TEXT,
                image_assets_json TEXT NOT NULL DEFAULT '{}',
                edit_token_hash TEXT NOT NULL,
                is_public INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        columns = {row["name"] for row in db.execute("PRAGMA table_info(planets)").fetchall()}
        if "image_assets_json" not in columns:
            db.execute(
                "ALTER TABLE planets ADD COLUMN image_assets_json TEXT NOT NULL DEFAULT '{}'"
            )
        db.execute("CREATE INDEX IF NOT EXISTS idx_planets_public_created ON planets(is_public, created_at DESC)")


def _row_to_detail(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "spec": json.loads(row["spec_json"]),
        "physics": json.loads(row["physics_json"]),
        "model": row["model"],
        "cover_image_url": row["cover_image_url"],
        "image_assets": json.loads(row["image_assets_json"] or "{}"),
        "is_public": bool(row["is_public"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def save_planet(
    *,
    spec: PlanetSpec,
    physics: dict[str, Any],
    model: str,
    cover_image_url: str | None,
    is_public: bool,
    image_assets: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    planet_id = secrets.token_urlsafe(9)
    edit_token = secrets.token_urlsafe(24)
    edit_token_hash = hashlib.sha256(edit_token.encode()).hexdigest()
    with _connect() as db:
        db.execute(
            """
            INSERT INTO planets (
                id, name, description, feature_type, gravity_g, inhabitant_count,
                spec_json, physics_json, model, cover_image_url, image_assets_json,
                edit_token_hash, is_public, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                planet_id,
                spec.planet.name,
                spec.surface.description,
                spec.surface.feature_type,
                spec.planet.gravity_g,
                len(spec.inhabitants),
                json.dumps(spec.model_dump(mode="json"), ensure_ascii=False),
                json.dumps(physics, ensure_ascii=False),
                model,
                cover_image_url,
                json.dumps(image_assets or {}, ensure_ascii=False),
                edit_token_hash,
                int(is_public),
                now,
                now,
            ),
        )
        row = db.execute("SELECT * FROM planets WHERE id = ?", (planet_id,)).fetchone()
    assert row is not None
    result = _row_to_detail(row)
    result["edit_token"] = edit_token
    return result


def list_public_planets(*, limit: int = 40, offset: int = 0) -> list[dict[str, Any]]:
    with _connect() as db:
        rows = db.execute(
            """
            SELECT id, name, description, feature_type, gravity_g, inhabitant_count,
                   cover_image_url, created_at
            FROM planets
            WHERE is_public = 1
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
    return [dict(row) for row in rows]


def get_planet(planet_id: str) -> dict[str, Any] | None:
    with _connect() as db:
        row = db.execute("SELECT * FROM planets WHERE id = ?", (planet_id,)).fetchone()
    return _row_to_detail(row) if row is not None else None


def update_cover(planet_id: str, cover_image_url: str, edit_token: str) -> dict[str, Any] | None:
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as db:
        auth = db.execute("SELECT edit_token_hash FROM planets WHERE id = ?", (planet_id,)).fetchone()
        if auth is None:
            return None
        token_hash = hashlib.sha256(edit_token.encode()).hexdigest()
        if not hmac.compare_digest(auth["edit_token_hash"], token_hash):
            raise PermissionError("행성 정보를 수정할 권한이 없습니다.")
        row = db.execute(
            "SELECT image_assets_json FROM planets WHERE id = ?", (planet_id,)
        ).fetchone()
        assets = json.loads(row["image_assets_json"] or "{}")
        previous = assets.get("planet", {})
        assets["planet"] = {**previous, "url": cover_image_url}
        db.execute(
            """UPDATE planets
               SET cover_image_url = ?, image_assets_json = ?, updated_at = ?
               WHERE id = ?""",
            (cover_image_url, json.dumps(assets, ensure_ascii=False), now, planet_id),
        )
        row = db.execute("SELECT * FROM planets WHERE id = ?", (planet_id,)).fetchone()
    return _row_to_detail(row) if row is not None else None


def update_image_asset(
    planet_id: str,
    key: str,
    asset: dict[str, Any],
    edit_token: str,
) -> dict[str, Any] | None:
    """편집 권한을 검증한 뒤 행성/거주민 생성 이미지를 하나 갱신한다."""
    now = datetime.now(timezone.utc).isoformat()
    with _connect() as db:
        row = db.execute(
            "SELECT edit_token_hash, image_assets_json FROM planets WHERE id = ?",
            (planet_id,),
        ).fetchone()
        if row is None:
            return None
        token_hash = hashlib.sha256(edit_token.encode()).hexdigest()
        if not hmac.compare_digest(row["edit_token_hash"], token_hash):
            raise PermissionError("행성 정보를 수정할 권한이 없습니다.")

        assets = json.loads(row["image_assets_json"] or "{}")
        assets[key] = asset
        cover_image_url = asset["url"] if key == "planet" else None
        if cover_image_url is None:
            db.execute(
                "UPDATE planets SET image_assets_json = ?, updated_at = ? WHERE id = ?",
                (json.dumps(assets, ensure_ascii=False), now, planet_id),
            )
        else:
            db.execute(
                """UPDATE planets
                   SET image_assets_json = ?, cover_image_url = ?, updated_at = ?
                   WHERE id = ?""",
                (json.dumps(assets, ensure_ascii=False), cover_image_url, now, planet_id),
            )
        updated = db.execute("SELECT * FROM planets WHERE id = ?", (planet_id,)).fetchone()
    return _row_to_detail(updated) if updated is not None else None


initialize()
