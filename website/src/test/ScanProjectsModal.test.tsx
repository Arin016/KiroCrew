import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import ScanProjectsModal from '../components/ScanProjectsModal'

const mockAgentsRescan = vi.fn()

vi.mock('../api/client', () => ({
  api: {
    agentsRescan: (...args: unknown[]) => mockAgentsRescan(...args),
  },
}))

const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
const Wrapper = ({ children }: { children: React.ReactNode }) => (
  <QueryClientProvider client={qc}>{children}</QueryClientProvider>
)

describe('ScanProjectsModal', () => {
  beforeEach(() => { vi.clearAllMocks(); qc.clear() })

  it('does not render when closed', () => {
    render(<ScanProjectsModal open={false} onClose={() => {}} onSuccess={() => {}} />, { wrapper: Wrapper })
    expect(screen.queryByText('Scan for project agents')).not.toBeInTheDocument()
  })

  it('renders input and disabled Scan button when open with empty input', () => {
    render(<ScanProjectsModal open={true} onClose={() => {}} onSuccess={() => {}} />, { wrapper: Wrapper })
    expect(screen.getByText('Scan for project agents')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Scan/ })).toBeDisabled()
  })

  it('enables Scan button when path is entered', () => {
    render(<ScanProjectsModal open={true} onClose={() => {}} onSuccess={() => {}} />, { wrapper: Wrapper })
    fireEvent.change(screen.getByPlaceholderText(/Documents or/), { target: { value: '/home/user/projects' } })
    expect(screen.getByRole('button', { name: /^Scan$/ })).not.toBeDisabled()
  })

  it('calls agentsRescan with path when Scan clicked', async () => {
    mockAgentsRescan.mockResolvedValue({ discovered: 2, agents: [] })
    const onSuccess = vi.fn()
    render(<ScanProjectsModal open={true} onClose={() => {}} onSuccess={onSuccess} />, { wrapper: Wrapper })
    fireEvent.change(screen.getByPlaceholderText(/Documents or/), { target: { value: '/home/user' } })
    fireEvent.click(screen.getByRole('button', { name: /^Scan$/ }))
    await waitFor(() => expect(mockAgentsRescan).toHaveBeenCalledWith(['/home/user']))
    await waitFor(() => expect(onSuccess).toHaveBeenCalled())
    expect(screen.getByText(/Discovered 2 project agent/)).toBeInTheDocument()
  })

  it('shows error message when scan fails', async () => {
    mockAgentsRescan.mockRejectedValue(new Error('Network error'))
    render(<ScanProjectsModal open={true} onClose={() => {}} onSuccess={() => {}} />, { wrapper: Wrapper })
    fireEvent.change(screen.getByPlaceholderText(/Documents or/), { target: { value: '/home/user' } })
    fireEvent.click(screen.getByRole('button', { name: /^Scan$/ }))
    await waitFor(() => expect(screen.getByText(/Error: Network error/)).toBeInTheDocument())
  })

  it('calls onClose when Cancel clicked', () => {
    const onClose = vi.fn()
    render(<ScanProjectsModal open={true} onClose={onClose} onSuccess={() => {}} />, { wrapper: Wrapper })
    fireEvent.click(screen.getByRole('button', { name: /Cancel/ }))
    expect(onClose).toHaveBeenCalled()
  })

  it('triggers scan on Enter key when path entered', async () => {
    mockAgentsRescan.mockResolvedValue({ discovered: 1, agents: [] })
    render(<ScanProjectsModal open={true} onClose={() => {}} onSuccess={() => {}} />, { wrapper: Wrapper })
    const input = screen.getByPlaceholderText(/Documents or/)
    fireEvent.change(input, { target: { value: '/home/user' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    await waitFor(() => expect(mockAgentsRescan).toHaveBeenCalledWith(['/home/user']))
  })
})
