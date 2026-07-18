"""Terra 백엔드 — 소설 텍스트 → 행성 스펙 분석 API."""

from __future__ import annotations

import logging
import asyncio
import hmac
import os
import re
import shutil
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .derive import derive_physics
from .gemini import MODEL, GeminiError, generate_json
from .images import (
    GENERATED_DIR,
    build_negative_prompt,
    build_inhabitant_prompt,
    build_planet_prompt,
    build_surface_prompt,
    provider_status,
)
from .image_jobs import ImageQueueFull, ImageStorageFull, image_jobs
from .http_security import ContentLengthLimitMiddleware, ProductionHeadersMiddleware
from .maintenance import cleanup_generated_images
from .observability import (
    CorrelationLogFilter,
    RequestObservabilityMiddleware,
    observe_readiness,
    prometheus_text,
)
from .rate_limit import RateLimiter
from .repository import (
    create_moderation_report,
    delete_planet,
    get_planet,
    healthcheck_database,
    list_public_planets,
    save_planet,
    update_cover,
    update_image_asset,
)
from .schema import GEMINI_SCHEMA, PlanetSpec, ShortText

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("terra")
if not any(getattr(handler, "terra_correlation", False) for handler in log.handlers):
    correlation_handler = logging.StreamHandler()
    correlation_handler.terra_correlation = True  # type: ignore[attr-defined]
    correlation_handler.addFilter(CorrelationLogFilter())
    correlation_handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s "
            "request_id=%(request_id)s job_id=%(job_correlation_id)s %(message)s"
        )
    )
    log.addHandler(correlation_handler)
log.setLevel(logging.INFO)
log.propagate = False
# 외부 요청 URL을 INFO로 남기지 않는다. API 키는 헤더로 전송하며 로그에도 기록하지 않는다.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

ENVIRONMENT = os.environ.get("TERRA_ENV", "development").strip().lower()
PUBLIC_DOCS = ENVIRONMENT != "production"
FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"


@asynccontextmanager
async def lifespan(_: FastAPI):
    initial_cleanup = await asyncio.to_thread(cleanup_generated_images)
    if initial_cleanup.aborted or initial_cleanup.errors:
        log.warning("generated asset cleanup incomplete: %s", initial_cleanup.to_dict())
    else:
        log.info("generated asset cleanup: %s", initial_cleanup.to_dict())
    maintenance_task = asyncio.create_task(_generated_cleanup_loop())
    try:
        yield
    finally:
        maintenance_task.cancel()
        with suppress(asyncio.CancelledError):
            await maintenance_task
        # 배포/종료 중 MLX·업스케일러 자식 프로세스가 고아로 남지 않게 한다.
        await image_jobs.shutdown()


async def _generated_cleanup_loop() -> None:
    try:
        interval_hours = float(os.environ.get("TERRA_GENERATED_CLEANUP_INTERVAL_HOURS", "6"))
    except ValueError:
        interval_hours = 6.0
    interval_seconds = max(3600.0, min(7 * 86400.0, interval_hours * 3600.0))
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            result = await asyncio.to_thread(cleanup_generated_images)
        except Exception:
            log.exception("unexpected generated asset cleanup failure")
            continue
        if result.aborted or result.errors or not result.limits_satisfied:
            log.warning("generated asset cleanup incomplete: %s", result.to_dict())
        elif result.deleted_count:
            log.info("generated asset cleanup: %s", result.to_dict())


app = FastAPI(
    title="Terra API",
    docs_url="/docs" if PUBLIC_DOCS else None,
    redoc_url="/redoc" if PUBLIC_DOCS else None,
    openapi_url="/openapi.json" if PUBLIC_DOCS else None,
    lifespan=lifespan,
)
allowed_hosts = [
    host.strip()
    for host in os.environ.get(
        "TERRA_ALLOWED_HOSTS",
        "terra.jiun.dev,localhost,127.0.0.1,testserver",
    ).split(",")
    if host.strip()
]
# Starlette는 마지막에 추가한 middleware가 가장 바깥쪽이다. CORS preflight를
# 포함한 모든 응답에 보안 헤더가 붙도록 CORS부터 안쪽에 등록한다.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        origin.strip()
        for origin in os.environ.get(
            "TERRA_CORS_ORIGINS",
            "http://localhost:5173,http://127.0.0.1:5173",
        ).split(",")
        if origin.strip()
    ],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)
