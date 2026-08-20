import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

import FolderSuggestionCard from '../pages/chat/FolderSuggestionCard'
import reducer, { setFolderSuggestion, clearFolderSuggestion } from '../store/chatSlice'

// No i18n mock on purpose: the visible question renders through <Trans>, which
// reads the REAL catalog no matter what `i18nT` is stubbed to return. Mocking one
// and not the other would assert a sentence the app never shows.
const SPOKEN = (name: string) => `Move this session into folder \u201c${name}\u201d?`

function renderCard(over: Partial<React.ComponentProps<typeof FolderSuggestionCard>> = {}) {
  const onAccept = vi.fn()
  const onDecline = vi.fn()
  const utils = render(
    <FolderSuggestionCard
      folderName="feature"
      breadcrumb="Kiro Crew › feature"
      onAccept={onAccept}
      onDecline={onDecline}
      {...over}
    />,
  )
  return { onAccept, onDecline, ...utils }
}

describe('FolderSuggestionCard', () => {
  it('renders the destination as a folder glyph plus the name in bold', () => {
    renderCard()
    const question = screen.getByTestId('folder-suggestion-question')
    // The whole sentence, assembled by Trans from ONE catalog value.
    expect(question.textContent).toBe('Move this session to feature?')
    // The word "folder" is now the glyph, and the name carries the weight.
    expect(question.querySelector('svg')).toBeTruthy()
    const name = screen.getByTestId('folder-suggestion-folder-name')
    expect(name.textContent).toBe('feature')
    // font-medium, the weight SessionMoveUndoBar gives the same destination name.
    expect(name.className).toContain('font-medium')
  })

  it('keeps the word "folder" for a screen reader, which cannot see the glyph', () => {
    // FolderGlyph is aria-hidden, so without this copy AT would hear
    // "Move this session to later?" — the ambiguity the card exists to remove.
    renderCard({ folderName: 'later', breadcrumb: 'later' })
    expect(screen.getByText(SPOKEN('later'))).toBeInTheDocument()
    // ...and the visible line is hidden from AT, so it is announced once.
    expect(screen.getByTestId('folder-suggestion-question').getAttribute('aria-hidden')).toBe('true')
  })

  it('tints the glyph with the folder color when one is known', () => {
    // Asserted on the svg's `fill` ATTRIBUTE, which is stored verbatim. The
    // stroke paint rides on `style.color`, and both paints are `color-mix(...)`
    // values that the test DOM drops as unparseable while a browser honours
    // them — so style is not observable here and the attribute is.
    const { unmount } = renderCard({ folderColor: 'var(--folder-violet)' })
    const tinted = screen.getByTestId('folder-suggestion-question').querySelector('svg') as SVGElement
    expect(tinted.getAttribute('fill') ?? '').toContain('var(--folder-violet)')
    unmount()
    renderCard()
    const plain = screen.getByTestId('folder-suggestion-question').querySelector('svg') as SVGElement
    expect(plain.getAttribute('fill')).toBe('var(--bg-elevated)')
  })

  it('shows the breadcrumb as ancestry context when the folder is nested', () => {
    renderCard()
    expect(screen.getByText('Kiro Crew › feature')).toBeInTheDocument()
  })

  it('hides the breadcrumb for a root folder, where it only repeats the name', () => {
    renderCard({ folderName: 'Errands', breadcrumb: 'Errands' })
    expect(screen.getByTestId('folder-suggestion-question').textContent).toBe('Move this session to Errands?')
    expect(screen.queryByTitle('Errands')).not.toBeInTheDocument()
  })

  it('keeps the whole question reachable on hover, since the name truncates', () => {
    // The name is the part that ellipsizes, and for a ROOT folder the breadcrumb
    // is suppressed — so this tooltip is the only way to recover a clipped name.
    renderCard({ folderName: 'Errands', breadcrumb: 'Errands' })
    expect(screen.getByTestId('folder-suggestion-question').getAttribute('title')).toBe(SPOKEN('Errands'))
  })

  it('calls onAccept once for the move button', async () => {
    const { onAccept, onDecline } = renderCard()
    await userEvent.click(screen.getByTestId('folder-suggestion-accept'))
    expect(onAccept).toHaveBeenCalledTimes(1)
    expect(onDecline).not.toHaveBeenCalled()
  })

  it('calls onDecline once for the dismiss button', async () => {
    const { onAccept, onDecline } = renderCard()
    await userEvent.click(screen.getByTestId('folder-suggestion-decline'))
    expect(onDecline).toHaveBeenCalledTimes(1)
    expect(onAccept).not.toHaveBeenCalled()
  })

  it('always renders the lucide glyph, never a folder emoji', () => {
    // The card takes no icon prop: an emoji is font-dependent (tofu box wherever
    // the platform has no emoji font) and would not inherit --accent.
    const { container } = render(
      <FolderSuggestionCard folderName="i18n" breadcrumb="Kiro Crew › i18n" onAccept={vi.fn()} onDecline={vi.fn()} />,
    )
    const svg = container.querySelector('svg')
    expect(svg).toBeTruthy()
    expect(svg?.getAttribute('aria-hidden')).toBe('true')
    // No stray emoji anywhere in the rendered text.
    expect(container.textContent ?? '').not.toMatch(/\p{Extended_Pictographic}/u)
  })
})

