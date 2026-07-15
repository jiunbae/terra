"""Cloudflare 타임아웃과 분리된 비동기 이미지 생성 작업 관리자."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import secrets
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal

from .image_pipeline import (
    QualityMode,
    ScoredCandidate,
    build_candidate_plans,
    build_upscaler_command,
    compose_pipeline_prompt,
    derive_candidate_seed,
    get_pipeline_config,
    select_winner,
)
from .image_verifier import ImageVerificationResult, verify_generated_image
from .images import (
    GENERATED_DIR,
    ImageGenerationError,
    generate_candidate_batch,
    image_subprocess_environment,
    provider_status,
)
from .schema import PlanetSpec
from .world_bible import build_world_bible

log = logging.getLogger("terra.image_jobs")
JOB_STATE_PATH = Path(__file__).resolve().parents[1] / "data" / "image_jobs.json"

JobStatus = Literal[
    "queued",
    "generating",
    "verifying",
    "refining",
    "upscaling",
    "completed",
    "failed",
]


class ImageQueueFull(RuntimeError):
    def __init__(self, retry_after: int) -> None:
        super().__init__("이미지 생성 대기열이 가득 찼습니다.")
        self.retry_after = retry_after


class ImageStorageFull(RuntimeError):
    pass


@dataclass
class ImageJob:
    id: str
    status: JobStatus
    created_at: float
    updated_at: float
    kind: str
    seed: int | None
    quality: str = "balanced"
    candidate_current: int = 0
    candidate_total: int = 1
    url: str | None = None
    actual_seed: int | None = None
    error: str | None = None
    quality_score: int | None = None
    verification_notes: list[str] = field(default_factory=list)

    def public(self) -> dict[str, Any]:
        provider = provider_status()
        return {
            "id": self.id,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "kind": self.kind,
            "quality": self.quality,
            "candidate_current": self.candidate_current,
            "candidate_total": self.candidate_total,
            "url": self.url,
            "seed": self.actual_seed,
            "error": self.error,
            "quality_score": self.quality_score,
            "verification_notes": self.verification_notes,
            "provider": provider.provider,
            "model": provider.model,
        }


class ImageJobManager:
    def __init__(self, *, state_path: Path | None = None) -> None:
        self._state_path = state_path
        self._jobs: dict[str, ImageJob] = self._load_state()
        self._tasks: set[asyncio.Task[None]] = set()
        self._task_by_job: dict[str, asyncio.Task[None]] = {}
        self._lock = asyncio.Lock()

    async def create(
        self,
        *,
        prompt: str,
        negative_prompt: str,
        spec: PlanetSpec,
        kind: str,
        seed: int | None,
        quality: QualityMode | str = QualityMode.BALANCED,
        inhabitant_index: int | None = None,
    ) -> ImageJob:
        config = get_pipeline_config(quality)
        now = time.time()
        base_seed = seed if seed is not None else secrets.randbelow(2**31 - 2) + 1
        job = ImageJob(
            id=secrets.token_urlsafe(12),
            status="queued",
            created_at=now,
            updated_at=now,
            kind=kind,
            seed=base_seed,
            quality=config.quality.value,
            candidate_total=config.candidate_count,
        )
        GENERATED_DIR.mkdir(parents=True, exist_ok=True)
        minimum_free = self._env_int("TERRA_MIN_FREE_DISK_MB", 2048, 128, 1_048_576) * 1024 * 1024
        if shutil.disk_usage(GENERATED_DIR).free < minimum_free:
            raise ImageStorageFull("이미지 저장 공간이 부족해 새 작업을 시작할 수 없습니다.")

        async with self._lock:
            # 완료된 작업 상태는 24시간만 보관한다. PNG 파일은 별도 보존된다.
            cutoff = now - 86400
            self._jobs = {
                key: value
                for key, value in self._jobs.items()
                if value.updated_at >= cutoff or value.status not in {"completed", "failed"}
            }
            active = [
                existing
                for existing in self._jobs.values()
                if existing.status not in {"completed", "failed"}
            ]
            active_units = sum(self._work_units(existing.quality) for existing in active)
            max_jobs = self._env_int("TERRA_IMAGE_MAX_ACTIVE_JOBS", 3, 1, 32)
            max_units = self._env_int("TERRA_IMAGE_MAX_WORK_UNITS", 8, 1, 128)
            if len(active) >= max_jobs or active_units + self._work_units(job.quality) > max_units:
                raise ImageQueueFull(
                    self._env_int("TERRA_IMAGE_QUEUE_RETRY_AFTER", 120, 5, 3600)
                )
            self._jobs[job.id] = job
            self._persist_state()

        task = asyncio.create_task(
            self._run(job.id, prompt, negative_prompt, spec, inhabitant_index)
        )
        self._tasks.add(task)
        self._task_by_job[job.id] = task
        task.add_done_callback(lambda completed, key=job.id: self._task_done(key, completed))
        return job

    def _task_done(self, job_id: str, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        self._task_by_job.pop(job_id, None)

    async def cancel(self, job_id: str) -> ImageJob | None:
        """Cancel one queued/running job and leave an honest terminal status."""
        async with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            if job.status in {"completed", "failed"}:
                return job
            job.status = "failed"
            job.error = "이미지 생성 작업이 취소되었습니다."
            job.updated_at = time.time()
            task = self._task_by_job.get(job_id)
            self._persist_state()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return job

    async def shutdown(self) -> None:
        """Cancel and reap every in-memory task during graceful application shutdown."""
        async with self._lock:
            tasks = tuple(self._tasks)
            now = time.time()
            for job_id, task in self._task_by_job.items():
                if not task.done() and (job := self._jobs.get(job_id)) is not None:
                    job.status = "failed"
                    job.error = "서버 종료로 이미지 생성 작업이 중단되었습니다."
                    job.updated_at = now
            self._persist_state()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def get(self, job_id: str) -> ImageJob | None:
        async with self._lock:
            return self._jobs.get(job_id)

    async def stats(self) -> dict[str, int]:
        async with self._lock:
            active = [
                job for job in self._jobs.values() if job.status not in {"completed", "failed"}
            ]
            return {
                "active_jobs": len(active),
                "active_work_units": sum(self._work_units(job.quality) for job in active),
                "max_jobs": self._env_int("TERRA_IMAGE_MAX_ACTIVE_JOBS", 3, 1, 32),
                "max_work_units": self._env_int(
                    "TERRA_IMAGE_MAX_WORK_UNITS", 8, 1, 128
                ),
            }

    @staticmethod
    def _work_units(quality: str) -> int:
        return {"fast": 1, "balanced": 2, "quality": 4}.get(quality, 2)

    @staticmethod
    def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
        try:
            value = int(os.environ.get(name, str(default)))
        except ValueError:
            value = default
        return max(minimum, min(maximum, value))

    def _load_state(self) -> dict[str, ImageJob]:
        if self._state_path is None or not self._state_path.is_file():
            return {}
        try:
            raw = json.loads(self._state_path.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                raise ValueError("job state must be a list")
            now = time.time()
            jobs: dict[str, ImageJob] = {}
            for value in raw[:1000]:
                if not isinstance(value, dict):
                    continue
                allowed = {field_name for field_name in ImageJob.__dataclass_fields__}
                job = ImageJob(**{key: item for key, item in value.items() if key in allowed})
                if job.updated_at < now - 86400:
                    continue
                if job.status not in {"completed", "failed"}:
                    job.status = "failed"
                    job.error = "서버 재시작으로 이미지 생성 작업이 중단되었습니다."
                    job.updated_at = now
                jobs[job.id] = job
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            log.exception("could not restore image job state")
            return {}
        self._jobs = jobs
        self._persist_state()
        return jobs

    def _persist_state(self) -> None:
        if self._state_path is None:
            return
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            recent = sorted(
                self._jobs.values(),
                key=lambda job: job.updated_at,
                reverse=True,
            )[:1000]
            temporary = self._state_path.with_name(f".{self._state_path.name}.{os.getpid()}.tmp")
            temporary.write_text(
                json.dumps([asdict(job) for job in recent], ensure_ascii=False),
                encoding="utf-8",
            )
            temporary.chmod(0o600)
            temporary.replace(self._state_path)
        except OSError:
            # 상태 저널 실패가 이미 비싼 생성 결과 자체를 실패시키면 안 된다.
            log.exception("could not persist image job state")

    async def _run(
        self,
        job_id: str,
        prompt: str,
        negative_prompt: str,
        spec: PlanetSpec,
        inhabitant_index: int | None,
    ) -> None:
        job = self._jobs[job_id]
        generated_urls: list[str] = []

        async def mark_generating() -> None:
            async with self._lock:
                job.status = "generating"
                job.candidate_current = 1
                job.updated_at = time.time()
                self._persist_state()

        try:
            config = get_pipeline_config(job.quality)
            bible = build_world_bible(spec)
            plans = build_candidate_plans(
                job.seed or 1,
                quality=config.quality,
                kind=job.kind,
                world_id=bible.world_id,
            )
            locked_prompt = compose_pipeline_prompt(
                prompt,
                bible,
                kind=job.kind,
                inhabitant_index=inhabitant_index,
            )
            dimensions = config.candidate_dimensions_for(job.kind)
            generated = await generate_candidate_batch(
                locked_prompt,
                kind=job.kind,
                seeds=[plan.seed for plan in plans],
                negative_prompt=negative_prompt,
                spec=spec,
                width=dimensions.width,
                height=dimensions.height,
                steps=config.steps,
                on_started=mark_generating,
            )
            generated_urls.extend(url for url, _ in generated)

            winner_index = 0
            winner_url, winner_seed = generated[0]
            winning_verification: ImageVerificationResult | None = None
            if config.verify_candidates:
                await self._set_phase(
                    job,
                    "verifying",
                    candidate_current=config.candidate_count,
                )
                inhabitant = (
                    spec.inhabitants[inhabitant_index]
                    if inhabitant_index is not None and inhabitant_index < len(spec.inhabitants)
                    else None
                )
                verification_results = await asyncio.gather(
                    *[
                        verify_generated_image(
                            spec,
                            job.kind,
                            self._generated_path(url),
                            inhabitant,
                        )
                        for url, _ in generated
                    ],
                    return_exceptions=True,
                )
                verifications: list[ImageVerificationResult | None] = []
                unavailable_notes: list[str] = []
                for index, result in enumerate(verification_results):
                    if isinstance(result, BaseException):
                        log.warning("candidate %d verification failed: %s", index, result)
                        verifications.append(None)
                        unavailable_notes.append(f"후보 {index + 1} 자동 검수를 건너뛰었습니다.")
                    else:
                        verifications.append(result)
                scored = [
                    ScoredCandidate(
                        index=index,
                        image_path=self._generated_path(generated[index][0]),
                        seed=generated[index][1],
                        total_score=result.total_score,
                        passed=result.passed,
                        verification_notes=tuple(result.missing_features + result.problems),
                    )
                    for index, result in enumerate(verifications)
                    if result is not None and result.verified
                ]
                if scored:
                    selected = select_winner(scored)
                    winner_index = selected.index
                    winner_url, winner_seed = generated[winner_index]
                    winning_verification = verifications[winner_index]
                    assert winning_verification is not None
                    job.quality_score = winning_verification.total_score
                    job.verification_notes = self._notes(winning_verification)
                else:
                    job.verification_notes = (
                        unavailable_notes + ["자동 검수를 사용할 수 없어 첫 번째 후보를 선택했습니다."]
                    )[:8]

            final_url, final_seed = winner_url, winner_seed
            if config.refine_winner:
                await self._set_phase(job, "refining", candidate_current=config.candidate_count)
                correction = (
                    winning_verification.refinement_prompt
                    if winning_verification and winning_verification.refinement_prompt
                    else "Increase fine material detail and terrain relief while preserving composition and every locked identity trait."
                )
                refine_prompt = compose_pipeline_prompt(
                    prompt,
                    bible,
                    kind=job.kind,
                    inhabitant_index=inhabitant_index,
                    refinement_prompt=correction,
                )
                final_dimensions = config.final_dimensions_for(job.kind)
                refine_seed = derive_candidate_seed(
                    job.seed or 1,
                    99 + winner_index,
                    world_id=bible.world_id,
                    kind=job.kind,
                )
                try:
                    refined = await generate_candidate_batch(
                        refine_prompt,
                        kind=job.kind,
                        seeds=[refine_seed],
                        negative_prompt=negative_prompt,
                        spec=spec,
                        width=final_dimensions.width,
                        height=final_dimensions.height,
                        steps=config.refinement_steps,
                        init_image_path=self._generated_path(winner_url),
                        image_strength=0.52,
                        use_planet_guide=False,
                    )
                except ImageGenerationError as exc:
                    # 후보 생성까지 성공했으므로 선택적 보정 실패로 전체 작업을 버리지 않는다.
                    log.warning("winner refinement skipped: %s", exc)
                    job.verification_notes.append("세부 보정에 실패해 검수된 원본 후보를 유지했습니다.")
                else:
                    refined_url, refined_seed = refined[0]
                    generated_urls.append(refined_url)
                    accept_refined = True

                    await self._set_phase(job, "verifying")
                    inhabitant = (
                        spec.inhabitants[inhabitant_index]
                        if inhabitant_index is not None and inhabitant_index < len(spec.inhabitants)
                        else None
                    )
                    try:
                        refined_verification = await verify_generated_image(
                            spec,
                            job.kind,
                            self._generated_path(refined_url),
                            inhabitant,
                        )
                    except Exception as exc:
                        log.warning("refined image verification failed: %s", exc)
                        job.verification_notes.append("보정 이미지 자동 검수를 사용할 수 없었습니다.")
                    else:
                        if refined_verification.verified:
                            if (
                                winning_verification is not None
                                and refined_verification.total_score < winning_verification.total_score
                            ):
                                accept_refined = False
                                job.verification_notes.append(
                                    "보정 이미지의 검수 점수가 낮아 더 나은 원본 후보를 유지했습니다."
                                )
                            else:
                                job.quality_score = refined_verification.total_score
                                job.verification_notes = self._notes(refined_verification)
                    if accept_refined:
                        final_url, final_seed = refined_url, refined_seed

            if config.upscale_winner:
                upscaled_url = await self._upscale_if_configured(job, final_url)
                if upscaled_url is not None:
                    generated_urls.append(upscaled_url)
                    final_url = upscaled_url

            self._cleanup_generated(generated_urls, keep=final_url)
        except ImageGenerationError as exc:
            log.warning("image job %s failed: %s", job.id, exc)
            self._cleanup_generated(generated_urls, keep="")
            async with self._lock:
                job.status = "failed"
                job.error = self._public_failure_message(exc)
                job.updated_at = time.time()
                self._persist_state()
            return
        except asyncio.CancelledError:
            self._cleanup_generated(generated_urls, keep="")
            async with self._lock:
                job.status = "failed"
                job.error = job.error or "이미지 생성 작업이 취소되었습니다."
                job.updated_at = time.time()
                self._persist_state()
            raise
        except Exception as exc:  # 작업이 조용히 유실되지 않도록 상태로 전달
            log.exception("unexpected image job failure")
            self._cleanup_generated(generated_urls, keep="")
            async with self._lock:
                job.status = "failed"
                job.error = "이미지 생성 중 내부 오류가 발생했습니다."
                job.updated_at = time.time()
                self._persist_state()
            return

        async with self._lock:
            job.status = "completed"
            job.candidate_current = job.candidate_total
            job.url = final_url
            job.actual_seed = final_seed
            job.updated_at = time.time()
            self._persist_state()

    async def _set_phase(
        self,
        job: ImageJob,
        phase: JobStatus,
        *,
        candidate_current: int | None = None,
    ) -> None:
        async with self._lock:
            job.status = phase
            if candidate_current is not None:
                job.candidate_current = candidate_current
            job.updated_at = time.time()
            self._persist_state()

    @staticmethod
    def _generated_path(url: str) -> Path:
        if not url.startswith("/generated/") or ".." in url:
            raise ImageGenerationError("안전하지 않은 생성 이미지 경로입니다.")
        return GENERATED_DIR / Path(url).name

    @staticmethod
    def _notes(result: ImageVerificationResult) -> list[str]:
        return (result.missing_features + result.problems)[:8]

    @staticmethod
    def _public_failure_message(exc: ImageGenerationError) -> str:
        # mflux stderr에는 로컬 경로·실행 인자 등이 섞일 수 있어 API에 그대로
        # 노출하지 않는다. 시간 초과만 사용자가 조치할 수 있는 범주로 구분한다.
        if "시간" in str(exc) and "초과" in str(exc):
            return "이미지 생성 시간이 초과되었습니다. 빠른 모드로 다시 시도해 주세요."
        return "이미지 생성기가 작업을 완료하지 못했습니다. 잠시 후 다시 시도해 주세요."

    async def _upscale_if_configured(self, job: ImageJob, source_url: str) -> str | None:
        source = self._generated_path(source_url)
        output = GENERATED_DIR / f"{source.stem}-upscaled.png"
        try:
            command = build_upscaler_command(source, output)
        except ValueError as exc:
            # 잘못된 업스케일러 설정으로 이미 성공한 작업을 실패시키지 않는다 — 선택적 단계일 뿐이다.
            log.warning("optional upscaler configuration rejected: %s", exc)
            job.verification_notes.append("선택적 업스케일 설정 오류로 원본을 유지했습니다.")
            return None
        if command is None:
            return None
        await self._set_phase(job, "upscaling")
        process: asyncio.subprocess.Process | None = None
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=image_subprocess_environment(),
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=self._upscale_timeout(),
            )
        except asyncio.CancelledError:
            await asyncio.shield(self._stop_process(process, graceful=True))
            self._unlink_quietly(output)
            raise
        except (OSError, TimeoutError) as exc:
            # 타임아웃 시 wait_for가 communicate만 취소하므로 자식 프로세스를 직접 정리한다.
            await self._stop_process(process, graceful=False)
            self._unlink_quietly(output)
            log.warning("optional upscaler skipped: %s", exc)
            job.verification_notes.append("선택적 업스케일을 완료하지 못해 원본을 유지했습니다.")
            return None
        if process.returncode != 0 or not output.is_file():
            detail = (stderr or stdout).decode("utf-8", errors="replace")[-240:]
            log.warning("optional upscaler failed: %s", detail or process.returncode)
            job.verification_notes.append("선택적 업스케일에 실패해 원본을 유지했습니다.")
            self._unlink_quietly(output)
            return None
        return f"/generated/{output.name}"

    @staticmethod
    def _upscale_timeout() -> int:
        try:
            timeout = int(os.environ.get("TERRA_UPSCALE_TIMEOUT", "900"))
        except ValueError:
            timeout = 900
        return max(30, min(3600, timeout))

    @staticmethod
    async def _stop_process(
        process: asyncio.subprocess.Process | None,
        *,
        graceful: bool,
    ) -> None:
        if process is None or process.returncode is not None:
            return
        try:
            if graceful:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                    return
                except TimeoutError:
                    pass
            process.kill()
        except ProcessLookupError:
            return
        await process.wait()

    @staticmethod
    def _unlink_quietly(path: Path) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    @staticmethod
    def _cleanup_generated(urls: list[str], *, keep: str) -> None:
        for url in set(urls):
            if url == keep:
                continue
            try:
                (GENERATED_DIR / Path(url).name).unlink(missing_ok=True)
            except OSError:
                pass


image_jobs = ImageJobManager(state_path=JOB_STATE_PATH)