app.add_middleware(
    ContentLengthLimitMiddleware,
    max_bytes=int(os.environ.get("TERRA_MAX_REQUEST_BYTES", str(1024 * 1024))),
)
app.add_middleware(ProductionHeadersMiddleware)
# 마지막에 등록한 user middleware가 가장 바깥에서 동작한다. 따라서 CORS,
# Host/body 제한 응답까지 같은 bounded route metric과 서버 생성 request ID를 받는다.
app.add_middleware(RequestObservabilityMiddleware)
GENERATED_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/generated", StaticFiles(directory=GENERATED_DIR), name="generated")

analyze_limiter = RateLimiter(
    limit=int(os.environ.get("TERRA_ANALYZE_RATE_LIMIT", "12")),
    window_seconds=3600,
)
image_limiter = RateLimiter(
    limit=int(os.environ.get("TERRA_IMAGE_RATE_LIMIT", "4")),
    window_seconds=3600,
)
image_global_limiter = RateLimiter(
    limit=int(os.environ.get("TERRA_IMAGE_GLOBAL_RATE_LIMIT", "12")),
    window_seconds=3600,
)
save_limiter = RateLimiter(
    limit=int(os.environ.get("TERRA_SAVE_RATE_LIMIT", "12")),
    window_seconds=3600,
)
report_limiter = RateLimiter(
    limit=int(os.environ.get("TERRA_REPORT_RATE_LIMIT", "5")),
    window_seconds=3600,
)
report_global_limiter = RateLimiter(
    limit=int(os.environ.get("TERRA_REPORT_GLOBAL_RATE_LIMIT", "60")),
    window_seconds=3600,
)
analyze_slots = asyncio.Semaphore(int(os.environ.get("TERRA_ANALYZE_CONCURRENCY", "3")))

SYSTEM_PROMPT = """당신은 SF 소설 텍스트에서 행성의 물리적·환경적 특성을 추출하는 행성과학 분석가다.

사용자가 가상 행성에 대한 소설풍 묘사를 준다. 텍스트에서 알아낼 수 있는 모든 것을 추출하되,
직접 명시된 것과 유추한 것을 엄격히 구분하라.

규칙:
1. 텍스트에 직접 근거가 있으면 confidence="stated", 물리 법칙이나 정황으로 유추하면 "inferred",
   근거가 약한 상상 보완이면 "speculative"로 표시한다.
2. inferences 배열에는 중요한 판단마다 하나씩 넣는다 (최소 8개 이상 권장):
   행성 형태, 중력, 자전, 대기, 기후, 바다/지형, 색채, 위성/고리, 거주민 생리 등.
   evidence_quote에는 원문 문장을 그대로 인용한다 (유추라면 유추의 출발점이 된 문장).
3. 수치는 물리적으로 일관되게 정하라. 예: "몸이 무겁게 느껴진다" → gravity_g > 1.
   "하늘에 두 개의 태양" → star.count=2이며 colors_hex에 각 항성색을 순서대로 넣는다.
   "짧은 하루" → rotation_hours < 24.
   빠른 자전(rotation_hours < 10)이면 oblateness를 크게 잡아라.
4. 색상은 텍스트의 묘사(하늘색, 바다색, 식생색)를 최대한 반영해 hex로 지정하라.
   palette는 바다 깊은곳→얕은곳→해안→저지대→중지대→고지대→봉우리 순의 지형 고도 색이다.
5. surface.feature_type은 가장 두드러지는 지표 구조를 고른다:
   일반 대륙=continents, 섬이 많은 해양=archipelago, 충돌구=cratered, 협곡= canyons,
   사막/모래언덕=dunes, 수정/유리/결정 지형=crystalline, 화산=volcanic,
   행성 규모 인공 구조=artificial. feature_scale과 biome_contrast도 묘사 강도에 맞춰라.
   material_type에는 확대했을 때 보일 대표 표면 재질을 넣는다. landmarks에는 원문에 근거가 있는
   동굴 입구, 결정 지대, 암석 첨탑, 화산 분출구, 사구, 인공 구조물, 거대 식생, 얼음 첨탑을 복수로 넣는다.
   visual_prompt는 행성 전체 이미지에서 반드시 보여야 할 독특한 지형·대기·날씨·색을 영어로
   2~4문장 작성한다. 지구나 태양계 행성 이름을 비교 대상으로 쓰지 말고, 원문에 없는 특징은 추가하지 않는다.
6. 거주민이 언급되면 inhabitants에 모두 넣고, 외형(appearance)·생리(physiology)·
   중력 적응(gravity_adaptation)을 정리하라. portrait_prompt는 초상화 이미지 생성용
   영어 프롬프트로 쓴다. 더듬이·눈·사지처럼 개수가 중요한 특징은 정확한 개수, 색, 신체 발생 위치를
   명시하고 외형·의복·재질·중력 적응을 빠짐없이 담되 원문에 없는 특징은 추가하지 않는다.
7. 언급이 전혀 없는 항목은 물리적으로 그럴듯한 기본값을 쓰되 inference로 남기지 마라.
8. 모든 서술형 텍스트 필드(claim, reasoning, appearance 등)는 한국어로 쓴다.
"""


