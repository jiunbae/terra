// 분석된 PlanetSpec으로 구동되는 절차적 행성계:
// 지형 행성 + 구름 + 대기 + 고리 + 위성 + 항성(1~2개)

import { useEffect, useMemo, useRef } from 'react'
import { useFrame } from '@react-three/fiber'
import * as THREE from 'three'
import type { PlanetSpec } from '../types'
import {
  assemble,
  PLANET_VERT,
  CLIPMAP_VERT,
  PLANET_FRAG,
  CLOUDS_VERT,
  CLOUDS_FRAG,
  ATMO_VERT,
  ATMO_FRAG,
  RING_VERT,
  RING_FRAG,
} from './shaders'
import { createClipmapGeometry } from './clipmap'

// 항성 방향은 월드 공간에 고정 (행성이 자전)
const SUN1_DIR = new THREE.Vector3(6, 2.5, 4).normalize()
const SUN2_DIR = new THREE.Vector3(-5, 1.2, -5).normalize()
const SUN2_COLOR = '#dfe9ff'

function starColor(spec: PlanetSpec, index: number): string {
  return spec.star.colors_hex?.[index] || (index === 0 ? spec.star.color_hex : SUN2_COLOR)
}

function hashSeed(name: string): THREE.Vector3 {
  let h = 2166136261
  for (let i = 0; i < name.length; i++) {
    h ^= name.charCodeAt(i)
    h = Math.imul(h, 16777619)
  }
  const r = (n: number) => (((h >>> n) & 1023) / 1023) * 40 - 20
  return new THREE.Vector3(r(0), r(10), r(20))
}

function col(hex: string): THREE.Color {
  try {
    return new THREE.Color(hex)
  } catch {
    return new THREE.Color('#888888')
  }
}

function seededRandom(label: string): () => number {
  let state = 2166136261
  for (let i = 0; i < label.length; i++) {
    state ^= label.charCodeAt(i)
    state = Math.imul(state, 16777619)
  }
  return () => {
    state += 0x6d2b79f5
    let value = state
    value = Math.imul(value ^ (value >>> 15), value | 1)
    value ^= value + Math.imul(value ^ (value >>> 7), value | 61)
    return ((value ^ (value >>> 14)) >>> 0) / 4294967296
  }
}

const FEATURE_MODE: Record<PlanetSpec['surface']['feature_type'], number> = {
  continents: 0,
  archipelago: 1,
  cratered: 2,
  canyons: 3,
  dunes: 4,
  crystalline: 5,
  volcanic: 6,
  artificial: 7,
}

const MATERIAL_MODE: Record<NonNullable<PlanetSpec['surface']['material_type']>, number> = {
  rock: 0,
  sand: 1,
  ice: 2,
  crystal: 3,
  metal: 4,
  organic: 5,
  volcanic: 6,
  mixed: 7,
}

function inferredMaterial(spec: PlanetSpec): number {
  if (spec.surface.material_type) return MATERIAL_MODE[spec.surface.material_type]
  const text = `${spec.surface.description} ${spec.atmosphere.weather_summary}`.toLowerCase()
  if (/수정|결정|유리|대리석|crystal|glass|marble/.test(text)) return MATERIAL_MODE.crystal
  if (/모래|사구|sand|dune/.test(text)) return MATERIAL_MODE.sand
  if (/얼음|빙하|ice|glacier/.test(text)) return MATERIAL_MODE.ice
  if (/금속|기계|metal|machine/.test(text)) return MATERIAL_MODE.metal
  if (/화산|용암|현무암|volcan|lava|basalt/.test(text)) return MATERIAL_MODE.volcanic
  if (/생체|유기|균사|organic|fung/.test(text)) return MATERIAL_MODE.organic
  return MATERIAL_MODE.rock
}

function caveStrength(spec: PlanetSpec): number {
  if (spec.surface.landmarks?.includes('cave_openings')) return 1
  return /동굴|cave|지하/.test(`${spec.surface.description} ${spec.atmosphere.weather_summary}`.toLowerCase()) ? 1 : 0
}

