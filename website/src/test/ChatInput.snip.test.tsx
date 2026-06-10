import { describe, it, expect, vi, beforeEach } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
import { renderWithProviders } from './helpers'

// Toggle capture-support + mobile per test.
const h = vi.hoisted(() => ({ supported: true, mobile: false }))
vi.mock('../hooks/useScreenSnip', () => ({ isScreenSnipSupported: () => h.supported }))
vi.mock('../hooks/useIsMobile', () => ({ useIsMobile: () => h.mobile }))
vi.mock('../api/client', () => ({ api: new Proxy({}, { get: () => vi.fn() }) }))

import ChatInput from '../components/ChatInput'

const base = { value: '', onChange: vi.fn(), onSend: vi.fn() }
const snipBtn = () => screen.queryByRole('button', { name: /snip/i })

beforeEach(() => {
  h.supported = true
  h.mobile = false
})

describe('ChatInput screen snip button', () => {
  it('shows the button and fires onScreenshot when screen capture is supported', () => {
    const onScreenshot = vi.fn()
    renderWithProviders(<ChatInput {...base} onScreenshot={onScreenshot} isMac={false} />)
    const btn = snipBtn()
    expect(btn).toBeInTheDocument()
    fireEvent.click(btn!)
    expect(onScreenshot).toHaveBeenCalledTimes(1)
  })

  it('shows the button as a native macOS fallback when capture is unsupported', () => {
    h.supported = false
    renderWithProviders(<ChatInput {...base} onScreenshot={vi.fn()} isMac={true} />)
    expect(snipBtn()).toBeInTheDocument()
  })

  it('hides the button when capture is unsupported and not macOS', () => {
    h.supported = false
    renderWithProviders(<ChatInput {...base} onScreenshot={vi.fn()} isMac={false} />)
    expect(snipBtn()).toBeNull()
  })

  it('hides the button on mobile even when capture is supported', () => {
    h.mobile = true
    renderWithProviders(<ChatInput {...base} onScreenshot={vi.fn()} isMac={true} />)
    expect(snipBtn()).toBeNull()
  })
})