class AnalyzeRequest(BaseModel):
    text: str = Field(min_length=20, max_length=100000)


class AnalyzeResponse(BaseModel):
    spec: PlanetSpec
    physics: dict[str, Any]
    model: str


class ImageRequest(BaseModel):
    spec: PlanetSpec
    kind: Literal["planet", "surface", "inhabitant"]
    inhabitant_index: int | None = Field(default=None, ge=0)
    seed: int | None = Field(default=None, ge=0, le=2**31 - 1)
    quality: Literal["fast", "balanced", "quality"] = "balanced"


class ImageJobResponse(BaseModel):
    id: str
    status: Literal[
        "queued",
        "generating",
        "verifying",
        "refining",
        "upscaling",
        "completed",
        "failed",
    ]
    created_at: float
    updated_at: float
    kind: str
    quality: Literal["fast", "balanced", "quality"] = "balanced"
    candidate_current: int = 0
    candidate_total: int = 1
    url: str | None = None
    seed: int | None = None
    error: str | None = None
    quality_score: int | None = None
    verification_notes: list[str] = Field(default_factory=list)
    provider: str
    model: str


class SavedImageAsset(BaseModel):
    url: str = Field(max_length=500)
    seed: int = Field(default=0, ge=0, le=2**31 - 1)
    provider: str = Field(default="unknown", max_length=100)
    model: str = Field(default="unknown", max_length=200)
    quality: Literal["fast", "balanced", "quality"] | None = None
    quality_score: int | None = Field(default=None, ge=0, le=100)
    verification_notes: list[ShortText] = Field(default_factory=list, max_length=8)


class SavePlanetRequest(BaseModel):
    spec: PlanetSpec
    physics: dict[str, Any] = Field(max_length=64)
    model: str = Field(max_length=200)
    cover_image_url: str | None = Field(default=None, max_length=500)
    image_assets: dict[str, SavedImageAsset] = Field(default_factory=dict, max_length=18)
    # 현재 제품의 저장 동작은 공개 갤러리 공유다. 소유자 인증이 없는 상태에서
    # private처럼 보이지만 URL로 읽히는 레코드를 만들지 않는다.
    public: Literal[True] = True


class UpdateImageAssetRequest(BaseModel):
    key: str = Field(max_length=40)
    image: SavedImageAsset
    edit_token: str = Field(min_length=20, max_length=200)


class UpdateCoverRequest(BaseModel):
    cover_image_url: str = Field(max_length=500)
    edit_token: str = Field(min_length=20, max_length=200)


class DeletePlanetRequest(BaseModel):
    edit_token: str = Field(min_length=20, max_length=200)


class ReportPlanetRequest(BaseModel):
    reason: Literal[
        "personal_information",
        "copyright",
        "harassment",
        "unsafe_content",
        "spam",
        "other",
    ]
    details: str = Field(default="", max_length=500)


