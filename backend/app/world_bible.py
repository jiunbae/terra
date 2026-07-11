"""Deterministic visual identity shared by every image of a planet.

This module deliberately has no model or I/O dependencies.  A bible can be
rebuilt from a saved :class:`PlanetSpec`, serialized, and injected into orbital,
surface, and inhabitant prompts without changing the identity between shots.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Literal

from .schema import Inhabitant, PlanetSpec

ImageKind = Literal["planet", "surface", "inhabitant"]

_FEATURES = {
    "continents": "asymmetric fractured continents and unfamiliar coastlines",
    "archipelago": "elongated alien island chains and shallow shelf seas",
    "cratered": "overlapping impact basins, ejecta rays, and broken crater rims",
    "canyons": "branching canyons, scarps, and exposed strata",
    "dunes": "wind-aligned dune seas and migrating ridges",
    "crystalline": "prismatic mineral fields and crystalline ridgelines",
    "volcanic": "calderas, basalt provinces, and fresh lava fractures",
    "artificial": "terrain-integrated engineered geometry and megastructures",
}

_LANDMARKS = {
    "rock_spires": "eroded rock spires",
    "crystal_fields": "reflective crystal fields",
    "cave_openings": "large natural cave mouths",
    "volcanic_vents": "volcanic vents and calderas",
    "dune_ridges": "layered dune ridges",
    "artificial_structures": "terrain-integrated artificial structures",
    "giant_flora": "giant native flora",
    "ice_spires": "towering ice spires",
}


def _clean(value: object, *, fallback: str = "unspecified", limit: int = 800) -> str:
    """Normalize model-authored prose while preserving its meaning."""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit] if text else fallback


def _hex(value: str, fallback: str) -> str:
    value = value.strip().lower()
    return value if re.fullmatch(r"#[0-9a-f]{6}", value) else fallback


@dataclass(frozen=True, slots=True)
class VisualPalette:
    ocean_deep: str
    ocean_shallow: str
    shore: str
    lowland: str
    midland: str
    highland: str
    peak: str
    atmosphere: str
    clouds: str
    ring: str
    star_lights: tuple[str, ...]

    def prompt_line(self) -> str:
        return (
            f"deep ocean {self.ocean_deep}; shallow ocean {self.ocean_shallow}; "
            f"shore {self.shore}; lowland {self.lowland}; midland {self.midland}; "
            f"highland {self.highland}; peaks {self.peak}; atmosphere {self.atmosphere}; "
            f"clouds {self.clouds}; stellar light {', '.join(self.star_lights)}"
        )


@dataclass(frozen=True, slots=True)
class SpeciesVisualIdentity:
    index: int
    name: str
    category: str
    silhouette: str
    physiology: str
    gravity_adaptation: str
    clothing_and_culture: str
    portrait_lock: str

    def prompt_block(self) -> str:
        return "\n".join(
            (
                f"- Species: {self.name} ({self.category}).",
                f"- Silhouette and visible anatomy: {self.silhouette}.",
                f"- Physiology/material cues: {self.physiology}.",
                f"- Gravity adaptation: {self.gravity_adaptation}.",
                f"- Clothing/cultural design language: {self.clothing_and_culture}.",
                f"- Portrait lock: {self.portrait_lock}.",
            )
        )


@dataclass(frozen=True, slots=True)
class WorldVisualBible:
    """A compact, immutable visual contract for one ``PlanetSpec``."""

    version: int
    world_id: str
    planet_name: str
    body_shape: str
    terrain_identity: str
    material_identity: str
    landmark_identity: tuple[str, ...]
    authored_identity: str
    palette: VisualPalette
    atmosphere_identity: str
    weather_identity: tuple[str, ...]
    illumination_identity: str
    satellite_identity: str
    species: tuple[SpeciesVisualIdentity, ...]
    forbidden_drift: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-compatible representation suitable for persistence."""
        return asdict(self)

    def identity_prompt(
        self,
        kind: ImageKind,
        *,
        inhabitant_index: int | None = None,
    ) -> str:
        """Render the relevant locked identity as a model-friendly block."""
        if kind not in {"planet", "surface", "inhabitant"}:
            raise ValueError(f"unsupported image kind: {kind}")
        lines = [
            f"LOCKED WORLD VISUAL BIBLE v{self.version} — {self.planet_name} [{self.world_id}]",
            "These facts are immutable across orbital, surface, and species images:",
            f"- Planetary silhouette: {self.body_shape}.",
            f"- Signature terrain: {self.terrain_identity}.",
            f"- Surface material language: {self.material_identity}.",
            f"- Authored visual identity: {self.authored_identity}.",
            f"- Exact role-based palette: {self.palette.prompt_line()}.",
            f"- Atmosphere: {self.atmosphere_identity}.",
            f"- Weather: {', '.join(self.weather_identity)}.",
            f"- Illumination: {self.illumination_identity}.",
            f"- Satellites/rings: {self.satellite_identity}.",
        ]
        if self.landmark_identity:
            lines.append(f"- Recurring landmarks: {', '.join(self.landmark_identity)}.")

        if kind == "planet":
            lines.append(
                "ORBITAL CONTINUITY: make the signature terrain, palette regions, atmosphere, "
                "weather, ring/moon state, and body silhouette readable on the complete globe."
            )
        elif kind == "surface":
            lines.append(
                "SURFACE CONTINUITY: this is the same world at ground level; preserve the exact "
                "materials, palette by elevation, weather, stellar light, and recurring landmarks."
            )
        else:
            if inhabitant_index is None:
                raise ValueError("inhabitant_index is required for an inhabitant prompt")
            if inhabitant_index < 0:
                raise ValueError("inhabitant_index must be non-negative")
            try:
                species = self.species[inhabitant_index]
            except IndexError as exc:
                raise ValueError("inhabitant_index is outside the world bible") from exc
            lines.extend(
                (
                    "INHABITANT CONTINUITY: the subject must visibly belong to this habitat and "
                    "be lit by the same stars through the same atmosphere.",
                    species.prompt_block(),
                )
            )

        lines.append(f"FORBID IDENTITY DRIFT: {', '.join(self.forbidden_drift)}.")
        return "\n".join(lines)


