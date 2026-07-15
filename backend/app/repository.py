"""공개/공유 행성 분석 결과를 보존하는 SQLite 저장소."""

from __future__ import annotations

import contextlib
import json
import hashlib
import hmac
import secrets
import sqlite3
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .schema import PlanetSpec

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
DB_PATH = DATA_DIR / "terra.sqlite3"
REPORT_REASONS = frozenset(
    {
        "personal_information",
        "copyright",
        "harassment",
        "unsafe_content",
        "spam",
        "other",
    }
)
PUBLIC_EVIDENCE_REDACTION_MIGRATION = "20260715_public_evidence_quote_redaction"


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout=10000")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


@contextlib.contextmanager
def _db(*, write: bool = False) -> Iterator[sqlite3.Connection]:
    """트랜잭션(커밋/롤백)을 감싸고, 블록을 벗어나면 연결을 확실히 닫는다."""
    connection = _connect()
    try:
        if write:
            # JSON 자산 필드는 read-modify-write이므로 SELECT 전에 쓰기 잠금을
            # 확보해야 동시 업데이트가 서로의 결과를 잃지 않는다.
            connection.execute("BEGIN IMMEDIATE")
        yield connection
        if write:
            connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()


def initialize() -> None:
    # journal_mode는 DB 파일에 지속된다. 매 요청 연결마다 재설정하면 불필요한
    # 잠금/PRAGMA 비용이 생기므로 초기화 시 한 번만 적용한다.
    connection = _connect()
    try:
        connection.execute("PRAGMA journal_mode=WAL")
    finally:
        connection.close()
    with _db(write=True) as db:
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
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_planets_public_created "
            "ON planets(is_public, created_at DESC)"
        )
        # 신고에는 IP나 편집 capability를 보존하지 않는다. 행성이 삭제되면 공개
        # 콘텐츠와 분리된 불필요한 신고 데이터도 같은 트랜잭션에서 정리된다.
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS moderation_reports (
                id TEXT PRIMARY KEY,
                planet_id TEXT NOT NULL REFERENCES planets(id) ON DELETE CASCADE,
                reason TEXT NOT NULL CHECK (reason IN (
                    'personal_information', 'copyright', 'harassment',
                    'unsafe_content', 'spam', 'other'
                )),
                details TEXT NOT NULL DEFAULT '' CHECK (length(details) <= 500),
                created_at TEXT NOT NULL
            )
            """
        )
        db.execute(
            "CREATE INDEX IF NOT EXISTS idx_moderation_reports_planet_created "
            "ON moderation_reports(planet_id, created_at DESC)"
        )
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                name TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """
        )
        applied = db.execute(
            "SELECT 1 FROM schema_migrations WHERE name = ?",
            (PUBLIC_EVIDENCE_REDACTION_MIGRATION,),
        ).fetchone()
        if applied is None:
            # 과거 공개 저장본에도 원문 직접 인용이 남아 있었다. 다른 필드와
            # 기존 ID/자산/시간은 보존하고 공개 spec의 인용만 한 트랜잭션에서 지운다.
            for row in db.execute(
                "SELECT id, spec_json FROM planets WHERE is_public = 1"
            ).fetchall():
                spec = json.loads(row["spec_json"])
                if not isinstance(spec, dict):
                    raise ValueError("saved public planet spec must be a JSON object")
                inferences = spec.get("inferences", [])
                if not isinstance(inferences, list):
                    raise ValueError("saved public planet inferences must be a JSON array")
                changed = False
                for inference in inferences:
                    if isinstance(inference, dict) and inference.get("evidence_quote"):
                        inference["evidence_quote"] = ""
                        changed = True
                if changed:
                    db.execute(
                        "UPDATE planets SET spec_json = ? WHERE id = ?",
                        (json.dumps(spec, ensure_ascii=False), row["id"]),
                    )
            db.execute(
                "INSERT INTO schema_migrations (name, applied_at) VALUES (?, ?)",
                (
                    PUBLIC_EVIDENCE_REDACTION_MIGRATION,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )


def healthcheck_database() -> bool:
    """Return whether SQLite can serve a simple read on a fresh connection."""
    try:
        with _db() as db:
            return db.execute("SELECT 1").fetchone()[0] == 1
    except sqlite3.Error:
        return False


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
    stored_spec = spec.model_copy(deep=True)
    if is_public:
        # API 외의 내부 호출도 공개 원문 인용을 다시 저장하지 못하게 영속성
        # 경계에서 한 번 더 최소화한다. 호출자가 가진 로컬 spec은 변경하지 않는다.
        for inference in stored_spec.inferences:
            inference.evidence_quote = ""
    with _db(write=True) as db:
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
                stored_spec.planet.name,
                stored_spec.surface.description,
                stored_spec.surface.feature_type,
                stored_spec.planet.gravity_g,
                len(stored_spec.inhabitants),
                json.dumps(stored_spec.model_dump(mode="json"), ensure_ascii=False),
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
    with _db() as db:
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


def get_planet(planet_id: str, *, public_only: bool = False) -> dict[str, Any] | None:
    with _db() as db:
        if public_only:
            row = db.execute(
                "SELECT * FROM planets WHERE id = ? AND is_public = 1",
                (planet_id,),
            ).fetchone()
        else:
            row = db.execute("SELECT * FROM planets WHERE id = ?", (planet_id,)).fetchone()
    return _row_to_detail(row) if row is not None else None


def update_cover(planet_id: str, cover_image_url: str, edit_token: str) -> dict[str, Any] | None:
    now = datetime.now(timezone.utc).isoformat()
    with _db(write=True) as db:
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
    with _db(write=True) as db:
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


def delete_planet(planet_id: str, edit_token: str) -> bool:
    """편집 capability를 검증하고 행성 레코드만 원자적으로 삭제한다.

    생성 파일은 여기서 건드리지 않는다. 삭제된 행성이 참조하던 파일은 기존
    유지보수 작업이 유예 기간과 디스크 정책에 따라 비동기로 회수한다.
    """
    with _db(write=True) as db:
        auth = db.execute(
            "SELECT edit_token_hash FROM planets WHERE id = ?",
            (planet_id,),
        ).fetchone()
        if auth is None:
            return False
        token_hash = hashlib.sha256(edit_token.encode()).hexdigest()
        if not hmac.compare_digest(auth["edit_token_hash"], token_hash):
            raise PermissionError("행성을 삭제할 권한이 없습니다.")
        deleted = db.execute("DELETE FROM planets WHERE id = ?", (planet_id,))
        return deleted.rowcount == 1


def create_moderation_report(planet_id: str, reason: str, details: str = "") -> bool:
    """공개 행성에 대한 최소한의 신고 내용을 저장한다.

    요청자 식별자와 편집 capability는 저장하지 않으며, 호출자 계층의 검증을
    우회한 내부 호출에도 DB 경계를 지키도록 허용값과 길이를 다시 확인한다.
    """
    if reason not in REPORT_REASONS:
        raise ValueError("unsupported moderation reason")
    if len(details) > 500:
        raise ValueError("moderation details are too long")

    now = datetime.now(timezone.utc).isoformat()
    report_id = secrets.token_urlsafe(12)
    with _db(write=True) as db:
        exists = db.execute(
            "SELECT 1 FROM planets WHERE id = ? AND is_public = 1",
            (planet_id,),
        ).fetchone()
        if exists is None:
            return False
        db.execute(
            """
            INSERT INTO moderation_reports (id, planet_id, reason, details, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (report_id, planet_id, reason, details, now),
        )
    return True


initialize()
