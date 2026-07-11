// 절차적 행성 셰이더 모음.
// 지형 높이 함수는 vertex(변위)와 fragment(법선·색)가 동일한 코드를 공유한다.

// Ashima Arts 3D simplex noise (MIT)
export const NOISE_GLSL = /* glsl */ `
vec3 mod289(vec3 x) { return x - floor(x * (1.0 / 289.0)) * 289.0; }
vec4 mod289(vec4 x) { return x - floor(x * (1.0 / 289.0)) * 289.0; }
vec4 permute(vec4 x) { return mod289(((x * 34.0) + 1.0) * x); }
vec4 taylorInvSqrt(vec4 r) { return 1.79284291400159 - 0.85373472095314 * r; }

float snoise(vec3 v) {
  const vec2 C = vec2(1.0 / 6.0, 1.0 / 3.0);
  const vec4 D = vec4(0.0, 0.5, 1.0, 2.0);
  vec3 i = floor(v + dot(v, C.yyy));
  vec3 x0 = v - i + dot(i, C.xxx);
  vec3 g = step(x0.yzx, x0.xyz);
  vec3 l = 1.0 - g;
  vec3 i1 = min(g.xyz, l.zxy);
  vec3 i2 = max(g.xyz, l.zxy);
  vec3 x1 = x0 - i1 + C.xxx;
  vec3 x2 = x0 - i2 + C.yyy;
  vec3 x3 = x0 - D.yyy;
  i = mod289(i);
  vec4 p = permute(permute(permute(
      i.z + vec4(0.0, i1.z, i2.z, 1.0))
    + i.y + vec4(0.0, i1.y, i2.y, 1.0))
    + i.x + vec4(0.0, i1.x, i2.x, 1.0));
  float n_ = 0.142857142857;
  vec3 ns = n_ * D.wyz - D.xzx;
  vec4 j = p - 49.0 * floor(p * ns.z * ns.z);
  vec4 x_ = floor(j * ns.z);
  vec4 y_ = floor(j - 7.0 * x_);
  vec4 x = x_ * ns.x + ns.yyyy;
  vec4 y = y_ * ns.x + ns.yyyy;
  vec4 h = 1.0 - abs(x) - abs(y);
  vec4 b0 = vec4(x.xy, y.xy);
  vec4 b1 = vec4(x.zw, y.zw);
  vec4 s0 = floor(b0) * 2.0 + 1.0;
  vec4 s1 = floor(b1) * 2.0 + 1.0;
  vec4 sh = -step(h, vec4(0.0));
  vec4 a0 = b0.xzyw + s0.xzyw * sh.xxyy;
  vec4 a1 = b1.xzyw + s1.xzyw * sh.zzww;
  vec3 p0 = vec3(a0.xy, h.x);
  vec3 p1 = vec3(a0.zw, h.y);
  vec3 p2 = vec3(a1.xy, h.z);
  vec3 p3 = vec3(a1.zw, h.w);
  vec4 norm = taylorInvSqrt(vec4(dot(p0, p0), dot(p1, p1), dot(p2, p2), dot(p3, p3)));
  p0 *= norm.x; p1 *= norm.y; p2 *= norm.z; p3 *= norm.w;
  vec4 m = max(0.6 - vec4(dot(x0, x0), dot(x1, x1), dot(x2, x2), dot(x3, x3)), 0.0);
  m = m * m;
  return 42.0 * dot(m * m, vec4(dot(p0, x0), dot(p1, x1), dot(p2, x2), dot(p3, x3)));
}

float fbm(vec3 p, int octaves) {
  float v = 0.0;
  float a = 0.5;
  for (int i = 0; i < 12; i++) {
    if (i >= octaves) break;
    v += a * snoise(p);
    p = p * 2.03 + vec3(17.13);
    a *= 0.5;
  }
  return v;
}
`

