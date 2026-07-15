import type {
  AnalyzeResponse,
  GeneratedImage,
  ImageJob,
  ImageJobProgress,
  ImageProviderStatus,
  ImageQuality,
  PlanetSpec,
  SavedPlanet,
  SavedPlanetSummary,
} from './types'

const BASE = (import.meta.env.VITE_API_BASE ?? '').replace(/\/$/, '')

async function apiError(res: Response, fallback: string): Promise<Error> {
  const detail = await res.json().catch(() => null)
  const message = detail?.detail
  if (typeof message === 'string' && message.trim()) return new Error(message)
  if (Array.isArray(message)) {
    const validation = message
      .map((item) => typeof item?.msg === 'string' ? item.msg : '')
      .filter(Boolean)
      .join(', ')
    if (validation) return new Error(validation)
  }
  return new Error(`${fallback} (HTTP ${res.status})`)
}

async function fetchWithTimeout(
  input: RequestInfo | URL,
  init: RequestInit = {},
  timeoutMs = 20_000,
  timeoutMessage = '서버 응답 시간이 초과되었습니다.',
): Promise<Response> {
  const controller = new AbortController()
  const sourceSignal = init.signal
  let timedOut = false
  const forwardAbort = () => controller.abort(sourceSignal?.reason)

  if (sourceSignal?.aborted) forwardAbort()
  else sourceSignal?.addEventListener('abort', forwardAbort, { once: true })

  const timer = window.setTimeout(() => {
    timedOut = true
    controller.abort()
  }, timeoutMs)

  try {
    return await fetch(input, { ...init, signal: controller.signal })
  } catch (error) {
    // 화면 전환 등 사용자 측 취소와 제한시간이 경합하면 취소를 우선 보존한다.
    if (sourceSignal?.aborted) throw error
    if (timedOut) throw new Error(timeoutMessage)
    throw error
  } finally {
    window.clearTimeout(timer)
    sourceSignal?.removeEventListener('abort', forwardAbort)
  }
}

// 취소 가능한 대기 — signal이 abort되면 타이머를 정리하고 즉시 거부한다.
function abortableDelay(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(new DOMException('이미지 생성이 취소되었습니다.', 'AbortError'))
      return
    }
    const timer = window.setTimeout(() => {
      signal?.removeEventListener('abort', onAbort)
      resolve()
    }, ms)
    const onAbort = () => {
      window.clearTimeout(timer)
      reject(new DOMException('이미지 생성이 취소되었습니다.', 'AbortError'))
    }
    signal?.addEventListener('abort', onAbort, { once: true })
  })
}

export async function analyzeText(text: string, signal?: AbortSignal): Promise<AnalyzeResponse> {
  const res = await fetchWithTimeout(`${BASE}/api/analyze`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text }),
    signal,
  }, 120_000, '행성 분석이 2분을 초과했습니다. 잠시 후 다시 시도해 주세요.')
  if (!res.ok) {
    throw await apiError(res, '분석 실패')
  }
  return res.json()
}

export async function getImageStatus(): Promise<ImageProviderStatus> {
  const res = await fetchWithTimeout(
    `${BASE}/api/image/status`,
    {},
    12_000,
    '이미지 생성기 상태 확인이 지연되고 있습니다.',
  )
  if (!res.ok) throw await apiError(res, '이미지 생성기 상태 확인 실패')
  return res.json()
}