def _safe_cover_url(value: str | None) -> str | None:
    if value is None:
        return None
    if not re.fullmatch(r"/generated/[A-Za-z0-9][A-Za-z0-9._-]*\.png", value):
        raise HTTPException(status_code=422, detail="대표 이미지는 Terra 생성 이미지여야 합니다.")
    return value


def _safe_image_key(value: str, *, inhabitant_count: int | None = None) -> str:
    if value in {"planet", "surface"}:
        return value
    suffix = value[11:]
    if value.startswith("inhabitant:") and suffix.isascii() and suffix.isdigit():
        index = int(suffix)
        if inhabitant_count is None or index < inhabitant_count:
            return value
    raise HTTPException(status_code=422, detail="올바르지 않은 이미지 자산 키입니다.")


def _safe_image_asset(value: SavedImageAsset) -> dict[str, Any]:
    result = value.model_dump()
    result["url"] = _safe_cover_url(value.url)
    return result


def _public_saved_spec(value: PlanetSpec) -> PlanetSpec:
    """공개 저장본에서 원문 직접 인용만 제거하고 로컬 분석값은 바꾸지 않는다."""
    public_spec = value.model_copy(deep=True)
    for inference in public_spec.inferences:
        inference.evidence_quote = ""
    return public_spec


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "model": MODEL}


@app.get("/api/livez", include_in_schema=False)
async def liveness() -> dict[str, str]:
    """Process-level probe; dependencies are intentionally checked by /readyz."""
    return {"status": "ok"}


@app.get("/api/readyz", include_in_schema=False)
async def readiness(response: Response) -> dict[str, Any]:
    """Deployment probe with safe, non-secret dependency diagnostics."""
    database_ready, queue = await asyncio.gather(
        asyncio.to_thread(healthcheck_database),
        image_jobs.stats(),
    )
    key_ready = bool(
        (os.environ.get("GEMINI_API_KEYS") or os.environ.get("GEMINI_API_KEY", "")).strip()
    )
    try:
        minimum_free_mb = max(128, int(os.environ.get("TERRA_MIN_FREE_DISK_MB", "2048")))
    except ValueError:
        minimum_free_mb = 2048
    try:
        free_disk_bytes = shutil.disk_usage(GENERATED_DIR).free
    except OSError:
        free_disk_bytes = 0
    free_disk_mb = free_disk_bytes // (1024 * 1024)
    disk_ready = free_disk_mb >= minimum_free_mb
    frontend_ready = ENVIRONMENT != "production" or FRONTEND_DIST.is_dir()
    image_provider = provider_status()
    ready = database_ready and key_ready and disk_ready and frontend_ready
    checks = {
        "database": database_ready,
        "analysis_provider": key_ready,
        "storage": disk_ready,
        "frontend": frontend_ready,
        # 이미지 생성기는 선택 기능이므로 readiness를 막지 않고 degraded로 표시한다.
        "image_provider": image_provider.available,
    }
    observe_readiness(checks, free_disk_bytes=free_disk_bytes)
    response.status_code = 200 if ready else 503
    return {
        "status": "ready" if ready else "not_ready",
        "checks": checks,
        "queue": queue,
        "free_disk_mb": free_disk_mb,
    }