// 지형 함수 — vertex/fragment 공유
export const TERRAIN_GLSL = /* glsl */ `
uniform vec3 uSeed;
uniform float uFreq;        // terrain_roughness 기반 기본 주파수
uniform float uMountain;    // mountain_height 0~1
uniform float uSeaLevel;    // ocean_coverage 기반 해수면 (-1~1)
uniform float uAmp;         // 변위 진폭 (중력이 크면 작아짐)
uniform float uOblate;      // 편평도
uniform float uIrregular;   // 불규칙 형태 0/1
uniform int uFeatureMode;   // 대표 지형: 대륙/군도/충돌구/협곡/사구/결정/화산/인공
uniform float uFeatureStrength;
uniform int uVertexOctaves;
uniform float uDetailMix;   // 카메라 근접도. 정점과 픽셀에서 같은 근접 지형을 사용
uniform float uCaveStrength;

float baseHeight(vec3 p, int oct) {
  return fbm(p * uFreq + uSeed, oct);
}

float naturalElevationFromBase(vec3 p, float h, int oct) {
  float land = smoothstep(uSeaLevel, uSeaLevel + 0.12, h);
  float ridged = 1.0 - abs(fbm(p * uFreq * 3.1 + uSeed * 2.7, oct));
  float mountains = ridged * ridged * ridged * uMountain;
  return max(h - uSeaLevel, 0.0) * 0.6 + mountains * land * 0.4;
}

float craterField(vec3 p) {
  float result = 0.0;
  for (int i = 0; i < 7; i++) {
    float fi = float(i) + dot(uSeed, vec3(0.013, 0.017, 0.019));
    vec3 center = normalize(vec3(
      sin(fi * 12.9898 + 1.7),
      cos(fi * 7.233 + 2.1),
      sin(fi * 4.123 + 4.6)
    ));
    float radius = mix(0.07, 0.19, fract(sin(fi * 91.17) * 43758.5453));
    float d = distance(p, center);
    float bowl = 1.0 - smoothstep(0.0, radius * 0.82, d);
    float rim = smoothstep(radius * 0.62, radius * 0.9, d) *
      (1.0 - smoothstep(radius * 0.9, radius * 1.16, d));
    result += rim * 0.055 - bowl * 0.075;
  }
  return result;
}

float featureHeight(vec3 p) {
  float strength = uFeatureStrength;
  if (uFeatureMode == 1) {
    // 군도: 작은 융기들이 해수면 근처에서 섬 사슬을 만든다.
    return snoise(p * mix(5.0, 9.0, strength) + uSeed * 0.31) * 0.035 * strength;
  }
  if (uFeatureMode == 2) return craterField(p) * mix(0.55, 1.35, strength);
  if (uFeatureMode == 3) {
    float veins = abs(snoise(p * mix(3.5, 7.0, strength) + uSeed * 1.9));
    return -(1.0 - smoothstep(0.025, 0.15, veins)) * 0.14 * strength;
  }
  if (uFeatureMode == 4) {
    float warp = snoise(p * 2.1 + uSeed) * 2.2;
    return sin((p.x * 0.8 + p.z) * mix(28.0, 58.0, strength) + warp) * 0.026 * strength;
  }
  if (uFeatureMode == 5) {
    float crystal = 1.0 - abs(snoise(p * mix(5.0, 10.0, strength) + uSeed * 2.3));
    return pow(crystal, 6.0) * 0.18 * strength;
  }
  if (uFeatureMode == 6) {
    float cones = max(snoise(p * mix(3.0, 5.5, strength) + uSeed * 2.8), 0.0);
    return pow(cones, 5.0) * 0.22 * strength + craterField(p) * 0.35 * strength;
  }
  if (uFeatureMode == 7) {
    float lon = atan(p.z, p.x);
    float lat = asin(clamp(p.y, -1.0, 1.0));
    float grid = pow(abs(sin(lon * 24.0)), 14.0) + pow(abs(sin(lat * 18.0)), 14.0);
    return grid * 0.018 * strength;
  }
  return 0.0;
}

float caveOpeningMask(vec3 p, float h) {
  if (uCaveStrength < 0.001) return 0.0;
  float land = smoothstep(uSeaLevel + 0.015, uSeaLevel + 0.11, h);
  vec3 warped = p * 13.0 + uSeed * 5.7;
  float pockets = snoise(warped + fbm(p * 4.2 + uSeed, 3) * 2.4) * 0.5 + 0.5;
  float openings = smoothstep(0.79, 0.91, pockets);
  float brokenEdge = smoothstep(0.38, 0.65, snoise(p * 37.0 + uSeed * 2.8) * 0.5 + 0.5);
  return openings * mix(0.62, 1.0, brokenEdge) * land * uCaveStrength;
}

// 저공 비행에서만 드러나는 중·미세 지질. 단순 색 노이즈가 아니라 정점 변위와
// 법선 계산에 함께 들어가므로 협곡의 홈, 사구의 능선, 결정 돌기 등이 실제로 빛을 받는다.
float closeRelief(vec3 p, float h) {
  if (uDetailMix < 0.001) return 0.0;

  float land = smoothstep(uSeaLevel - 0.01, uSeaLevel + 0.08, h);
  float broad = snoise(p * 17.0 + uSeed * 2.1) * 0.42;
  float grain = snoise(p * 47.0 + uSeed * 4.7) * 0.16;
  float signature = 0.0;

  if (uFeatureMode == 0) {
    // 침식된 암반과 접힌 산줄기
    float fold = 1.0 - abs(snoise(p * 23.0 + uSeed * 3.4));
    signature = pow(fold, 3.0) * 0.34 - 0.10;
  } else if (uFeatureMode == 1) {
    // 다공성 섬 암반과 해식 능선
    float reef = 1.0 - abs(snoise(p * 32.0 + uSeed * 1.8));
    signature = pow(reef, 5.0) * 0.28 - grain * 0.35;
  } else if (uFeatureMode == 2) {
    // 다수의 작은 충돌 흔적: 좁은 융기 테두리와 함몰부
    float impact = abs(snoise(p * 29.0 + uSeed * 5.2));
    float rim = 1.0 - smoothstep(0.12, 0.22, abs(impact - 0.28));
    float pit = 1.0 - smoothstep(0.0, 0.11, impact);
    signature = rim * 0.26 - pit * 0.34;
  } else if (uFeatureMode == 3) {
    // 여러 규모의 가늘고 깊은 균열 계곡
    float fissure = 1.0 - abs(snoise(p * 25.0 + uSeed * 6.1));
    float tributary = 1.0 - abs(snoise(p * 61.0 + uSeed * 2.6));
    signature = -pow(fissure, 8.0) * 0.48 - pow(tributary, 12.0) * 0.20;
  } else if (uFeatureMode == 4) {
    // 바람 방향이 읽히는 촘촘한 사구 물결
    float warp = snoise(p * 9.0 + uSeed * 1.3) * 5.0;
    signature = sin((p.x * 0.72 + p.z) * 125.0 + warp) * 0.19;
  } else if (uFeatureMode == 5) {
    // 날카로운 결정맥과 솟은 결정군
    float facet = 1.0 - abs(snoise(p * 34.0 + uSeed * 7.3));
    float shard = 1.0 - abs(snoise(p * 73.0 + uSeed * 2.2));
    signature = pow(facet, 7.0) * 0.42 + pow(shard, 11.0) * 0.20;
  } else if (uFeatureMode == 6) {
    // 굳은 용암판과 작은 화산성 돌기
    float basalt = 1.0 - abs(snoise(p * 31.0 + uSeed * 8.1));
    float cone = max(snoise(p * 19.0 + uSeed * 3.9), 0.0);
    signature = pow(cone, 5.0) * 0.34 - pow(basalt, 10.0) * 0.18;
  } else {
    // 경·위도 격자와 패널 이음매
    float lon = atan(p.z, p.x);
    float lat = asin(clamp(p.y, -1.0, 1.0));
    float grid = pow(abs(sin(lon * 72.0)), 24.0) + pow(abs(sin(lat * 54.0)), 24.0);
    signature = grid * 0.17 - 0.025;
  }

  float strength = mix(0.55, 1.15, uFeatureStrength);
  float caves = caveOpeningMask(p, h);
  return (broad * 0.16 + grain + signature * strength) * land - caves * 0.52;
}

float elevationFromBase(vec3 p, float h, int oct) {
  return naturalElevationFromBase(p, h, oct) + featureHeight(p);
}

float elevation(vec3 p, int oct) {
  return elevationFromBase(p, baseHeight(p, oct), oct);
}

// 최종 표면 좌표 (변위 + 편평도 + 불규칙형)
vec3 surfPoint(vec3 p, int oct) {
  float bulge = uIrregular > 0.5 ? 1.0 + 0.16 * fbm(p * 1.4 + uSeed * 0.5, 4) : 1.0;
  float h = baseHeight(p, oct);
  float relief = elevationFromBase(p, h, oct) + closeRelief(p, h) * uDetailMix;
  vec3 P = p * (1.0 + relief * uAmp) * bulge;
  P.y *= 1.0 - uOblate;
  return P;
}
`

