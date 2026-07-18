#!/usr/bin/env python3
"""Create and verify self-contained Terra SQLite + referenced-image backups.

The live database is copied with SQLite's online backup API. Only PNG files
referenced by the resulting snapshot are staged, so unrelated or abandoned
generated images are deliberately excluded from the archive.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import sys
import tarfile
import tempfile
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO
from urllib.parse import quote


FORMAT_VERSION = 1
ARCHIVE_PREFIX = "terra-backup"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE = PROJECT_ROOT / "backend" / "data" / "terra.sqlite3"
DEFAULT_GENERATED_DIR = PROJECT_ROOT / "backend" / "generated"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "backups"
DATABASE_ARCHIVE_PATH = "database/terra.sqlite3"
MANIFEST_ARCHIVE_PATH = "manifest.json"
GENERATED_URL = re.compile(r"^/generated/([A-Za-z0-9][A-Za-z0-9._-]*\.png)$")
BUFFER_SIZE = 1024 * 1024


class BackupError(RuntimeError):
    """Expected backup validation or consistency failure."""


def _sha256_stream(source: BinaryIO) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    while chunk := source.read(BUFFER_SIZE):
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def _sha256_file(path: Path) -> tuple[str, int]:
    with path.open("rb") as source:
        return _sha256_stream(source)


def _sqlite_uri(path: Path) -> str:
    return f"file:{quote(path.resolve().as_posix(), safe='/')}?mode=ro"


def _quick_check(connection: sqlite3.Connection) -> None:
    rows = connection.execute("PRAGMA quick_check").fetchall()
    messages = [str(row[0]) for row in rows]
    if messages != ["ok"]:
        raise BackupError(f"SQLite quick_check failed: {'; '.join(messages[:10])}")


def create_database_snapshot(source_path: Path, destination_path: Path) -> dict[str, Any]:
    """Copy a live WAL database into one standalone, integrity-checked file."""

    if not source_path.is_file():
        raise BackupError(f"database does not exist: {source_path}")
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(_sqlite_uri(source_path), uri=True, timeout=30)
    destination = sqlite3.connect(destination_path, timeout=30)
    try:
        source.execute("PRAGMA query_only=ON")
        source.execute("PRAGMA busy_timeout=30000")
        source.backup(destination)
        destination.commit()
        # A backup made from WAL is self-contained. Persist DELETE mode so restore
        # never depends on a sidecar file that is intentionally not archived.
        destination.execute("PRAGMA journal_mode=DELETE")
        destination.commit()
        _quick_check(destination)
        user_version = int(destination.execute("PRAGMA user_version").fetchone()[0])
    except sqlite3.Error as exc:
        raise BackupError(f"SQLite online backup failed: {exc}") from exc
    finally:
        destination.close()
        source.close()

    os.chmod(destination_path, 0o600)
    sha256, size = _sha256_file(destination_path)
    return {
        "archive_path": DATABASE_ARCHIVE_PATH,
        "sha256": sha256,
        "size_bytes": size,
        "sqlite_user_version": user_version,
        "quick_check": "ok",
    }


def _generated_name(url: object) -> str | None:
    if url is None or url == "":
        return None
    if not isinstance(url, str):
        raise BackupError("generated image reference is not a string")
    if not url.startswith("/generated/"):
        # Remote/non-Terra assets are not local files and are outside this backup.
        return None
    match = GENERATED_URL.fullmatch(url)
    if match is None:
        raise BackupError(f"unsafe or unsupported generated image URL: {url!r}")
    return match.group(1)


def read_snapshot_references(snapshot_path: Path) -> tuple[dict[str, list[dict[str, str]]], dict[str, int]]:
    """Return referenced filenames and non-sensitive reference locations."""

    connection = sqlite3.connect(_sqlite_uri(snapshot_path), uri=True, timeout=10)
    connection.row_factory = sqlite3.Row
    try:
        _quick_check(connection)
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(planets)").fetchall()
        }
        required = {"id", "is_public", "cover_image_url"}
        if not required.issubset(columns):
            raise BackupError("snapshot does not contain the expected planets table")
        has_assets = "image_assets_json" in columns
        asset_select = "image_assets_json" if has_assets else "'{}' AS image_assets_json"
        rows = connection.execute(
            f"SELECT id, is_public, cover_image_url, {asset_select} FROM planets"  # noqa: S608 - fixed column
        ).fetchall()
    except (sqlite3.Error, json.JSONDecodeError) as exc:
        raise BackupError(f"cannot read image references from snapshot: {exc}") from exc
    finally:
        connection.close()

    references: dict[str, list[dict[str, str]]] = {}

    def record(url: object, planet_id: str, field: str) -> None:
        name = _generated_name(url)
        if name is None:
            return
        entry = {"planet_id": planet_id, "field": field}
        values = references.setdefault(name, [])
        if entry not in values:
            values.append(entry)

    public_count = 0
    for row in rows:
        planet_id = str(row["id"])
        public_count += int(bool(row["is_public"]))
        record(row["cover_image_url"], planet_id, "cover_image_url")
        try:
            assets = json.loads(row["image_assets_json"] or "{}")
        except json.JSONDecodeError as exc:
            raise BackupError(f"invalid image_assets_json for planet {planet_id}") from exc
        if not isinstance(assets, dict):
            raise BackupError(f"image_assets_json is not an object for planet {planet_id}")
        for key, asset in assets.items():
            if not isinstance(asset, dict):
                raise BackupError(f"image asset {key!r} is not an object for planet {planet_id}")
            record(asset.get("url"), planet_id, f"image_assets.{key}")

    for values in references.values():
        values.sort(key=lambda item: (item["planet_id"], item["field"]))
    return dict(sorted(references.items())), {
        "planets": len(rows),
        "public_planets": public_count,
        "private_or_unlisted_planets": len(rows) - public_count,
    }


def _copy_regular_file(source_path: Path, destination_path: Path) -> tuple[str, int]:
    """Copy without following symlinks and hash exactly the staged bytes."""

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(source_path, flags)
    except (FileNotFoundError, OSError) as exc:
        raise BackupError(f"cannot open referenced image {source_path.name}: {exc}") from exc

    digest = hashlib.sha256()
    size = 0
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise BackupError(f"referenced image is not a regular file: {source_path.name}")
        with os.fdopen(descriptor, "rb", closefd=True) as source, destination_path.open("xb") as destination:
            descriptor = -1
            while chunk := source.read(BUFFER_SIZE):
                destination.write(chunk)
                digest.update(chunk)
                size += len(chunk)
            destination.flush()
            os.fsync(destination.fileno())
            after = os.fstat(source.fileno())
        if size != before.st_size or after.st_size != before.st_size or after.st_mtime_ns != before.st_mtime_ns:
            raise BackupError(f"referenced image changed during backup: {source_path.name}")
    finally:
        if descriptor >= 0:
            os.close(descriptor)

    os.chmod(destination_path, 0o600)
    return digest.hexdigest(), size


def stage_referenced_images(
    generated_dir: Path,
    staging_dir: Path,
    references: dict[str, list[dict[str, str]]],
    *,
    allow_missing: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    generated_root = generated_dir.resolve()
    images: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    for name, reference_list in references.items():
        source = generated_root / name
        destination = staging_dir / "generated" / name
        try:
            sha256, size = _copy_regular_file(source, destination)
        except BackupError as exc:
            missing.append({"filename": name, "references": reference_list, "error": str(exc)})
            continue
        images.append(
            {
                "archive_path": f"generated/{name}",
                "source_url": f"/generated/{name}",
                "sha256": sha256,
                "size_bytes": size,
                "references": reference_list,
            }
        )

    if missing and not allow_missing:
        names = ", ".join(item["filename"] for item in missing[:10])
        suffix = " ..." if len(missing) > 10 else ""
        raise BackupError(
            f"{len(missing)} referenced PNG file(s) are missing or unsafe: {names}{suffix}; "
            "repair them or explicitly use --allow-missing"
        )
    return images, missing


def _tar_metadata(member: tarfile.TarInfo) -> tarfile.TarInfo:
    member.uid = 0
    member.gid = 0
    member.uname = ""
    member.gname = ""
    member.mode = 0o600
    return member


def _write_manifest(path: Path, manifest: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    os.chmod(path, 0o600)


def _next_archive_path(output_dir: Path, created_at: datetime) -> Path:
    stem = f"{ARCHIVE_PREFIX}-{created_at.strftime('%Y%m%dT%H%M%SZ')}"
    for suffix in ("", *(f"-{index}" for index in range(1, 1000))):
        candidate = output_dir / f"{stem}{suffix}.tar.gz"
        if not candidate.exists() and not candidate.with_name(f".{candidate.name}.partial").exists():
            return candidate
    raise BackupError("cannot allocate a unique backup archive name")


def prune_old_archives(output_dir: Path, keep: int, *, keep_path: Path | None = None) -> list[Path]:
    """가장 최신 `keep`개만 남기고 오래된 백업 아카이브를 제거한다.

    keep <= 0이면 아무것도 지우지 않는다(로테이션 비활성화). 아카이브 명명 패턴에
    정확히 맞는 파일만 대상으로 하고 `.partial`·스테이징·무관 파일은 건드리지 않으며,
    방금 만든 아카이브(keep_path)는 항상 보존한다.
    """
    if keep <= 0:
        return []
    archives = sorted(
        (path for path in output_dir.glob(f"{ARCHIVE_PREFIX}-*.tar.gz") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    removed: list[Path] = []
    for index, archive in enumerate(archives):
        if index < keep or archive == keep_path:
            continue
        try:
            archive.unlink()
            removed.append(archive)
        except OSError as exc:
            print(f"warning: could not prune {archive}: {exc}", file=sys.stderr)
    return removed


def _expected_archive_paths(manifest: dict[str, Any]) -> set[str]:
    database = manifest.get("database")
    images = manifest.get("images")
    if not isinstance(database, dict) or not isinstance(images, list):
        raise BackupError("manifest database/images sections are invalid")
    if database.get("archive_path") != DATABASE_ARCHIVE_PATH:
        raise BackupError("manifest database archive path is invalid")

    paths = {MANIFEST_ARCHIVE_PATH, DATABASE_ARCHIVE_PATH}
    for image in images:
        if not isinstance(image, dict):
            raise BackupError("manifest image entry is invalid")
        source_url = image.get("source_url")
        name = _generated_name(source_url)
        expected_path = f"generated/{name}" if name is not None else None
        if expected_path is None or image.get("archive_path") != expected_path:
            raise BackupError("manifest image archive path is invalid")
        if expected_path in paths:
            raise BackupError(f"manifest contains a duplicate image path: {expected_path}")
        paths.add(expected_path)
    return paths


def _verified_member(archive: tarfile.TarFile, name: str) -> tarfile.ExFileObject:
    try:
        member = archive.getmember(name)
    except KeyError as exc:
        raise BackupError(f"archive member is missing: {name}") from exc
    if not member.isfile() or member.issym() or member.islnk():
        raise BackupError(f"archive member is not a regular file: {name}")
    source = archive.extractfile(member)
    if source is None:
        raise BackupError(f"cannot read archive member: {name}")
    return source


def verify_archive(archive_path: Path) -> dict[str, Any]:
    """Verify member allowlist, hashes, sizes, and SQLite integrity."""

    if not archive_path.is_file():
        raise BackupError(f"backup archive does not exist: {archive_path}")
    try:
        archive = tarfile.open(archive_path, "r:gz")
    except (tarfile.TarError, OSError) as exc:
        raise BackupError(f"cannot open backup archive: {exc}") from exc

    with archive:
        members = archive.getmembers()
        names = [member.name for member in members]
        if len(names) != len(set(names)):
            raise BackupError("archive contains duplicate member names")
        with _verified_member(archive, MANIFEST_ARCHIVE_PATH) as source:
            try:
                manifest = json.load(source)
            except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                raise BackupError(f"manifest is not valid JSON: {exc}") from exc
        if not isinstance(manifest, dict) or manifest.get("format_version") != FORMAT_VERSION:
            raise BackupError("unsupported or invalid backup manifest version")
        expected = _expected_archive_paths(manifest)
        if set(names) != expected:
            extra = sorted(set(names) - expected)
            missing = sorted(expected - set(names))
            raise BackupError(f"archive member mismatch; extra={extra}, missing={missing}")

        file_entries = [manifest["database"], *manifest["images"]]
        for entry in file_entries:
            name = str(entry["archive_path"])
            with _verified_member(archive, name) as source:
                sha256, size = _sha256_stream(source)
            if sha256 != entry.get("sha256") or size != entry.get("size_bytes"):
                raise BackupError(f"hash or size mismatch for {name}")

        with tempfile.TemporaryDirectory(prefix="terra-backup-verify-") as temporary:
            database_path = Path(temporary) / "terra.sqlite3"
            with _verified_member(archive, DATABASE_ARCHIVE_PATH) as source, database_path.open("xb") as destination:
                shutil.copyfileobj(source, destination, length=BUFFER_SIZE)
            connection = sqlite3.connect(_sqlite_uri(database_path), uri=True, timeout=10)
            try:
                _quick_check(connection)
            finally:
                connection.close()
    return manifest


def create_backup(
    database_path: Path,
    generated_dir: Path,
    output_dir: Path,
    *,
    allow_missing: bool,
) -> Path:
    output_dir = output_dir.resolve()
    generated_dir = generated_dir.resolve()
    if output_dir == generated_dir or output_dir.is_relative_to(generated_dir):
        raise BackupError("backup output directory must not be inside the publicly served generated directory")
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(output_dir, 0o700)
    except OSError:
        pass

    created_at = datetime.now(timezone.utc)
    archive_path = _next_archive_path(output_dir, created_at)
    partial_path = archive_path.with_name(f".{archive_path.name}.partial")
    try:
        with tempfile.TemporaryDirectory(prefix=".terra-backup-stage-", dir=output_dir) as temporary:
            staging_dir = Path(temporary)
            os.chmod(staging_dir, 0o700)
            snapshot_path = staging_dir / DATABASE_ARCHIVE_PATH
            database_manifest = create_database_snapshot(database_path, snapshot_path)
            references, counts = read_snapshot_references(snapshot_path)
            images, missing = stage_referenced_images(
                generated_dir,
                staging_dir,
                references,
                allow_missing=allow_missing,
            )
            manifest: dict[str, Any] = {
                "format": "terra-backup",
                "format_version": FORMAT_VERSION,
                "created_at": created_at.isoformat().replace("+00:00", "Z"),
                "database": database_manifest,
                "images": images,
                "missing_images": missing,
                "counts": {
                    **counts,
                    "referenced_image_files": len(references),
                    "archived_image_files": len(images),
                    "missing_image_files": len(missing),
                },
            }
            manifest_path = staging_dir / MANIFEST_ARCHIVE_PATH
            _write_manifest(manifest_path, manifest)

            with tarfile.open(partial_path, "w:gz", compresslevel=6) as archive:
                archive.add(manifest_path, arcname=MANIFEST_ARCHIVE_PATH, recursive=False, filter=_tar_metadata)
                archive.add(snapshot_path, arcname=DATABASE_ARCHIVE_PATH, recursive=False, filter=_tar_metadata)
                for image in images:
                    archive.add(
                        staging_dir / image["archive_path"],
                        arcname=image["archive_path"],
                        recursive=False,
                        filter=_tar_metadata,
                    )
        os.chmod(partial_path, 0o600)
        verify_archive(partial_path)
        os.replace(partial_path, archive_path)
        os.chmod(archive_path, 0o600)
    except Exception:
        partial_path.unlink(missing_ok=True)
        raise
    return archive_path


def _path(value: str) -> Path:
    return Path(value).expanduser()


def _env_keep() -> int:
    try:
        return max(0, int(os.environ.get("TERRA_BACKUP_KEEP", "0")))
    except ValueError:
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create or verify a Terra database and referenced-image backup archive."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create", help="create and immediately verify a new archive")
    create.add_argument("--database", type=_path, default=DEFAULT_DATABASE)
    create.add_argument("--generated-dir", type=_path, default=DEFAULT_GENERATED_DIR)
    create.add_argument("--output-dir", type=_path, default=DEFAULT_OUTPUT_DIR)
    create.add_argument(
        "--allow-missing",
        action="store_true",
        help="archive a snapshot with missing references documented in the manifest (default: fail)",
    )
    create.add_argument(
        "--keep",
        type=int,
        default=_env_keep(),
        help="retain only the newest N archives after a successful backup "
        "(0 = keep all; env TERRA_BACKUP_KEEP)",
    )

    verify = subparsers.add_parser("verify", help="verify hashes, member allowlist, and SQLite integrity")
    verify.add_argument("archive", type=_path)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "create":
            archive_path = create_backup(
                args.database,
                args.generated_dir,
                args.output_dir,
                allow_missing=args.allow_missing,
            )
            archive_sha256, archive_size = _sha256_file(archive_path)
            print(f"created: {archive_path}")
            print(f"size: {archive_size} bytes")
            print(f"sha256: {archive_sha256}")
            for pruned in prune_old_archives(
                archive_path.parent, args.keep, keep_path=archive_path
            ):
                print(f"pruned: {pruned}")
        else:
            manifest = verify_archive(args.archive)
            counts = manifest.get("counts", {})
            print(f"verified: {args.archive}")
            print(
                "contents: "
                f"{counts.get('planets', '?')} planet(s), "
                f"{counts.get('archived_image_files', '?')} referenced PNG(s)"
            )
    except (BackupError, OSError, sqlite3.Error, tarfile.TarError) as exc:
        print(f"backup error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
