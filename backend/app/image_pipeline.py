"""Pure configuration and selection primitives for the image quality pipeline.

The integration layer owns actual image generation, verification, refinement,
and subprocess execution.  Keeping those effects out of this module makes mode
semantics deterministic and straightforward to test.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Iterable

from .world_bible import ImageKind, WorldVisualBible


class QualityMode(StrEnum):
    FAST = "fast"
    BALANCED = "balanced"
    QUALITY = "quality"


@dataclass(frozen=True, slots=True)
class ImageDimensions:
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class PipelineConfig:
    quality: QualityMode
    candidate_count: int
    preview_scale: float
    steps: int
    refinement_steps: int
    verify_candidates: bool
    refine_winner: bool
    upscale_winner: bool
    planet_dimensions: ImageDimensions
    surface_dimensions: ImageDimensions
    inhabitant_dimensions: ImageDimensions

    def final_dimensions_for(self, kind: ImageKind) -> ImageDimensions:
        """Return the intended final dimensions for the selected image kind."""
        if kind == "planet":
            return self.planet_dimensions
        if kind == "surface":
            return self.surface_dimensions
        if kind == "inhabitant":
            return self.inhabitant_dimensions
        raise ValueError(f"unsupported image kind: {kind}")

    def dimensions_for(self, kind: ImageKind) -> ImageDimensions:
        """Backward-friendly alias for :meth:`final_dimensions_for`."""
        return self.final_dimensions_for(kind)

    def candidate_dimensions_for(self, kind: ImageKind) -> ImageDimensions:
        """Return model-safe preview dimensions, rounded down to multiples of 32."""
        final = self.final_dimensions_for(kind)
        return ImageDimensions(
            max(512, int(final.width * self.preview_scale) // 32 * 32),
            max(512, int(final.height * self.preview_scale) // 32 * 32),
        )


_CONFIGS = {
    QualityMode.FAST: PipelineConfig(
        quality=QualityMode.FAST,
        candidate_count=1,
        preview_scale=1.0,
        steps=9,
        refinement_steps=0,
        verify_candidates=False,
        refine_winner=False,
        upscale_winner=False,
        planet_dimensions=ImageDimensions(1344, 896),
        surface_dimensions=ImageDimensions(1344, 896),
        inhabitant_dimensions=ImageDimensions(896, 1152),
    ),
    QualityMode.BALANCED: PipelineConfig(
        quality=QualityMode.BALANCED,
        candidate_count=2,
        preview_scale=1.0,
        # Z-Image Turbo는 9-step distilled 모델이다. 후보 수로 품질을 높이고
        # 권장 step을 넘겨 디테일을 오히려 손상시키지 않는다.
        steps=9,
        refinement_steps=0,
        verify_candidates=True,
        refine_winner=False,
        upscale_winner=False,
        planet_dimensions=ImageDimensions(1344, 896),
        surface_dimensions=ImageDimensions(1344, 896),
        inhabitant_dimensions=ImageDimensions(896, 1152),
    ),
    QualityMode.QUALITY: PipelineConfig(
        quality=QualityMode.QUALITY,
        candidate_count=3,
        preview_scale=0.75,
        steps=9,
        refinement_steps=9,
        verify_candidates=True,
        refine_winner=True,
        upscale_winner=True,
        planet_dimensions=ImageDimensions(1536, 1024),
        surface_dimensions=ImageDimensions(1536, 1024),
        inhabitant_dimensions=ImageDimensions(1024, 1344),
    ),
}


def get_pipeline_config(quality: QualityMode | str) -> PipelineConfig:
    """Resolve a public quality value, rejecting silent unknown-mode fallback."""
    try:
        mode = quality if isinstance(quality, QualityMode) else QualityMode(quality)
    except ValueError as exc:
        raise ValueError("quality must be one of: fast, balanced, quality") from exc
    return _CONFIGS[mode]


def derive_candidate_seed(
    base_seed: int,
    candidate_index: int,
    *,
    world_id: str = "",
    kind: ImageKind = "planet",
) -> int:
    """Derive a stable positive 31-bit seed for one world/kind/candidate."""
    if candidate_index < 0:
        raise ValueError("candidate_index must be non-negative")
    if kind not in {"planet", "surface", "inhabitant"}:
        raise ValueError(f"unsupported image kind: {kind}")
    material = f"terra-pipeline-v1\0{base_seed}\0{world_id}\0{kind}\0{candidate_index}"
    digest = hashlib.blake2s(material.encode("utf-8"), digest_size=8).digest()
    # MLX accepts a signed-int-sized seed; reserve zero to avoid tool-specific
    # interpretations of zero as an unspecified/random seed.
    return int.from_bytes(digest, "big") % (2**31 - 2) + 1


@dataclass(frozen=True, slots=True)
class CandidatePlan:
    index: int
    seed: int
    width: int
    height: int
    steps: int


def build_candidate_plans(
    base_seed: int,
    *,
    quality: QualityMode | str,
    kind: ImageKind,
    world_id: str = "",
) -> tuple[CandidatePlan, ...]:
    """Describe every generation call without running the image provider."""
    config = get_pipeline_config(quality)
    dimensions = config.candidate_dimensions_for(kind)
    return tuple(
        CandidatePlan(
            index=index,
            seed=derive_candidate_seed(base_seed, index, world_id=world_id, kind=kind),
            width=dimensions.width,
            height=dimensions.height,
            steps=config.steps,
        )
        for index in range(config.candidate_count)
    )


def compose_pipeline_prompt(
    base_prompt: str,
    bible: WorldVisualBible,
    *,
    kind: ImageKind,
    inhabitant_index: int | None = None,
    refinement_prompt: str | None = None,
) -> str:
    """Compose generation/refinement prompts with the bible in highest priority."""
    if not base_prompt.strip():
        raise ValueError("base_prompt must not be empty")
    sections = [
        bible.identity_prompt(kind, inhabitant_index=inhabitant_index),
        "SHOT-SPECIFIC BRIEF (obey without changing the locked identity):\n" + base_prompt.strip(),
    ]
    if refinement_prompt and refinement_prompt.strip():
        sections.append(
            "VERIFIER-GUIDED CORRECTIONS (fix only these defects; preserve composition and world identity):\n"
            + refinement_prompt.strip()
        )
    return "\n\n".join(sections)


@dataclass(frozen=True, slots=True)
class ScoredCandidate:
    index: int
    image_path: Path
    seed: int
    total_score: float
    passed: bool = False
    verification_notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.index < 0:
            raise ValueError("candidate index must be non-negative")
        if not 0 <= self.total_score <= 100:
            raise ValueError("candidate total_score must be between 0 and 100")


def select_winner(candidates: Iterable[ScoredCandidate]) -> ScoredCandidate:
    """Select by verifier score with deterministic pass/index tie breaking."""
    values = tuple(candidates)
    if not values:
        raise ValueError("at least one scored candidate is required")
    if len({candidate.index for candidate in values}) != len(values):
        raise ValueError("candidate indexes must be unique")
    return max(values, key=lambda candidate: (candidate.total_score, candidate.passed, -candidate.index))


_EXECUTABLE = re.compile(r"(?:[A-Za-z0-9._+-]+|(?:/[A-Za-z0-9._+-]+)+)")
_MODEL_TOKEN = re.compile(r"[A-Za-z0-9._+-]+")


def build_upscaler_command(
    input_path: str | Path,
    output_path: str | Path,
    *,
    executable: str | None = None,
    scale: int = 2,
    model: str | None = None,
) -> tuple[str, ...] | None:
    """Build a shell-free Real-ESRGAN-style argv tuple.

    Missing configuration means that optional upscaling is disabled.  The
    executable is intentionally one token: shell snippets, flags embedded in the
    command, and environment-variable expansion are rejected.  The caller may
    pass the returned tuple directly to ``asyncio.create_subprocess_exec``.
    """
    command = executable if executable is not None else os.environ.get("TERRA_UPSCALER_COMMAND", "")
    command = command.strip()
    if not command:
        return None
    if not _EXECUTABLE.fullmatch(command) or ".." in Path(command).parts:
        raise ValueError("upscaler executable must be a command name or absolute path without shell syntax")
    if scale not in {2, 3, 4}:
        raise ValueError("upscaler scale must be 2, 3, or 4")

    raw_source = os.fspath(input_path)
    raw_destination = os.fspath(output_path)
    if not raw_source or not raw_destination or "\x00" in raw_source or "\x00" in raw_destination:
        raise ValueError("upscaler paths must be non-empty and contain no NUL bytes")
    source = str(Path(raw_source))
    destination = str(Path(raw_destination))
    if Path(source) == Path(destination):
        raise ValueError("upscaler input and output paths must differ")

    args = [command, "-i", source, "-o", destination, "-s", str(scale)]
    selected_model = model if model is not None else os.environ.get("TERRA_UPSCALER_MODEL", "")
    selected_model = selected_model.strip()
    if selected_model:
        if not _MODEL_TOKEN.fullmatch(selected_model) or selected_model.startswith("-"):
            raise ValueError("upscaler model must be a safe model token")
        args.extend(("-n", selected_model))
    return tuple(args)
