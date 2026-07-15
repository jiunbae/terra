"""Safe retention maintenance for locally generated PNG assets.

The cleaner is synchronous by design so callers can choose when and where it
runs.  FastAPI integration should invoke :func:`cleanup_generated_images` via
``asyncio.to_thread``.  Database references are loaded before any deletion; if
that scan is incomplete, cleanup aborts rather than risking a gallery asset.
"""

from __future__ import annotations

import json
import math
import os
import re
import shutil
import sqlite3
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

_GENERATED_PNG = re.compile(r"^/generated/([^/?#]+\.png)$", re.IGNORECASE)
_MEBIBYTE = 1024 * 1024


class ReferenceScanError(RuntimeError):
    """Raised when the cleaner cannot prove which generated files are referenced."""


@dataclass(frozen=True, slots=True)
class GeneratedCleanupPolicy:
    ttl_hours: float = 168.0
    min_free_bytes: int = 2_048 * _MEBIBYTE
    max_store_bytes: int = 0
    grace_minutes: float = 60.0

    def __post_init__(self) -> None:
        if not math.isfinite(self.ttl_hours) or self.ttl_hours < 0:
            raise ValueError("ttl_hours must be a finite non-negative number")
        if self.min_free_bytes < 0 or self.max_store_bytes < 0:
            raise ValueError("storage thresholds must be non-negative")
        if not math.isfinite(self.grace_minutes) or self.grace_minutes < 0:
            raise ValueError("grace_minutes must be a finite non-negative number")

    @classmethod
    def from_env(cls) -> GeneratedCleanupPolicy:
        return cls(
            ttl_hours=_env_float("TERRA_GENERATED_TTL_HOURS", 168.0),
            min_free_bytes=_env_int("TERRA_MIN_FREE_DISK_MB", 2_048) * _MEBIBYTE,
            max_store_bytes=_env_int("TERRA_GENERATED_MAX_MB", 0) * _MEBIBYTE,
            grace_minutes=_env_float("TERRA_GENERATED_CLEANUP_GRACE_MINUTES", 60.0),
        )


@dataclass(frozen=True, slots=True)
class GeneratedCleanupResult:
    aborted: bool
    scanned_pngs: int
    referenced_pngs: int
    protected_recent_pngs: int
    deleted_ttl: tuple[str, ...]
    deleted_pressure: tuple[str, ...]
    reclaimed_bytes: int
    store_bytes_before: int
    store_bytes_after: int
    free_bytes_before: int | None
    estimated_free_bytes_after: int | None
    pressure_was_active: bool
    limits_satisfied: bool
    errors: tuple[str, ...]
    elapsed_seconds: float

    @property
    def deleted_count(self) -> int:
        return len(self.deleted_ttl) + len(self.deleted_pressure)

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["deleted_count"] = self.deleted_count
        return value


@dataclass(frozen=True, slots=True)
class _PngFile:
    path: Path
    name: str
    size: int
    modified_at: float


