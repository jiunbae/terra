import { useEffect, useRef, useState } from 'react'
import PlanetScene from './three/PlanetScene'
import InputPanel from './components/InputPanel'
import ReportPanel from './components/ReportPanel'
import GalleryPanel from './components/GalleryPanel'
import { useTerra } from './store'

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
  } = useTerra()
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
      openSavedPlanet(id).then(() => setTab('report'))
    }
  }, [openSavedPlanet])

  // 분석이 끝나면 리포트 탭으로
  const effectiveTab: Tab = result && tab === 'input' && !loading ? 'report' : tab

  return (
    <div className={`app quality-${renderQuality}`}>
      <PlanetScene />

      <header className="brand">
        <h1>TERRA</h1>
        <span>소설 속 행성 재구성기</span>
      </header>

      <button className="panel-toggle" onClick={() => setPanelOpen(!panelOpen)} title="패널 접기/펼치기">
        {panelOpen ? '◀' : '▶'}
      </button>

      {panelOpen && (
        <aside className="panel">
          <nav className="tabs">
            <button className={effectiveTab === 'input' ? 'active' : ''} onClick={() => setTab('input')}>
              소설 입력
            </button>
            <button
              className={effectiveTab === 'report' ? 'active' : ''}
              onClick={() => setTab('report')}
              disabled={!result}
            >
              분석 리포트
            </button>
            <button className={effectiveTab === 'gallery' ? 'active' : ''} onClick={() => setTab('gallery')}>
              갤러리
            </button>
            {result && (
              <button
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
          <div className="panel-body">
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

      <div className="view-controls">
        <button onClick={() => requestView('orbit')}>🛰 궤도</button>
        <button onClick={() => requestView('low')}>✈ 저공비행</button>
        <button onClick={() => requestView('north')}>⌖ 극지 관측</button>
        <button
          className="quality-control"
          onClick={cycleRenderQuality}
          title="자동 → 성능 우선 → 고화질 순서로 전환"
        >
          {QUALITY_LABEL[renderQuality]}
        </button>
      </div>

      {loading && (
        <div className="scanning">
          <div className="scan-ring" />
          <span>행성 재구성 중…</span>
        </div>
      )}

      <footer className="help">드래그: 회전 · 휠: 접근/이탈 — 가까이 갈수록 지형 디테일이 올라갑니다</footer>
    </div>
  )
}
