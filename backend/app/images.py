"""로컬 MLX 이미지 생성기(mflux) 연동과 프롬프트 구성."""

from __future__ import annotations

import asyncio
import colorsys
import os
import secrets
import shutil
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Awaitable, Callable

from .planet_guide import create_planet_guide
from .schema import Inhabitant, PlanetSpec

DEFAULT_COMMAND = "mflux-generate-z-image-turbo"
DEFAULT_MODEL = "filipstrand/Z-Image-Turbo-mflux-4bit"
GENERATED_DIR = Path(__file__).resolve().parents[1] / "generated"

FEATURE_NAMES = {
    "continents": "broad fractured continental plates with irregular coastlines",
    "archipelago": "long chains of alien islands and shallow shelf seas",
    "cratered": "overlapping impact basins, ejecta rays and broken crater rims",
    "canyons": "branching planetary canyons, scarps and exposed strata",
    "dunes": "continent-scale dune seas with visible prevailing-wind patterns",
    "crystalline": "crystalline ridges, glassy mineral fields and prismatic facets",
    "volcanic": "dark volcanic provinces, calderas and fresh lava fractures",
    "artificial": "planet-scale engineered terrain and geometric megastructures",
}

LANDMARK_NAMES = {
    "rock_spires": "eroded rock spires",
    "crystal_fields": "reflective crystal fields",
    "cave_openings": "large natural cave openings and partly subterranean settlements",
    "volcanic_vents": "volcanic vents and calderas",
    "dune_ridges": "layered dune ridges",
    "artificial_structures": "integrated artificial structures",
    "giant_flora": "giant native flora",
    "ice_spires": "towering ice spires",
}


class ImageGenerationError(RuntimeError):
    pass


@dataclass(frozen=True)
class ImageProviderStatus:
    available: bool
    provider: str
    model: str
    command: str
    message: str


def _command_name() -> str:
    """관리자가 지정한 실행 파일명만 허용한다 (셸 구문은 실행하지 않음)."""
    command = os.environ.get("TERRA_IMAGE_COMMAND", DEFAULT_COMMAND).strip()
    return command or DEFAULT_COMMAND


def provider_status() -> ImageProviderStatus:
    command = _command_name()
    executable = shutil.which(command)
    if executable:
        return ImageProviderStatus(
            available=True,
            provider="mflux",
            model=os.environ.get("TERRA_IMAGE_MODEL", DEFAULT_MODEL),
            command=executable,
            message="Apple Silicon 로컬 이미지 생성 준비 완료",
        )
    return ImageProviderStatus(
        available=False,
        provider="mflux",
        model=os.environ.get("TERRA_IMAGE_MODEL", DEFAULT_MODEL),
        command=command,
        message=(
            "mflux가 설치되지 않았습니다. `uv tool install --upgrade mflux` 후 "
            "백엔드를 다시 시작하면 이미지 생성이 활성화됩니다."
        ),
    )


def _color_description(value: str) -> str:
    """hex만 전달하지 않고 모델이 이해하기 쉬운 색 이름을 함께 만든다."""
    raw = value.strip().lstrip("#")
    if len(raw) != 6:
        return value
    try:
        red, green, blue = (int(raw[index:index + 2], 16) / 255 for index in (0, 2, 4))
    except ValueError:
        return value
    hue, saturation, brightness = colorsys.rgb_to_hsv(red, green, blue)
    if saturation < 0.10:
        tone = "white" if brightness > 0.88 else "light gray" if brightness > 0.62 else "charcoal gray" if brightness < 0.30 else "gray"
    else:
        names = ["red", "orange", "yellow", "green", "cyan", "blue", "violet", "magenta"]
        tone = names[int((hue * len(names) + 0.5) % len(names))]
        if brightness > 0.82:
            tone = f"bright {tone}"
        elif brightness < 0.35:
            tone = f"deep {tone}"
        elif saturation < 0.38:
            tone = f"muted {tone}"
    return f"{tone} ({value})"


