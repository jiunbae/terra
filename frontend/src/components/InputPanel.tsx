import { useState } from 'react'
import { SAMPLE_TEXT } from '../api'
import { useTerra } from '../store'

const LOADING_MESSAGES = [
  '궤도 망원경 조준 중…',
  '대기 스펙트럼 분석 중…',
  '중력장 측정 중…',
  '지형 노이즈 합성 중…',
]

export default function InputPanel() {
  const [text, setText] = useState('')
  const { analyze, loading, error } = useTerra()
  const msg = LOADING_MESSAGES[Math.floor(Date.now() / 1500) % LOADING_MESSAGES.length]

  return (
    <div className="input-panel">
      <p className="hint">
        가상 행성에 대한 소설 묘사를 붙여넣으면, 텍스트에서 유추 가능한 물리·기후·거주민
        정보를 추출해 행성을 3D로 재구성합니다.
      </p>
      <textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        placeholder={'예) "칼리페른의 하늘은 언제나 연보랏빛이었다. 두 개의 태양이…"'}
        spellCheck={false}
      />
      <div className="row">
        <button className="ghost" onClick={() => setText(SAMPLE_TEXT)} disabled={loading}>
          샘플 소설 불러오기
        </button>
        <button
          className="primary"
          onClick={() => analyze(text)}
          disabled={loading || text.trim().length < 20}
        >
          {loading ? msg : '행성 재구성 ✦'}
        </button>
      </div>
      {error && <div className="error">⚠ {error}</div>}
      {text.trim().length > 0 && text.trim().length < 20 && (
        <div className="hint small">묘사가 너무 짧습니다 (20자 이상)</div>
      )}
    </div>
  )
}