export const PLANET_VERT = /* glsl */ `
varying vec3 vSphere;   // 단위구 좌표 (노이즈 도메인)
varying vec3 vWorldPos;
varying vec3 vSmoothNormal;
__NOISE__
__TERRAIN__

void main() {
  vec3 p = normalize(position);
  vSphere = p;
  vec3 P = surfPoint(p, uVertexOctaves);
  vec4 worldPos = modelMatrix * vec4(P, 1.0);
  vWorldPos = worldPos.xyz;
  float s = max(1.0 - uOblate, 0.001);
  vec3 ellipsoidNormal = normalize(vec3(p.x, p.y / (s * s), p.z));
  vSmoothNormal = normalize(mat3(modelMatrix) * ellipsoidNormal);
  gl_Position = projectionMatrix * viewMatrix * worldPos;
}
`

// 카메라 주변의 정규 격자를 단위구에 투영하는 clipmap vertex shader.
// 모든 단계가 동일한 행성 좌표 p를 사용하므로 재중심화되어도 지형이 미끄러지지 않는다.
export const CLIPMAP_VERT = /* glsl */ `
varying vec3 vSphere;
varying vec3 vWorldPos;
varying vec3 vSmoothNormal;
varying float vPatchAlpha;

uniform vec3 uPatchCenter;
uniform vec3 uPatchEast;
uniform vec3 uPatchNorth;
uniform float uPatchScale;
uniform float uPatchInnerRatio;
uniform float uPatchLift;

__NOISE__
__TERRAIN__

void main() {
  vec2 q = position.xy;
  vec3 p = normalize(uPatchCenter + (uPatchEast * q.x + uPatchNorth * q.y) * uPatchScale);
  vSphere = p;
  vec3 P = surfPoint(p, uVertexOctaves) * (1.0 + uPatchLift);
  vec4 worldPos = modelMatrix * vec4(P, 1.0);
  vWorldPos = worldPos.xyz;
  float s = max(1.0 - uOblate, 0.001);
  vec3 ellipsoidNormal = normalize(vec3(p.x, p.y / (s * s), p.z));
  vSmoothNormal = normalize(mat3(modelMatrix) * ellipsoidNormal);

  float edge = max(abs(q.x), abs(q.y));
  float outerFade = 1.0 - smoothstep(0.84, 1.0, edge);
  float innerFade = uPatchInnerRatio < 0.001
    ? 1.0
    : smoothstep(uPatchInnerRatio, min(uPatchInnerRatio + 0.14, 0.78), edge);
  vPatchAlpha = outerFade * innerFade;
  gl_Position = projectionMatrix * viewMatrix * worldPos;
}
`

