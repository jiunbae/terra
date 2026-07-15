import { Component, type ReactNode } from 'react'

interface Props {
  children: ReactNode
}

interface State {
  failed: boolean
}

export default class SceneBoundary extends Component<Props, State> {
  state: State = { failed: false }

  static getDerivedStateFromError(): State {
    return { failed: true }
  }

  render() {
    if (!this.state.failed) return this.props.children
    return (
      <div className="scene-fallback" role="alert">
        <strong>3D 장면을 표시할 수 없습니다.</strong>
        <span>분석 리포트와 생성 이미지는 계속 이용할 수 있습니다.</span>
        <button type="button" onClick={() => window.location.reload()}>3D 다시 불러오기</button>
      </div>
    )
  }
}
