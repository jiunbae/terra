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

interface TerraState {
  result: AnalyzeResponse | null
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
  cycleRenderQuality: () => void
  requestView: (mode: ViewMode) => void
  loadImageStatus: () => Promise<void>
  setImageQuality: (quality: ImageQuality) => void
  generateImage: (spec: PlanetSpec, kind: 'planet' | 'surface' | 'inhabitant', index?: number) => Promise<void>
  loadGallery: () => Promise<void>
  saveCurrentPlanet: () => Promise<void>
  openSavedPlanet: (id: string) => Promise<void>
  analyze: (text: string) => Promise<void>
  reset: () => void
}

export const useTerra = create<TerraState>((set, get) => ({
  result: null,
  loading: false,
  error: null,
  viewRequest: { mode: 'orbit', nonce: 0 },
  panelOpen: true,
  renderQuality: 'auto',
  imageStatus: null,
  imageQuality: 'balanced',
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
  setPanelOpen: (open) => set({ panelOpen: open }),
  cycleRenderQuality: () =>
    set((s) => ({
      renderQuality:
        s.renderQuality === 'auto'
          ? 'performance'
          : s.renderQuality === 'performance'
            ? 'quality'
            : 'auto',
    })),
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
  setImageQuality: (imageQuality) => set({ imageQuality }),
  generateImage: async (spec, kind, index) => {
    const key = kind === 'inhabitant' ? `inhabitant:${index}` : kind
    set((s) => ({
      generating: { ...s.generating, [key]: true },
      imagePhases: { ...s.imagePhases, [key]: 'requesting' },
      imageProgress: Object.fromEntries(
        Object.entries(s.imageProgress).filter(([progressKey]) => progressKey !== key),
      ) as TerraState['imageProgress'],
      imageErrors: { ...s.imageErrors, [key]: '' },
    }))
    try {
      const image = await generateArtwork(spec, kind, index, get().imageQuality, (progress) => {
        set((s) => ({
          imagePhases: { ...s.imagePhases, [key]: progress.status },
          imageProgress: { ...s.imageProgress, [key]: progress },
        }))
      })
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
          await updateSavedImage(savedPlanetId, savedEditToken, key, image)
          if (kind === 'planet') await get().loadGallery()
        } catch (e) {
          set({ saveError: e instanceof Error ? e.message : String(e) })
        }
      }
    } catch (e) {
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
    }
  },
  loadGallery: async () => {
    set({ galleryLoading: true, galleryError: null })
    try {
      set({ gallery: await getGallery(), galleryLoading: false })
    } catch (e) {
      set({
        galleryLoading: false,
        galleryError: e instanceof Error ? e.message : String(e),
      })
    }
  },
  saveCurrentPlanet: async () => {
    const { result, images } = get()
    if (!result) return
    set({ savingPlanet: true, saveError: null })
    try {
      const saved = await savePlanet(result, images)
      if (saved.edit_token) {
        window.localStorage.setItem(`terra:edit:${saved.id}`, saved.edit_token)
      }
      set({
        savedPlanetId: saved.id,
        savedEditToken: saved.edit_token ?? null,
        savingPlanet: false,
      })
      window.history.replaceState(null, '', `/?planet=${encodeURIComponent(saved.id)}`)
      await get().loadGallery()
    } catch (e) {
      set({
        savingPlanet: false,
        saveError: e instanceof Error ? e.message : String(e),
      })
    }
  },
  openSavedPlanet: async (id) => {
    set({ loading: true, error: null, saveError: null })
    try {
      const saved = await getSavedPlanet(id)
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
      const savedEditToken = window.localStorage.getItem(`terra:edit:${saved.id}`) || null
      set({
        result: { spec: saved.spec, physics: saved.physics, model: saved.model },
        images,
        loading: false,
        savedPlanetId: saved.id,
        savedEditToken,
      })
      window.history.replaceState(null, '', `/?planet=${encodeURIComponent(saved.id)}`)
    } catch (e) {
      set({ error: e instanceof Error ? e.message : String(e), loading: false })
    }
  },
  analyze: async (text: string) => {
    set({ loading: true, error: null })
    try {
      const result = await analyzeText(text)
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
      set({ error: e instanceof Error ? e.message : String(e), loading: false })
    }
  },
  reset: () => {
    window.history.replaceState(null, '', '/')
    set({
      result: null,
      error: null,
      images: {},
      imageErrors: {},
      generating: {},
      imagePhases: {},
      imageProgress: {},
      savedPlanetId: null,
      savedEditToken: null,
      saveError: null,
    })
  },
}))