export const PLANET_FRAG = /* glsl */ `
precision highp float;
varying vec3 vSphere;
varying vec3 vWorldPos;
varying vec3 vSmoothNormal;
#ifdef CLIPMAP_PATCH
varying float vPatchAlpha;
#endif

uniform mat4 modelMatrix; // three가 프로그램 유니폼으로 채워줌 (fragment엔 자동 선언 안 됨)
uniform int uOctaves;         // 카메라 거리에 따른 디테일
uniform vec3 uLightDir1;      // 월드공간 광원 방향
uniform vec3 uLightColor1;
uniform vec3 uLightDir2;
uniform vec3 uLightColor2;
uniform float uLight2On;
uniform float uLightIntensity;

uniform vec3 uOceanDeep;
uniform vec3 uOceanShallow;
uniform vec3 uShore;
uniform vec3 uLowland;
uniform vec3 uMidland;
uniform vec3 uHighland;
uniform vec3 uPeak;

uniform float uIce;
uniform float uVegetation;
uniform float uLava;
uniform float uCity;
uniform float uAtmoDensity;
uniform vec3 uAtmoColor;
uniform float uTempNorm;      // 0(극한) ~ 1(작열) — 만년설 위치에 영향
uniform float uBiomeContrast;
uniform float uHumidity;
uniform int uMaterialMode;     // rock/sand/ice/crystal/metal/organic/volcanic/mixed
__NOISE__
__TERRAIN__

vec3 objToWorld(vec3 n) { return normalize(mat3(modelMatrix) * n); }

void main() {
#ifdef CLIPMAP_PATCH
  // 투명 depth 정렬 대신 화면 디더로 clipmap 링 경계를 부드럽고 watertight하게 교체한다.
  float dither = fract(52.9829189 * fract(dot(gl_FragCoord.xy, vec2(0.06711056, 0.00583715))));
  if (vPatchAlpha < dither) discard;
#endif
  vec3 p = normalize(vSphere);
  int oct = uOctaves;

  float h = baseHeight(p, oct);
  float feature = featureHeight(p);
  float terrainElevation = naturalElevationFromBase(p, h, oct) + feature;
  float closeDetail = closeRelief(p, h) * uDetailMix;
  float microDetail = 0.0;
  if (uDetailMix > 0.001) {
    microDetail = snoise(p * 83.0 + uSeed * 1.7) * 0.55 +
      snoise(p * 167.0 + uSeed * 3.1) * 0.20;
  }
  float shadingElevation = terrainElevation + closeDetail + microDetail * uDetailMix * 0.065;
  bool isOcean = h < uSeaLevel;

  // ── 법선: 이미 변위된 메시의 화면 미분. 기존 중앙차분은 픽셀마다
  // surfPoint를 세 번 더 계산해 가장 큰 GPU 병목이었다.
  vec3 t = normalize(abs(p.y) < 0.99 ? cross(p, vec3(0.0, 1.0, 0.0)) : cross(p, vec3(1.0, 0.0, 0.0)));
  vec3 b = normalize(cross(p, t));
  vec3 n;
  if (isOcean) {
    // 타원체 법선: n = (x, y/s^2, z), s = 1-편평도
    float s = max(1.0 - uOblate, 0.001);
    vec3 nObj = normalize(vec3(p.x, p.y / (s * s), p.z));
    // 잔물결
    float w = snoise(p * 60.0 + uSeed * 3.0) * 0.5 + snoise(p * 120.0 + uSeed * 5.0) * 0.25;
    nObj = normalize(nObj + (t * w + b * w) * 0.015);
    n = objToWorld(nObj);
  } else {
    // 부드러운 타원체 법선에 연속적인 지형 높이의 화면 기울기만 더한다.
    // 삼각형 면 법선을 직접 쓰지 않아 낮은 LOD에서도 폴리곤 경계가 드러나지 않는다.
    vec3 smoothN = normalize(vSmoothNormal);
    vec3 dpdx = dFdx(vWorldPos);
    vec3 dpdy = dFdy(vWorldPos);
    float dhdx = dFdx(shadingElevation);
    float dhdy = dFdy(shadingElevation);
    vec3 r1 = cross(dpdy, smoothN);
    vec3 r2 = cross(smoothN, dpdx);
    float det = max(abs(dot(dpdx, r1)), 0.00001);
    vec3 gradient = (dhdx * r1 + dhdy * r2) / det;
    n = normalize(smoothN - gradient * uAmp * 2.4);
  }

  // ── 조명 (두 항성 지원)
  vec3 L1 = normalize(uLightDir1);
  vec3 L2 = normalize(uLightDir2);
  float d1 = max(dot(n, L1), 0.0);
  float d2 = max(dot(n, L2), 0.0) * uLight2On;
  vec3 diffuse = (d1 * uLightColor1 + d2 * uLightColor2 * 0.6) * uLightIntensity;
  float dayness = clamp(d1 + d2, 0.0, 1.0);

  // ── 지표 색
  vec3 col;
  float shininess = 8.0;
  float specStrength = 0.02;
  if (isOcean) {
    float depth = clamp((uSeaLevel - h) / 0.45, 0.0, 1.0);
    col = mix(uOceanShallow, uOceanDeep, sqrt(depth));
    shininess = 90.0;
    specStrength = 0.55;
  } else {
    float e = terrainElevation / (0.6 + uMountain * 0.4); // 0~1 근사
    float jitter = snoise(p * 18.0 + uSeed) * 0.025;
    e = clamp(e + jitter, 0.0, 1.0);
    vec3 low = mix(uMidland, uLowland, clamp(uVegetation * 1.4, 0.0, 1.0));
    if (e < 0.06)      col = mix(uShore, low, smoothstep(0.015, 0.06, e));
    else if (e < 0.35) col = mix(low, uMidland, smoothstep(0.06, 0.35, e));
    else if (e < 0.65) col = mix(uMidland, uHighland, smoothstep(0.35, 0.65, e));
    else               col = mix(uHighland, uPeak, smoothstep(0.65, 0.9, e));

    // 고도색 위에 기후 기반 바이옴 패치를 얹어 단순 팔레트 띠를 피한다.
    float biomeNoise = snoise(p * mix(5.0, 10.0, uBiomeContrast) + uSeed * 0.23) * 0.5 + 0.5;
    float biomePatch = smoothstep(0.42, 0.66, biomeNoise + (uHumidity - 0.5) * 0.25);
    vec3 lush = mix(uMidland, uLowland, clamp(uVegetation * 1.6, 0.0, 1.0));
    col = mix(col, lush, biomePatch * uBiomeContrast * uVegetation * 0.42);

    if (uFeatureMode == 2) col = mix(col, uHighland * 0.68, clamp(abs(feature) * 7.0, 0.0, 0.55));
    if (uFeatureMode == 3) col = mix(col, uOceanDeep * 0.55, clamp(-feature * 6.0, 0.0, 0.7));
    if (uFeatureMode == 4) col = mix(col, uShore, 0.38 * uFeatureStrength);
    if (uFeatureMode == 5) col = mix(col, uPeak, clamp(feature * 5.0, 0.0, 0.75));
    if (uFeatureMode == 6) col = mix(col, vec3(0.16, 0.12, 0.11), clamp(feature * 4.0, 0.0, 0.6));
    if (uFeatureMode == 7) col = mix(col, uHighland, clamp(feature * 12.0, 0.0, 0.65));

    // 근접 재질은 높이와 같은 좌표계의 다중 스케일 색 변화를 사용한다.
    // 별도 오브젝트를 얹지 않아 지표에 자연스럽게 이어지고 반복 경계도 없다.
    float reliefEdge = clamp(abs(closeDetail) * 7.0, 0.0, 1.0);
    float strata = snoise(p * 71.0 + uSeed * 5.9) * 0.5 + 0.5;
    float mineral = fbm(p * 21.0 + uSeed * 1.37, 4) * 0.5 + 0.5;
    float veins = pow(1.0 - abs(snoise(p * 48.0 + uSeed * 4.1)), 5.0);
    col *= 1.0 + (microDetail * 0.20 + (strata - 0.5) * 0.16) * uDetailMix;

    if (uMaterialMode == 0) {
      vec3 iron = mix(vec3(0.24, 0.16, 0.11), vec3(0.46, 0.35, 0.22), mineral);
      col = mix(col, iron, uDetailMix * (0.05 + veins * 0.16));
    } else if (uMaterialMode == 1) {
      vec3 sandTone = mix(vec3(0.48, 0.30, 0.16), vec3(0.82, 0.68, 0.42), strata);
      col = mix(col, sandTone, uDetailMix * 0.22);
    } else if (uMaterialMode == 2) {
      vec3 iceTone = mix(vec3(0.35, 0.63, 0.82), vec3(0.88, 0.96, 1.0), mineral);
      col = mix(col, iceTone, uDetailMix * (0.13 + veins * 0.22));
      shininess = mix(shininess, 58.0, uDetailMix * 0.55);
      specStrength = mix(specStrength, 0.30, uDetailMix * 0.55);
    } else if (uMaterialMode == 3) {
      vec3 spectral = 0.58 + 0.42 * cos(6.2831853 * (vec3(0.0, 0.34, 0.68) + mineral * 0.42 + p.y * 0.13));
      col = mix(col, spectral, uDetailMix * (0.08 + veins * 0.28));
      shininess = mix(shininess, 74.0, uDetailMix * reliefEdge);
      specStrength = mix(specStrength, 0.38, uDetailMix * reliefEdge);
    } else if (uMaterialMode == 4) {
      vec3 oxide = mix(vec3(0.16, 0.19, 0.22), vec3(0.42, 0.27, 0.16), veins);
      col = mix(col, oxide, uDetailMix * 0.25);
      shininess = mix(shininess, 42.0, uDetailMix * 0.45);
      specStrength = mix(specStrength, 0.24, uDetailMix * 0.45);
    } else if (uMaterialMode == 5) {
      vec3 living = mix(vec3(0.08, 0.23, 0.12), vec3(0.38, 0.19, 0.44), mineral);
      col = mix(col, living, uDetailMix * (0.08 + uVegetation * 0.22));
    } else if (uMaterialMode == 6) {
      vec3 basalt = mix(vec3(0.035, 0.028, 0.026), vec3(0.22, 0.075, 0.035), veins);
      col = mix(col, basalt, uDetailMix * 0.34);
    } else {
      vec3 accent = mix(uLowland, uShore, mineral);
      col = mix(col, accent, uDetailMix * veins * 0.13);
    }

    float cave = caveOpeningMask(p, h);
    vec3 caveWall = mix(vec3(0.012, 0.014, 0.018), uHighland * 0.18, strata);
    col = mix(col, caveWall, smoothstep(0.08, 0.72, cave));
    if (uFeatureMode == 0) col = mix(col, uHighland * 0.78, reliefEdge * 0.20);
    if (uFeatureMode == 1) col = mix(col, uShore * 0.82, reliefEdge * 0.22);
    if (uFeatureMode == 2) col = mix(col, uHighland * 0.52, reliefEdge * 0.42);
    if (uFeatureMode == 3) col = mix(col, uOceanDeep * 0.30, clamp(-closeDetail * 8.0, 0.0, 0.72));
    if (uFeatureMode == 4) col = mix(col, uShore * (0.72 + strata * 0.38), uDetailMix * 0.28);
    if (uFeatureMode == 5) {
      col = mix(col, uPeak * (0.72 + strata * 0.38), reliefEdge * 0.58);
      shininess = mix(shininess, 64.0, reliefEdge);
      specStrength = mix(specStrength, 0.34, reliefEdge);
    }
    if (uFeatureMode == 6) col = mix(col, vec3(0.055, 0.045, 0.04), reliefEdge * 0.52);
    if (uFeatureMode == 7) {
      col = mix(col, uHighland * (0.65 + strata * 0.35), reliefEdge * 0.48);
      shininess = mix(shininess, 32.0, reliefEdge);
      specStrength = mix(specStrength, 0.18, reliefEdge);
    }
  }

  // ── 만년설: 고위도 + 고도. 평균기온이 낮으면 넓게 내려옴
  float latitude = abs(p.y);
  float iceLine = 1.0 - uIce * (1.2 - uTempNorm * 0.6);
  float iceNoise = snoise(p * 6.0 + uSeed * 4.0) * 0.06;
  float iceMask = smoothstep(iceLine, iceLine + 0.1, latitude + iceNoise + (isOcean ? 0.0 : terrainElevation * 0.6));
  col = mix(col, vec3(0.92, 0.95, 0.99), iceMask);

  // ── 스펙큘러 (Blinn-Phong, 주 광원만)
  vec3 V = normalize(cameraPosition - vWorldPos);
  vec3 H = normalize(L1 + V);
  float spec = pow(max(dot(n, H), 0.0), shininess) * specStrength * d1 * (1.0 - iceMask * 0.7);

  // ── 발광: 용암 균열
  vec3 emissive = vec3(0.0);
  if (uLava > 0.001) {
    float crack = 1.0 - abs(fbm(p * uFreq * 4.5 + uSeed * 3.3, 4));
    // 활동도가 낮으면 균열 발광이 거의 안 보이도록 게이트
    float lava = smoothstep(0.88, 0.97, crack) * smoothstep(0.06, 0.65, uLava);
    emissive += vec3(1.0, 0.32, 0.04) * lava * (isOcean ? 0.3 : 2.2);
  }
  if (!isOcean && uFeatureMode == 6 && uDetailMix > 0.001) {
    float hotCrack = 1.0 - abs(snoise(p * 31.0 + uSeed * 8.1));
    float glow = pow(hotCrack, 13.0) * uFeatureStrength * uDetailMix;
    emissive += vec3(1.0, 0.18, 0.015) * glow * (0.18 + uLava * 1.8);
  }

  // ── 발광: 밤면의 도시 불빛 (육지 저지대에만)
  if (!isOcean && uCity > 0.001) {
    float cells = snoise(p * 42.0 + uSeed * 7.0) * 0.6 + snoise(p * 90.0 + uSeed * 11.0) * 0.4;
    float coast = 1.0 - smoothstep(0.0, 0.25, h - uSeaLevel); // 해안 선호
    float cities = smoothstep(0.62, 0.9, cells) * uCity * (0.35 + coast * 0.65) * (1.0 - iceMask);
    float night = 1.0 - clamp(dayness * 3.0, 0.0, 1.0);
    emissive += vec3(1.0, 0.72, 0.35) * cities * night * 1.6;
  }
  if (!isOcean && uFeatureMode == 7) {
    float structures = smoothstep(0.012, 0.028, feature) * uFeatureStrength;
    emissive += vec3(0.25, 0.62, 1.0) * structures * (0.35 + (1.0 - dayness));
  }

  // ── 림 대기 산란 근사
  float rim = pow(1.0 - max(dot(n, V), 0.0), 2.5);
  vec3 atmo = uAtmoColor * rim * uAtmoDensity * dayness * 0.55;

  vec3 ambient = vec3(0.018, 0.02, 0.028);
  vec3 outCol = col * (ambient + diffuse) + spec * uLightColor1 + emissive + atmo;
  gl_FragColor = vec4(outCol, 1.0);
  #include <tonemapping_fragment>
  #include <colorspace_fragment>
}
`

