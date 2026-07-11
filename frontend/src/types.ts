// 백엔드 app/schema.py 와 동기 유지되는 계약 타입

export interface Planet {
  name: string
  shape: 'sphere' | 'oblate' | 'irregular'
  oblateness: number
  radius_km: number
  gravity_g: number
  rotation_hours: number
  axial_tilt_deg: number
}

export interface Star {
  count: number
  color_hex: string
  colors_hex: string[]
  intensity: number
}

export interface Atmosphere {
  present: boolean
  density: number
  color_hex: string
  composition: string
  weather_summary: string
}

export interface Climate {
  avg_temp_c: number
  temp_min_c: number
  temp_max_c: number
  humidity: number
  phenomena: string[]
}

export interface Palette {
  ocean_deep: string
  ocean_shallow: string
  shore: string
  lowland: string
  midland: string
  highland: string
  peak: string
}

export interface Surface {
  ocean_coverage: number
  terrain_roughness: number
  mountain_height: number
  ice_coverage: number
  vegetation_coverage: number
  lava_activity: number
  city_lights: number
  feature_type: 'continents' | 'archipelago' | 'cratered' | 'canyons' | 'dunes' | 'crystalline' | 'volcanic' | 'artificial'
  feature_scale: number
  biome_contrast: number
  material_type?: 'rock' | 'sand' | 'ice' | 'crystal' | 'metal' | 'organic' | 'volcanic' | 'mixed'
  landmarks?: Array<'rock_spires' | 'crystal_fields' | 'cave_openings' | 'volcanic_vents' | 'dune_ridges' | 'artificial_structures' | 'giant_flora' | 'ice_spires'>
  visual_prompt?: string
  palette: Palette
  description: string
}

export interface Clouds {
  coverage: number
  color_hex: string
  speed: number
  storminess: number
}

export interface Rings {
  present: boolean
  color_hex: string
  inner_ratio: number
  outer_ratio: number
  opacity: number
}

export interface Moon {
  name: string
  size_ratio: number
  distance_ratio: number
  color_hex: string
}

export interface Inhabitant {
  name: string
  category: string
  height_m: number
  appearance: string
  physiology: string
  culture: string
  gravity_adaptation: string
  portrait_prompt: string
}

export interface Inference {
  topic: string
  claim: string
  confidence: 'stated' | 'inferred' | 'speculative'
  evidence_quote: string
  reasoning: string
}

export interface PlanetSpec {
  planet: Planet
  star: Star
  atmosphere: Atmosphere
  climate: Climate
  surface: Surface
  clouds: Clouds
  rings: Rings
  moons: Moon[]
  inhabitants: Inhabitant[]
  inferences: Inference[]
}

export interface DerivedPhysics {
  mass_kg: number
  mass_earths: number
  surface_gravity_ms2: number
  density_g_cm3: number
  circumference_km: number
  volume_earths: number
  escape_velocity_kms: number
  low_orbit_period_min: number
  equator_speed_kmh: number
  rotational_flattening_theory: number
  centrifugal_acceleration_ms2: number
  effective_equator_gravity_g: number
  synchronous_orbit_altitude_km: number
  breakup_period_hours: number
  surface_area_earths: number
  human_weight_kg: number
  can_hold_atmosphere: boolean
  day_length_vs_earth: number
}

export interface AnalyzeResponse {
  spec: PlanetSpec
  physics: DerivedPhysics
  model: string
}

export interface ImageProviderStatus {
  available: boolean
  provider: string
  model: string
  message: string
}

export type ImageQuality = 'fast' | 'balanced' | 'quality'

export type ImageJobPhase =
  | 'queued'
  | 'generating'
  | 'verifying'
  | 'refining'
  | 'upscaling'
  | 'completed'
  | 'failed'

export type CompatibleImageJobStatus = ImageJobPhase | 'running'

export interface GeneratedImage {
  url: string
  seed: number
  provider: string
  model: string
  quality?: ImageQuality
  quality_score?: number | null
  verification_notes?: string | string[] | null
}

export interface ImageJob {
  id: string
  status: CompatibleImageJobStatus
  created_at: number
  updated_at: number
  kind: 'planet' | 'surface' | 'inhabitant'
  url: string | null
  seed: number | null
  error: string | null
  provider: string
  model: string
  quality?: ImageQuality | null
  candidate_current?: number | null
  candidate_total?: number | null
  quality_score?: number | null
  verification_notes?: string | string[] | null
}

export type ImageJobProgress = Pick<
  ImageJob,
  | 'status'
  | 'quality'
  | 'candidate_current'
  | 'candidate_total'
  | 'quality_score'
  | 'verification_notes'
>

export interface SavedPlanetSummary {
  id: string
  name: string
  description: string
  feature_type: Surface['feature_type']
  gravity_g: number
  inhabitant_count: number
  cover_image_url: string | null
  created_at: string
}

export interface SavedPlanet extends AnalyzeResponse {
  id: string
  cover_image_url: string | null
  image_assets: Record<string, GeneratedImage>
  is_public: boolean
  created_at: string
  updated_at: string
  edit_token?: string
}
