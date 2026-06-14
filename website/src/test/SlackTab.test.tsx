import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, waitFor, fireEvent } from '@testing-library/react'

/* ── Mocks: must run before importing the component ── */
const mockApi = vi.hoisted(() => ({
  sttConfig: vi.fn(),
  saveSttConfig: vi.fn(),
  sttInstall: vi.fn(),
}))
vi.mock('../api/client', () => ({ api: mockApi }))

import SlackTab from '../pages/overview/SlackTab'

const baseConfig = {
  enabled: true,
  model: 'turbo',
  mlx_model: 'mlx-community/whisper-large-v3-turbo',
  available: false,
  docker_mode: false,
  models: { turbo: '~1.6 GB' },
  mlx_models: { 'mlx-community/whisper-large-v3-turbo': '~809 MB' },
  providers: ['whisper', 'mlx', 'transcribe'],
  provider: 'whisper',
  streaming: false,
  install_step: 'idle',
  install_detail: '',
  install_error: '',
  prereqs: [],
}

beforeEach(() => {
  Object.values(mockApi).forEach(m => m.mockReset())
  mockApi.sttConfig.mockResolvedValue({ ...baseConfig })
  mockApi.saveSttConfig.mockImplementation(async (body: Record<string, unknown>) => ({
    ...baseConfig,
    ...body,
  }))
})

describe('SlackTab — STT provider', () => {
  it('renders the provider dropdown with backend-supplied options', async () => {
    render(<SlackTab />)
    const select = (await screen.findByLabelText('STT provider')) as HTMLSelectElement
    expect(select.value).toBe('whisper')
    expect(screen.getByRole('option', { name: /Whisper MLX/i })).toBeInTheDocument()
    expect(screen.getByRole('option', { name: /Transcribe/i })).toBeInTheDocument()
  })

  it('shows the whisper size selector for the whisper provider', async () => {
    render(<SlackTab />)
    await screen.findByLabelText('STT provider')
    expect(screen.getByText('Model')).toBeInTheDocument()
    expect(screen.queryByLabelText('MLX model')).not.toBeInTheDocument()
  })

  it('switches to mlx: shows the MLX model dropdown and MLX install button', async () => {
    render(<SlackTab />)
    const select = (await screen.findByLabelText('STT provider')) as HTMLSelectElement

    mockApi.saveSttConfig.mockResolvedValueOnce({ ...baseConfig, provider: 'mlx' })
    fireEvent.change(select, { target: { value: 'mlx' } })

    await waitFor(() =>
      expect(mockApi.saveSttConfig).toHaveBeenCalledWith({ provider: 'mlx' })
    )
    // MLX model dropdown replaces the whisper size selector.
    expect(await screen.findByLabelText('MLX model')).toBeInTheDocument()
    expect(screen.queryByText('Model')).not.toBeInTheDocument()
    // Provider-aware install button.
    expect(screen.getByRole('button', { name: /Install MLX Whisper/ })).toBeInTheDocument()
  })

  it('persists a new MLX model selection', async () => {
    mockApi.sttConfig.mockResolvedValue({ ...baseConfig, provider: 'mlx' })
    render(<SlackTab />)
    const select = (await screen.findByLabelText('MLX model')) as HTMLSelectElement

    mockApi.saveSttConfig.mockResolvedValueOnce({ ...baseConfig, provider: 'mlx', mlx_model: 'mlx-community/whisper-large-v3-turbo' })
    fireEvent.change(select, { target: { value: 'mlx-community/whisper-large-v3-turbo' } })

    await waitFor(() =>
      expect(mockApi.saveSttConfig).toHaveBeenCalledWith({
        mlx_model: 'mlx-community/whisper-large-v3-turbo',
      })
    )
  })
})