def _coverage(value: float, noun: str) -> str:
    if value < 0.03:
        return f"almost no {noun}"
    if value < 0.22:
        return f"sparse {noun}"
    if value < 0.48:
        return f"scattered {noun}"
    if value < 0.72:
        return f"widespread {noun}"
    return f"{noun} dominating most of the visible surface"


def build_planet_prompt(spec: PlanetSpec) -> str:
    p = spec.planet
    s = spec.surface
    a = spec.atmosphere
    c = spec.climate
    star_colors = ", ".join(spec.star.colors_hex or [spec.star.color_hex])
    phenomena = ", ".join(c.phenomena) or "subtle physically plausible weather"
    shape = {
        "sphere": "nearly spherical",
        "oblate": f"visibly oblate, flattening {p.oblateness:.2f}",
        "irregular": "irregular and asymmetric",
    }.get(p.shape, p.shape)
    identity = s.visual_prompt.strip() or s.description or "A wholly original world with unfamiliar geology."
    landmarks = ", ".join(LANDMARK_NAMES[item] for item in s.landmarks if item in LANDMARK_NAMES)
    satellites: list[str] = []
    if spec.moons:
        satellites.append(
            f"Visible natural satellites: {', '.join(m.name or 'an unnamed moon' for m in spec.moons)}."
        )
    if spec.rings.present:
        satellites.append(
            f"A clearly visible {_color_description(spec.rings.color_hex)} ring system encircles the globe."
        )

    return "\n".join(
        [
            f"ORIGINAL FICTIONAL EXOPLANET DESIGN — {p.name}",
            "NON-NEGOTIABLE VISUAL IDENTITY:",
            f"- {identity}",
            "- Wholly invented, asymmetrical continental silhouettes and unfamiliar coastlines; every landmass must look newly designed.",
            f"- Dominant terrain: {FEATURE_NAMES.get(s.feature_type, s.feature_type)}, strongly visible at orbital scale.",
            *([f"- Surface landmarks integrated into the terrain: {landmarks}."] if landmarks else []),
            "CAMERA AND COMPOSITION:",
            "- One complete isolated planetary globe centered in frame, the entire circular limb visible against deep black space.",
            "- Three-quarter orbital view showing atmosphere, topography, coastlines and weather simultaneously.",
            "PHYSICAL FORM AND SURFACE:",
            f"- Body shape: {shape}; {_coverage(s.ocean_coverage, 'oceans')}; {_coverage(s.ice_coverage, 'polar ice')}; {_coverage(s.vegetation_coverage, 'native vegetation')}.",
            f"- Surface material: {s.material_type}; terrain prominence {s.feature_scale:.0%}; biome contrast {s.biome_contrast:.0%}.",
            f"- Color geography: deep water {_color_description(s.palette.ocean_deep)}, shallow water {_color_description(s.palette.ocean_shallow)}, shores {_color_description(s.palette.shore)}, lowlands {_color_description(s.palette.lowland)}, highlands {_color_description(s.palette.highland)}, peaks {_color_description(s.palette.peak)}.",
            "ATMOSPHERE AND LIGHT:",
            f"- Atmosphere {a.density:.0%} density with {_color_description(a.color_hex)} limb glow; composition {a.composition or 'unspecified'}.",
            f"- Weather that must be visibly expressed: {phenomena}; average climate {c.avg_temp_c:.0f} Celsius.",
            f"- Illumination from {spec.star.count} star(s), light color {star_colors}; physically coherent shadows and atmospheric scattering.",
            *satellites,
            "IMAGE QUALITY:",
            "- Premium science-fiction concept art with fine geological relief, crisp cloud structure, layered haze, detailed shorelines, mineral variation and high-frequency surface texture.",
            "- Cinematic realism, natural color separation, sharp focus across the planetary disk, rich detail without a map-like graphic appearance.",
        ]
    )


