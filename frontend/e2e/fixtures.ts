import type { Page } from '@playwright/test'

const transparentPng = Buffer.from(
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=',
  'base64',
)

const spec = {
  planet: {
    name: '아르카디아',
    shape: 'oblate',
    oblateness: 0.08,
    radius_km: 7200,
    gravity_g: 1.18,
    rotation_hours: 18,
    axial_tilt_deg: 14,
  },
  star: { count: 2, color_hex: '#ffe6c4', colors_hex: ['#ffe6c4', '#c7ddff'], intensity: 1.1 },
  atmosphere: {
    present: true,
    density: 0.72,
    color_hex: '#7799cc',
    composition: '질소와 아르곤',
    weather_summary: '온대성 구름대',
  },
  climate: {
    avg_temp_c: 16,
    temp_min_c: -22,
    temp_max_c: 44,
    humidity: 0.56,
    phenomena: ['푸른 오로라'],
  },
  surface: {
    ocean_coverage: 0.48,
    terrain_roughness: 0.55,
    mountain_height: 0.44,
    ice_coverage: 0.08,
    vegetation_coverage: 0.62,
    lava_activity: 0.01,
    city_lights: 0.2,
    feature_type: 'archipelago',
    feature_scale: 0.52,
    biome_contrast: 0.58,
    material_type: 'mixed',
    landmarks: ['giant_flora'],
    visual_prompt: 'emerald archipelago from orbit',
    palette: {
      ocean_deep: '#09284c',
      ocean_shallow: '#167a96',
      shore: '#d8ca8a',
      lowland: '#3d7448',
      midland: '#607143',
      highland: '#7b745f',
      peak: '#e7e3d6',
    },
    description: '비취빛 군도와 거대 수목이 이어진 행성',
  },
  clouds: { coverage: 0.38, color_hex: '#ffffff', speed: 0.25, storminess: 0.12 },
  rings: { present: false, color_hex: '#c9b797', inner_ratio: 1.4, outer_ratio: 2.2, opacity: 0.5 },
  moons: [{ name: '세라', size_ratio: 0.18, distance_ratio: 8, color_hex: '#aaaaaa' }],
  inhabitants: [
    {
      name: '루멘',
      category: '지성 종족',
      height_m: 1.7,
      appearance: '은빛 피부와 네 개의 눈',
      physiology: '저조도 시야',
      culture: '수목 도시 공동체',
      gravity_adaptation: '넓은 발과 치밀한 골격',
      portrait_prompt: 'a silver skinned Lumen resident',
    },
    {
      name: '모라',
      category: '수중 종족',
      height_m: 1.2,
      appearance: '청록색 비늘과 지느러미',
      physiology: '양서 호흡',
      culture: '산호 기록 보관소',
      gravity_adaptation: '유선형 체형',
      portrait_prompt: 'an amphibious Mora resident',
    },
  ],
  inferences: [
    {
      topic: '생태',
      claim: '대규모 수목 생태계가 존재한다.',
      confidence: 'inferred',
      evidence_quote: '하늘을 가리는 수목',
      reasoning: '지표 묘사와 대기 조성을 함께 반영했다.',
    },
  ],
}

const physics = {
  mass_kg: 8.42e24,
  mass_earths: 1.41,
  surface_gravity_ms2: 11.57,
  density_g_cm3: 5.4,
  circumference_km: 45239,
  volume_earths: 1.44,
  escape_velocity_kms: 12.9,
  low_orbit_period_min: 102,
  equator_speed_kmh: 2513,
  rotational_flattening_theory: 0.04,
  centrifugal_acceleration_ms2: 0.24,
  effective_equator_gravity_g: 1.16,
  synchronous_orbit_altitude_km: 31000,
  breakup_period_hours: 2.5,
  surface_area_earths: 1.28,
  human_weight_kg: 82.6,
  can_hold_atmosphere: true,
  day_length_vs_earth: 0.75,
}

export const gallery = [
  {
    id: 'gallery-world',
    name: '아르카디아',
    description: spec.surface.description,
    feature_type: spec.surface.feature_type,
    gravity_g: spec.planet.gravity_g,
    inhabitant_count: spec.inhabitants.length,
    cover_image_url: '/generated/gallery-cover.png',
    created_at: '2026-07-15T00:00:00Z',
  },
]

export function savedPlanet(id = 'shared-world') {
  return {
    id,
    spec,
    physics,
    model: 'browser-mock',
    cover_image_url: '/generated/planet.png',
    image_assets: {
      planet: { url: '/generated/planet.png', seed: 10, provider: 'fixture', model: 'fixture-v1' },
      surface: { url: '/generated/surface.png', seed: 11, provider: 'fixture', model: 'fixture-v1' },
      'inhabitant:0': { url: '/generated/inhabitant-lumen.png', seed: 12, provider: 'fixture', model: 'fixture-v1' },
      'inhabitant:1': { url: '/generated/inhabitant-mora.png', seed: 13, provider: 'fixture', model: 'fixture-v1' },
    },
    is_public: true,
    created_at: '2026-07-15T00:00:00Z',
    updated_at: '2026-07-15T00:00:00Z',
  }
}

export async function installBrowserMocks(page: Page) {
  const unexpectedApiRequests: string[] = []

  // R3F normally renders at the display refresh rate. Software WebGL on a
  // hosted runner needs only a few frames to prove the real canvas path, so
  // throttle RAF before application code runs and keep UI/network work timely.
  await page.addInitScript(() => {
    window.requestAnimationFrame = (callback) => window.setTimeout(
      () => callback(window.performance.now()),
      100,
    )
    window.cancelAnimationFrame = (handle) => window.clearTimeout(handle)
  })

  await page.route('**/generated/*.png', (route) => route.fulfill({
    status: 200,
    contentType: 'image/png',
    body: transparentPng,
  }))

  await page.route('**/api/**', async (route) => {
    const request = route.request()
    const url = new URL(request.url())

    if (request.method() === 'GET' && url.pathname === '/api/image/status') {
      await route.fulfill({ json: { available: true, provider: 'mock', model: 'mock-v1', message: 'ready' } })
      return
    }
    if (request.method() === 'GET' && url.pathname === '/api/planets') {
      await route.fulfill({ json: gallery })
      return
    }
    const planetMatch = url.pathname.match(/^\/api\/planets\/([^/]+)$/)
    if (request.method() === 'GET' && planetMatch) {
      await route.fulfill({ json: savedPlanet(decodeURIComponent(planetMatch[1])) })
      return
    }

    unexpectedApiRequests.push(`${request.method()} ${url.pathname}`)
    await route.fulfill({ status: 501, json: { detail: 'Unmocked browser request' } })
  })

  return unexpectedApiRequests
}

export function captureSevereBrowserErrors(page: Page) {
  const errors: string[] = []
  page.on('console', (message) => {
    if (message.type() === 'error') errors.push(`console: ${message.text()}`)
  })
  page.on('pageerror', (error) => errors.push(`page: ${error.message}`))
  return errors
}