export const CLOUDS_VERT = /* glsl */ `
varying vec3 vPos;
varying vec3 vNormal;
void main() {
  vPos = normalize(position);
  vNormal = normalize(normalMatrix * normal);
  gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
}
`

export const CLOUDS_FRAG = /* glsl */ `
precision highp float;
varying vec3 vPos;
uniform mat4 modelMatrix;
uniform float uTime;
uniform float uCoverage;
uniform float uSpeed;
uniform float uStorm;
uniform float uBanding;
uniform float uNearFade;
uniform vec3 uColor;
uniform vec3 uLightDir1;
uniform vec3 uLightColor1;
uniform vec3 uLightDir2;
uniform vec3 uLightColor2;
uniform float uLight2On;
uniform vec3 uSeed;
__NOISE__

void main() {
  vec3 p = normalize(vPos);
  float t = uTime * (0.008 + uSpeed * 0.025);
  vec3 q = p * (2.2 + uStorm * 1.8) + uSeed;
  // 도메인 워프 — 폭풍이 강할수록 소용돌이침
  vec3 warp = vec3(
    fbm(q + vec3(t, 0.0, 0.0), 3),
    fbm(q + vec3(5.2, t * 0.7, 0.0), 3),
    snoise(q + vec3(0.0, 9.7, t * 0.4))
  );
  float c = fbm(q + warp * (0.6 + uStorm * 1.6) + vec3(t * 2.0, 0.0, t), 4);
  // 빠른 자전 행성은 목성형 제트기류처럼 위도 방향의 구름 띠가 강해진다.
  float jets = sin(p.y * mix(22.0, 42.0, uBanding) + warp.x * 2.4 + t * 4.0) * 0.5 + 0.5;
  c += (jets - 0.5) * uBanding * 0.32;
  float threshold = mix(0.55, -0.45, uCoverage);
  float alpha = smoothstep(threshold, threshold + 0.35, c);

  vec3 nWorld = normalize(mat3(modelMatrix) * p);
  float d1 = max(dot(nWorld, normalize(uLightDir1)), 0.0);
  float d2 = max(dot(nWorld, normalize(uLightDir2)), 0.0) * uLight2On;
  vec3 lit = uColor * (0.04 + d1 * uLightColor1 + d2 * uLightColor2 * 0.6);

  gl_FragColor = vec4(lit * uNearFade, alpha * 0.85 * uNearFade);
  #include <tonemapping_fragment>
  #include <colorspace_fragment>
}
`