function usePlanetParams(spec: PlanetSpec) {
  return useMemo(() => {
    const p = spec.planet
    const s = spec.surface
    const seaLevel = THREE.MathUtils.clamp((s.ocean_coverage - 0.5) * 1.3, -0.95, 0.95)
    const amp =
      (0.055 * (0.35 + s.mountain_height)) / Math.sqrt(Math.max(p.gravity_g, 0.2))
    const oblate =
      p.shape === 'oblate' ? Math.max(p.oblateness, 0.06) : p.shape === 'irregular' ? 0.02 : p.oblateness * 0.5
    return {
      seed: hashSeed(p.name),
      freq: 1.6 + s.terrain_roughness * 2.4,
      amp,
      oblate,
      irregular: p.shape === 'irregular' ? 1 : 0,
      seaLevel,
      tempNorm: THREE.MathUtils.clamp((spec.climate.avg_temp_c + 50) / 150, 0, 1),
      rotSpeed: THREE.MathUtils.clamp(0.03 * (24 / Math.max(p.rotation_hours, 0.1)), 0.004, 0.25),
      tiltRad: (-p.axial_tilt_deg * Math.PI) / 180,
      twoSuns: spec.star.count >= 2,
    }
  }, [spec])
}

interface ClipmapLevel {
  scale: number
  innerRatio: number
  octaves: number
  lift: number
}

const CLIPMAP_LEVELS: ClipmapLevel[] = [
  { scale: 0.105, innerRatio: 0, octaves: 9, lift: 0.00065 },
  { scale: 0.25, innerRatio: 0.32, octaves: 8, lift: 0.00042 },
  { scale: 0.52, innerRatio: 0.37, octaves: 7, lift: 0.00022 },
]

function SphericalClipmap({
  anchorRef,
  sharedUniforms,
  quality,
  name,
}: {
  anchorRef: React.RefObject<THREE.Group | null>
  sharedUniforms: Record<string, THREE.IUniform>
  quality: number
  name: string
}) {
  const meshRefs = useRef<Array<THREE.Mesh | null>>([])
  const segments = quality < 0.6 ? 64 : quality < 0.9 ? 88 : 112
  const geometries = useMemo(
    () => CLIPMAP_LEVELS.map((level) => createClipmapGeometry(segments, level.innerRatio)),
    [segments],
  )
  const patchUniforms = useMemo(
    () => CLIPMAP_LEVELS.map((level) => ({
      ...sharedUniforms,
      uVertexOctaves: { value: level.octaves },
      uPatchCenter: { value: new THREE.Vector3(0, 0, 1) },
      uPatchEast: { value: new THREE.Vector3(1, 0, 0) },
      uPatchNorth: { value: new THREE.Vector3(0, 1, 0) },
      uPatchScale: { value: level.scale },
      uPatchInnerRatio: { value: level.innerRatio },
      uPatchLift: { value: level.lift },
    })),
    [sharedUniforms],
  )
  const scratch = useMemo(() => ({
    center: new THREE.Vector3(),
    east: new THREE.Vector3(),
    north: new THREE.Vector3(),
    helper: new THREE.Vector3(),
  }), [])

  useEffect(() => () => geometries.forEach((geometry) => geometry.dispose()), [geometries])

  useFrame(({ camera }) => {
    const anchor = anchorRef.current
    if (!anchor) return
    scratch.center.copy(camera.position)
    anchor.worldToLocal(scratch.center)
    scratch.center.normalize()
    scratch.helper.set(0, 1, 0)
    if (Math.abs(scratch.center.y) > 0.94) scratch.helper.set(1, 0, 0)
    scratch.east.crossVectors(scratch.helper, scratch.center).normalize()
    scratch.north.crossVectors(scratch.center, scratch.east).normalize()
    const visible = camera.position.length() < 2.72

    patchUniforms.forEach((uniforms, index) => {
      uniforms.uPatchCenter.value.copy(scratch.center)
      uniforms.uPatchEast.value.copy(scratch.east)
      uniforms.uPatchNorth.value.copy(scratch.north)
      if (meshRefs.current[index]) meshRefs.current[index]!.visible = visible
    })
  })

  return (
    <>
      {CLIPMAP_LEVELS.map((_, index) => (
        <mesh
          key={`${name}-clip-${index}-${segments}`}
          ref={(mesh) => { meshRefs.current[index] = mesh }}
          geometry={geometries[index]}
          frustumCulled={false}
          renderOrder={20 + index}
          dispose={null}
        >
          <shaderMaterial
            vertexShader={assemble(CLIPMAP_VERT)}
            fragmentShader={`#define CLIPMAP_PATCH\n${assemble(PLANET_FRAG)}`}
            uniforms={patchUniforms[index]}
            depthWrite
            depthTest
          />
        </mesh>
      ))}
    </>
  )
}

