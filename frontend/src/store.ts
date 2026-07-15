import { create } from 'zustand'
import {
  analyzeText,
  assetUrl,
  generateArtwork,
  getGallery,
  getImageStatus,
  getSavedPlanet,
  savePlanet,
  updateSavedImage,
} from './api'
import type {
  AnalyzeResponse,
  GeneratedImage,
  ImageJobProgress,
  ImageProviderStatus,
  ImageQuality,
  PlanetSpec,
  SavedPlanetSummary,
} from './types'

export type ViewMode = 'orbit' | 'low' | 'north'
export type RenderQuality = 'auto' | 'performance' | 'quality'

const RENDER_QUALITY_KEY = 'terra:render-quality'
const IMAGE_QUALITY_KEY = 'terra:image-quality'
const PANEL_OPEN_KEY = 'terra:panel-open'

function readChoice<T extends string>(key: string, allowed: readonly T[], fallback: T): T {
  try {
    const value = window.localStorage.getItem(key)
    return allowed.includes(value as T) ? value as T : fallback
  } catch {
    return fallback
  }
}

function readPanelOpen(): boolean {
  try {
    return window.localStorage.getItem(PANEL_OPEN_KEY) !== 'false'
  } catch {
    return true
  }
}

function persistPreference(key: string, value: string | boolean) {
  try {
    window.localStorage.setItem(key, String(value))
  } catch {
    // 저장 공간이 차단된 환경에서는 현재 세션 상태만 유지한다.
  }
}

// 진행 중인 이미지 생성 폴링을 취소하기 위한 컨트롤러. 렌더에 영향을 주지 않도록 스토어 밖에 둔다.
const genControllers = new Map<string, AbortController>()
let analysisController: AbortController | null = null
let planetLoadController: AbortController | null = null
let saveController: AbortController | null = null
let galleryRequestVersion = 0

function abortAllGenerations() {
  for (const controller of genControllers.values()) controller.abort()
  genControllers.clear()
}

function abortAnalysis() {
  analysisController?.abort()
  analysisController = null
}

function abortPlanetLoad() {
  planetLoadController?.abort()
  planetLoadController = null
}

function abortSave() {
  saveController?.abort()
  saveController = null
}

interface TerraState {
  result: AnalyzeResponse | null
  draftText: string
  loading: boolean
  error: string | null
  viewRequest: { mode: ViewMode; nonce: number }
  panelOpen: boolean
  renderQuality: RenderQuality
  imageStatus: ImageProviderStatus | null
  imageQuality: ImageQuality
  images: Record<string, GeneratedImage>
  generating: Record<string, boolean>
  imagePhases: Record<string, 'requesting' | ImageJobProgress['status']>
  imageProgress: Record<string, ImageJobProgress>
  imageErrors: Record<string, string>
  gallery: SavedPlanetSummary[]
  galleryLoading: boolean
  galleryError: string | null
  savedPlanetId: string | null
  savedEditToken: string | null
  savingPlanet: boolean
  saveError: string | null
  setPanelOpen: (open: boolean) => void
  setDraftText: (text: string) => void
  cycleRenderQuality: () => void
  requestView: (mode: ViewMode) => void
  loadImageStatus: () => Promise<void>
  setImageQuality: (quality: ImageQuality) => void
  generateImage: (spec: PlanetSpec, kind: 'planet' | 'surface' | 'inhabitant', index?: number) => Promise<void>
  loadGallery: (signal?: AbortSignal) => Promise<void>
  saveCurrentPlanet: () => Promise<void>
  openSavedPlanet: (id: string) => Promise<boolean>
  analyze: (text: string) => Promise<void>
  reset: () => void
}