def _env_float(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return value if math.isfinite(value) and value >= 0 else default


def _env_int(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return value if value >= 0 else default


def _local_png_name(url: object) -> str | None:
    if not isinstance(url, str):
        return None
    match = _GENERATED_PNG.fullmatch(url.strip())
    if match is None:
        return None
    name = match.group(1)
    if name in {".", ".."} or Path(name).name != name:
        return None
    return name


def _asset_urls(value: object) -> list[str]:
    """Extract URL fields recursively to remain compatible with future asset metadata."""
    urls: list[str] = []
    if isinstance(value, dict):
        url = value.get("url")
        if isinstance(url, str):
            urls.append(url)
        for nested in value.values():
            if isinstance(nested, (dict, list)):
                urls.extend(_asset_urls(nested))
    elif isinstance(value, list):
        for nested in value:
            urls.extend(_asset_urls(nested))
    return urls


def load_referenced_generated_pngs(db_path: str | Path | None = None) -> frozenset[str]:
    """Return generated PNG basenames referenced by any saved planet.

    The database is opened read-only. Missing schema, malformed asset JSON, or
    read failures raise :class:`ReferenceScanError`; callers must not delete on
    such an incomplete reference scan.
    """
    if db_path is None:
        # Lazy import honors tests/operations that replace repository.DB_PATH and
        # avoids copying the value at module import time.
        from . import repository

        path = Path(repository.DB_PATH)
    else:
        path = Path(db_path)
    if not path.is_file():
        raise ReferenceScanError(f"planet database does not exist: {path}")

    uri = f"{path.resolve().as_uri()}?mode=ro"
    references: set[str] = set()
    try:
        connection = sqlite3.connect(uri, uri=True, timeout=5)
        try:
            rows = connection.execute(
                "SELECT cover_image_url, image_assets_json FROM planets"
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise ReferenceScanError(f"failed to read planet image references: {exc}") from exc

    for cover_url, raw_assets in rows:
        if (name := _local_png_name(cover_url)) is not None:
            references.add(name)
        try:
            assets = json.loads(raw_assets or "{}")
        except (TypeError, json.JSONDecodeError) as exc:
            raise ReferenceScanError("planet image_assets_json is malformed") from exc
        if not isinstance(assets, dict):
            raise ReferenceScanError("planet image_assets_json is not an object")
        for url in _asset_urls(assets):
            if (name := _local_png_name(url)) is not None:
                references.add(name)
    return frozenset(references)


def _scan_pngs(directory: Path) -> tuple[list[_PngFile], list[str]]:
    files: list[_PngFile] = []
    errors: list[str] = []
    try:
        entries = tuple(directory.iterdir())
    except OSError as exc:
        return files, [f"failed to scan generated directory: {exc}"]
    for entry in entries:
        # Never recurse into .guides (or any directory), never follow symlinks,
        # and never inspect/delete non-PNG files.
        if entry.suffix.lower() != ".png" or entry.is_symlink():
            continue
        try:
            stat = entry.stat()
        except OSError as exc:
            errors.append(f"failed to stat {entry.name}: {exc}")
            continue
        if not entry.is_file():
            continue
        files.append(_PngFile(entry, entry.name, stat.st_size, stat.st_mtime))
    return files, errors


def _disk_free(directory: Path) -> tuple[int | None, str | None]:
    try:
        return shutil.disk_usage(directory).free, None
    except OSError as exc:
        return None, f"failed to read disk usage: {exc}"


def cleanup_generated_images(
    *,
    generated_dir: str | Path | None = None,
    db_path: str | Path | None = None,
    policy: GeneratedCleanupPolicy | None = None,
    now: float | None = None,
) -> GeneratedCleanupResult:
    """Delete expired/pressure-eligible unreferenced top-level PNG files.

    Cleanup order is deterministic: TTL-expired files first, then oldest
    remaining unreferenced files while the generated store exceeds
    ``max_store_bytes`` or disk free space is below ``min_free_bytes``. Files
    younger than ``grace_minutes`` are never pressure-deleted, protecting active
    jobs and newly generated images before the user saves a planet.
    """
    started = time.monotonic()
    selected_policy = policy or GeneratedCleanupPolicy.from_env()
    if generated_dir is None:
        from .images import GENERATED_DIR

        directory = Path(GENERATED_DIR)
    else:
        directory = Path(generated_dir)

    if not directory.exists():
        return GeneratedCleanupResult(
            aborted=False,
            scanned_pngs=0,
            referenced_pngs=0,
            protected_recent_pngs=0,
            deleted_ttl=(),
            deleted_pressure=(),
            reclaimed_bytes=0,
            store_bytes_before=0,
            store_bytes_after=0,
            free_bytes_before=None,
            estimated_free_bytes_after=None,
            pressure_was_active=False,
            limits_satisfied=True,
            errors=(),
            elapsed_seconds=time.monotonic() - started,
        )

    try:
        references = load_referenced_generated_pngs(db_path)
    except ReferenceScanError as exc:
        return GeneratedCleanupResult(
            aborted=True,
            scanned_pngs=0,
            referenced_pngs=0,
            protected_recent_pngs=0,
            deleted_ttl=(),
            deleted_pressure=(),
            reclaimed_bytes=0,
            store_bytes_before=0,
            store_bytes_after=0,
            free_bytes_before=None,
            estimated_free_bytes_after=None,
            pressure_was_active=False,
            limits_satisfied=False,
            errors=(str(exc),),
            elapsed_seconds=time.monotonic() - started,
        )

    files, errors = _scan_pngs(directory)
    total_before = sum(item.size for item in files)
    remaining_bytes = total_before
    referenced_present = sum(item.name in references for item in files)
    current_time = time.time() if now is None else now
    grace_cutoff = current_time - selected_policy.grace_minutes * 60
    ttl_age_seconds = max(
        selected_policy.ttl_hours * 3600,
        selected_policy.grace_minutes * 60,
    )
    ttl_cutoff = current_time - ttl_age_seconds
    free_before, disk_error = _disk_free(directory)
    if disk_error is not None:
        errors.append(disk_error)
    estimated_free = free_before
    deleted_names: set[str] = set()
    deleted_ttl: list[str] = []
    deleted_pressure: list[str] = []
    reclaimed = 0

    def delete(item: _PngFile, target: list[str]) -> bool:
        nonlocal remaining_bytes, estimated_free, reclaimed
        try:
            item.path.unlink()
        except FileNotFoundError:
            # Another cleanup/process already removed it; update the scanned store
            # accounting without claiming bytes reclaimed by this run.
            remaining_bytes = max(0, remaining_bytes - item.size)
            deleted_names.add(item.name)
            return False
        except OSError as exc:
            errors.append(f"failed to delete {item.name}: {exc}")
            return False
        target.append(item.name)
        deleted_names.add(item.name)
        reclaimed += item.size
        remaining_bytes = max(0, remaining_bytes - item.size)
        if estimated_free is not None:
            estimated_free += item.size
        return True

    unreferenced = sorted(
        (item for item in files if item.name not in references),
        key=lambda item: (item.modified_at, item.name),
    )
    for item in unreferenced:
        if item.modified_at <= ttl_cutoff:
            delete(item, deleted_ttl)

    def under_pressure() -> bool:
        capacity = (
            selected_policy.max_store_bytes > 0
            and remaining_bytes > selected_policy.max_store_bytes
        )
        disk = (
            selected_policy.min_free_bytes > 0
            and estimated_free is not None
            and estimated_free < selected_policy.min_free_bytes
        )
        return capacity or disk

    pressure_was_active = under_pressure()
    if pressure_was_active:
        for item in unreferenced:
            if not under_pressure():
                break
            if item.name in deleted_names or item.modified_at > grace_cutoff:
                continue
            delete(item, deleted_pressure)

    protected_recent = sum(
        item.name not in references
        and item.name not in deleted_names
        and item.modified_at > grace_cutoff
        for item in files
    )
    capacity_satisfied = (
        selected_policy.max_store_bytes <= 0
        or remaining_bytes <= selected_policy.max_store_bytes
    )
    disk_satisfied = (
        selected_policy.min_free_bytes <= 0
        or (estimated_free is not None and estimated_free >= selected_policy.min_free_bytes)
    )

    return GeneratedCleanupResult(
        aborted=False,
        scanned_pngs=len(files),
        referenced_pngs=referenced_present,
        protected_recent_pngs=protected_recent,
        deleted_ttl=tuple(deleted_ttl),
        deleted_pressure=tuple(deleted_pressure),
        reclaimed_bytes=reclaimed,
        store_bytes_before=total_before,
        store_bytes_after=remaining_bytes,
        free_bytes_before=free_before,
        estimated_free_bytes_after=estimated_free,
        pressure_was_active=pressure_was_active,
        limits_satisfied=capacity_satisfied and disk_satisfied,
        errors=tuple(errors),
        elapsed_seconds=time.monotonic() - started,
    )
