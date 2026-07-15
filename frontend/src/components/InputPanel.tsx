import { useEffect, useState } from 'react'
import { useShallow } from 'zustand/react/shallow'
import { SAMPLE_TEXT } from '../api'
import { useTerra } from '../store'

const LOADING_MESSAGES = [
  '궤도 망원경 조준 중…',
  '대기 스펙트럼 분석 중…',
  '중력장 측정 중…',
  '지형 노이즈 합성 중…',
]

export default function InputPanel() {
  const { text, setText, analyze, loading, error } = useTerra(useShallow((state) => ({
    text: state.draftText,
    setText: state.setDraftText,
    analyze: state.analyze,
    loading: state.loading,
    error: state.error,
  })))
  const [msgIndex, setMsgIndex] = useState(0)

  // 분석 중일 때만 로딩 메시지를 순환시킨다 (렌더 시점에 Date.now()를 읽으면 갱신되지 않는다).
  useEffect(() => {
    if (!loading) return
    const timer = window.setInterval(
      () => setMsgIndex((i) => (i + 1) % LOADING_MESSAGES.length),
      1500,
    )
    return () => window.clearInterval(timer)
  }, [loading])

  const msg = LOADING_MESSAGES[msgIndex]
  const trimmedLength = text.trim().length

  return (
    <form
      className="input-panel"
      onSubmit={(event) => {
        event.preventDefault()
        if (!loading && trimmedLength >= 20) analyze(text)
      }}
    >
      <p className="hint">
        가상 행성에 대한 소설 묘사를 붙여넣으면, 텍스트에서 유추 가능한 물리·기후·거주민
        정보를 추출해 행성을 3D로 재구성합니다.
      </p>
      <p className="privacy-note" id="novel-privacy-note">
        <span aria-hidden="true">ⓘ</span>
        <span>
          소설 본문은 Gemini 분석을 위해 전송됩니다. 분석 결과와 생성 이미지는
          ‘공개 갤러리에 저장’을 누를 때만 공개되므로 민감정보는 입력하지 마세요.
        </span>
      </p>
      <label className="sr-only" htmlFor="novel-description">가상 행성 소설 묘사</label>
      <textarea
        id="novel-description"
        value={text}
        onChange={(e) => setText(e.target.value)}
        placeholder={'예) "칼리페른의 하늘은 언제나 연보랏빛이었다. 두 개의 태양이…"'}
        spellCheck={false}
        maxLength={100000}
        aria-describedby="novel-privacy-note novel-description-help"
        aria-invalid={trimmedLength > 0 && trimmedLength < 20}
      />
      <div className="row">
        <button type="button" className="ghost" onClick={() => setText(SAMPLE_TEXT)} disabled={loading}>
          샘플 소설 불러오기
        </button>
        <button
          type="submit"
          className="primary"
          disabled={loading || trimmedLength < 20}
          aria-busy={loading}
        >
          {loading ? msg : '행성 재구성 ✦'}
        </button>
      </div>
      {error && <div className="error" role="alert">⚠ {error}</div>}
      <div className="input-help" id="novel-description-help">
        {trimmedLength > 0 && trimmedLength < 20 ? (
          <span className="hint small">묘사가 너무 짧습니다 (20자 이상)</span>
        ) : (
          <span className="hint small">{text.length.toLocaleString('ko-KR')} / 100,000자</span>
        )}
      </div>
    </form>
  )
}
