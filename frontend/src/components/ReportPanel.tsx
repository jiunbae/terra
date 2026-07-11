import { useState } from 'react'
import { useTerra } from '../store'
import type { AnalyzeResponse, Inference } from '../types'
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

function fmt(n: number, digits = 1): string {
  return n.toLocaleString('ko-KR', { maximumFractionDigits: digits })
}

function Meter({ label, value }: { label: string; value: number }) {
  return (
    <div className="meter">
      <span className="meter-label">{label}</span>
      <div className="meter-track">
        <div className="meter-fill" style={{ width: `${Math.round(value * 100)}%` }} />
      </div>
      <span className="meter-num">{Math.round(value * 100)}%</span>
    </div>
  )
}

function Swatch({ hex, title }: { hex: string; title: string }) {
  return <span className="swatch" style={{ background: hex }} title={`${title} ${hex}`} />
}

export default function ReportPanel({ data }: { data: AnalyzeResponse }) {
  const { spec, physics } = data
  const p = spec.planet
  const s = spec.surface
  const pal = s.palette
  const { savedPlanetId, savingPlanet, saveError, saveCurrentPlanet } = useTerra()
  const [copied, setCopied] = useState(false)
  const shareUrl = savedPlanetId
    ? `${window.location.origin}/?planet=${encodeURIComponent(savedPlanetId)}`
    : null

  const share = async () => {
    if (!shareUrl) return
    try {
      if (navigator.share) {
        await navigator.share({ title: `${p.name} — TERRA`, url: shareUrl })
      } else {
        await navigator.clipboard.writeText(shareUrl)
        setCopied(true)
        window.setTimeout(() => setCopied(false), 1800)
      }
    } catch {
      // 사용자가 시스템 공유창을 닫은 경우 상태를 바꾸지 않는다.
    }
  }

  return (
    <div className="report">
      <section>
        <h2>{p.name}</h2>
        {s.description && <p className="desc">{s.description}</p>}
        <div className="share-actions">
          {!savedPlanetId ? (
            <button className="archive-save" onClick={saveCurrentPlanet} disabled={savingPlanet}>
              {savingPlanet ? '아카이브 저장 중…' : '공개 갤러리에 저장'}
            </button>
          ) : (
            <>
              <span className="archive-saved">✓ 공개 저장됨</span>
              <button className="archive-share" onClick={share}>{copied ? '복사됨' : '공유'}</button>
            </>
          )}
        </div>
        {saveError && <p className="image-note error-text">{saveError}</p>}
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
