import { Component, type ReactNode } from 'react'

interface Props {
  children: ReactNode
  // 값이 바뀌면 (예: 탭 전환) 실패 상태를 해제해 사용자가 복구 화면에서 벗어날 수 있게 한다.
  resetKey?: unknown
  onReset?: () => void
}

interface State {
  failed: boolean
  lastResetKey: unknown
}

// 손상/부분 저장된 행성 spec이 패널 렌더 중 throw해도 앱 전체 트리가 언마운트되어
// 흰 화면이 되지 않도록 정보 패널 하위를 감싼다.
export default class PanelBoundary extends Component<Props, State> {
  state: State = { failed: false, lastResetKey: undefined }

  static getDerivedStateFromError(): Partial<State> {
    return { failed: true }
  }

  // resetKey가 바뀌면(예: 탭 전환) 실패 상태를 해제해 복구 화면에서 벗어나게 한다.
  static getDerivedStateFromProps(props: Props, state: State): Partial<State> | null {
    if (props.resetKey !== state.lastResetKey) {
      return { failed: false, lastResetKey: props.resetKey }
    }
    return null
  }

  private handleReset = () => {
    this.props.onReset?.()
    this.setState({ failed: false })
  }

  render() {
    if (!this.state.failed) return this.props.children
    return (
      <div className="panel-fallback" role="alert">
        <strong>정보를 표시하는 중 문제가 발생했습니다.</strong>
        <span>저장된 행성 데이터가 손상되었을 수 있습니다.</span>
        <button type="button" onClick={this.handleReset}>처음으로</button>
      </div>
    )
  }
}
