import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Canvas, useFrame, useThree } from '@react-three/fiber'
import { OrbitControls, PerformanceMonitor, Stars } from '@react-three/drei'
import type { OrbitControls as OrbitControlsImpl } from 'three-stdlib'
import * as THREE from 'three'
import { useTerra } from '../store'
import type { PlanetSpec } from '../types'
import PlanetSystem from './PlanetSystem'

// 분석 전 대기 화면용 기본 행성
const IDLE_SPEC: PlanetSpec = {
  planet: { name: 'Terra Incognita', shape: 'sphere', oblateness: 0.01, radius_km: 6371, gravity_g: 1, rotation_hours: 30, axial_tilt_deg: 20 },
  star: { count: 1, color_hex: '#fff2dd', colors_hex: ['#fff2dd'], intensity: 1 },
  atmosphere: { present: true, density: 0.45, color_hex: '#6fa8ff', composition: '', weather_summary: '' },
  climate: { avg_temp_c: 12, temp_min_c: -30, temp_max_c: 40, humidity: 0.5, phenomena: [] },
  surface: {
    ocean_coverage: 0.62, terrain_roughness: 0.5, mountain_height: 0.55, ice_coverage: 0.12,
    vegetation_coverage: 0.45, lava_activity: 0, city_lights: 0,
    feature_type: 'continents', feature_scale: 0.5, biome_contrast: 0.55,
    palette: { ocean_deep: '#0a2a52', ocean_shallow: '#19629c', shore: '#c8b98c', lowland: '#48734a', midland: '#6f6b4a', highland: '#847e70', peak: '#eef0f2' },
    description: '',
  },
  clouds: { coverage: 0.42, color_hex: '#ffffff', speed: 0.3, storminess: 0.15 },
  rings: { present: false, color_hex: '#c9b797', inner_ratio: 1.4, outer_ratio: 2.2, opacity: 0.6 },
  moons: [{ name: '', size_ratio: 0.2, distance_ratio: 10, color_hex: '#9a9a9a' }],
  inhabitants: [],
  inferences: [],
}

const VIEW_DIST: Record<'orbit' | 'low' | 'north', number> = { orbit: 3.6, low: 1.32, north: 3.1 }

function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(() => window.matchMedia(query).matches)
  useEffect(() => {
    const media = window.matchMedia(query)
    const update = () => setMatches(media.matches)
    update()
    media.addEventListener('change', update)
    return () => media.removeEventListener('change', update)
  }, [query])
  return matches
}

function CameraRig({
  onProximityChange,
  reducedMotion,
}: {
  onProximityChange: (near: boolean) => void
  reducedMotion: boolean
}) {
  const controlsRef = useRef<OrbitControlsImpl>(null)
  const { camera } = useThree()
  const viewRequest = useTerra((s) => s.viewRequest)
  const target = useRef<THREE.Vector3 | null>(null)
  const lastNonce = useRef(0)
  const wasNear = useRef(false)

  useEffect(() => {
    if (viewRequest.nonce !== lastNonce.current) {
      lastNonce.current = viewRequest.nonce
      const distance = VIEW_DIST[viewRequest.mode]
      target.current = viewRequest.mode === 'north'
        ? new THREE.Vector3(0.04, distance, 0.04)
        : camera.position.clone().setLength(distance)
    }
  }, [camera, viewRequest])

  useFrame((_, delta) => {
    // 뷰 전환 애니메이션: 현재 시선 방향을 유지한 채 반경만 이동
    if (target.current !== null) {
      if (reducedMotion) camera.position.copy(target.current)
      else camera.position.lerp(target.current, 1 - Math.exp(-3.2 * delta))
      if (camera.position.distanceTo(target.current) < 0.01) target.current = null
    }
    const dist = camera.position.length()
    const isNear = dist < 1.85
    if (isNear !== wasNear.current) {
      wasNear.current = isNear
      onProximityChange(isNear)
    }
    // 저공비행 감성: 표면에 가까울수록 회전/줌 속도를 줄인다
    const c = controlsRef.current
    if (c) {
      c.rotateSpeed = THREE.MathUtils.clamp((dist - 1.02) * 0.55, 0.045, 0.9)
      c.zoomSpeed = THREE.MathUtils.clamp((dist - 1.02) * 0.7, 0.12, 1.1)
    }
  })

  return (
    <OrbitControls
      ref={controlsRef}
      makeDefault
      enablePan={false}
      enableDamping
      dampingFactor={0.07}
      minDistance={1.16}
      maxDistance={40}
    />
  )
}

