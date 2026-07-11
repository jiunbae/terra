"""Deterministic semantic conditioning maps for Terra image generation.

Unlike the shaded RGB preview in :mod:`planet_guide`, every color in this PPM is
an exact class label. It is suitable for a future ControlNet/segmentation adapter
and also provides a compact visual layout hint to img2img models.
"""

from __future__ import annotations

import hashlib
import math
import random
from pathlib import Path
from typing import NamedTuple, TypeAlias

from .schema import PlanetSpec

RGB: TypeAlias = tuple[int, int, int]

# Colors are deliberately high-contrast and immutable by PlanetSpec palette. A
# consumer can therefore interpret the same pixel value across every world.
SEMANTIC_CLASS_LEGEND: dict[str, RGB] = {
    "background": (0, 0, 0),
    "ocean": (0, 72, 255),
    "land_low": (92, 196, 76),
    "land_mid": (210, 166, 55),
    "land_high": (245, 235, 210),
    "cave": (54, 32, 24),
    "crystal_field": (205, 48, 255),
    "artificial_structure": (0, 235, 220),
    "ice": (170, 235, 255),
    "vegetation": (12, 116, 42),
    "weather": (255, 58, 72),
}


class SemanticGuideResult(NamedTuple):
    """Path and exact class-to-RGB contract for a generated guide."""

    path: Path
    legend: dict[str, RGB]


def create_semantic_guide(
    spec: PlanetSpec,
    path: str | Path,
    *,
    seed: int,
    width: int = 672,
    height: int = 448,
) -> SemanticGuideResult:
    """Create a P6 PPM semantic globe and return it with its RGB class legend.

    Layout is deterministic for the complete visual portion of ``PlanetSpec`` and
    ``seed``. Optional feature classes are emitted only when supported by the spec,
    preventing a conditioning map from inventing caves, crystals, or structures.
    """

    if width < 32 or height < 32:
        raise ValueError("semantic guide dimensions must both be at least 32 pixels")
    if width > 4096 or height > 4096:
        raise ValueError("semantic guide dimensions must not exceed 4096 pixels")

    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    stable = _stable_seed(spec, seed)
    rng = random.Random(stable)

    radius = height * 0.43
    center_x = width * 0.5
    center_y = height * 0.5
    samples, values = _sample_globe(width, height, center_x, center_y, radius, stable, rng)
    ordered = sorted(values)
    ocean_fraction = max(0.03, min(0.95, spec.surface.ocean_coverage))
    sea_level = _quantile(ordered, ocean_fraction)
    land_values = sorted(value for _, _, _, _, _, value in samples if value >= sea_level)
    mid_level = _quantile(land_values, 0.48)
    high_level = _quantile(land_values, 0.82)

    pixels = bytearray(SEMANTIC_CLASS_LEGEND["background"] * (width * height))
    globe_mask = bytearray(width * height)
    land_mask = bytearray(width * height)
    land_points: list[tuple[int, int]] = []

    ice_cutoff = 1.0 - min(0.82, spec.surface.ice_coverage * 1.6)
    vegetation = spec.surface.vegetation_coverage
    for index, px, py, nx, ny, value in samples:
        globe_mask[index] = 1
        if value < sea_level:
            class_name = "ocean"
        else:
            land_mask[index] = 1
            land_points.append((px, py))
            if value >= high_level:
                class_name = "land_high"
            elif value >= mid_level:
                class_name = "land_mid"
            else:
                class_name = "land_low"

            vegetation_noise = _wave_noise(nx, ny, stable + 101)
            if vegetation > 0 and abs(ny) < 0.78 and vegetation_noise < vegetation * 0.78:
                class_name = "vegetation"

        polar_noise = _wave_noise(nx * 1.7, ny * 1.3, stable + 211) * 0.12
        if spec.surface.ice_coverage > 0 and abs(ny) + polar_noise >= ice_cutoff:
            class_name = "ice"
        _set_pixel(pixels, index, SEMANTIC_CLASS_LEGEND[class_name])

    anchors = _choose_land_anchors(land_points, rng, center_x, center_y, radius)
    landmarks = set(spec.surface.landmarks)

    if "cave_openings" in landmarks:
        for order, (cx, cy) in enumerate(anchors[:3]):
            _paint_blob(
                pixels,
                land_mask,
                width,
                height,
                cx,
                cy,
                max(2.0, radius * (0.018 + order * 0.003)),
                max(2.0, radius * 0.012),
                SEMANTIC_CLASS_LEGEND["cave"],
                stable + order,
            )

    crystal_enabled = (
        spec.surface.feature_type == "crystalline"
        or spec.surface.material_type == "crystal"
        or "crystal_fields" in landmarks
    )
    if crystal_enabled:
        start = 3 if "cave_openings" in landmarks else 0
        for order, (cx, cy) in enumerate(anchors[start : start + 4]):
            scale = radius * (0.025 + spec.surface.feature_scale * 0.025)
            _paint_blob(
                pixels,
                land_mask,
                width,
                height,
                cx,
                cy,
                scale,
                scale * 0.62,
                SEMANTIC_CLASS_LEGEND["crystal_field"],
                stable + 31 + order,
            )

    artificial_enabled = (
        spec.surface.feature_type == "artificial"
        or "artificial_structures" in landmarks
        or spec.surface.city_lights > 0.05
    )
    if artificial_enabled:
        start = 7 if crystal_enabled else 3
        for order, (cx, cy) in enumerate(anchors[start : start + 3]):
            _paint_structure(
                pixels,
                land_mask,
                width,
                height,
                cx,
                cy,
                max(3, round(radius * (0.025 + spec.surface.city_lights * 0.025))),
                SEMANTIC_CLASS_LEGEND["artificial_structure"],
                order,
            )

    weather_enabled = bool(spec.climate.phenomena) or spec.clouds.storminess >= 0.35
    if weather_enabled:
        _paint_weather(
            pixels,
            globe_mask,
            width,
            height,
            center_x,
            center_y,
            radius,
            stable,
            density=max(0.12, min(0.82, spec.clouds.coverage + spec.clouds.storminess * 0.3)),
        )

    header = f"P6\n{width} {height}\n255\n".encode("ascii")
    output.write_bytes(header + pixels)
    return SemanticGuideResult(output, dict(SEMANTIC_CLASS_LEGEND))


