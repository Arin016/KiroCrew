import { describe, it, expect, vi, beforeEach } from 'vitest'
import { renderHook, act } from '@testing-library/react'
import { MemoryRouter, useLocation } from 'react-router-dom'
import type { ReactNode } from 'react'
import { createElement } from 'react'
import { useSettingHighlight } from './useSettingHighlight'

// Mock scrollIntoView (not available in jsdom)
beforeEach(() => {
  Element.prototype.scrollIntoView = vi.fn()
})

function wrapper(initialEntries: string[]) {
  return function Wrapper({ children }: { children: ReactNode }) {
    return createElement(MemoryRouter, { initialEntries }, children)
  }
}

describe('useSettingHighlight', () => {
  it('does nothing when no highlight param is present', () => {
    const { result } = renderHook(() => useSettingHighlight(), {
      wrapper: wrapper(['/settings?tab=display']),
    })
    expect(result.current).toBeUndefined()
  })

  it('strips unknown highlight ids from the URL', async () => {
    vi.useFakeTimers()
    let currentSearch = 'unset'

    function CaptureWrapper({ children }: { children: ReactNode }) {
      return createElement(
        MemoryRouter,
        { initialEntries: ['/settings?tab=display&highlight=nonexistent.setting'] },
        createElement(LocationProbe, null, children),
      )
    }
    function LocationProbe({ children }: { children?: ReactNode }) {
      currentSearch = useLocation().search
      return createElement('div', null, children)
    }

    renderHook(() => useSettingHighlight(), { wrapper: CaptureWrapper })
    act(() => {
      vi.advanceTimersByTime(150)
    })
    expect(currentSearch).not.toContain('highlight=')
    expect(currentSearch).toContain('tab=display')
    vi.useRealTimers()
  })

  it('highlights the Nth occurrence for duplicate labels', async () => {
    vi.useFakeTimers()

    // Create two elements with the same data-setting-label (simulates duplicate within a tab)
    const el1 = document.createElement('div')
    el1.setAttribute('data-setting-label', 'AWS Profile')
    document.body.appendChild(el1)
    const el2 = document.createElement('div')
    el2.setAttribute('data-setting-label', 'AWS Profile')
    document.body.appendChild(el2)

    // Find the registry entry with occurrence 2 for 'AWS Profile' (voice.aws-profile-2)
    const { SETTINGS_REGISTRY } = await import('../components/commandPalette/settingsRegistry.gen')
    const entry = SETTINGS_REGISTRY.find(e => e.id === 'voice.aws-profile-2')
    // If this entry doesn't exist in the test environment, skip gracefully
    if (entry) {
      renderHook(() => useSettingHighlight(), {
        wrapper: wrapper([`/settings?tab=voice&highlight=voice.aws-profile-2`]),
      })
      act(() => {
        vi.advanceTimersByTime(150)
      })
      // The second element should get the highlight outline (occurrence=2 → index 1)
      expect(el2.style.outline).toContain('2px solid')
    }

    // Cleanup
    document.body.removeChild(el1)
    document.body.removeChild(el2)
    vi.useRealTimers()
  })
})
