"""추출된 스펙에서 계산으로 파생되는 물리량.

LLM이 말한 값(반지름, 중력, 자전주기)만으로 뉴턴 역학을 통해
질량·밀도·탈출속도 등을 '유추'해 보여주는 부분 — 사이트의 재미 포인트.
"""

from __future__ import annotations

import math
from typing import Any

from .schema import PlanetSpec

G = 6.674e-11  # 중력상수
EARTH_G = 9.81  # m/s^2
EARTH_MASS = 5.972e24  # kg
EARTH_RADIUS_KM = 6371


def derive_physics(spec: PlanetSpec) -> dict[str, Any]:
    p = spec.planet
    r_m = p.radius_km * 1000
    g_ms2 = p.gravity_g * EARTH_G

    # g = GM/r^2  →  M = g r^2 / G
    mass_kg = g_ms2 * r_m**2 / G
    volume_m3 = (4 / 3) * math.pi * r_m**3 * (1 - p.oblateness)
    density = mass_kg / volume_m3 if volume_m3 > 0 else 0

    # 탈출속도 v = sqrt(2GM/r)
    escape_kms = math.sqrt(2 * G * mass_kg / r_m) / 1000

    # 저궤도 공전주기 T = 2π sqrt(r^3 / GM)
    leo_r = r_m * 1.05
    leo_minutes = 2 * math.pi * math.sqrt(leo_r**3 / (G * mass_kg)) / 60

    # 적도 자전 선속도
    equator_speed_kmh = (2 * math.pi * p.radius_km) / p.rotation_hours

    # 자전에 의한 이론적 편평도 근사: f ≈ ω²r / (2g) 의 스케일업
    omega = 2 * math.pi / (p.rotation_hours * 3600)
    rotational_flattening = min(0.5, (omega**2 * r_m) / (2 * g_ms2) * 2.5)
    centrifugal_ms2 = omega**2 * r_m
    effective_equator_g = max(0.0, (g_ms2 - centrifugal_ms2) / EARTH_G)

    # 같은 자전 각속도로 도는 동기궤도. 행성 안쪽이면 안정적인 동기궤도가 없다.
    synchronous_radius_m = (G * mass_kg / omega**2) ** (1 / 3)
    synchronous_altitude_km = (synchronous_radius_m - r_m) / 1000
    breakup_period_hours = 2 * math.pi * math.sqrt(r_m**3 / (G * mass_kg)) / 3600

    # 인간 기준 체감: 지구인 70kg이 느끼는 무게
    human_weight_kg = 70 * p.gravity_g

    # 대기 유지 가능성 힌트: 탈출속도 낮고 고온이면 대기 이탈
    can_hold_atmosphere = escape_kms > 4.0 and spec.climate.avg_temp_c < 400

    return {
        "mass_kg": mass_kg,
        "mass_earths": mass_kg / EARTH_MASS,
        "surface_gravity_ms2": g_ms2,
        "density_g_cm3": density / 1000,
        "circumference_km": 2 * math.pi * p.radius_km,
        "volume_earths": (p.radius_km / EARTH_RADIUS_KM) ** 3 * (1 - p.oblateness),
        "escape_velocity_kms": escape_kms,
        "low_orbit_period_min": leo_minutes,
        "equator_speed_kmh": equator_speed_kmh,
        "rotational_flattening_theory": rotational_flattening,
        "centrifugal_acceleration_ms2": centrifugal_ms2,
        "effective_equator_gravity_g": effective_equator_g,
        "synchronous_orbit_altitude_km": synchronous_altitude_km,
        "breakup_period_hours": breakup_period_hours,
        "surface_area_earths": (p.radius_km / EARTH_RADIUS_KM) ** 2,
        "human_weight_kg": human_weight_kg,
        "can_hold_atmosphere": can_hold_atmosphere,
        "day_length_vs_earth": p.rotation_hours / 24,
    }
