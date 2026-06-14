import { describe, it, expect } from 'vitest'
import { render, screen, act } from '@testing-library/react'

import BrowserLiveView from '../components/BrowserLiveView'

function frameEvent(data: string) {
  return new CustomEvent('kiroclaw-browser-frame', { detail: { data, format: 'jpeg' } })
}
function toggle() {
  return new CustomEvent('kiroclaw-toggle-browser-live')
}

describe('BrowserLiveView', () => {
  it('renders nothing until a frame or toggle', () => {
    const { container } = render(<BrowserLiveView />)
    expect(container.firstChild).toBeNull()
  })

  it('auto-opens at the SMALL size and renders the frame on first browser_frame', async () => {
    render(<BrowserLiveView />)
    await act(async () => { window.dispatchEvent(frameEvent('QUJD')) })
    const img = await screen.findByAltText('Live browser session') as HTMLImageElement
    expect(img.src).toContain('data:image/jpeg;base64,QUJD')
    // small by default => 150px wide, an unobtrusive corner thumbnail
    expect((screen.getByRole('dialog') as HTMLElement).style.width).toBe('150px')
  })

  it('expands to the large size and shrinks back via the size toggle', async () => {
    render(<BrowserLiveView />)
    await act(async () => { window.dispatchEvent(frameEvent('QUJD')) })
    const expand = await screen.findByLabelText('Expand live browser view')
    await act(async () => { expand.click() })
    expect((screen.getByRole('dialog') as HTMLElement).style.width).toBe('620px')
    const shrink = await screen.findByLabelText('Shrink live browser view')
    await act(async () => { shrink.click() })
    expect((screen.getByRole('dialog') as HTMLElement).style.width).toBe('150px')
  })

  it('minimizes to a corner chip and re-opens from it', async () => {
    render(<BrowserLiveView />)
    await act(async () => { window.dispatchEvent(frameEvent('QUJD')) })
    const min = await screen.findByLabelText('Minimize live browser view to corner')
    await act(async () => { min.click() })
    // full panel gone, chip present
    expect(screen.queryByText('Browser — live')).toBeNull()
    const chip = await screen.findByLabelText('Show live browser view')
    await act(async () => { chip.click() })
    expect(await screen.findByText('Browser — live')).toBeInTheDocument()
  })

  it('stays collapsed (chip) when a stray frame arrives after minimize', async () => {
    render(<BrowserLiveView />)
    await act(async () => { window.dispatchEvent(frameEvent('QUJD')) })
    const min = await screen.findByLabelText('Minimize live browser view to corner')
    await act(async () => { min.click() })
    expect(screen.queryByText('Browser — live')).toBeNull()
    // a later frame must NOT force the full panel back open
    await act(async () => { window.dispatchEvent(frameEvent('WFla')) })
    expect(screen.queryByText('Browser — live')).toBeNull()
    expect(screen.getByLabelText('Show live browser view')).toBeInTheDocument()
  })

  it('opens via the programmatic toggle before any frame arrives', async () => {
    render(<BrowserLiveView />)
    await act(async () => { window.dispatchEvent(toggle()) })
    expect(await screen.findByText('Browser — live')).toBeInTheDocument()
    expect(screen.getByText(/Waiting for the browser/)).toBeInTheDocument()
  })
})