def build_inhabitant_prompt(spec: PlanetSpec, inhabitant: Inhabitant) -> str:
    environment = spec.surface.description or spec.atmosphere.weather_summary
    prompt = inhabitant.portrait_prompt.strip()
    if not prompt:
        prompt = (
            f"Portrait of {inhabitant.name or 'an alien inhabitant'}, {inhabitant.appearance}. "
            f"Physiology: {inhabitant.physiology}."
        )
    traits: list[str] = []
    trait_text = f"{inhabitant.appearance} {inhabitant.physiology} {prompt}".lower()
    if "더듬이" in trait_text or "antenna" in trait_text:
        antenna_color = "pink " if "분홍" in trait_text or "pink" in trait_text else ""
        traits.append(
            f"Exactly two clearly separated slender {antenna_color}antennae emerging from the crown of the head, both fully visible and unmistakable."
        )
    if "반투명" in trait_text or "translucent" in trait_text:
        traits.append("Translucent body material with visible subsurface depth rather than opaque human skin.")
    if "무지개" in trait_text or "iridescent" in trait_text:
        traits.append("Strong but natural iridescent rainbow reflections across the body surface.")
    if "외골격" in trait_text or "exoskeleton" in trait_text:
        traits.append("A continuous articulated exoskeleton structure visibly shaping the torso and limbs.")
    if "튜닉" in trait_text or "tunic" in trait_text:
        traits.append("An elegant flowing tunic that is clearly readable as clothing, with folds and fabric texture.")
    if "샌들" in trait_text or "sandal" in trait_text:
        traits.append("Purpose-built sandals clearly visible on both feet.")

    return "\n".join(
        [
            f"ORIGINAL ALIEN SPECIES PORTRAIT — {inhabitant.name or 'unnamed native'}",
            "SUBJECT LOCK — every listed feature must be clearly visible:",
            f"- Core design: {prompt}",
            f"- Appearance: {inhabitant.appearance or 'a nonhuman native anatomy'}.",
            f"- Physiology: {inhabitant.physiology or 'scientifically plausible nonhuman physiology'}.",
            *[f"- {trait}" for trait in traits],
            "BODY AND POSE:",
            "- One individual, full body from head to feet, standing in a relaxed three-quarter pose; face, hands and both feet unobstructed.",
            f"- Native surface gravity {spec.planet.gravity_g:.2f} g visibly influences proportions and stance.",
            f"- Gravity adaptation: {inhabitant.gravity_adaptation or 'biologically plausible adaptation'}.",
            "ENVIRONMENT:",
            f"- Native habitat visibly surrounding the subject: {environment or 'an original alien planetary environment'}.",
            "- The subject occupies most of the frame while environmental materials remain sharp enough to establish scale and habitat.",
            "IMAGE QUALITY:",
            "- High-end cinematic xenobiology field photograph, anatomically coherent joints, tactile skin and clothing materials, fine microtexture, sharp eyes, controlled depth of field, natural dramatic light, high detail.",
        ]
    )