export const useTerra = create<TerraState>((set, get) => ({
  result: null,
  draftText: '',
  loading: false,
  error: null,
  viewRequest: { mode: 'orbit', nonce: 0 },
  panelOpen: readPanelOpen(),
  renderQuality: readChoice<RenderQuality>(RENDER_QUALITY_KEY, ['auto', 'performance', 'quality'], 'auto'),
  imageStatus: null,
  imageQuality: readChoice<ImageQuality>(IMAGE_QUALITY_KEY, ['fast', 'balanced', 'quality'], 'balanced'),
  images: {},
  generating: {},
  imagePhases: {},
  imageProgress: {},
  imageErrors: {},
  gallery: [],
  galleryLoading: false,
  galleryError: null,
  savedPlanetId: null,
  savedEditToken: null,
  savingPlanet: false,
  saveError: null,
  setPanelOpen: (panelOpen) => {
    persistPreference(PANEL_OPEN_KEY, panelOpen)
    set({ panelOpen })
  },
  setDraftText: (draftText) => set({ draftText }),
  cycleRenderQuality: () =>
    set((s) => {
      const renderQuality =
        s.renderQuality === 'auto'
          ? 'performance'
          : s.renderQuality === 'performance'
            ? 'quality'
            : 'auto'
      persistPreference(RENDER_QUALITY_KEY, renderQuality)
      return { renderQuality }
    }),
  requestView: (mode) =>
    set((s) => ({ viewRequest: { mode, nonce: s.viewRequest.nonce + 1 } })),
  loadImageStatus: async () => {
    try {
      set({ imageStatus: await getImageStatus() })
    } catch (e) {
      set({
        imageStatus: {
          available: false,
          provider: 'mflux',
          model: 'unknown',
          message: e instanceof Error ? e.message : String(e),
        },
      })
    }
  },
  setImageQuality: (imageQuality) => {
    persistPreference(IMAGE_QUALITY_KEY, imageQuality)
    set({ imageQuality })
  },
  generateImage: async (spec, kind, index) => {
    const key = kind === 'inhabitant' ? `inhabitant:${index}` : kind
    // 같은 키의 이전 생성을 취소하고 새 컨트롤러를 등록한다.
    genControllers.get(key)?.abort()
    const controller = new AbortController()
    genControllers.set(key, controller)
    set((s) => ({
      generating: { ...s.generating, [key]: true },
      imagePhases: { ...s.imagePhases, [key]: 'requesting' },
      imageProgress: Object.fromEntries(
        Object.entries(s.imageProgress).filter(([progressKey]) => progressKey !== key),
      ) as TerraState['imageProgress'],
      imageErrors: { ...s.imageErrors, [key]: '' },
    }))
    try {
      const image = await generateArtwork(
        spec,
        kind,
        index,
        get().imageQuality,
        (progress) => {
          if (controller.signal.aborted) return
          set((s) => ({
            imagePhases: { ...s.imagePhases, [key]: progress.status },
            imageProgress: { ...s.imageProgress, [key]: progress },
          }))
        },
        controller.signal,
      )
      // 대기 중 analyze/reset/openSavedPlanet 등으로 취소되었으면 이전 행성 결과를 새 상태에 주입하지 않는다.
      if (controller.signal.aborted) return
      set((s) => ({
        images: { ...s.images, [key]: image },
        generating: { ...s.generating, [key]: false },
        imagePhases: Object.fromEntries(
          Object.entries(s.imagePhases).filter(([phaseKey]) => phaseKey !== key),
        ) as TerraState['imagePhases'],
        imageProgress: Object.fromEntries(
          Object.entries(s.imageProgress).filter(([progressKey]) => progressKey !== key),
        ) as TerraState['imageProgress'],
      }))
      const { savedPlanetId, savedEditToken } = get()
      if (savedPlanetId && savedEditToken) {
        try {
          await updateSavedImage(savedPlanetId, savedEditToken, key, image, controller.signal)
          if (controller.signal.aborted) return
          if (kind === 'planet') await get().loadGallery()
        } catch (e) {
          if (controller.signal.aborted) return
          set({ saveError: e instanceof Error ? e.message : String(e) })
        }
      }
    } catch (e) {
      // 사용자가 다른 화면으로 넘어가 취소된 경우는 오류로 표시하지 않는다.
      if (controller.signal.aborted || (e instanceof DOMException && e.name === 'AbortError')) {
        return
      }
      set((s) => ({
        generating: { ...s.generating, [key]: false },
        imagePhases: Object.fromEntries(
          Object.entries(s.imagePhases).filter(([phaseKey]) => phaseKey !== key),
        ) as TerraState['imagePhases'],
        imageProgress: Object.fromEntries(
          Object.entries(s.imageProgress).filter(([progressKey]) => progressKey !== key),
        ) as TerraState['imageProgress'],
        imageErrors: {
          ...s.imageErrors,
          [key]: e instanceof Error ? e.message : String(e),
        },
      }))
    } finally {
      if (genControllers.get(key) === controller) genControllers.delete(key)
    }
  },
  loadGallery: async (signal) => {
    const requestVersion = ++galleryRequestVersion
    set({ galleryLoading: true, galleryError: null })
    try {
      const gallery = await getGallery(signal)
      if (signal?.aborted || requestVersion !== galleryRequestVersion) return
      set({ gallery, galleryLoading: false })
    } catch (e) {
      if (signal?.aborted || requestVersion !== galleryRequestVersion) return
      set({
        galleryLoading: false,
        galleryError: e instanceof Error ? e.message : String(e),
      })
    }
  },
  saveCurrentPlanet: async () => {
    const { result, images } = get()
    if (!result) return
    abortSave()
    const controller = new AbortController()
    saveController = controller
    set({ savingPlanet: true, saveError: null })
    try {
      const saved = await savePlanet(result, images, controller.signal)
      if (controller.signal.aborted) return
      if (saved.edit_token) {
        try {
          window.localStorage.setItem(`terra:edit:${saved.id}`, saved.edit_token)
        } catch {
          // 현재 탭에서는 토큰을 유지하되 저장소가 차단되면 재방문 편집만 비활성화된다.
        }
      }
      set({
        savedPlanetId: saved.id,
        savedEditToken: saved.edit_token ?? null,
        savingPlanet: false,
      })
      window.history.replaceState(null, '', `/?planet=${encodeURIComponent(saved.id)}`)
      await get().loadGallery()
    } catch (e) {
      if (controller.signal.aborted) return
      set({
        savingPlanet: false,
        saveError: e instanceof Error ? e.message : String(e),
      })
    } finally {
      if (saveController === controller) saveController = null
    }
  },
  openSavedPlanet: async (id) => {
    abortAllGenerations()
    abortAnalysis()
    abortPlanetLoad()
    abortSave()
    const controller = new AbortController()
    planetLoadController = controller
    set({
      loading: true,
      error: null,
      saveError: null,
      savingPlanet: false,
      generating: {},
      imagePhases: {},
      imageProgress: {},
      imageErrors: {},
    })
    try {
      const saved = await getSavedPlanet(id, controller.signal)
      if (controller.signal.aborted) return false
      const images: Record<string, GeneratedImage> = Object.fromEntries(
        Object.entries(saved.image_assets ?? {}).map(([key, image]) => [
          key,
          { ...image, url: assetUrl(image.url) },
        ]),
      )
      if (!images.planet && saved.cover_image_url) {
        images.planet = {
          url: assetUrl(saved.cover_image_url),
          seed: 0,
          provider: 'gallery',
          model: saved.model,
        }
      }
      let savedEditToken: string | null = null
      try {
        savedEditToken = window.localStorage.getItem(`terra:edit:${saved.id}`) || null
      } catch {
        // 로컬 저장소가 차단된 공개 페이지도 읽기 전용으로 정상 표시한다.
      }
      set({
        result: { spec: saved.spec, physics: saved.physics, model: saved.model },
        images,
        loading: false,
        savedPlanetId: saved.id,
        savedEditToken,
        // 이전 행성의 진행 중 생성 UI 상태가 새로 연 행성에 남지 않도록 초기화한다.
        generating: {},
        imagePhases: {},
        imageProgress: {},
        imageErrors: {},
      })
      window.history.replaceState(null, '', `/?planet=${encodeURIComponent(saved.id)}`)
      return true
    } catch (e) {
      if (controller.signal.aborted) return false
      set({ error: e instanceof Error ? e.message : String(e), loading: false })
      return false
    } finally {
      if (planetLoadController === controller) planetLoadController = null
    }
  },
  analyze: async (text: string) => {
    abortAllGenerations()
    abortPlanetLoad()
    abortAnalysis()
    abortSave()
    const controller = new AbortController()
    analysisController = controller
    set({
      loading: true,
      error: null,
      savingPlanet: false,
      generating: {},
      imagePhases: {},
      imageProgress: {},
      imageErrors: {},
    })
    try {
      const result = await analyzeText(text, controller.signal)
      if (controller.signal.aborted) return
      set({
        result,
        loading: false,
        images: {},
        imageErrors: {},
        generating: {},
        imagePhases: {},
        imageProgress: {},
        savedPlanetId: null,
        savedEditToken: null,
        saveError: null,
      })
    } catch (e) {
      if (controller.signal.aborted) return
      set({ error: e instanceof Error ? e.message : String(e), loading: false })
    } finally {
      if (analysisController === controller) analysisController = null
    }
  },
  reset: () => {
    abortAllGenerations()
    abortAnalysis()
    abortPlanetLoad()
    abortSave()
    window.history.replaceState(null, '', '/')
    set({
      result: null,
      loading: false,
      error: null,
      images: {},
      imageErrors: {},
      generating: {},
      imagePhases: {},
      imageProgress: {},
      savedPlanetId: null,
      savedEditToken: null,
      saveError: null,
      savingPlanet: false,
    })
  },
}))
