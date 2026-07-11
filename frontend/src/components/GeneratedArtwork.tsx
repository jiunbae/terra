import { useTerra } from '../store'
import type { CompatibleImageJobStatus, ImageQuality, PlanetSpec } from '../types'

interface Props {
  spec: PlanetSpec
  kind: 'planet' | 'surface' | 'inhabitant'
  index?: number
  alt: string
}

const QUALITY_OPTIONS: Array<{ value: ImageQuality; label: string; description: string }> = [
  { value: 'fast', label: '빠름', description: '후보 1장' },
  { value: 'balanced', label: '균형', description: '후보 2장 · 자동 검증' },
  { value: 'quality', label: '고품질', description: '후보 3장 · 보정' },
]

const PHASE_LABELS: Record<CompatibleImageJobStatus | 'requesting', string> = {
  requesting: '작업 등록 중',
  queued: '생성 대기 중',
  running: '이미지 생성 중',
  generating: '이미지 생성 중',
  verifying: '결과 검증 중',
  refining: '디테일 보정 중',
  upscaling: '해상도 향상 중',
  completed: '생성 완료',
  failed: '생성 실패',
}

function verificationNotes(notes: string | string[] | null | undefined): string[] {
  if (!notes) return []
  return Array.isArray(notes) ? notes.filter(Boolean) : [notes]
}

export default function GeneratedArtwork({ spec, kind, index, alt }: Props) {
  const key = kind === 'inhabitant' ? `inhabitant:${index}` : kind
  const {
    imageStatus,
    imageQuality,
    setImageQuality,
    images,
    generating,
    imagePhases,
    imageProgress,
    imageErrors,
    generateImage,
  } = useTerra()
  const image = images[key]
  const busy = Boolean(generating[key])
  const phase = imagePhases[key]
  const progress = imageProgress[key]
  const error = imageErrors[key]
  const notes = verificationNotes(image?.verification_notes)
  const selectedQuality = QUALITY_OPTIONS.find((option) => option.value === imageQuality)!
  const candidateText = progress?.candidate_total
    ? `후보 ${progress.candidate_current ?? 1}/${progress.candidate_total}`
    : null

  if (!imageStatus) return <div className="image-status">이미지 생성기 확인 중…</div>

  return (
    <div className={`artwork ${image ? 'has-image' : ''}`}>
      {image ? (
        <>
          <img src={image.url} alt={alt} />
          <div className="artwork-meta">
            <span>{image.provider} · {image.model}</span>
            <span>seed {image.seed}</span>
          </div>
          {(image.quality || image.quality_score != null) && (
            <div className="artwork-quality-meta">
              {image.quality && <span>{QUALITY_OPTIONS.find((option) => option.value === image.quality)?.label ?? image.quality}</span>}
              {image.quality_score != null && <strong>품질 점수 {Math.round(image.quality_score)}</strong>}
            </div>
          )}
          {notes.length > 0 && (
            <details className="artwork-verification">
              <summary>자동 검증 메모</summary>
              <ul>{notes.map((note, noteIndex) => <li key={`${noteIndex}:${note}`}>{note}</li>)}</ul>
            </details>
          )}
        </>
      ) : (
        <div className="artwork-placeholder" aria-hidden="true">
          <span>{kind === 'planet' ? '◉' : kind === 'surface' ? '⌁' : '◇'}</span>
          <small>{kind === 'planet' ? '행성 궤도 이미지' : kind === 'surface' ? '지표 탐사 이미지' : '거주민 환경 초상'}</small>
        </div>
      )}

      <div className="image-quality-picker">
        <div className="image-quality-heading">
          <span>생성 품질</span>
          <small>{selectedQuality.description}</small>
        </div>
        <div className="image-quality-options" role="radiogroup" aria-label="이미지 생성 품질">
          {QUALITY_OPTIONS.map((option) => (
            <button
              type="button"
              role="radio"
              aria-checked={imageQuality === option.value}
              className={imageQuality === option.value ? 'active' : ''}
              disabled={busy}
              key={option.value}
              onClick={() => setImageQuality(option.value)}
              title={option.description}
            >
              {option.label}
            </button>
          ))}
        </div>
      </div>

      <button
        className="image-generate"
        onClick={() => generateImage(spec, kind, index)}
        disabled={!imageStatus.available || busy}
        title={imageStatus.available ? '' : imageStatus.message}
      >
        {busy
          ? `${PHASE_LABELS[phase ?? 'requesting']}…`
          : image ? '다른 이미지 생성' : '이미지 생성'}
      </button>
      {!imageStatus.available && <p className="image-note">{imageStatus.message}</p>}
      {busy && (
        <div className="image-progress" role="status" aria-live="polite">
          <div className="image-progress-head">
            <span>{PHASE_LABELS[phase ?? 'requesting']}</span>
            {candidateText && <strong>{candidateText}</strong>}
          </div>
          <div className="image-progress-track" aria-hidden="true"><span /></div>
          <p>
            {phase === 'queued'
              ? '앞선 작업이 끝나면 시작합니다. MLX 메모리 보호를 위해 한 장씩 처리합니다.'
              : phase === 'verifying'
                ? '소설의 필수 특징과 생성 결과가 일치하는지 확인하고 있습니다.'
                : phase === 'refining'
                  ? '선정된 결과의 표면과 인물 디테일을 보강하고 있습니다.'
                  : phase === 'upscaling'
                    ? '최종 이미지의 해상도와 선명도를 높이고 있습니다.'
                    : `${selectedQuality.label} 모드로 처리 중입니다. 단계가 바뀌면 여기에 표시됩니다.`}
          </p>
        </div>
      )}
      {error && <p className="image-note error-text">{error}</p>}
    </div>
  )
}
