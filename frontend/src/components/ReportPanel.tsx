import { useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import { useShallow } from 'zustand/react/shallow'
import { downloadPlanetJson, reportSavedPlanet } from '../api'
import { useTerra } from '../store'
import type { AnalyzeResponse, Inference, ReportReason } from '../types'
import GeneratedArtwork from './GeneratedArtwork'

const SHAPE_KO: Record<string, string> = {
  sphere: '구형',
  oblate: '편평 타원체 (회전 타원체)',
  irregular: '불규칙형',
}

const CONF_KO: Record<Inference['confidence'], { label: string; cls: string }> = {
  stated: { label: '원문 명시', cls: 'conf-stated' },
  inferred: { label: '유추', cls: 'conf-inferred' },
  speculative: { label: '추정', cls: 'conf-spec' },
}

const FEATURE_KO: Record<string, string> = {
  continents: '대륙형',
  archipelago: '군도형',
  cratered: '충돌구 지형',
  canyons: '대협곡 지형',
  dunes: '사구 지형',
  crystalline: '결정질 지형',
  volcanic: '화산 지형',
  artificial: '행성 규모 인공구조',
}

const REPORT_REASON_KO: Record<ReportReason, string> = {
  personal_information: '개인정보 노출',
  copyright: '저작권·권리 침해',
  harassment: '괴롭힘·혐오 표현',
  unsafe_content: '유해하거나 부적절한 콘텐츠',
  spam: '스팸·중복 콘텐츠',
  other: '기타',
}

function fmt(n: number, digits = 1): string {
  return n.toLocaleString('ko-KR', { maximumFractionDigits: digits })
}

function Meter({ label, value }: { label: string; value: number }) {
  const percent = Math.round(Math.min(1, Math.max(0, value)) * 100)
  return (
    <div className="meter">
      <span className="meter-label">{label}</span>
      <div
        className="meter-track"
        role="progressbar"
        aria-label={label}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={percent}
      >
        <div className="meter-fill" style={{ width: `${percent}%` }} />
      </div>
      <span className="meter-num">{percent}%</span>
    </div>
  )
}

function Swatch({ hex, title }: { hex: string; title: string }) {
  return <span className="swatch" style={{ background: hex }} title={`${title} ${hex}`} role="img" aria-label={`${title} 색상 ${hex}`} />
}

export default function ReportPanel({ data }: { data: AnalyzeResponse }) {
  const { spec, physics } = data
  const p = spec.planet
  const s = spec.surface
  const pal = s.palette
  const {
    savedPlanetId,
    savedEditToken,
    savingPlanet,
    deletingPlanet,
    saveError,
    images,
    saveCurrentPlanet,
    deleteCurrentPlanet,
  } = useTerra(
    useShallow((state) => ({
      savedPlanetId: state.savedPlanetId,
      savedEditToken: state.savedEditToken,
      savingPlanet: state.savingPlanet,
      deletingPlanet: state.deletingPlanet,
      saveError: state.saveError,
      images: state.images,
      saveCurrentPlanet: state.saveCurrentPlanet,
      deleteCurrentPlanet: state.deleteCurrentPlanet,
    })),
  )
  const [copied, setCopied] = useState(false)
  const [shareError, setShareError] = useState<string | null>(null)
  const [exportError, setExportError] = useState<string | null>(null)
  const [reportOpen, setReportOpen] = useState(false)
  const [reportReason, setReportReason] = useState<ReportReason>('personal_information')
  const [reportDetails, setReportDetails] = useState('')
  const [reporting, setReporting] = useState(false)
  const [reportStatus, setReportStatus] = useState<string | null>(null)
  const [reportError, setReportError] = useState<string | null>(null)
  const copiedTimer = useRef<number | null>(null)
  const shareUrl = savedPlanetId
    ? `${window.location.origin}/?planet=${encodeURIComponent(savedPlanetId)}`
    : null

  useEffect(() => () => {
    if (copiedTimer.current !== null) window.clearTimeout(copiedTimer.current)
  }, [])

  const share = async () => {
    if (!shareUrl) return
    setShareError(null)
    try {
      if (navigator.share) {
        await navigator.share({ title: `${p.name} — TERRA`, url: shareUrl })
      } else {
        await navigator.clipboard.writeText(shareUrl)
        setCopied(true)
        if (copiedTimer.current !== null) window.clearTimeout(copiedTimer.current)
        copiedTimer.current = window.setTimeout(() => setCopied(false), 1800)
      }
    } catch (error) {
      if (error instanceof DOMException && error.name === 'AbortError') return
      setShareError('공유 링크를 복사하지 못했습니다. 브라우저 권한을 확인해 주세요.')
    }
  }

  const exportJson = () => {
    setExportError(null)
    try {
      downloadPlanetJson(data, images)
    } catch {
      setExportError('JSON 파일을 만들지 못했습니다. 브라우저 다운로드 설정을 확인해 주세요.')
    }
  }

  const submitReport = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (!savedPlanetId || reporting) return
    setReporting(true)
    setReportError(null)
    setReportStatus(null)
    try {
      await reportSavedPlanet(savedPlanetId, reportReason, reportDetails.trim())
      setReportStatus('신고가 접수되었습니다. 검토 전까지 추가 개인정보는 보내지 마세요.')
      setReportOpen(false)
      setReportDetails('')
    } catch (error) {
      setReportError(error instanceof Error ? error.message : String(error))
    } finally {
      setReporting(false)
    }
  }

  const removeFromGallery = async () => {
    const confirmed = window.confirm(
      '이 행성을 공개 갤러리에서 삭제할까요? 현재 탭의 분석은 남지만 공유 링크는 더 이상 열리지 않습니다.',
    )
    if (!confirmed) return
    if (await deleteCurrentPlanet()) {
      setReportOpen(false)
      setReportStatus(null)
    }
  }

  return (
    <div className="report">
      <section>
        <h2>{p.name}</h2>
        {s.description && <p className="desc">{s.description}</p>}
        <div className="share-actions">
          {!savedPlanetId ? (
            <button type="button" className="archive-save" onClick={saveCurrentPlanet} disabled={savingPlanet} aria-busy={savingPlanet}>
              {savingPlanet ? '아카이브 저장 중…' : '공개 갤러리에 저장'}
            </button>
          ) : (
            <>
              <span className="archive-saved" role="status">✓ 공개 저장됨</span>
              <button type="button" className="archive-share" onClick={share}>{copied ? '복사됨' : '공유'}</button>
            </>
          )}
          <button type="button" className="archive-share" onClick={exportJson}>JSON 내보내기</button>
        </div>
        <p className="archive-privacy">
          JSON은 편집 권한 없이 현재 분석(로컬 근거 인용 포함)과 이미지 메타데이터만 저장합니다.
          공개 갤러리 저장본에서는 원문 근거 인용을 제외합니다.
        </p>
        {savedPlanetId && (
          <div className="archive-management">
            <button
              type="button"
              className="archive-report"
              onClick={() => {
                setReportOpen((open) => !open)
                setReportError(null)
                setReportStatus(null)
              }}
              aria-expanded={reportOpen}
            >
              신고
            </button>
            {savedEditToken && (
              <button
                type="button"
                className="archive-delete"
                onClick={removeFromGallery}
                disabled={deletingPlanet}
                aria-busy={deletingPlanet}
              >
                {deletingPlanet ? '삭제 중…' : '공개본 삭제'}
              </button>
            )}
          </div>
        )}
        {reportOpen && savedPlanetId && (
          <form className="report-form" onSubmit={submitReport}>
            <label htmlFor="report-reason">신고 사유</label>
            <select
              id="report-reason"
              value={reportReason}
              onChange={(event) => setReportReason(event.target.value as ReportReason)}
            >
              {(Object.entries(REPORT_REASON_KO) as Array<[ReportReason, string]>).map(([value, label]) => (
                <option key={value} value={value}>{label}</option>
              ))}
            </select>
            <label htmlFor="report-details">
              설명 <span>{reportReason === 'other' ? '필수' : '선택'} · {reportDetails.length}/500</span>
            </label>
            <textarea
              id="report-details"
              value={reportDetails}
              onChange={(event) => setReportDetails(event.target.value)}
              maxLength={500}
              rows={3}
              required={reportReason === 'other'}
              placeholder="민감한 개인정보나 원문 전체를 붙여넣지 마세요."
            />
            <div className="report-form-actions">
              <button type="button" className="ghost" onClick={() => setReportOpen(false)}>취소</button>
              <button
                type="submit"
                disabled={reporting || (reportReason === 'other' && !reportDetails.trim())}
                aria-busy={reporting}
              >
                {reporting ? '접수 중…' : '신고 접수'}
              </button>
            </div>
          </form>
        )}
        {saveError && <p className="image-note error-text" role="alert">{saveError}</p>}
        {shareError && <p className="image-note error-text" role="alert">{shareError}</p>}
        {exportError && <p className="image-note error-text" role="alert">{exportError}</p>}
        {reportError && <p className="image-note error-text" role="alert">{reportError}</p>}
        {reportStatus && <p className="image-note report-success" role="status">{reportStatus}</p>}
        <GeneratedArtwork spec={spec} kind="planet" alt={`${p.name} 행성 콘셉트 아트`} />
        <dl>
          <dt>형태</dt>
          <dd>
            {SHAPE_KO[p.shape] ?? p.shape}
            {p.oblateness > 0.03 && ` · 편평도 ${fmt(p.oblateness, 2)}`}
          </dd>
          <dt>반지름</dt>
          <dd>{fmt(p.radius_km, 0)} km (지구의 {fmt(p.radius_km / 6371, 2)}배)</dd>
          <dt>표면 중력</dt>
          <dd>{fmt(p.gravity_g, 2)} g</dd>
          <dt>하루 길이</dt>
          <dd>{fmt(p.rotation_hours, 1)}시간 (지구의 {fmt(physics.day_length_vs_earth, 2)}배)</dd>
          <dt>자전축 기울기</dt>
          <dd>{fmt(p.axial_tilt_deg, 0)}°</dd>
          <dt>항성</dt>
          <dd>
            {spec.star.count}개 {Array.from({ length: spec.star.count }, (_, i) => (
              <Swatch
                key={i}
                hex={spec.star.colors_hex?.[i] || (i === 0 ? spec.star.color_hex : '#dfe9ff')}
                title={`항성 ${i + 1}`}
              />
            ))}
          </dd>
        </dl>
      </section>

      <section>
        <h3>추론된 물리량 <span className="sub">반지름·중력에서 뉴턴 역학으로 계산</span></h3>
        <dl>
          <dt>질량</dt>
          <dd>지구의 {fmt(physics.mass_earths, 2)}배</dd>
          <dt>표면 중력가속도</dt>
          <dd>{fmt(physics.surface_gravity_ms2, 2)} m/s²</dd>
          <dt>평균 밀도</dt>
          <dd>{fmt(physics.density_g_cm3, 1)} g/cm³ {physics.density_g_cm3 > 8 ? '· 중원소 행성' : physics.density_g_cm3 < 3 ? '· 저밀도(얼음/기체 많음)' : '· 암석 행성'}</dd>
          <dt>탈출 속도</dt>
          <dd>{fmt(physics.escape_velocity_kms, 1)} km/s</dd>
          <dt>저궤도 공전 주기</dt>
          <dd>{fmt(physics.low_orbit_period_min, 0)}분</dd>
          <dt>적도 자전 속도</dt>
          <dd>{fmt(physics.equator_speed_kmh, 0)} km/h</dd>
          <dt>적도 유효 중력</dt>
          <dd>{fmt(physics.effective_equator_gravity_g, 2)} g · 원심가속도 {fmt(physics.centrifugal_acceleration_ms2, 2)} m/s²</dd>
          <dt>동기궤도 고도</dt>
          <dd>{physics.synchronous_orbit_altitude_km > 0 ? `${fmt(physics.synchronous_orbit_altitude_km, 0)} km` : '형성 불가 (행성 내부)'}</dd>
          <dt>지구인 70kg 체감</dt>
          <dd>{fmt(physics.human_weight_kg, 0)} kg</dd>
          <dt>표면적</dt>
          <dd>지구의 {fmt(physics.surface_area_earths, 2)}배</dd>
          <dt>둘레 · 부피</dt>
          <dd>{fmt(physics.circumference_km, 0)} km · 지구 부피의 {fmt(physics.volume_earths, 2)}배</dd>
          <dt>대기 유지</dt>
          <dd>{physics.can_hold_atmosphere ? '가능 (탈출속도 충분)' : '어려움 (대기 이탈 위험)'}</dd>
        </dl>
      </section>

      <section>
        <h3>대기 · 기후</h3>
        {spec.atmosphere.present ? (
          <dl>
            <dt>대기 색</dt>
            <dd><Swatch hex={spec.atmosphere.color_hex} title="대기" /> 밀도 {Math.round(spec.atmosphere.density * 100)}%</dd>
            {spec.atmosphere.composition && (<><dt>조성</dt><dd>{spec.atmosphere.composition}</dd></>)}
            <dt>기온</dt>
            <dd>평균 {fmt(spec.climate.avg_temp_c, 0)}°C ({fmt(spec.climate.temp_min_c, 0)}°C ~ {fmt(spec.climate.temp_max_c, 0)}°C)</dd>
            {spec.atmosphere.weather_summary && (<><dt>날씨</dt><dd>{spec.atmosphere.weather_summary}</dd></>)}
          </dl>
        ) : (
          <p className="desc">대기 없음 — 진공에 노출된 표면</p>
        )}
        {spec.climate.phenomena.length > 0 && (
          <div className="tags">
            {spec.climate.phenomena.map((ph, i) => (
              <span className="tag" key={i}>{ph}</span>
            ))}
          </div>
        )}
      </section>

      <section>
        <h3>지표</h3>
        <dl>
          <dt>대표 지형</dt>
          <dd>{FEATURE_KO[s.feature_type] ?? s.feature_type} · 특징 강도 {Math.round(s.feature_scale * 100)}%</dd>
        </dl>
        <Meter label="바다" value={s.ocean_coverage} />
        <Meter label="지형 기복" value={s.terrain_roughness} />
        <Meter label="산악" value={s.mountain_height} />
        <Meter label="만년설" value={s.ice_coverage} />
        <Meter label="식생" value={s.vegetation_coverage} />
        {s.lava_activity > 0.02 && <Meter label="화산 활동" value={s.lava_activity} />}
        {s.city_lights > 0.02 && <Meter label="도시 불빛" value={s.city_lights} />}
        <div className="palette-row">
          <Swatch hex={pal.ocean_deep} title="심해" />
          <Swatch hex={pal.ocean_shallow} title="연안" />
          <Swatch hex={pal.shore} title="해안" />
          <Swatch hex={pal.lowland} title="저지대" />
          <Swatch hex={pal.midland} title="중지대" />
          <Swatch hex={pal.highland} title="고지대" />
          <Swatch hex={pal.peak} title="봉우리" />
          <span className="sub">고도별 색상</span>
        </div>
        <GeneratedArtwork
          spec={spec}
          kind="surface"
          alt={`${p.name} 지표 탐사 콘셉트 아트`}
        />
        {(spec.rings.present || spec.moons.length > 0) && (
          <dl>
            {spec.rings.present && (<><dt>고리</dt><dd><Swatch hex={spec.rings.color_hex} title="고리" /> 있음</dd></>)}
            {spec.moons.length > 0 && (
              <>
                <dt>위성</dt>
                <dd>{spec.moons.map((m) => m.name || '무명').join(', ')} ({spec.moons.length}개)</dd>
              </>
            )}
          </dl>
        )}
      </section>

      {spec.inhabitants.length > 0 && (
        <section>
          <h3>거주민</h3>
          {spec.inhabitants.map((inh, i) => (
            <div className="card" key={i}>
              <div className="card-head">
                <strong>{inh.name}</strong>
                {inh.category && <span className="tag">{inh.category}</span>}
                <span className="sub">평균 {inh.height_m >= 1 ? `${fmt(inh.height_m, 1)}m` : `${fmt(inh.height_m * 100, 0)}cm`}</span>
              </div>
              {inh.appearance && <p><b>외형</b> — {inh.appearance}</p>}
              {inh.physiology && <p><b>생리</b> — {inh.physiology}</p>}
              {inh.gravity_adaptation && <p><b>중력 적응</b> — {inh.gravity_adaptation}</p>}
              {inh.culture && <p><b>문화</b> — {inh.culture}</p>}
              <GeneratedArtwork
                spec={spec}
                kind="inhabitant"
                index={i}
                alt={`${inh.name || '거주민'} 초상`}
              />
              {inh.portrait_prompt && (
                <details>
                  <summary>초상 생성 프롬프트 (이미지 생성 단계에서 사용)</summary>
                  <p className="mono">{inh.portrait_prompt}</p>
                </details>
              )}
            </div>
          ))}
        </section>
      )}

      {spec.inferences.length > 0 && (
        <section>
          <h3>분석 근거 <span className="sub">{spec.inferences.length}건</span></h3>
          {spec.inferences.map((inf, i) => {
            const conf = CONF_KO[inf.confidence] ?? CONF_KO.inferred
            return (
              <div className="inference" key={i}>
                <div className="inf-head">
                  <span className={`badge ${conf.cls}`}>{conf.label}</span>
                  <strong>{inf.topic}</strong>
                </div>
                <p>{inf.claim}</p>
                {inf.evidence_quote && <blockquote>“{inf.evidence_quote}”</blockquote>}
                {inf.reasoning && <p className="reasoning">{inf.reasoning}</p>}
              </div>
            )
          })}
        </section>
      )}
    </div>
  )
}