# A shorter alias is convenient for pipeline code while preserving a descriptive API.
create_guide = create_semantic_guide


def _stable_seed(spec: PlanetSpec, seed: int) -> int:
    visual_signature = (
        spec.planet.name,
        spec.surface.ocean_coverage,
        spec.surface.terrain_roughness,
        spec.surface.mountain_height,
        spec.surface.ice_coverage,
        spec.surface.vegetation_coverage,
        spec.surface.city_lights,
        spec.surface.feature_type,
        spec.surface.feature_scale,
        spec.surface.material_type,
        tuple(spec.surface.landmarks),
        tuple(spec.climate.phenomena),
        spec.clouds.coverage,
        spec.clouds.storminess,
        seed,
    )
    return int.from_bytes(hashlib.sha256(repr(visual_signature).encode()).digest()[:8], "big")


def _sample_globe(
    width: int,
    height: int,
    center_x: float,
    center_y: float,
    radius: float,
    stable: int,
    rng: random.Random,
) -> tuple[list[tuple[int, int, int, float, float, float]], list[float]]:
    land_centers: list[tuple[float, float, float, float, float]] = []
    for _ in range(26):
        y = rng.uniform(-0.94, 0.94)
        angle = rng.uniform(0, math.tau)
        radial = math.sqrt(1 - y * y)
        land_centers.append(
            (
                math.cos(angle) * radial,
                y,
                math.sin(angle) * radial,
                rng.uniform(0.018, 0.105),
                rng.uniform(0.5, 1.15),
            )
        )

    samples: list[tuple[int, int, int, float, float, float]] = []
    values: list[float] = []
    min_y = max(0, round(center_y - radius))
    max_y = min(height, round(center_y + radius) + 1)
    min_x = max(0, round(center_x - radius))
    max_x = min(width, round(center_x + radius) + 1)
    roughness = 0.035 + stable % 19 / 1000
    for py in range(min_y, max_y):
        ny = -(py - center_y) / radius
        for px in range(min_x, max_x):
            nx = (px - center_x) / radius
            squared = nx * nx + ny * ny
            if squared > 1:
                continue
            nz = math.sqrt(max(0.0, 1 - squared))
            value = 0.0
            for cx, cy, cz, spread, weight in land_centers:
                dot = nx * cx + ny * cy + nz * cz
                value += math.exp((dot - 1) / spread) * weight
            value += math.sin(nx * 17.7 + ny * 7.3 + stable % 29) * roughness
            value += math.sin(ny * 25.1 - nz * 12.6 + stable % 23) * roughness * 0.72
            index = py * width + px
            samples.append((index, px, py, nx, ny, value))
            values.append(value)
    return samples, values


