"""텍스트-이미지 모델의 지구 지도 편향을 막는 절차적 행성 img2img 가이드."""

from __future__ import annotations

import hashlib
import math
import random
from pathlib import Path

from .schema import PlanetSpec


def _rgb(value: str, fallback: tuple[int, int, int]) -> tuple[int, int, int]:
    raw = value.strip().lstrip("#")
    if len(raw) != 6:
        return fallback
    try:
        return tuple(int(raw[index:index + 2], 16) for index in (0, 2, 4))  # type: ignore[return-value]
    except ValueError:
        return fallback


def _mix(a: tuple[int, int, int], b: tuple[int, int, int], amount: float) -> tuple[int, int, int]:
    amount = max(0.0, min(1.0, amount))
    return tuple(round(left + (right - left) * amount) for left, right in zip(a, b))  # type: ignore[return-value]


def _shade(color: tuple[int, int, int], amount: float) -> tuple[int, int, int]:
    return tuple(max(0, min(255, round(channel * amount))) for channel in color)  # type: ignore[return-value]


def create_planet_guide(
    spec: PlanetSpec,
    path: Path,
    *,
    seed: int,
    width: int = 672,
    height: int = 448,
) -> Path:
    """비지구형 대륙 실루엣과 분석 팔레트를 가진 PPM 구체를 생성한다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    stable = int.from_bytes(
        hashlib.sha256(f"{spec.planet.name}:{seed}".encode()).digest()[:8],
        "big",
    )
    rng = random.Random(stable)
    palette = spec.surface.palette
    deep = _rgb(palette.ocean_deep, (15, 37, 66))
    shallow = _rgb(palette.ocean_shallow, (38, 102, 145))
    shore = _rgb(palette.shore, (184, 170, 132))
    low = _rgb(palette.lowland, (72, 105, 63))
    high = _rgb(palette.highland, (125, 113, 96))
    peak = _rgb(palette.peak, (224, 224, 220))
    atmo = _rgb(spec.atmosphere.color_hex, (112, 164, 220))

    centers: list[tuple[float, float, float, float, float]] = []
    for _ in range(30):
        y = rng.uniform(-0.92, 0.92)
        angle = rng.uniform(0, math.tau)
        radial = math.sqrt(1 - y * y)
        centers.append(
            (
                math.cos(angle) * radial,
                y,
                math.sin(angle) * radial,
                rng.uniform(0.018, 0.095),
                rng.uniform(0.48, 1.12),
            )
        )
    rifts: list[tuple[float, float, float, float, float]] = []
    for _ in range(13):
        y = rng.uniform(-0.88, 0.88)
        angle = rng.uniform(0, math.tau)
        radial = math.sqrt(1 - y * y)
        rifts.append(
            (
                math.cos(angle) * radial,
                y,
                math.sin(angle) * radial,
                rng.uniform(0.012, 0.055),
                rng.uniform(0.35, 0.78),
            )
        )

    radius = height * 0.43
    center_x = width * 0.5
    center_y = height * 0.5
    samples: list[tuple[int, float, float, float, float]] = []
    field_values: list[float] = []
    for py in range(max(0, round(center_y - radius)), min(height, round(center_y + radius) + 1)):
        ny = -(py - center_y) / radius
        for px in range(max(0, round(center_x - radius)), min(width, round(center_x + radius) + 1)):
            nx = (px - center_x) / radius
            squared = nx * nx + ny * ny
            if squared > 1:
                continue
            nz = math.sqrt(max(0.0, 1 - squared))
            value = 0.0
            for cx, cy, cz, spread, weight in centers:
                dot = nx * cx + ny * cy + nz * cz
                value += math.exp((dot - 1) / spread) * weight
            for cx, cy, cz, spread, weight in rifts:
                dot = nx * cx + ny * cy + nz * cz
                value -= math.exp((dot - 1) / spread) * weight
            longitude = math.atan2(nz, nx)
            latitude = math.asin(max(-1.0, min(1.0, ny)))
            channel_a = abs(
                math.sin(longitude * 2.35 + latitude * 4.1 + math.sin(latitude * 3.0) * 0.8 + stable % 17)
            )
            channel_b = abs(
                math.sin(longitude * 3.7 - latitude * 2.6 + math.sin(longitude * 2.0) * 0.45 + stable % 13)
            )
            if channel_a < 0.16:
                value -= (0.16 - channel_a) * 2.8
            if channel_b < 0.10:
                value -= (0.10 - channel_b) * 2.1
            value += math.sin(nx * 19.3 + ny * 8.1 + stable % 31) * 0.055
            value += math.sin(ny * 27.7 - nz * 11.4 + stable % 19) * 0.042
            index = py * width + px
            samples.append((index, nx, ny, nz, value))
            field_values.append(value)

    ordered = sorted(field_values)
    ocean_fraction = max(0.05, min(0.93, spec.surface.ocean_coverage))
    sea_level = ordered[min(len(ordered) - 1, round((len(ordered) - 1) * ocean_fraction))]
    low_value = ordered[max(0, round(len(ordered) * 0.05))]
    high_value = ordered[min(len(ordered) - 1, round(len(ordered) * 0.96))]
    span = max(0.001, high_value - low_value)

    pixels = bytearray(width * height * 3)
    for index, nx, ny, nz, value in samples:
        normalized = max(0.0, min(1.0, (value - low_value) / span))
        if value < sea_level:
            depth = max(0.0, min(1.0, (sea_level - value) / max(span * 0.32, 0.001)))
            color = _mix(shallow, deep, depth)
        else:
            elevation = max(0.0, min(1.0, (value - sea_level) / max(high_value - sea_level, 0.001)))
            if elevation < 0.08:
                color = _mix(shore, low, elevation / 0.08)
            elif elevation < 0.58:
                color = _mix(low, high, (elevation - 0.08) / 0.5)
            else:
                color = _mix(high, peak, (elevation - 0.58) / 0.42)

        light = max(0.0, nx * -0.42 + ny * 0.52 + nz * 0.74)
        color = _shade(color, 0.28 + light * 0.88)
        rim = max(0.0, 1 - nz) ** 2.2
        color = _mix(color, atmo, rim * spec.atmosphere.density * 0.85)

        offset = index * 3
        pixels[offset:offset + 3] = bytes(color)

    header = f"P6\n{width} {height}\n255\n".encode("ascii")
    path.write_bytes(header + pixels)
    return path
