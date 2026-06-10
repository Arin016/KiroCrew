import { describe, it, expect, vi } from 'vitest'
import { screen } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import WelcomeView from '../components/WelcomeView'

const defaultProps = {
  setInput: vi.fn(),
}

describe('WelcomeView', () => {
  it('renders welcome heading by default', () => {
    renderWithProviders(<WelcomeView {...defaultProps} />)
    expect(screen.getByText('What can I do for you?')).toBeInTheDocument()
  })

  it('renders Autopilot heading in orchestrator mode', () => {
    renderWithProviders(<WelcomeView {...defaultProps} mode="orchestrator" />)
    expect(screen.getByText('Autopilot')).toBeInTheDocument()
  })

  it('shows the orchestrator try button only in orchestrator mode', () => {
    const { rerender } = renderWithProviders(<WelcomeView {...defaultProps} />)
    expect(screen.queryByText(/Try:/)).not.toBeInTheDocument()
    rerender(<WelcomeView {...defaultProps} mode="orchestrator" />)
    expect(screen.getByText(/Try:/)).toBeInTheDocument()
  })

  it('shows ephemeral mode toggle when onSwitchMode is provided', () => {
    renderWithProviders(<WelcomeView {...defaultProps} onSwitchMode={vi.fn()} />)
    expect(screen.getByText('Switch to ephemeral mode')).toBeInTheDocument()
  })

  it('shows revert toggle in incognito mode', () => {
    renderWithProviders(<WelcomeView {...defaultProps} onSwitchMode={vi.fn()} memoryMode="incognito" />)
    expect(screen.getByText('Switch back to default mode')).toBeInTheDocument()
  })
})