def _choose_land_anchors(
    land_points: list[tuple[int, int]],
    rng: random.Random,
    center_x: float,
    center_y: float,
    radius: float,
) -> list[tuple[int, int]]:
    if not land_points:
        return []
    pool = land_points[:]
    rng.shuffle(pool)
    anchors: list[tuple[int, int]] = []
    minimum_distance_sq = (radius * 0.09) ** 2
    for point in pool:
        if all((point[0] - x) ** 2 + (point[1] - y) ** 2 >= minimum_distance_sq for x, y in anchors):
            anchors.append(point)
            if len(anchors) >= 14:
                return anchors
    # Small maps or almost-ocean worlds may not meet spacing; repeat deterministic
    # valid land positions so every explicitly requested feature remains representable.
    index = 0
    while len(anchors) < 14:
        anchors.append(pool[index % len(pool)])
        index += max(1, len(pool) // 13)
    return anchors


def _paint_blob(
    pixels: bytearray,
    allowed_mask: bytearray,
    width: int,
    height: int,
    cx: int,
    cy: int,
    radius_x: float,
    radius_y: float,
    color: RGB,
    noise_seed: int,
) -> None:
    for py in range(max(0, round(cy - radius_y - 1)), min(height, round(cy + radius_y + 2))):
        for px in range(max(0, round(cx - radius_x - 1)), min(width, round(cx + radius_x + 2))):
            index = py * width + px
            if not allowed_mask[index]:
                continue
            dx = (px - cx) / max(1.0, radius_x)
            dy = (py - cy) / max(1.0, radius_y)
            edge_noise = _wave_noise(px * 0.17, py * 0.19, noise_seed) * 0.24
            if dx * dx + dy * dy <= 0.82 + edge_noise:
                _set_pixel(pixels, index, color)


def _paint_structure(
    pixels: bytearray,
    allowed_mask: bytearray,
    width: int,
    height: int,
    cx: int,
    cy: int,
    radius: int,
    color: RGB,
    orientation: int,
) -> None:
    for offset in range(-radius, radius + 1):
        coordinates = (
            ((cx + offset, cy), (cx, cy + offset))
            if orientation % 2 == 0
            else ((cx + offset, cy + offset), (cx + offset, cy - offset))
        )
        for px, py in coordinates:
            if 0 <= px < width and 0 <= py < height:
                index = py * width + px
                if allowed_mask[index]:
                    _set_pixel(pixels, index, color)


def _paint_weather(
    pixels: bytearray,
    globe_mask: bytearray,
    width: int,
    height: int,
    center_x: float,
    center_y: float,
    radius: float,
    stable: int,
    density: float,
) -> None:
    color = SEMANTIC_CLASS_LEGEND["weather"]
    spacing = max(7, round(18 - density * 10))
    slant = -1 if stable % 2 else 1
    for band in range(-round(radius), round(radius) + 1, spacing):
        length = max(3, round(radius * (0.035 + density * 0.045)))
        base_x = round(center_x + band * 0.73)
        base_y = round(center_y + band * 0.31)
        for step in range(length):
            px = base_x + slant * step
            py = base_y + step
            if 0 <= px < width and 0 <= py < height:
                index = py * width + px
                if globe_mask[index]:
                    _set_pixel(pixels, index, color)


def _quantile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    index = min(len(values) - 1, max(0, round((len(values) - 1) * fraction)))
    return values[index]


def _wave_noise(x: float, y: float, seed: int) -> float:
    # 0..1 deterministic pseudo-noise without external imaging/numeric packages.
    value = math.sin(x * 12.9898 + y * 78.233 + seed % 997) * 43758.5453
    return value - math.floor(value)


def _set_pixel(pixels: bytearray, index: int, color: RGB) -> None:
    offset = index * 3
    pixels[offset : offset + 3] = bytes(color)