export async function generateArtwork(
  spec: PlanetSpec,
  kind: 'planet' | 'surface' | 'inhabitant',
  inhabitantIndex?: number,
  quality: ImageQuality = 'balanced',
  onStatus?: (progress: ImageJobProgress) => void,
  signal?: AbortSignal,
): Promise<GeneratedImage> {
  const res = await fetchWithTimeout(`${BASE}/api/image/generate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ spec, kind, inhabitant_index: inhabitantIndex, quality }),
    signal,
  }, 30_000, '이미지 생성 작업 등록이 지연되고 있습니다.')
  if (!res.ok) throw await apiError(res, '이미지 생성 요청 실패')
  let job: ImageJob = await res.json()
  let settled = false
  let cancelRequested = false
  const requestServerCancel = () => {
    if (settled || cancelRequested) return
    cancelRequested = true
    void fetch(`${BASE}/api/image/jobs/${encodeURIComponent(job.id)}`, {
      method: 'DELETE',
      cache: 'no-store',
      keepalive: true,
    }).catch(() => undefined)
  }
  signal?.addEventListener('abort', requestServerCancel, { once: true })

  try {
    onStatus?.(job)
    const timeoutMinutes = quality === 'quality' ? 45 : quality === 'balanced' ? 35 : 25
    const deadline = Date.now() + timeoutMinutes * 60 * 1000

    const activeStatuses: ImageJob['status'][] = [
      'queued',
      'running',
      'generating',
      'verifying',
      'refining',
      'upscaling',
    ]
    let consecutivePollFailures = 0
    while (activeStatuses.includes(job.status)) {
      if (signal?.aborted) throw new DOMException('이미지 생성이 취소되었습니다.', 'AbortError')
      if (Date.now() >= deadline) throw new Error(`이미지 생성이 ${timeoutMinutes}분을 초과했습니다.`)
      await abortableDelay(2500, signal)
      try {
        const statusRes = await fetchWithTimeout(
          `${BASE}/api/image/jobs/${job.id}`,
          { cache: 'no-store', signal },
          15_000,
          '이미지 작업 상태 확인이 지연되고 있습니다.',
        )
        if (!statusRes.ok) throw await apiError(statusRes, '이미지 생성 상태 확인 실패')
        job = await statusRes.json()
        consecutivePollFailures = 0
        onStatus?.(job)
      } catch (error) {
        if (signal?.aborted || (error instanceof DOMException && error.name === 'AbortError')) throw error
        consecutivePollFailures += 1
        if (consecutivePollFailures >= 3) throw error
      }
    }

    if (job.status === 'failed') throw new Error(job.error || '이미지 생성에 실패했습니다.')
    if (!job.url || job.seed === null) throw new Error('이미지 생성 결과가 비어 있습니다.')
    settled = true
    return {
      url: assetUrl(job.url),
      seed: job.seed,
      provider: job.provider,
      model: job.model,
      quality: job.quality ?? quality,
      quality_score: job.quality_score,
      verification_notes: job.verification_notes,
    }
  } catch (error) {
    requestServerCancel()
    throw error
  } finally {
    signal?.removeEventListener('abort', requestServerCancel)
  }
}

export function assetUrl(url: string): string {
  return url.startsWith('http') ? url : `${BASE}${url}`
}

export function assetPath(url: string): string {
  if (url.startsWith('/generated/')) return url
  try {
    return new URL(url, window.location.origin).pathname
  } catch {
    return url
  }
}

export async function savePlanet(
  analysis: AnalyzeResponse,
  images: Record<string, GeneratedImage>,
  signal?: AbortSignal,
): Promise<SavedPlanet> {
  const imageAssets = Object.fromEntries(
    Object.entries(images).map(([key, image]) => [
      key,
      { ...image, url: assetPath(image.url) },
    ]),
  )
  const res = await fetchWithTimeout(`${BASE}/api/planets`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      ...analysis,
      cover_image_url: imageAssets.planet?.url ?? null,
      image_assets: imageAssets,
      public: true,
    }),
    signal,
  }, 30_000, '행성 저장이 지연되고 있습니다.')
  if (!res.ok) throw await apiError(res, '행성 저장 실패')
  return res.json()
}

export async function getGallery(signal?: AbortSignal): Promise<SavedPlanetSummary[]> {
  const res = await fetchWithTimeout(
    `${BASE}/api/planets`,
    { cache: 'no-store', signal },
    20_000,
    '갤러리 조회가 지연되고 있습니다.',
  )
  if (!res.ok) throw await apiError(res, '갤러리 조회 실패')
  return res.json()
}

export async function getSavedPlanet(id: string, signal?: AbortSignal): Promise<SavedPlanet> {
  const res = await fetchWithTimeout(
    `${BASE}/api/planets/${encodeURIComponent(id)}`,
    { cache: 'no-store', signal },
    20_000,
    '공유 행성 조회가 지연되고 있습니다.',
  )
  if (!res.ok) throw await apiError(res, '공유 행성 조회 실패')
  return res.json()
}

export async function updateSavedCover(id: string, editToken: string, url: string): Promise<void> {
  const res = await fetchWithTimeout(`${BASE}/api/planets/${encodeURIComponent(id)}/cover`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ cover_image_url: assetPath(url), edit_token: editToken }),
  }, 20_000, '대표 이미지 연결이 지연되고 있습니다.')
  if (!res.ok) throw await apiError(res, '대표 이미지 연결 실패')
}

export async function updateSavedImage(
  id: string,
  editToken: string,
  key: string,
  image: GeneratedImage,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetchWithTimeout(`${BASE}/api/planets/${encodeURIComponent(id)}/images`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      key,
      image: { ...image, url: assetPath(image.url) },
      edit_token: editToken,
    }),
    signal,
  }, 20_000, '생성 이미지 연결이 지연되고 있습니다.')
  if (!res.ok) throw await apiError(res, '생성 이미지 연결 실패')
}

export const SAMPLE_TEXT = `칼리페른의 하늘은 언제나 연보랏빛이었다. 두 개의 태양 — 늙고 붉은 카르와 어리고 하얀 벨 — 이 지평선 위에서 서로를 쫓았고, 그 빛이 짙은 대기를 통과하며 보라색으로 부서졌다. 이 행성의 하루는 고작 여섯 시간. 미친 듯이 도는 자전 때문에 행성 자체가 눈에 띄게 납작했고, 적도의 바람은 결코 멈추는 법이 없었다.

바다는 행성의 칠 할을 덮고 있었는데, 물빛이 지구의 그것과 달리 청록이 아니라 와인빛 적자색이었다. 바다에 녹아 있는 철 화합물 때문이라고 정착민들은 말했다. 해안선을 따라 자라는 유리수풀은 규소 골격의 반투명한 식물로, 바람이 불 때마다 수정 풍경(風磬)처럼 울렸다.

이곳 사람들 — 스스로를 '케른'이라 부르는 원주민들은 키가 작고 다부졌다. 평균 신장은 백사십 센티미터 남짓. 어깨는 넓고 다리는 굵었으며, 뼈는 지구인의 두 배로 치밀했다. 무거운 세상이 그들을 그렇게 빚었다. 처음 이곳에 내린 지구 탐사대원들은 트랩을 내려서는 순간 무릎이 꺾였다고 한다. 몸무게가 절반쯤 더 불어난 것 같았다고, 일지에 그렇게 적혀 있다.

케른인의 피부는 잿빛이 도는 청색이고, 눈은 가로로 긴 동공을 가졌다. 두 태양의 강한 자외선을 막느라 눈꺼풀이 이중으로 되어 있다. 그들은 바닷가 절벽에 도시를 파서 지었다. 밤이 되면 — 그 짧은 세 시간의 밤 — 절벽 도시의 창들이 일제히 호박색으로 빛나서, 궤도에서 내려다보면 행성의 가장자리가 금실로 수놓인 것처럼 보였다.

겨울은 없었다. 대신 '대풍계'라 불리는 계절이 있어, 일 년에 두 번 적도 폭풍이 극지방까지 번졌다. 그때가 되면 하늘은 온통 감청색 구름으로 덮이고 보랏빛 번개가 쉬지 않고 내리쳤다. 케른인들은 그 계절에 절벽 깊숙이 물러나 노래를 지었다. 그들의 음악은 유리수풀의 울림을 흉내 낸 것이라 한다.

위성은 하나뿐이었다. '재의 달'이라 불리는 그것은 잿빛의 작은 위성으로, 사흘에 한 번 하늘을 가로질렀다.`
