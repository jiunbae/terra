"""PlanetSpec: 소설 텍스트에서 추출하는 구조화된 행성 명세.

이 스키마는 세 곳에서 공유되는 계약이다:
- Gemini responseSchema (GEMINI_SCHEMA)
- 백엔드 검증/클램핑 (Pydantic 모델)
- 프론트엔드 3D 렌더러 파라미터 (frontend/src/types.ts와 동기 유지)
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, Field

_HEX3 = re.compile(r"#[0-9a-f]{3}")
_HEX6 = re.compile(r"#[0-9a-f]{6}")


def _coerce_hex_color(value: Any) -> Any:
    """LLM이 준 색 표기를 #rrggbb로 보정한다.

    strict 패턴으로 하드 거부하면 색 하나 때문에 섹션 전체(_lenient_parse)가
    기본값으로 버려지므로, 3자리 축약·공백·# 누락을 흡수하고 알 수 없는 값은
    중립 회색으로 대체해 다른 필드를 지킨다.
    """
    if not isinstance(value, str):
        # 비문자열(None/숫자/리스트 등)도 섹션 전체를 잃지 않도록 중립 회색으로 대체한다.
        return "#888888"
    text = value.strip().lower()
    if text and not text.startswith("#"):
        text = "#" + text
    if _HEX3.fullmatch(text):
        text = "#" + "".join(channel * 2 for channel in text[1:])
    if _HEX6.fullmatch(text):
        return text
    return "#888888"


NameText = Annotated[str, Field(max_length=120)]
ShortText = Annotated[str, Field(max_length=512)]
LongText = Annotated[str, Field(max_length=4096)]
PromptText = Annotated[str, Field(max_length=8192)]
HexColor = Annotated[
    str, BeforeValidator(_coerce_hex_color), Field(max_length=7, pattern=r"^#[0-9a-fA-F]{6}$")
]


class Planet(BaseModel):
    name: NameText = "이름 없는 행성"
    shape: Literal["sphere", "oblate", "irregular"] = "sphere"
    oblateness: float = Field(default=0.0, ge=0.0, le=0.35)
    radius_km: float = Field(default=6371, gt=100, lt=200000)
    gravity_g: float = Field(default=1.0, gt=0.01, lt=20)
    rotation_hours: float = Field(default=24, gt=0.1, lt=10000)
    axial_tilt_deg: float = Field(default=23.5, ge=0, le=90)


class Star(BaseModel):
    count: int = Field(default=1, ge=0, le=3)
    color_hex: HexColor = "#fff4e0"
    colors_hex: list[HexColor] = Field(default_factory=list, max_length=3)
    intensity: float = Field(default=1.0, ge=0.2, le=3.0)


class Atmosphere(BaseModel):
    present: bool = True
    density: float = Field(default=0.5, ge=0.0, le=1.0)
    color_hex: HexColor = "#7ab8ff"
    composition: LongText = ""
    weather_summary: LongText = ""


class Climate(BaseModel):
    avg_temp_c: float = Field(default=15, ge=-270, le=1500)
    temp_min_c: float = -20
    temp_max_c: float = 45
    humidity: float = Field(default=0.5, ge=0.0, le=1.0)
    phenomena: list[ShortText] = Field(default_factory=list, max_length=16)


class Palette(BaseModel):
    ocean_deep: HexColor = "#0b2e59"
    ocean_shallow: HexColor = "#1d6fa5"
    shore: HexColor = "#c2b280"
    lowland: HexColor = "#4a7c3a"
    midland: HexColor = "#7a6f45"
    highland: HexColor = "#8a8578"
    peak: HexColor = "#e8e8e8"


class Surface(BaseModel):
    ocean_coverage: float = Field(default=0.6, ge=0.0, le=1.0)
    terrain_roughness: float = Field(default=0.5, ge=0.0, le=1.0)
    mountain_height: float = Field(default=0.5, ge=0.0, le=1.0)
    ice_coverage: float = Field(default=0.1, ge=0.0, le=1.0)
    vegetation_coverage: float = Field(default=0.4, ge=0.0, le=1.0)
    lava_activity: float = Field(default=0.0, ge=0.0, le=1.0)
    city_lights: float = Field(default=0.0, ge=0.0, le=1.0)
    feature_type: Literal[
        "continents",
        "archipelago",
        "cratered",
        "canyons",
        "dunes",
        "crystalline",
        "volcanic",
        "artificial",
    ] = "continents"
    feature_scale: float = Field(default=0.5, ge=0.0, le=1.0)
    biome_contrast: float = Field(default=0.5, ge=0.0, le=1.0)
    material_type: Literal[
        "rock",
        "sand",
        "ice",
        "crystal",
        "metal",
        "organic",
        "volcanic",
        "mixed",
    ] = "mixed"
    landmarks: list[
        Literal[
            "rock_spires",
            "crystal_fields",
            "cave_openings",
            "volcanic_vents",
            "dune_ridges",
            "artificial_structures",
            "giant_flora",
            "ice_spires",
        ]
    ] = Field(default_factory=list, max_length=8)
    visual_prompt: PromptText = ""
    palette: Palette = Palette()
    description: PromptText = ""


class Clouds(BaseModel):
    coverage: float = Field(default=0.4, ge=0.0, le=1.0)
    color_hex: HexColor = "#ffffff"
    speed: float = Field(default=0.3, ge=0.0, le=1.0)
    storminess: float = Field(default=0.2, ge=0.0, le=1.0)


class Rings(BaseModel):
    present: bool = False
    color_hex: HexColor = "#c9b797"
    inner_ratio: float = Field(default=1.4, ge=1.1, le=3.0)
    outer_ratio: float = Field(default=2.2, ge=1.2, le=5.0)
    opacity: float = Field(default=0.7, ge=0.0, le=1.0)


class Moon(BaseModel):
    name: NameText = ""
    size_ratio: float = Field(default=0.27, ge=0.01, le=0.8)
    distance_ratio: float = Field(default=8.0, ge=2.0, le=30.0)
    color_hex: HexColor = "#b8b8b8"


class Inhabitant(BaseModel):
    name: NameText = ""
    category: ShortText = ""  # 예: 지성체, 동물, 식물, 기계
    height_m: float = Field(default=1.7, gt=0.001, lt=1000)
    appearance: LongText = ""
    physiology: LongText = ""
    culture: LongText = ""
    gravity_adaptation: LongText = ""
    portrait_prompt: PromptText = ""  # 이후 이미지 생성 단계에서 사용


class Inference(BaseModel):
    topic: ShortText
    claim: LongText
    confidence: Literal["stated", "inferred", "speculative"] = "inferred"
    evidence_quote: LongText = ""
    reasoning: LongText = ""


class PlanetSpec(BaseModel):
    planet: Planet = Planet()
    star: Star = Star()
    atmosphere: Atmosphere = Atmosphere()
    climate: Climate = Climate()
    surface: Surface = Surface()
    clouds: Clouds = Clouds()
    rings: Rings = Rings()
    moons: list[Moon] = Field(default_factory=list, max_length=8)
    inhabitants: list[Inhabitant] = Field(default_factory=list, max_length=16)
    inferences: list[Inference] = Field(default_factory=list, max_length=64)


def _hex() -> dict[str, Any]:
    return {
        "type": "string",
        "description": "#rrggbb 형식 hex 색상",
    }


def _text(max_length: int = 4096, desc: str = "") -> dict[str, Any]:
    # Gemini generateContent responseSchema가 지원한다고 문서화한 OpenAPI
    # 부분집합에는 string의 maxLength/pattern이 없다. 길이/형식 제한은 아래
    # Pydantic 모델에서 강제하고, 모델에는 의미 설명만 전달한다.
    del max_length
    result: dict[str, Any] = {"type": "string"}
    if desc:
        result["description"] = desc
    return result


def _num(lo: float | None = None, hi: float | None = None, desc: str = "") -> dict[str, Any]:
    d: dict[str, Any] = {"type": "number"}
    if desc:
        d["description"] = desc
    if lo is not None:
        d["minimum"] = lo
    if hi is not None:
        d["maximum"] = hi
    return d


# Gemini responseSchema (OpenAPI 3.0 부분집합)
GEMINI_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "planet": {
            "type": "object",
            "properties": {
                "name": _text(120),
                "shape": {"type": "string", "enum": ["sphere", "oblate", "irregular"]},
                "oblateness": _num(0, 0.35, "편평도. 빠른 자전/타원형 묘사 시 큼"),
                "radius_km": _num(100, 200000),
                "gravity_g": _num(0.01, 20, "지구=1 기준 표면 중력"),
                "rotation_hours": _num(0.1, 10000),
                "axial_tilt_deg": _num(0, 90),
            },
            "required": ["name", "shape", "gravity_g", "radius_km"],
        },
        "star": {
            "type": "object",
            "properties": {
                "count": {"type": "integer", "minimum": 0, "maximum": 3},
                "color_hex": _hex(),
                "colors_hex": {
                    "type": "array",
                    "maxItems": 3,
                    "description": "항성별 빛 색상. count와 같은 개수",
                    "items": _hex(),
                },
                "intensity": _num(0.2, 3.0),
            },
            "required": ["count", "color_hex", "colors_hex"],
        },
        "atmosphere": {
            "type": "object",
            "properties": {
                "present": {"type": "boolean"},
                "density": _num(0, 1),
                "color_hex": _hex(),
                "composition": _text(),
                "weather_summary": _text(),
            },
        },
        "climate": {
            "type": "object",
            "properties": {
                "avg_temp_c": _num(-270, 1500),
                "temp_min_c": _num(-270, 1500),
                "temp_max_c": _num(-270, 1500),
                "humidity": _num(0, 1),
                "phenomena": {"type": "array", "maxItems": 16, "items": _text(512)},
            },
        },
        "surface": {
            "type": "object",
            "properties": {
                "ocean_coverage": _num(0, 1),
                "terrain_roughness": _num(0, 1),
                "mountain_height": _num(0, 1),
                "ice_coverage": _num(0, 1),
                "vegetation_coverage": _num(0, 1),
                "lava_activity": _num(0, 1),
                "city_lights": _num(0, 1, "문명의 야간 불빛 정도"),
                "feature_type": {
                    "type": "string",
                    "description": "묘사에서 가장 두드러지는 지표 구조 유형",
                    "enum": [
                        "continents",
                        "archipelago",
                        "cratered",
                        "canyons",
                        "dunes",
                        "crystalline",
                        "volcanic",
                        "artificial",
                    ],
                },
                "feature_scale": _num(0, 1, "특징 구조의 크기와 두드러짐"),
                "biome_contrast": _num(0, 1, "서로 다른 생태/지질 구역의 대비"),
                "material_type": {
                    "type": "string",
                    "description": "확대 시 보이는 대표 표면 재질",
                    "enum": ["rock", "sand", "ice", "crystal", "metal", "organic", "volcanic", "mixed"],
                },
                "landmarks": {
                    "type": "array",
                    "maxItems": 8,
                    "description": "원문에서 시각적으로 드러나는 지표 랜드마크. 명시된 것만 선택",
                    "items": {
                        "type": "string",
                        "enum": [
                            "rock_spires",
                            "crystal_fields",
                            "cave_openings",
                            "volcanic_vents",
                            "dune_ridges",
                            "artificial_structures",
                            "giant_flora",
                            "ice_spires",
                        ],
                    },
                },
                "visual_prompt": _text(
                    8192,
                    "행성 전체 이미지 생성용 영어 시각 지시. 원문에 근거한 독특한 지형·날씨·색만 구체적으로 기술",
                ),
                "palette": {
                    "type": "object",
                    "properties": {
                        "ocean_deep": _hex(),
                        "ocean_shallow": _hex(),
                        "shore": _hex(),
                        "lowland": _hex(),
                        "midland": _hex(),
                        "highland": _hex(),
                        "peak": _hex(),
                    },
                },
                "description": _text(8192),
            },
            "required": ["feature_type", "feature_scale", "biome_contrast", "material_type", "landmarks", "visual_prompt"],
        },
        "clouds": {
            "type": "object",
            "properties": {
                "coverage": _num(0, 1),
                "color_hex": _hex(),
                "speed": _num(0, 1),
                "storminess": _num(0, 1),
            },
        },
        "rings": {
            "type": "object",
            "properties": {
                "present": {"type": "boolean"},
                "color_hex": _hex(),
                "inner_ratio": _num(1.1, 3.0),
                "outer_ratio": _num(1.2, 5.0),
                "opacity": _num(0, 1),
            },
        },
        "moons": {
            "type": "array",
            "maxItems": 8,
            "items": {
                "type": "object",
                "properties": {
                    "name": _text(120),
                    "size_ratio": _num(0.01, 0.8),
                    "distance_ratio": _num(2.0, 30.0),
                    "color_hex": _hex(),
                },
            },
        },
        "inhabitants": {
            "type": "array",
            "maxItems": 16,
            "items": {
                "type": "object",
                "properties": {
                    "name": _text(120),
                    "category": _text(512),
                    "height_m": _num(0.001, 1000),
                    "appearance": _text(),
                    "physiology": _text(),
                    "culture": _text(),
                    "gravity_adaptation": _text(),
                    "portrait_prompt": _text(
                        8192,
                        "이 거주민의 초상을 그리기 위한 영어 이미지 생성 프롬프트",
                    ),
                },
                "required": ["name", "appearance"],
            },
        },
        "inferences": {
            "type": "array",
            "maxItems": 64,
            "items": {
                "type": "object",
                "properties": {
                    "topic": _text(512),
                    "claim": _text(),
                    "confidence": {
                        "type": "string",
                        "enum": ["stated", "inferred", "speculative"],
                    },
                    "evidence_quote": _text(),
                    "reasoning": _text(),
                },
                "required": ["topic", "claim", "confidence"],
            },
        },
    },
    "required": ["planet", "surface", "inferences"],
}