describe('folderSuggestions reducers', () => {
  const base = () => reducer(undefined, { type: '@@INIT' })

  const setAction = (over: Record<string, unknown> = {}) =>
    setFolderSuggestion({
      slot: 'dashboard_chat-1',
      folderId: 'f1',
      folderName: 'feature',
      breadcrumb: 'Kiro Crew › feature',
      ts: 100,
      ...over,
    } as Parameters<typeof setFolderSuggestion>[0])

  it('stores a card under its own slot key', () => {
    const s = reducer(base(), setAction())
    expect(s.folderSuggestions['dashboard_chat-1']).toMatchObject({ folderId: 'f1', folderName: 'feature', ts: 100 })
  })

  it('ignores a card with no slot, folder id, or folder name', () => {
    let s = reducer(base(), setAction({ slot: '' }))
    expect(Object.keys(s.folderSuggestions)).toHaveLength(0)
    s = reducer(base(), setAction({ folderId: '' }))
    expect(Object.keys(s.folderSuggestions)).toHaveLength(0)
    s = reducer(base(), setAction({ folderName: '' }))
    expect(Object.keys(s.folderSuggestions)).toHaveLength(0)
  })

  it('never indexes state with a prototype-polluting key', () => {
    const s = reducer(base(), setAction({ slot: '__proto__' }))
    expect(Object.keys(s.folderSuggestions)).toHaveLength(0)
  })

  it('clears the card the user acted on', () => {
    const s = reducer(reducer(base(), setAction()), clearFolderSuggestion({ slot: 'dashboard_chat-1', ts: 100 }))
    expect(s.folderSuggestions['dashboard_chat-1']).toBeUndefined()
  })

  it('keeps a NEWER card when a stale clear arrives for the one it replaced', () => {
    let s = reducer(base(), setAction({ ts: 100 }))
    s = reducer(s, setAction({ ts: 200, folderId: 'f2', folderName: 'other' }))
    // The click that started against ts=100 must not delete the ts=200 card the
    // user has not answered yet.
    s = reducer(s, clearFolderSuggestion({ slot: 'dashboard_chat-1', ts: 100 }))
    expect(s.folderSuggestions['dashboard_chat-1']).toMatchObject({ folderId: 'f2', ts: 200 })
  })

  it('clears unconditionally when no ts is supplied', () => {
    const s = reducer(reducer(base(), setAction({ ts: 100 })), clearFolderSuggestion({ slot: 'dashboard_chat-1' }))
    expect(s.folderSuggestions['dashboard_chat-1']).toBeUndefined()
  })

  it('keeps cards for two slots independent', () => {
    let s = reducer(base(), setAction({ slot: 'a', ts: 1 }))
    s = reducer(s, setAction({ slot: 'b', ts: 2, folderId: 'f2', folderName: 'other' }))
    s = reducer(s, clearFolderSuggestion({ slot: 'a', ts: 1 }))
    expect(s.folderSuggestions['a']).toBeUndefined()
    expect(s.folderSuggestions['b']).toMatchObject({ folderId: 'f2' })
  })
})