function TerrainPlanet({ spec, quality }: { spec: PlanetSpec; quality: number }) {
  const P = usePlanetParams(spec)
  const anchorRef = useRef<THREE.Group>(null)
  const meshRef = useRef<THREE.Mesh>(null)
  const matRef = useRef<THREE.ShaderMaterial>(null)
  const lastOctave = useRef(0)
  const activeLod = useRef(1)
  const geometries = useMemo(() => {
    // 전역 구체는 원거리/반대편만 담당한다. 근접 영역은 clipmap 링이 훨씬 조밀하게 덮는다.
    const details = quality < 0.6 ? [14, 20, 28] : quality < 0.9 ? [18, 26, 34] : [22, 32, 42]
    return details.map((detail) => new THREE.IcosahedronGeometry(1, detail))
  }, [quality])

  useEffect(() => {
    activeLod.current = 1
    if (meshRef.current) meshRef.current.geometry = geometries[1]
    return () => {
      geometries.forEach((geometry) => geometry.dispose())
    }
  }, [geometries])

  const uniforms = useMemo(() => {
    const pal = spec.surface.palette
    return {
      uSeed: { value: P.seed },
      uFreq: { value: P.freq },
      uMountain: { value: spec.surface.mountain_height },
      uSeaLevel: { value: P.seaLevel },
      uAmp: { value: P.amp },
      uOblate: { value: P.oblate },
      uIrregular: { value: P.irregular },
      uFeatureMode: { value: FEATURE_MODE[spec.surface.feature_type] ?? 0 },
      uFeatureStrength: { value: spec.surface.feature_scale },
      uCaveStrength: { value: caveStrength(spec) },
      uMaterialMode: { value: inferredMaterial(spec) },
      uBiomeContrast: { value: spec.surface.biome_contrast },
      uHumidity: { value: spec.climate.humidity },
      uVertexOctaves: { value: 5 },
      uDetailMix: { value: 0 },
      uOctaves: { value: quality < 0.6 ? 5 : quality < 0.9 ? 6 : 7 },
      uLightDir1: { value: SUN1_DIR },
      uLightColor1: { value: col(starColor(spec, 0)) },
      uLightDir2: { value: SUN2_DIR },
      uLightColor2: { value: col(starColor(spec, 1)) },
      uLight2On: { value: P.twoSuns ? 1 : 0 },
      uLightIntensity: { value: spec.star.intensity },
      uOceanDeep: { value: col(pal.ocean_deep) },
      uOceanShallow: { value: col(pal.ocean_shallow) },
      uShore: { value: col(pal.shore) },
      uLowland: { value: col(pal.lowland) },
      uMidland: { value: col(pal.midland) },
      uHighland: { value: col(pal.highland) },
      uPeak: { value: col(pal.peak) },
      uIce: { value: spec.surface.ice_coverage },
      uVegetation: { value: spec.surface.vegetation_coverage },
      uLava: { value: spec.surface.lava_activity },
      uCity: { value: spec.surface.city_lights },
      uAtmoDensity: { value: spec.atmosphere.present ? spec.atmosphere.density : 0 },
      uAtmoColor: { value: col(spec.atmosphere.color_hex) },
      uTempNorm: { value: P.tempNorm },
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [quality, spec])

  // 카메라가 가까울수록 고해상도 메시와 실제 근접 지질 변위를 함께 올린다.
  useFrame(({ camera }) => {
    if (!matRef.current || !meshRef.current) return
    const dist = camera.position.length()
    const lod = dist < 1.75 ? 2 : dist < 2.8 ? 1 : 0
    if (lod !== activeLod.current) {
      activeLod.current = lod
      meshRef.current.geometry = geometries[lod]
      matRef.current.uniforms.uVertexOctaves.value = lod === 2 ? 6 : lod === 1 ? 5 : 4
    }
    // 2.45R부터 서서히 나타나며 저공 뷰(1.32R)에서는 완전히 활성화된다.
    // vertex/fragment가 같은 uniform을 공유해 요철과 그 그림자가 어긋나지 않는다.
    matRef.current.uniforms.uDetailMix.value = 1 - THREE.MathUtils.smoothstep(dist, 1.28, 2.45)
    const maxOctaves = quality < 0.6 ? 5 : quality < 0.9 ? 6 : 7
    const oct = Math.round(THREE.MathUtils.clamp(8.2 - (dist - 1.05) * 1.25, 4, maxOctaves))
    if (oct !== lastOctave.current) {
      lastOctave.current = oct
      matRef.current.uniforms.uOctaves.value = oct
    }
  })

  return (
    <group ref={anchorRef}>
      <mesh ref={meshRef} geometry={geometries[1]} dispose={null}>
        <shaderMaterial
          key={`planet-${spec.planet.name}`}
          ref={matRef}
          vertexShader={assemble(PLANET_VERT)}
          fragmentShader={assemble(PLANET_FRAG)}
          uniforms={uniforms}
        />
      </mesh>
      <SphericalClipmap
        anchorRef={anchorRef}
        sharedUniforms={uniforms}
        quality={quality}
        name={spec.planet.name}
      />
    </group>
  )
}

function SolidPrecipitation({ spec, motionScale }: { spec: PlanetSpec; motionScale: number }) {
  const pointsRef = useRef<THREE.Points>(null)
  const materialRef = useRef<THREE.PointsMaterial>(null)
  const count = 360
  const active = /고체|우박|결정.*비|입자.*강수|solid|hail/i.test(
    `${spec.atmosphere.weather_summary} ${spec.climate.phenomena.join(' ')}`,
  )
  const data = useMemo(() => {
    const random = seededRandom(`${spec.planet.name}:precipitation`)
    const directions = new Float32Array(count * 3)
    const radii = new Float32Array(count)
    const positions = new Float32Array(count * 3)
    for (let i = 0; i < count; i++) {
      const y = random() * 2 - 1
      const azimuth = random() * Math.PI * 2
      const radial = Math.sqrt(1 - y * y)
      directions[i * 3] = Math.cos(azimuth) * radial
      directions[i * 3 + 1] = y
      directions[i * 3 + 2] = Math.sin(azimuth) * radial
      radii[i] = 1.055 + random() * 0.32
    }
    return { directions, radii, positions }
  }, [spec.planet.name])

  useFrame(({ camera }, delta) => {
    if (!active || !pointsRef.current || !materialRef.current) return
    const attribute = pointsRef.current.geometry.getAttribute('position') as THREE.BufferAttribute
    for (let i = 0; i < count; i++) {
      data.radii[i] -= delta * motionScale * (0.045 + (i % 7) * 0.004)
      if (data.radii[i] < 1.04) data.radii[i] = 1.36
      const radius = data.radii[i]
      data.positions[i * 3] = data.directions[i * 3] * radius
      data.positions[i * 3 + 1] = data.directions[i * 3 + 1] * radius
      data.positions[i * 3 + 2] = data.directions[i * 3 + 2] * radius
    }
    attribute.needsUpdate = true
    materialRef.current.opacity = 0.18 +
      (1 - THREE.MathUtils.smoothstep(camera.position.length(), 1.45, 3.2)) * 0.72
  })

  if (!active) return null
  return (
    <points ref={pointsRef} frustumCulled={false}>
      <bufferGeometry>
        <bufferAttribute attach="attributes-position" args={[data.positions, 3]} />
      </bufferGeometry>
      <pointsMaterial
        ref={materialRef}
        color={spec.surface.palette.peak}
        size={0.009}
        sizeAttenuation
        transparent
        opacity={0.2}
        depthWrite={false}
      />
    </points>
  )
}

function CloudShell({ spec, quality, motionScale }: { spec: PlanetSpec; quality: number; motionScale: number }) {
  const P = usePlanetParams(spec)
  const matRef = useRef<THREE.ShaderMaterial>(null)
  const uniforms = useMemo(
    () => ({
      uTime: { value: 0 },
      uCoverage: { value: spec.clouds.coverage },
      uSpeed: { value: spec.clouds.speed },
      uStorm: { value: spec.clouds.storminess },
      uBanding: {
        value: THREE.MathUtils.clamp((24 / Math.max(spec.planet.rotation_hours, 0.1) - 1) / 3, 0, 1),
      },
      uColor: { value: col(spec.clouds.color_hex) },
      uLightDir1: { value: SUN1_DIR },
      uLightColor1: { value: col(starColor(spec, 0)) },
      uLightDir2: { value: SUN2_DIR },
      uLightColor2: { value: col(starColor(spec, 1)) },
      uLight2On: { value: P.twoSuns ? 1 : 0 },
      uSeed: { value: P.seed.clone().multiplyScalar(0.7) },
      uNearFade: { value: 1 },
    }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [spec],
  )
  useFrame(({ clock, camera }) => {
    if (matRef.current) {
      matRef.current.uniforms.uTime.value = clock.elapsedTime * motionScale
      matRef.current.uniforms.uNearFade.value = THREE.MathUtils.smoothstep(
        camera.position.length(),
        1.1,
        1.52,
      )
    }
  })
  if (spec.clouds.coverage < 0.02 || !spec.atmosphere.present) return null
  const segments = quality < 0.6 ? [40, 24] : quality < 0.9 ? [56, 36] : [72, 48]
  return (
    <mesh scale={[1, 1 - P.oblate, 1]}>
      <sphereGeometry args={[1.07, segments[0], segments[1]]} />
      <shaderMaterial
        key={`clouds-${spec.planet.name}`}
        ref={matRef}
        vertexShader={CLOUDS_VERT}
        fragmentShader={assemble(CLOUDS_FRAG)}
        uniforms={uniforms}
        transparent
        depthWrite={false}
      />
    </mesh>
  )
}

function AtmosphereShell({ spec, quality }: { spec: PlanetSpec; quality: number }) {
  const P = usePlanetParams(spec)
  const matRef = useRef<THREE.ShaderMaterial>(null)
  const uniforms = useMemo(
    () => ({
      uColor: { value: col(spec.atmosphere.color_hex) },
      uDensity: { value: spec.atmosphere.density },
      uLightDir1: { value: SUN1_DIR },
      uNearFade: { value: 1 },
    }),
    [spec],
  )
  useFrame(({ camera }) => {
    if (matRef.current) {
      matRef.current.uniforms.uNearFade.value = THREE.MathUtils.smoothstep(
        camera.position.length(),
        1.14,
        1.68,
      )
    }
  })
  if (!spec.atmosphere.present || spec.atmosphere.density < 0.02) return null
  const segments = quality < 0.6 ? [32, 20] : quality < 0.9 ? [44, 28] : [56, 36]
  return (
    <mesh scale={[1, 1 - P.oblate, 1]}>
      <sphereGeometry args={[1.18, segments[0], segments[1]]} />
      <shaderMaterial
        key={`atmo-${spec.planet.name}`}
        ref={matRef}
        vertexShader={ATMO_VERT}
        fragmentShader={ATMO_FRAG}
        uniforms={uniforms}
        transparent
        side={THREE.BackSide}
        depthWrite={false}
        blending={THREE.AdditiveBlending}
      />
    </mesh>
  )
}

function RingSystem({ spec, quality }: { spec: PlanetSpec; quality: number }) {
  const P = usePlanetParams(spec)
  const uniforms = useMemo(
    () => ({
      uColor: { value: col(spec.rings.color_hex) },
      uInner: { value: spec.rings.inner_ratio },
      uOuter: { value: Math.max(spec.rings.outer_ratio, spec.rings.inner_ratio + 0.15) },
      uOpacity: { value: spec.rings.opacity },
      uLightDir1: { value: SUN1_DIR },
      uSeed: { value: P.seed.clone().multiplyScalar(0.3) },
    }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [spec],
  )
  if (!spec.rings.present) return null
  return (
    <mesh rotation={[-Math.PI / 2, 0, 0]}>
      <ringGeometry
        args={[
          spec.rings.inner_ratio,
          Math.max(spec.rings.outer_ratio, spec.rings.inner_ratio + 0.15),
          quality < 0.6 ? 96 : quality < 0.9 ? 144 : 192,
          4,
        ]}
      />
      <shaderMaterial
        key={`ring-${spec.planet.name}`}
        vertexShader={RING_VERT}
        fragmentShader={assemble(RING_FRAG)}
        uniforms={uniforms}
        transparent
        side={THREE.DoubleSide}
        depthWrite={false}
      />
    </mesh>
  )
}

function Moons({ spec, motionScale }: { spec: PlanetSpec; motionScale: number }) {
  const group = useRef<THREE.Group>(null)
  const moons = useMemo(
    () =>
      spec.moons.slice(0, 6).map((m, i) => ({
        ...m,
        orbitR: 1.7 + m.distance_ratio * 0.11,
        size: THREE.MathUtils.clamp(m.size_ratio * 0.45, 0.02, 0.32),
        speed: 0.25 / Math.sqrt(1.7 + m.distance_ratio * 0.11),
        phase: i * 2.1,
        incline: (i % 2 === 0 ? 1 : -1) * (0.1 + i * 0.07),
      })),
    [spec],
  )
  useFrame(({ clock }) => {
    if (!group.current) return
    const t = clock.elapsedTime * motionScale
    group.current.children.forEach((child, i) => {
      const m = moons[i]
      if (!m) return
      const a = t * m.speed + m.phase
      child.position.set(
        Math.cos(a) * m.orbitR,
        Math.sin(a * 0.9) * m.orbitR * Math.sin(m.incline) * 0.4,
        Math.sin(a) * m.orbitR,
      )
      child.rotation.y = t * 0.1
    })
  })
  if (moons.length === 0) return null
  return (
    <group ref={group}>
      {moons.map((m, i) => (
        <mesh key={i}>
          <icosahedronGeometry args={[m.size, 3]} />
          <meshStandardMaterial color={m.color_hex} roughness={0.95} metalness={0} flatShading />
        </mesh>
      ))}
    </group>
  )
}

function makeGlowTexture(): THREE.Texture {
  const c = document.createElement('canvas')
  c.width = c.height = 128
  const ctx = c.getContext('2d')!
  const g = ctx.createRadialGradient(64, 64, 0, 64, 64, 64)
  g.addColorStop(0, 'rgba(255,255,255,1)')
  g.addColorStop(0.25, 'rgba(255,255,255,0.55)')
  g.addColorStop(1, 'rgba(255,255,255,0)')
  ctx.fillStyle = g
  ctx.fillRect(0, 0, 128, 128)
  const tex = new THREE.CanvasTexture(c)
  return tex
}

function Suns({ spec }: { spec: PlanetSpec }) {
  const glow = useMemo(makeGlowTexture, [])
  // R3F는 선언적으로 만든 객체만 자동 dispose하므로 직접 만든 CanvasTexture는 수동 해제한다.
  useEffect(() => () => glow.dispose(), [glow])
  const suns = useMemo(() => {
    const arr = [{ dir: SUN1_DIR, color: starColor(spec, 0), dist: 60, size: 2.6 }]
    if (spec.star.count >= 2) arr.push({ dir: SUN2_DIR, color: starColor(spec, 1), dist: 75, size: 1.8 })
    return arr
  }, [spec])
  return (
    <>
      {suns.map((s, i) => {
        const pos = s.dir.clone().multiplyScalar(s.dist)
        return (
          <group key={i} position={pos.toArray()}>
            <mesh>
              <sphereGeometry args={[s.size, 32, 24]} />
              <meshBasicMaterial color={s.color} toneMapped={false} />
            </mesh>
            <sprite scale={[s.size * 9, s.size * 9, 1]}>
              <spriteMaterial
                map={glow}
                color={s.color}
                transparent
                blending={THREE.AdditiveBlending}
                depthWrite={false}
              />
            </sprite>
          </group>
        )
      })}
      {/* 위성 등 표준 재질용 라이트 (행성 본체는 자체 셰이더 조명) */}
      <directionalLight
        position={SUN1_DIR.clone().multiplyScalar(10).toArray()}
        color={starColor(spec, 0)}
        intensity={2.2 * spec.star.intensity}
      />
      {spec.star.count >= 2 && (
        <directionalLight
          position={SUN2_DIR.clone().multiplyScalar(10).toArray()}
          color={starColor(spec, 1)}
          intensity={1.1}
        />
      )}
      <ambientLight intensity={0.06} />
    </>
  )
}

export default function PlanetSystem({
  spec,
  quality,
  motionScale = 1,
}: {
  spec: PlanetSpec
  quality: number
  motionScale?: number
}) {
  const P = usePlanetParams(spec)
  const spinRef = useRef<THREE.Group>(null)

  useFrame((_, delta) => {
    if (spinRef.current) spinRef.current.rotation.y += P.rotSpeed * delta * motionScale
  })

  return (
    <>
      <Suns spec={spec} />
      {/* 자전축 기울기 그룹 → 자전 그룹 */}
      <group rotation={[0, 0, P.tiltRad]}>
        <group ref={spinRef}>
          <TerrainPlanet spec={spec} quality={quality} />
          <SolidPrecipitation spec={spec} motionScale={motionScale} />
          <CloudShell spec={spec} quality={quality} motionScale={motionScale} />
        </group>
        <AtmosphereShell spec={spec} quality={quality} />
        <RingSystem spec={spec} quality={quality} />
        <Moons spec={spec} motionScale={motionScale} />
      </group>
    </>
  )
}