def build_surface_prompt(spec: PlanetSpec) -> str:
    s = spec.surface
    c = spec.climate
    a = spec.atmosphere
    identity = s.visual_prompt.strip() or s.description or "A wholly original alien landscape."
    landmarks = ", ".join(LANDMARK_NAMES[item] for item in s.landmarks if item in LANDMARK_NAMES)
    return "\n".join(
        [
            f"ALIEN PLANETARY SURFACE EXPEDITION — {spec.planet.name}",
            "NON-NEGOTIABLE TERRAIN IDENTITY:",
            f"- {identity}",
            f"- Dominant geology: {FEATURE_NAMES.get(s.feature_type, s.feature_type)}.",
            f"- Surface material: {s.material_type}; roughness {s.terrain_roughness:.0%}; mountain prominence {s.mountain_height:.0%}.",
            *([f"- Clearly visible terrain landmarks: {landmarks}."] if landmarks else []),
            "TERRAIN SCALE AND COMPOSITION:",
            "- Ground-level wide-angle expedition view with a strong foreground, midground and distant horizon.",
            "- Obvious elevation differences: eroded foreground microrelief, medium-scale ridges or depressions, and large geological formations in the distance.",
            "- The terrain must read as a traversable physical place with natural transitions, material layering, erosion, debris and scale cues.",
            "COLOR AND ENVIRONMENT:",
            f"- Ground colors transition naturally from {_color_description(s.palette.lowland)} through {_color_description(s.palette.midland)} to {_color_description(s.palette.highland)} and {_color_description(s.palette.peak)} on exposed heights.",
            f"- Atmosphere: {_color_description(a.color_hex)} haze at {a.density:.0%} density; visible weather: {', '.join(c.phenomena) or a.weather_summary or 'subtle atmospheric movement'}.",
            f"- Native vegetation coverage {s.vegetation_coverage:.0%}, ice coverage {s.ice_coverage:.0%}, volcanic activity {s.lava_activity:.0%}; show only where physically appropriate.",
            "IMAGE QUALITY:",
            "- Premium cinematic science-fiction environment concept art, photoreal geological materials, crisp rock and soil microtexture, physically coherent lighting, atmospheric depth, sharp focal details and rich but natural color variation.",
        ]
    )


def build_negative_prompt(
    kind: str,
    spec: PlanetSpec,
    inhabitant: Inhabitant | None = None,
) -> str:
    if kind == "planet":
        return (
            "Earth, planet Earth, terrestrial world map, recognizable Earth continents, Africa, Europe, "
            "Asia, North America, South America, NASA Earth photograph, flat map, cropped globe, duplicate "
            "planets, labels, text, logo, watermark, blur, low resolution, smooth featureless surface"
        )
    if kind == "surface":
        return (
            "Earth landscape, familiar terrestrial landmark, flat texture, smooth empty ground, featureless plain, "
            "aerial map, orbital view, floating rocks, geometric primitives, plastic material, miniature diorama, "
            "buildings without story evidence, people, text, logo, watermark, blur, low resolution"
        )
    trait_text = f"{inhabitant.appearance if inhabitant else ''} {inhabitant.portrait_prompt if inhabitant else ''}".lower()
    missing_traits = ""
    if "더듬이" in trait_text or "antenna" in trait_text:
        missing_traits = ", missing antennae, one antenna, extra antennae, antennae replaced by ears"
    return (
        "ordinary human, generic grey alien, human skin, costume mask, malformed anatomy, extra limbs, "
        "missing limbs, fused fingers, cropped head, cropped feet, multiple people, blurry face, plastic toy, "
        f"low detail, text, logo, watermark{missing_traits}"
    )


_generation_lock = asyncio.Lock()


async def generate_image(
    prompt: str,
    *,
    kind: str,
    negative_prompt: str = "",
    spec: PlanetSpec | None = None,
    seed: int | None = None,
    on_started: Callable[[], Awaitable[None]] | None = None,
) -> tuple[str, int]:
    actual_seed = seed if seed is not None else secrets.randbelow(2**31 - 1)
    results = await generate_candidate_batch(
        prompt,
        kind=kind,
        negative_prompt=negative_prompt,
        spec=spec,
        seeds=[actual_seed],
        on_started=on_started,
    )
    return results[0]


