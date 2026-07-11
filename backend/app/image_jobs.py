"""Cloudflare 타임아웃과 분리된 비동기 이미지 생성 작업 관리자."""

from __future__ import annotations

import asyncio
import os
import secrets
import time
from dataclasses import dataclass, field
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
from .images import GENERATED_DIR, ImageGenerationError, generate_candidate_batch, provider_status
from .schema import PlanetSpec
from .world_bible import build_world_bible

JobStatus = Literal[
    "queued",
    "generating",
    "verifying",
    "refining",
    "upscaling",
    "completed",
    "failed",
]


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
    def __init__(self) -> None:
        self._jobs: dict[str, ImageJob] = {}
        self._tasks: set[asyncio.Task[None]] = set()
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
        async with self._lock:
            # 완료된 작업 상태는 24시간만 보관한다. PNG 파일은 별도 보존된다.
            cutoff = now - 86400
            self._jobs = {
                key: value
                for key, value in self._jobs.items()
                if value.updated_at >= cutoff or value.status not in {"completed", "failed"}
            }
            self._jobs[job.id] = job

        task = asyncio.create_task(
            self._run(job.id, prompt, negative_prompt, spec, inhabitant_index)
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job

    async def get(self, job_id: str) -> ImageJob | None:
        async with self._lock:
            return self._jobs.get(job_id)

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
                verifications = await asyncio.gather(
                    *[
                        verify_generated_image(
                            spec,
                            job.kind,
                            self._generated_path(url),
                            inhabitant,
                        )
                        for url, _ in generated
                    ]
                )
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
                    if result.verified
                ]
                if scored:
                    selected = select_winner(scored)
                    winner_index = selected.index
                    winner_url, winner_seed = generated[winner_index]
                    winning_verification = verifications[winner_index]
                    job.quality_score = winning_verification.total_score
                    job.verification_notes = self._notes(winning_verification)
                else:
                    job.verification_notes = ["자동 검수를 사용할 수 없어 첫 번째 후보를 선택했습니다."]

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
                final_url, final_seed = refined[0]
                generated_urls.append(final_url)

                await self._set_phase(job, "verifying")
                inhabitant = (
                    spec.inhabitants[inhabitant_index]
                    if inhabitant_index is not None and inhabitant_index < len(spec.inhabitants)
                    else None
                )
                refined_verification = await verify_generated_image(
                    spec,
                    job.kind,
                    self._generated_path(final_url),
                    inhabitant,
                )
                if refined_verification.verified:
                    job.quality_score = refined_verification.total_score
                    job.verification_notes = self._notes(refined_verification)

            if config.upscale_winner:
                upscaled_url = await self._upscale_if_configured(job, final_url)
                if upscaled_url is not None:
                    generated_urls.append(upscaled_url)
                    final_url = upscaled_url

            self._cleanup_generated(generated_urls, keep=final_url)
        except ImageGenerationError as exc:
            self._cleanup_generated(generated_urls, keep="")
            async with self._lock:
                job.status = "failed"
                job.error = str(exc)
                job.updated_at = time.time()
            return
        except Exception as exc:  # 작업이 조용히 유실되지 않도록 상태로 전달
            self._cleanup_generated(generated_urls, keep="")
            async with self._lock:
                job.status = "failed"
                job.error = f"예상하지 못한 이미지 생성 오류: {exc}"
                job.updated_at = time.time()
            return

        async with self._lock:
            job.status = "completed"
            job.candidate_current = job.candidate_total
            job.url = final_url
            job.actual_seed = final_seed
            job.updated_at = time.time()

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

    @staticmethod
    def _generated_path(url: str) -> Path:
        if not url.startswith("/generated/") or ".." in url:
            raise ImageGenerationError("안전하지 않은 생성 이미지 경로입니다.")
        return GENERATED_DIR / Path(url).name

    @staticmethod
    def _notes(result: ImageVerificationResult) -> list[str]:
        return (result.missing_features + result.problems)[:8]

    async def _upscale_if_configured(self, job: ImageJob, source_url: str) -> str | None:
        source = self._generated_path(source_url)
        output = GENERATED_DIR / f"{source.stem}-upscaled.png"
        command = build_upscaler_command(source, output)
        if command is None:
            return None
        await self._set_phase(job, "upscaling")
        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=int(os.environ.get("TERRA_UPSCALE_TIMEOUT", "900")),
            )
        except (OSError, TimeoutError) as exc:
            job.verification_notes.append(f"선택적 업스케일을 건너뛰었습니다: {exc}")
            return None
        if process.returncode != 0 or not output.is_file():
            detail = (stderr or stdout).decode("utf-8", errors="replace")[-240:]
            job.verification_notes.append(f"선택적 업스케일 실패: {detail or process.returncode}")
            output.unlink(missing_ok=True)
            return None
        return f"/generated/{output.name}"

    @staticmethod
    def _cleanup_generated(urls: list[str], *, keep: str) -> None:
        for url in set(urls):
            if url == keep:
                continue
            try:
                (GENERATED_DIR / Path(url).name).unlink(missing_ok=True)
            except OSError:
                pass


image_jobs = ImageJobManager()
