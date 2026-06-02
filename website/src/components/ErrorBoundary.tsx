import { Component, type ReactNode } from 'react'
import { AlertTriangle } from 'lucide-react'

interface State { error: Error | null }

export default class ErrorBoundary extends Component<{ children: ReactNode; fallback?: ReactNode }, State> {
  state: State = { error: null }
  static getDerivedStateFromError(error: Error) { return { error } }
  render() {
    if (!this.state.error) return this.props.children
    if (this.props.fallback) return this.props.fallback
    return (
      <div className="flex flex-col items-center justify-center h-full gap-4 text-center p-8">
        <div className="text-4xl"><AlertTriangle className="lucide-inline" /></div>
        <div className="text-lg font-bold text-text-strong">Something went wrong</div>
        <div className="text-sm text-muted max-w-md break-words">{this.state.error.message}</div>
        <button className="px-4 py-1.5 rounded-lg text-[13px] font-medium cursor-pointer bg-accent text-accent-fg border-none hover:opacity-90 transition-opacity"
          onClick={() => this.setState({ error: null })}>Try Again</button>
      </div>
    )
  }
}