async def generate_candidate_batch(
    prompt: str,
    *,
    kind: str,
    seeds: list[int],
    negative_prompt: str = "",
    spec: PlanetSpec | None = None,
    width: int | None = None,
    height: int | None = None,
    steps: int | None = None,
    init_image_path: Path | None = None,
    image_strength: float | None = None,
    use_planet_guide: bool = True,
    on_started: Callable[[], Awaitable[None]] | None = None,
) -> list[tuple[str, int]]:
    """한 번 모델을 로드해 여러 seed 후보를 생성한다."""
    status = provider_status()
    if not status.available:
        raise ImageGenerationError(status.message)
    if not seeds:
        raise ImageGenerationError("이미지 후보 seed가 비어 있습니다.")
    if len(seeds) > 4:
        raise ImageGenerationError("한 작업에서 생성할 수 있는 후보는 최대 4장입니다.")

    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    default_width, default_height = (896, 1152) if kind == "inhabitant" else (1344, 896)
    actual_width = width or int(os.environ.get(f"TERRA_IMAGE_{kind.upper()}_WIDTH", str(default_width)))
    actual_height = height or int(os.environ.get(f"TERRA_IMAGE_{kind.upper()}_HEIGHT", str(default_height)))
    actual_width = max(512, min(2048, actual_width - actual_width % 32))
    actual_height = max(512, min(2048, actual_height - actual_height % 32))
    actual_steps = steps or int(os.environ.get("TERRA_IMAGE_STEPS", "9"))
    quantize = os.environ.get("TERRA_IMAGE_QUANTIZE", "0").strip().lower()
    timeout = int(os.environ.get("TERRA_IMAGE_TIMEOUT", "1200"))
    token = secrets.token_hex(8)
    output_template = GENERATED_DIR / f"{kind}-{token}-{{seed}}.png"
    outputs = [GENERATED_DIR / f"{kind}-{token}-{seed}.png" for seed in seeds]
    guide_path: Path | None = None

    args = [
        status.command,
        "--model",
        status.model,
        "--prompt",
        prompt,
        "--width",
        str(actual_width),
        "--height",
        str(actual_height),
        "--seed",
        *[str(seed) for seed in seeds],
        "--steps",
        str(actual_steps),
        "--output",
        str(output_template),
    ]
    if negative_prompt:
        args.extend(["--negative-prompt", negative_prompt])
    conditioning_path = init_image_path
    if conditioning_path is None and kind == "planet" and spec is not None and use_planet_guide:
        guide_path = GENERATED_DIR / ".guides" / f"planet-{secrets.token_hex(8)}.ppm"
        create_planet_guide(spec, guide_path, seed=seeds[0])
        conditioning_path = guide_path
        guide_strength = float(os.environ.get("TERRA_PLANET_GUIDE_STRENGTH", "0.52"))
        guide_strength = max(0.15, min(0.75, guide_strength))
        image_strength = guide_strength
    if conditioning_path is not None:
        strength = max(0.05, min(0.9, image_strength if image_strength is not None else 0.62))
        args.extend(["--image-path", str(conditioning_path), "--image-strength", str(strength)])
    guidance = os.environ.get("TERRA_IMAGE_GUIDANCE", "").strip()
    if guidance:
        args.extend(["--guidance", guidance])
    if quantize not in {"", "0", "none", "false"}:
        args.extend(["--quantize", quantize])

    # MLX 모델을 동시에 여러 개 올리면 통합 메모리가 급증하므로 직렬화한다.
    async with _generation_lock:
        if on_started is not None:
            await on_started()
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except TimeoutError as exc:
            if "process" in locals():
                process.kill()
                await process.wait()
            raise ImageGenerationError(f"이미지 생성 시간이 {timeout}초를 초과했습니다.") from exc
        except asyncio.CancelledError:
            if "process" in locals() and process.returncode is None:
                process.terminate()
                await process.wait()
            raise
        except OSError as exc:
            raise ImageGenerationError(f"이미지 생성기를 시작하지 못했습니다: {exc}") from exc
        finally:
            if guide_path is not None:
                guide_path.unlink(missing_ok=True)

    if process.returncode != 0:
        detail = (stderr or stdout).decode("utf-8", errors="replace")[-1200:]
        raise ImageGenerationError(f"mflux 생성 실패: {detail or f'exit {process.returncode}'}")
    missing = [output.name for output in outputs if not output.is_file()]
    if missing:
        raise ImageGenerationError(f"mflux가 완료되었지만 출력 이미지를 찾을 수 없습니다: {', '.join(missing)}")
    return [(f"/generated/{output.name}", seed) for output, seed in zip(outputs, seeds)]