export default function PlanetScene() {
  const result = useTerra((s) => s.result)
  const renderQuality = useTerra((s) => s.renderQuality)
  const spec = result?.spec ?? IDLE_SPEC
  const [performanceFactor, setPerformanceFactor] = useState(0.5)
  const [nearSurface, setNearSurface] = useState(false)
  const [devicePixelRatio, setDevicePixelRatio] = useState(() => window.devicePixelRatio || 1)
  const [pageVisible, setPageVisible] = useState(() => !document.hidden)
  const reducedMotion = useMediaQuery('(prefers-reduced-motion: reduce)')
  const handleProximityChange = useCallback((near: boolean) => setNearSurface(near), [])

  useEffect(() => {
    const updatePixelRatio = () => setDevicePixelRatio(window.devicePixelRatio || 1)
    window.addEventListener('resize', updatePixelRatio, { passive: true })
    return () => window.removeEventListener('resize', updatePixelRatio)
  }, [])

  useEffect(() => {
    const updateVisibility = () => setPageVisible(!document.hidden)
    document.addEventListener('visibilitychange', updateVisibility)
    return () => document.removeEventListener('visibilitychange', updateVisibility)
  }, [])

  const profile = useMemo(() => {
    if (renderQuality === 'performance') {
      return { dpr: 0.72, sceneQuality: 0.5, stars: 2200 }
    }
    if (renderQuality === 'quality') {
      return {
        dpr: Math.min(devicePixelRatio, 1.5),
        sceneQuality: 1,
        stars: 5000,
      }
    }
    // Retina 2x는 1x보다 fragment 연산이 4배다. 자동 모드의 상한을 1.25로 제한한다.
    const sceneQuality = nearSurface
      ? performanceFactor > 0.62 ? 1 : 0.72
      : performanceFactor < 0.38 ? 0.5 : performanceFactor > 0.78 ? 1 : 0.72
    const adaptiveDpr = 0.72 + performanceFactor * 0.53
    const dprFloor = nearSurface ? 1.0 : 0.72
    return {
      dpr: Math.min(devicePixelRatio, Math.max(dprFloor, adaptiveDpr)),
      sceneQuality,
      stars: sceneQuality < 0.6 ? 2200 : sceneQuality < 0.9 ? 3400 : 5000,
    }
  }, [devicePixelRatio, nearSurface, performanceFactor, renderQuality])

  return (
    <Canvas
      dpr={profile.dpr}
      frameloop={pageVisible ? 'always' : 'never'}
      camera={{ fov: 42, near: 0.01, far: 300, position: [0.8, 0.9, 3.6] }}
      gl={{ antialias: profile.dpr <= 1, alpha: false, stencil: false, powerPreference: 'high-performance' }}
      style={{ position: 'absolute', inset: 0 }}
    >
      <color attach="background" args={['#02030a']} />
      <PerformanceMonitor
        iterations={6}
        ms={350}
        flipflops={3}
        onChange={({ factor }) => setPerformanceFactor(factor)}
        onFallback={() => setPerformanceFactor(0)}
      />
      <Stars
        radius={120}
        depth={60}
        count={profile.stars}
        factor={3}
        saturation={0.35}
        fade
        speed={reducedMotion ? 0 : 0.2}
      />
      <PlanetSystem
        key={spec.planet.name + spec.surface.description}
        spec={spec}
        quality={profile.sceneQuality}
        motionScale={reducedMotion ? 0 : 1}
      />
      <CameraRig onProximityChange={handleProximityChange} reducedMotion={reducedMotion} />
    </Canvas>
  )
}