export const ATMO_VERT = /* glsl */ `
varying vec3 vNormal;
varying vec3 vWorldPos;
void main() {
  vNormal = normalize(mat3(modelMatrix) * normal);
  vec4 wp = modelMatrix * vec4(position, 1.0);
  vWorldPos = wp.xyz;
  gl_Position = projectionMatrix * viewMatrix * wp;
}
`

export const ATMO_FRAG = /* glsl */ `
precision highp float;
varying vec3 vNormal;
varying vec3 vWorldPos;
uniform vec3 uColor;
uniform float uDensity;
uniform vec3 uLightDir1;
uniform float uNearFade;

void main() {
  vec3 V = normalize(cameraPosition - vWorldPos);
  // BackSide라 법선을 뒤집어 사용
  float fresnel = pow(1.0 - abs(dot(V, normalize(vNormal))), 3.2);
  float day = clamp(dot(normalize(vNormal), normalize(uLightDir1)) * 0.65 + 0.45, 0.05, 1.0);
  vec3 col = uColor * fresnel * uDensity * day * 1.8 * uNearFade;
  gl_FragColor = vec4(col, fresnel * uDensity * uNearFade);
  #include <tonemapping_fragment>
  #include <colorspace_fragment>
}
`

export const RING_VERT = /* glsl */ `
varying vec3 vLocal;
varying vec3 vWorldPos;
void main() {
  vLocal = position;
  vec4 wp = modelMatrix * vec4(position, 1.0);
  vWorldPos = wp.xyz;
  gl_Position = projectionMatrix * viewMatrix * wp;
}
`