@app.get("/api/admin/metrics", include_in_schema=False)
async def prometheus_metrics(request: Request) -> Response:
    """Expose bounded process metrics only to a bearer-authenticated scraper."""
    expected = os.environ.get("TERRA_METRICS_TOKEN", "").strip()
    if len(expected) < 32:
        # 비활성 설치에서는 라우트 존재 자체를 공개하지 않는다.
        raise HTTPException(status_code=404, detail="Not found")
    scheme, separator, supplied = request.headers.get("authorization", "").partition(" ")
    if (
        separator != " "
        or scheme.lower() != "bearer"
        or not hmac.compare_digest(supplied, expected)
    ):
        raise HTTPException(
            status_code=401,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return Response(
        prometheus_text(),
        media_type="text/plain; version=0.0.4",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/image/status")
async def image_status() -> dict[str, str | bool]:
    status = provider_status()
    return {
        "available": status.available,
        "provider": status.provider,
        "model": status.model,
        "message": status.message,
    }


@app.post("/api/analyze", response_model=AnalyzeResponse)
async def analyze(req: AnalyzeRequest, request: Request) -> AnalyzeResponse:
    await analyze_limiter.check(request)
    try:
        async with analyze_slots:
            raw = await generate_json(
                system=SYSTEM_PROMPT,
                user_text=f"다음 소설 텍스트를 분석하라:\n\n---\n{req.text}\n---",
                response_schema=GEMINI_SCHEMA,
            )
    except GeminiError as e:
        log.error("분석 실패: %s", e)
        raise HTTPException(
            status_code=502,
            detail="행성 분석 서비스에 일시적인 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.",
        ) from e

    # Pydantic이 범위를 벗어난 값은 거부하므로, 실패 시 원인 필드를 알려준다
    try:
        spec = PlanetSpec.model_validate(raw)
    except Exception as e:
        log.warning("스펙 검증 실패, 관대한 파싱 시도: %s", e)
        spec = _lenient_parse(raw)

    return AnalyzeResponse(spec=spec, physics=derive_physics(spec), model=MODEL)


@app.post("/api/image/generate", response_model=ImageJobResponse, status_code=202)
async def create_image(req: ImageRequest, request: Request) -> dict[str, Any]:
    await image_limiter.check(request)
    await image_global_limiter.check(request, key_override="global")
    if req.kind == "planet":
        prompt = build_planet_prompt(req.spec)
        negative_prompt = build_negative_prompt("planet", req.spec)
    elif req.kind == "surface":
        prompt = build_surface_prompt(req.spec)
        negative_prompt = build_negative_prompt("surface", req.spec)
    else:
        if req.inhabitant_index is None or req.inhabitant_index >= len(req.spec.inhabitants):
            raise HTTPException(status_code=400, detail="올바른 거주민 인덱스가 필요합니다.")
        inhabitant = req.spec.inhabitants[req.inhabitant_index]
        prompt = build_inhabitant_prompt(req.spec, inhabitant)
        negative_prompt = build_negative_prompt("inhabitant", req.spec, inhabitant)

    try:
        job = await image_jobs.create(
            prompt=prompt,
            negative_prompt=negative_prompt,
            spec=req.spec,
            kind=req.kind,
            seed=req.seed,
            quality=req.quality,
            inhabitant_index=req.inhabitant_index,
        )
    except ImageQueueFull as exc:
        raise HTTPException(
            status_code=503,
            detail=str(exc),
            headers={"Retry-After": str(exc.retry_after)},
        ) from exc
    except ImageStorageFull as exc:
        raise HTTPException(status_code=507, detail=str(exc)) from exc
    return job.public()


@app.get("/api/image/jobs/{job_id}", response_model=ImageJobResponse)
async def get_image_job(job_id: str) -> dict[str, Any]:
    job = await image_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="이미지 생성 작업을 찾을 수 없습니다.")
    return job.public()


@app.delete("/api/image/jobs/{job_id}", response_model=ImageJobResponse)
async def cancel_image_job(job_id: str) -> dict[str, Any]:
    job = await image_jobs.cancel(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="이미지 생성 작업을 찾을 수 없습니다.")
    return job.public()


@app.post("/api/planets", status_code=201)
async def create_saved_planet(req: SavePlanetRequest, request: Request) -> dict[str, Any]:
    await save_limiter.check(request)
    public_spec = _public_saved_spec(req.spec)
    image_assets = {
        _safe_image_key(key, inhabitant_count=len(req.spec.inhabitants)): _safe_image_asset(asset)
        for key, asset in req.image_assets.items()
    }
    cover_image_url = _safe_cover_url(req.cover_image_url)
    if "planet" in image_assets:
        cover_image_url = image_assets["planet"]["url"]
    return await asyncio.to_thread(
        save_planet,
        spec=public_spec,
        # 공개 영구 데이터는 클라이언트가 제출한 파생값을 신뢰하지 않는다.
        physics=derive_physics(public_spec),
        model=MODEL,
        cover_image_url=cover_image_url,
        is_public=req.public,
        image_assets=image_assets,
    )


@app.get("/api/planets")
async def gallery(
    limit: int = Query(default=40, ge=1, le=100),
    offset: int = Query(default=0, ge=0, le=10000),
) -> list[dict[str, Any]]:
    return await asyncio.to_thread(list_public_planets, limit=limit, offset=offset)


@app.get("/api/planets/{planet_id}")
async def saved_planet(planet_id: str) -> dict[str, Any]:
    planet = await asyncio.to_thread(get_planet, planet_id, public_only=True)
    if planet is None:
        raise HTTPException(status_code=404, detail="저장된 행성을 찾을 수 없습니다.")
    return planet


@app.post("/api/planets/{planet_id}/reports", status_code=202)
async def report_saved_planet(
    planet_id: str,
    req: ReportPlanetRequest,
    request: Request,
) -> dict[str, str]:
    await report_limiter.check(request)
    await report_global_limiter.check(request, key_override="global")
    details = req.details.strip()
    if req.reason == "other" and not details:
        raise HTTPException(status_code=422, detail="기타 신고 사유를 간단히 적어 주세요.")
    created = await asyncio.to_thread(
        create_moderation_report,
        planet_id,
        req.reason,
        details,
    )
    if not created:
        raise HTTPException(status_code=404, detail="저장된 행성을 찾을 수 없습니다.")
    # 내부 신고 ID나 요청자 관련 정보는 공개 응답에 포함하지 않는다.
    return {"status": "received"}


@app.delete("/api/planets/{planet_id}", status_code=204)
async def delete_saved_planet(
    planet_id: str,
    req: DeletePlanetRequest,
    request: Request,
) -> Response:
    await save_limiter.check(request)
    try:
        deleted = await asyncio.to_thread(delete_planet, planet_id, req.edit_token)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="저장된 행성을 찾을 수 없습니다.")
    # capability와 삭제된 데이터가 응답으로 되돌아가지 않게 빈 응답만 보낸다.
    return Response(status_code=204)


