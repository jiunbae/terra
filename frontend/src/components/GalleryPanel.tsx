import { useEffect, useState } from 'react'
import { useShallow } from 'zustand/react/shallow'
import { assetUrl } from '../api'
import { useTerra } from '../store'

const FEATURE_KO: Record<string, string> = {
  continents: '대륙형',
  archipelago: '군도형',
  cratered: '충돌구',
  canyons: '대협곡',
  dunes: '사구',
  crystalline: '결정질',
  volcanic: '화산형',
  artificial: '인공구조',
}

export default function GalleryPanel({ onOpen }: { onOpen: () => void }) {
  const { gallery, galleryLoading, galleryError, openError, loadGallery, openSavedPlanet } = useTerra(
    useShallow((state) => ({
      gallery: state.gallery,
      galleryLoading: state.galleryLoading,
      galleryError: state.galleryError,
      openError: state.error,
      loadGallery: state.loadGallery,
      openSavedPlanet: state.openSavedPlanet,
    })),
  )
  const [openingId, setOpeningId] = useState<string | null>(null)

  useEffect(() => {
    const controller = new AbortController()
    loadGallery(controller.signal)
    return () => controller.abort()
  }, [loadGallery])

  const open = async (id: string) => {
    setOpeningId(id)
    if (await openSavedPlanet(id)) {
      onOpen()
      return
    }
    setOpeningId(null)
  }

  return (
    <div className="gallery-panel">
      <div className="gallery-heading">
        <div>
          <h2>행성 아카이브</h2>
          <p className="hint">공개 저장된 행성을 다시 열고 공유할 수 있습니다.</p>
        </div>
        <button
          type="button"
          className="ghost gallery-refresh"
          onClick={() => loadGallery()}
          disabled={galleryLoading}
          aria-label="행성 갤러리 새로고침"
          title="새로고침"
        >
          ↻
        </button>
      </div>

      {galleryLoading && gallery.length === 0 && <p className="hint" role="status">아카이브 수신 중…</p>}
      {galleryError && <div className="error" role="alert">⚠ {galleryError}</div>}
      {openError && <div className="error" role="alert">⚠ {openError}</div>}
      {!galleryLoading && gallery.length === 0 && (
        <div className="gallery-empty">
          <span>◎</span>
          <p>아직 공개 저장된 행성이 없습니다.</p>
        </div>
      )}

      <div className="gallery-grid">
        {gallery.map((planet) => (
          <button
            type="button"
            className="gallery-card"
            key={planet.id}
            onClick={() => open(planet.id)}
            disabled={openingId !== null}
            aria-busy={openingId === planet.id}
          >
            {planet.cover_image_url ? (
              <img
                src={assetUrl(planet.cover_image_url)}
                alt={`${planet.name} 대표 이미지`}
                loading="lazy"
                decoding="async"
              />
            ) : (
              <div className="gallery-planet-placeholder" aria-hidden="true"><span>◉</span></div>
            )}
            <div className="gallery-card-body">
              <strong>{planet.name}</strong>
              <div className="gallery-meta">
                <span>{FEATURE_KO[planet.feature_type] ?? planet.feature_type}</span>
                <span>{planet.gravity_g.toFixed(2)} g</span>
                {planet.inhabitant_count > 0 && <span>거주민 {planet.inhabitant_count}</span>}
              </div>
              {planet.description && <p>{planet.description}</p>}
            </div>
          </button>
        ))}
      </div>
    </div>
  )
}
