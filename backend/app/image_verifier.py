"""Gemini vision verifier for generated Terra artwork.

The verifier is intentionally isolated from the generation pipeline: callers always
receive a typed result, including when credentials, the network, or the model fail.
That lets image generation complete even when optional quality verification is
temporarily unavailable.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
from pathlib import Path
from typing import Any, Literal

import httpx
from pydantic import BaseModel, Field

from . import gemini
from .schema import Inhabitant, PlanetSpec

log = logging.getLogger("terra.image_verifier")

ImageKind = Literal["planet", "surface", "inhabitant"]

# Base64 adds roughly one third, so this keeps the JSON request comfortably below
# common proxy/body limits while still accommodating the current generated assets.
MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_REFINEMENT_PROMPT_CHARS = 700
PASS_SCORE = 72
_RETRY_STATUSES = {429, 500, 503}


class CriterionScores(BaseModel):
    """Visually observable quality dimensions, each scored from 0 to 100."""

    specification_fidelity: int = Field(ge=0, le=100)
    distinctive_features: int = Field(ge=0, le=100)
    environment_fidelity: int = Field(ge=0, le=100)
    material_detail: int = Field(ge=0, le=100)
    technical_quality: int = Field(ge=0, le=100)


class ImageVerificationResult(BaseModel):
    """Stable result contract returned to the image pipeline."""

    total_score: int = Field(ge=0, le=100)
    criterion_scores: CriterionScores
    passed: bool
    missing_features: list[str] = Field(default_factory=list)
    problems: list[str] = Field(default_factory=list)
    refinement_prompt: str = ""
    verified: bool = True
    error: str | None = None


VERIFICATION_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "total_score": {"type": "integer", "minimum": 0, "maximum": 100},
        "criterion_scores": {
            "type": "object",
            "properties": {
                "specification_fidelity": {"type": "integer", "minimum": 0, "maximum": 100},
                "distinctive_features": {"type": "integer", "minimum": 0, "maximum": 100},
                "environment_fidelity": {"type": "integer", "minimum": 0, "maximum": 100},
                "material_detail": {"type": "integer", "minimum": 0, "maximum": 100},
                "technical_quality": {"type": "integer", "minimum": 0, "maximum": 100},
            },
            "required": [
                "specification_fidelity",
                "distinctive_features",
                "environment_fidelity",
                "material_detail",
                "technical_quality",
            ],
        },
        "passed": {"type": "boolean"},
        "missing_features": {"type": "array", "items": {"type": "string"}},
        "problems": {"type": "array", "items": {"type": "string"}},
        "refinement_prompt": {
            "type": "string",
            "description": "Concise English image-to-image correction instructions only.",
        },
    },
    "required": [
        "total_score",
        "criterion_scores",
        "passed",
        "missing_features",
        "problems",
        "refinement_prompt",
    ],
}


_WEIGHTS = {
    "specification_fidelity": 0.35,
    "distinctive_features": 0.25,
    "environment_fidelity": 0.15,
    "material_detail": 0.15,
    "technical_quality": 0.10,
}

_MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


def verification_failure(reason: str) -> ImageVerificationResult:
    """Return an explicit, non-raising fallback for an unavailable verifier."""

    safe_reason = _clean_text(reason, 300) or "unknown verifier failure"
    return ImageVerificationResult(
        total_score=0,
        criterion_scores=CriterionScores(
            specification_fidelity=0,
            distinctive_features=0,
            environment_fidelity=0,
            material_detail=0,
            technical_quality=0,
        ),
        passed=False,
        problems=["자동 이미지 검수를 완료하지 못했습니다."],
        refinement_prompt="",
        verified=False,
        error=safe_reason,
    )


async def verify_image(
    spec: PlanetSpec,
    kind: ImageKind,
    image_path: str | Path,
    inhabitant: Inhabitant | None = None,
    *,
    pass_score: int = PASS_SCORE,
) -> ImageVerificationResult:
    """Score a generated local image against the structured world specification.

    No exception escapes this boundary. A failed verification is distinguishable
    from a genuinely low-scoring image through ``verified`` and ``error``.
    """

    try:
        if kind not in {"planet", "surface", "inhabitant"}:
            raise ValueError(f"unsupported image kind: {kind}")
        if not 0 <= pass_score <= 100:
            raise ValueError("pass_score must be between 0 and 100")

        mime_type, encoded_image = await _read_bounded_image(Path(image_path))
        prompt = _build_verification_prompt(spec, kind, inhabitant)
        raw = await _generate_verification(prompt, mime_type, encoded_image)
        return _normalize_result(raw, pass_score)
    except Exception as exc:  # verifier must never take down the generation job
        log.warning("Image verification unavailable: %s", exc)
        return verification_failure(str(exc))


# Descriptive alias for integration code.
verify_generated_image = verify_image


async def _read_bounded_image(path: Path) -> tuple[str, str]:
    suffix = path.suffix.lower()
    mime_type = _MIME_BY_SUFFIX.get(suffix)
    if mime_type is None:
        raise ValueError(f"unsupported image format: {suffix or '(none)'}")

    def read() -> bytes:
        size = path.stat().st_size
        if size <= 0:
            raise ValueError("image is empty")
        if size > MAX_IMAGE_BYTES:
            raise ValueError(f"image exceeds {MAX_IMAGE_BYTES} byte verifier limit")
        data = path.read_bytes()
        if len(data) != size or len(data) > MAX_IMAGE_BYTES:
            raise ValueError("image changed or exceeded the verifier limit while reading")
        return data

    image_bytes = await asyncio.to_thread(read)
    return mime_type, base64.b64encode(image_bytes).decode("ascii")


def _build_verification_prompt(
    spec: PlanetSpec,
    kind: ImageKind,
    inhabitant: Inhabitant | None,
) -> str:
    reference: dict[str, Any] = {
        "image_kind": kind,
        "planet": spec.planet.model_dump(mode="json"),
        "star": spec.star.model_dump(mode="json"),
        "atmosphere": spec.atmosphere.model_dump(mode="json"),
        "climate": spec.climate.model_dump(mode="json"),
        "surface": spec.surface.model_dump(mode="json"),
        "clouds": spec.clouds.model_dump(mode="json"),
        "rings": spec.rings.model_dump(mode="json"),
        "moons": [moon.model_dump(mode="json") for moon in spec.moons],
    }
    if kind == "inhabitant":
        reference["inhabitant"] = (
            inhabitant.model_dump(mode="json") if inhabitant is not None else None
        )

    kind_focus = {
        "planet": (
            "Evaluate orbital silhouette, non-Earth geography, atmosphere, clouds, rings/moons, "
            "large-scale terrain, weather, palette, and clearly visible surface relief."
        ),
        "surface": (
            "Evaluate foreground/midground/horizon depth, landform relief, geology, material texture, "
            "weather particles, landmarks, atmosphere, and palette."
        ),
        "inhabitant": (
            "Evaluate exact visible anatomy and counted traits, proportions, surface/skin material, "
            "gravity adaptation, clothing/culture, environmental context, and anatomical integrity."
        ),
    }[kind]

    return (
        "You are a strict visual QA inspector for a fictional-world image pipeline. "
        "Compare only visually observable properties of the attached image with the JSON reference. "
        "Do not assume that prompt text alone proves a feature is present. "
        "Penalize generic Earth-like results, smooth featureless terrain, wrong counts/colors/materials, "
        "missing signature landmarks or weather, and image artifacts. "
        f"{kind_focus} "
        "Score every criterion independently from 0 to 100. List short concrete missing features and "
        "problems. The refinement_prompt must be one concise English instruction (maximum 90 words) "
        "that preserves successful composition while fixing the most important visible failures. "
        "Return only the requested structured JSON.\nREFERENCE_JSON:\n"
        + json.dumps(reference, ensure_ascii=False, separators=(",", ":"))
    )


async def _generate_verification(prompt: str, mime_type: str, encoded_image: str) -> dict[str, Any]:
    keys = gemini._key_order()
    model = os.environ.get("GEMINI_VISION_MODEL") or gemini.MODEL
    body: dict[str, Any] = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": prompt},
                    {"inlineData": {"mimeType": mime_type, "data": encoded_image}},
                ],
            }
        ],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 2048,
            "responseMimeType": "application/json",
            "responseSchema": VERIFICATION_RESPONSE_SCHEMA,
        },
    }

    last_error = ""
    async with httpx.AsyncClient(timeout=120) as client:
        for attempt in range(len(keys) * 2):
            key = keys[attempt % len(keys)]
            try:
                response = await client.post(
                    f"{gemini.BASE}/models/{model}:generateContent",
                    headers={"x-goog-api-key": key},
                    json=body,
                )
            except httpx.HTTPError as exc:
                last_error = f"network: {exc}"
                continue

            if response.status_code in _RETRY_STATUSES:
                last_error = f"HTTP {response.status_code}: {response.text[:200]}"
                if attempt >= len(keys) - 1:
                    await asyncio.sleep(2)
                continue
            if response.status_code != 200:
                raise gemini.GeminiError(
                    f"Gemini verifier HTTP {response.status_code}: {response.text[:300]}"
                )

            try:
                payload = response.json()
                text = "".join(
                    part.get("text", "")
                    for part in payload["candidates"][0]["content"]["parts"]
                )
                return _parse_json_object(text)
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                raise gemini.GeminiError(f"Gemini verifier response parsing failed: {exc}") from exc

    raise gemini.GeminiError(f"all Gemini verifier attempts failed: {last_error}")


def _parse_json_object(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        first_newline = cleaned.find("\n")
        cleaned = cleaned[first_newline + 1 :] if first_newline >= 0 else cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()

    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("response contains no JSON object")
        value = json.loads(cleaned[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("response JSON is not an object")
    return value


def _normalize_result(raw: dict[str, Any], pass_score: int) -> ImageVerificationResult:
    raw_scores = raw.get("criterion_scores")
    if not isinstance(raw_scores, dict):
        raise ValueError("criterion_scores is missing")

    score_values = {
        name: _bounded_score(raw_scores.get(name))
        for name in _WEIGHTS
    }
    criteria = CriterionScores(**score_values)
    total = round(sum(score_values[name] * weight for name, weight in _WEIGHTS.items()))
    missing = _clean_list(raw.get("missing_features"))
    problems = _clean_list(raw.get("problems"))
    refinement = _clean_text(raw.get("refinement_prompt"), MAX_REFINEMENT_PROMPT_CHARS)
    if not refinement and (missing or problems):
        issues = "; ".join((missing + problems)[:3])
        refinement = f"Preserve the composition and correct these visible issues: {issues}."

    # A critical feature reported missing is a failure even if aesthetic scores inflate
    # the weighted total. The model-provided total/pass fields are deliberately ignored.
    passed = total >= pass_score and not missing
    return ImageVerificationResult(
        total_score=total,
        criterion_scores=criteria,
        passed=passed,
        missing_features=missing,
        problems=problems,
        refinement_prompt=refinement,
    )


def _bounded_score(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("criterion score cannot be boolean")
    try:
        numeric = round(float(value))
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"invalid criterion score: {value!r}") from exc
    return min(100, max(0, numeric))


def _clean_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value[:10]:
        cleaned = _clean_text(item, 240)
        if cleaned and cleaned not in result:
            result.append(cleaned)
    return result


def _clean_text(value: Any, max_length: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:max_length].strip()