@app.patch("/api/planets/{planet_id}/cover")
async def update_saved_planet_cover(
    planet_id: str,
    req: UpdateCoverRequest,
    request: Request,
) -> dict[str, Any]:
    await save_limiter.check(request)
    try:
        planet = await asyncio.to_thread(
            update_cover,
            planet_id,
            _safe_cover_url(req.cover_image_url),
            req.edit_token,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if planet is None:
        raise HTTPException(status_code=404, detail="저장된 행성을 찾을 수 없습니다.")
    return planet


@app.patch("/api/planets/{planet_id}/images")
async def update_saved_planet_image(
    planet_id: str,
    req: UpdateImageAssetRequest,
    request: Request,
) -> dict[str, Any]:
    await save_limiter.check(request)
    existing = await asyncio.to_thread(get_planet, planet_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="저장된 행성을 찾을 수 없습니다.")
    inhabitant_count = len(existing.get("spec", {}).get("inhabitants", []))
    try:
        planet = await asyncio.to_thread(
            update_image_asset,
            planet_id,
            _safe_image_key(req.key, inhabitant_count=inhabitant_count),
            _safe_image_asset(req.image),
            req.edit_token,
        )
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if planet is None:
        raise HTTPException(status_code=404, detail="저장된 행성을 찾을 수 없습니다.")
    return planet


def _lenient_parse(raw: dict[str, Any]) -> PlanetSpec:
    """LLM이 범위를 벗어난 값을 준 경우 섹션별로 살리고 나머지는 기본값."""
    spec = PlanetSpec()
    for section in PlanetSpec.model_fields:
        if raw.get(section) is None:
            continue
        try:
            validated = PlanetSpec.model_validate({section: raw[section]})
            setattr(spec, section, getattr(validated, section))
        except Exception:
            log.warning("섹션 %s 검증 실패 — 기본값 사용", section)
    return spec


# 프로덕션 빌드가 있으면 FastAPI가 SPA도 함께 서비스한다. API 라우트보다 반드시 뒤에 둔다.
if FRONTEND_DIST.is_dir():
    app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")
