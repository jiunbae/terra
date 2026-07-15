import { lazy, Suspense, useEffect, useRef, useState } from 'react'
import { useShallow } from 'zustand/react/shallow'
import InputPanel from './components/InputPanel'
import ReportPanel from './components/ReportPanel'
import GalleryPanel from './components/GalleryPanel'
import SceneBoundary from './components/SceneBoundary'
import { useTerra } from './store'

// Three.js/R3F/drei 3D 엔진(번들의 대부분)을 별도 청크로 분리해 초기 로드에서 떼어낸다.
// 로딩 중에는 body 배경(#02030a)이 Canvas 배경과 같아 깜빡임이 없다.
const PlanetScene = lazy(() => import('./three/PlanetScene'))

type Tab = 'input' | 'report' | 'gallery'

const QUALITY_LABEL = {
  auto: '⚡ 자동 품질',
  performance: '⚡ 성능 우선',
  quality: '✦ 고화질',
} as const

export default function App() {
  const {
    result,
    loading,
    requestView,
    reset,
    panelOpen,
    setPanelOpen,
    loadImageStatus,
    renderQuality,
    cycleRenderQuality,
    openSavedPlanet,
  } = useTerra(useShallow((state) => ({
    result: state.result,
    loading: state.loading,
    requestView: state.requestView,
    reset: state.reset,
    panelOpen: state.panelOpen,
    setPanelOpen: state.setPanelOpen,
    loadImageStatus: state.loadImageStatus,
    renderQuality: state.renderQuality,
    cycleRenderQuality: state.cycleRenderQuality,
    openSavedPlanet: state.openSavedPlanet,
  })))
  const [tab, setTab] = useState<Tab>('input')
  const sharedLoaded = useRef(false)

  useEffect(() => {
    loadImageStatus()
  }, [loadImageStatus])

  useEffect(() => {
    if (sharedLoaded.current) return
    sharedLoaded.current = true
    const id = new URLSearchParams(window.location.search).get('planet')
    if (id) {
      openSavedPlanet(id).then((ok) => {
        if (ok) setTab('report')
      })
    }
  }, [openSavedPlanet])

  // 분석이 끝나면 리포트 탭으로
  const effectiveTab: Tab = result && tab === 'input' && !loading ? 'report' : tab

  return (
    <div className={`app quality-${renderQuality}`}>
      <section className="scene-container" aria-label={`${result?.spec.planet.name ?? '미지의 행성'} 3D 탐색 장면`}>
        <SceneBoundary>
          <Suspense fallback={<div className="scene-loading" role="status">3D 장면 불러오는 중…</div>}>
            <PlanetScene />
          </Suspense>
        </SceneBoundary>
      </section>

      <header className="brand">
        <h1>TERRA</h1>
        <span>소설 속 행성 재구성기</span>
      </header>

      <button
        type="button"
        className="panel-toggle"
        onClick={() => setPanelOpen(!panelOpen)}
        title="패널 접기/펼치기"
        aria-label={panelOpen ? '정보 패널 접기' : '정보 패널 펼치기'}
        aria-expanded={panelOpen}
        aria-controls="terra-panel"
      >
        {panelOpen ? '◀' : '▶'}
      </button>

      {panelOpen && (
        <aside className="panel" id="terra-panel" aria-label="행성 재구성 정보 패널">
          <nav className="tabs" aria-label="정보 패널 메뉴">
            <button
              type="button"
              className={effectiveTab === 'input' ? 'active' : ''}
              onClick={() => setTab('input')}
              aria-pressed={effectiveTab === 'input'}
              aria-controls="terra-panel-content"
            >
              소설 입력
            </button>
            <button
              type="button"
              className={effectiveTab === 'report' ? 'active' : ''}
              onClick={() => setTab('report')}
              disabled={!result}
              aria-pressed={effectiveTab === 'report'}
              aria-controls="terra-panel-content"
            >
              분석 리포트
            </button>
            <button
              type="button"
              className={effectiveTab === 'gallery' ? 'active' : ''}
              onClick={() => setTab('gallery')}
              aria-pressed={effectiveTab === 'gallery'}
              aria-controls="terra-panel-content"
            >
              갤러리
            </button>
            {result && (
              <button
                type="button"
                className="ghost small-btn"
                onClick={() => {
                  reset()
                  setTab('input')
                }}
              >
                새 행성
              </button>
            )}
          </nav>
          <div className="panel-body" id="terra-panel-content">
            {effectiveTab === 'gallery' ? (
              <GalleryPanel onOpen={() => setTab('report')} />
            ) : effectiveTab === 'input' || !result ? (
              <InputPanel />
            ) : (
              <ReportPanel data={result} />
            )}
          </div>
        </aside>
      )}

      <nav className="view-controls" aria-label="3D 행성 시점">
        <button type="button" onClick={() => requestView('orbit')}>🛰 궤도</button>
        <button type="button" onClick={() => requestView('low')}>✈ 저공비행</button>
        <button type="button" onClick={() => requestView('north')}>⌖ 극지 관측</button>
        <button
          type="button"
          className="quality-control"
          onClick={cycleRenderQuality}
          title="자동 → 성능 우선 → 고화질 순서로 전환"
        >
          {QUALITY_LABEL[renderQuality]}
        </button>
      </nav>

      {loading && (
        <div className="scanning" role="status" aria-live="polite" aria-label="행성 재구성 중">
          <div className="scan-ring" aria-hidden="true" />
          <span>행성 재구성 중…</span>
        </div>
      )}

      <footer className="help">드래그: 회전 · 휠: 접근/이탈 — 가까이 갈수록 지형 디테일이 올라갑니다</footer>
    </div>
  )
}