export const RING_FRAG = /* glsl */ `
precision highp float;
varying vec3 vLocal;
varying vec3 vWorldPos;
uniform vec3 uColor;
uniform float uInner;
uniform float uOuter;
uniform float uOpacity;
uniform vec3 uLightDir1;
uniform vec3 uSeed;
__NOISE__

void main() {
  float r = length(vLocal.xy);
  float x = clamp((r - uInner) / (uOuter - uInner), 0.0, 1.0);
  // 반경 방향 밴드
  float bands = 0.55 + 0.45 * snoise(vec3(x * 26.0, uSeed.x, uSeed.y));
  bands *= 0.7 + 0.3 * snoise(vec3(x * 90.0, uSeed.y, uSeed.z));
  float edge = smoothstep(0.0, 0.06, x) * (1.0 - smoothstep(0.94, 1.0, x));

  // 행성 그림자: 태양 반대편(axial<0)이고 그림자 원기둥 안이면 어둡게
  vec3 L = normalize(uLightDir1);
  float axial = dot(vWorldPos, L);
  float perp = length(vWorldPos - axial * L);
  float inCyl = 1.0 - smoothstep(0.95, 1.15, perp);
  float shadow = 1.0 - inCyl * step(axial, 0.0) * 0.85;

  float alpha = bands * edge * uOpacity * shadow;
  gl_FragColor = vec4(uColor * (0.25 + 0.75 * shadow), alpha);
  #include <tonemapping_fragment>
  #include <colorspace_fragment>
}
`

export function assemble(src: string): string {
  return src.replace('__NOISE__', NOISE_GLSL).replace('__TERRAIN__', TERRAIN_GLSL)
}