def _species_identity(index: int, inhabitant: Inhabitant) -> SpeciesVisualIdentity:
    return SpeciesVisualIdentity(
        index=index,
        name=_clean(inhabitant.name, fallback=f"native species {index + 1}", limit=120),
        category=_clean(inhabitant.category, fallback="alien native", limit=120),
        silhouette=_clean(inhabitant.appearance, fallback="nonhuman native anatomy"),
        physiology=_clean(inhabitant.physiology, fallback="biologically coherent alien physiology"),
        gravity_adaptation=_clean(
            inhabitant.gravity_adaptation,
            fallback="proportions and stance adapted to local gravity",
        ),
        clothing_and_culture=_clean(
            inhabitant.culture,
            fallback="materials and objects derived from the native environment",
        ),
        portrait_lock=_clean(
            inhabitant.portrait_prompt,
            fallback="preserve every stated anatomical and material trait",
        ),
    )


def _fingerprint(spec: PlanetSpec) -> str:
    # Exclude prose-only inference records: the visual contract is derived from the
    # actual world specification and remains stable if reasoning notes are edited.
    payload = spec.model_dump(mode="json", exclude={"inferences"})
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def build_world_bible(spec: PlanetSpec) -> WorldVisualBible:
    """Create the same visual bible for the same validated planet specification."""
    surface = spec.surface
    palette = surface.palette
    star_colors = tuple(
        _hex(color, _hex(spec.star.color_hex, "#fff4e0"))
        for color in (spec.star.colors_hex or [spec.star.color_hex])[:3]
    ) or ("#fff4e0",)
    weather = tuple(
        cleaned
        for item in spec.climate.phenomena
        if (cleaned := _clean(item, fallback="", limit=160))
    )
    if not weather:
        weather = (_clean(spec.atmosphere.weather_summary, fallback="subtle atmospheric movement"),)

    landmarks = tuple(
        _LANDMARKS[item]
        for item in surface.landmarks
        if item in _LANDMARKS
    )
    body_shape = {
        "sphere": "nearly spherical globe",
        "oblate": f"visibly oblate globe with flattening {spec.planet.oblateness:.2f}",
        "irregular": "irregular asymmetric planetary body",
    }.get(spec.planet.shape, _clean(spec.planet.shape))
    ring_state = (
        f"a {_hex(spec.rings.color_hex, '#c9b797')} ring system with inner/outer ratios "
        f"{spec.rings.inner_ratio:.2f}/{spec.rings.outer_ratio:.2f}"
        if spec.rings.present
        else "no planetary rings"
    )
    moon_state = (
        ", ".join(_clean(moon.name, fallback=f"moon {index + 1}", limit=100) for index, moon in enumerate(spec.moons))
        if spec.moons
        else "no visible moons"
    )

    return WorldVisualBible(
        version=1,
        world_id=_fingerprint(spec),
        planet_name=_clean(spec.planet.name, fallback="unnamed planet", limit=160),
        body_shape=body_shape,
        terrain_identity=_FEATURES.get(surface.feature_type, _clean(surface.feature_type)),
        material_identity=(
            f"{surface.material_type} terrain, roughness {surface.terrain_roughness:.0%}, "
            f"mountain prominence {surface.mountain_height:.0%}, biome contrast {surface.biome_contrast:.0%}"
        ),
        landmark_identity=landmarks,
        authored_identity=_clean(
            surface.visual_prompt or surface.description,
            fallback="an original alien geology with no recognizable terrestrial map",
        ),
        palette=VisualPalette(
            ocean_deep=_hex(palette.ocean_deep, "#0b2e59"),
            ocean_shallow=_hex(palette.ocean_shallow, "#1d6fa5"),
            shore=_hex(palette.shore, "#c2b280"),
            lowland=_hex(palette.lowland, "#4a7c3a"),
            midland=_hex(palette.midland, "#7a6f45"),
            highland=_hex(palette.highland, "#8a8578"),
            peak=_hex(palette.peak, "#e8e8e8"),
            atmosphere=_hex(spec.atmosphere.color_hex, "#7ab8ff"),
            clouds=_hex(spec.clouds.color_hex, "#ffffff"),
            ring=_hex(spec.rings.color_hex, "#c9b797"),
            star_lights=star_colors,
        ),
        atmosphere_identity=(
            f"{'present' if spec.atmosphere.present else 'absent'}, density {spec.atmosphere.density:.0%}, "
            f"composition {_clean(spec.atmosphere.composition)}"
        ),
        weather_identity=weather,
        illumination_identity=(
            f"{spec.star.count} star(s), colors {', '.join(star_colors)}, intensity {spec.star.intensity:.2f}"
        ),
        satellite_identity=f"{ring_state}; {moon_state}",
        species=tuple(_species_identity(index, value) for index, value in enumerate(spec.inhabitants)),
        forbidden_drift=(
            "recognizable real-world continental maps",
            "unrequested palette replacement",
            "changed atmospheric color",
            "changed ring or moon count",
            "generic terrestrial scenery",
            "species anatomy changes between images",
        ),
    )
