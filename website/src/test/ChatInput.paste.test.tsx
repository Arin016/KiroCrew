import { describe, it, expect, vi } from 'vitest'
import { screen, fireEvent } from '@testing-library/react'
import { renderWithProviders } from './helpers'
import ChatInput from '../components/ChatInput'

/**
 * fireEvent.paste passes eventProperties into the native event's clipboardData,
 * but jsdom's DataTransferItemList doesn't support our custom items array.
 * Instead we rely on the fact that React's SyntheticEvent reads from the native
 * event's clipboardData. We set `types` (which jsdom respects) and for the
 * file-upload path we verify the guard logic via the negative tests.
 */

describe('ChatInput paste: prefer text over image', () => {
  it('does NOT upload files when clipboard has text/plain alongside image (macOS Office copy)', () => {
    const onUploadFiles = vi.fn()
    renderWithProviders(
      <ChatInput value="" onChange={vi.fn()} onSend={vi.fn()} onUploadFiles={onUploadFiles} />,
    )
    const textarea = screen.getByRole('textbox')
    // Simulate macOS Office clipboard: text/plain + text/html + Files (with image representation)
    fireEvent.paste(textarea, {
      clipboardData: {
        types: ['text/plain', 'text/html', 'Files'],
        items: [
          { kind: 'text', type: 'text/plain', getAsFile: () => null },
          { kind: 'file', type: 'image/png', getAsFile: () => new File(['px'], 'image.png', { type: 'image/png' }) },
        ],
        getData: () => 'Hello from Word',
      },
    })
    expect(onUploadFiles).not.toHaveBeenCalled()
  })

  it('does NOT upload files when clipboard has text/html alongside image', () => {
    const onUploadFiles = vi.fn()
    renderWithProviders(
      <ChatInput value="" onChange={vi.fn()} onSend={vi.fn()} onUploadFiles={onUploadFiles} />,
    )
    const textarea = screen.getByRole('textbox')
    fireEvent.paste(textarea, {
      clipboardData: {
        types: ['text/html', 'Files'],
        items: [
          { kind: 'file', type: 'image/png', getAsFile: () => new File(['px'], 'image.png', { type: 'image/png' }) },
        ],
        getData: () => '<b>rich</b>',
      },
    })
    expect(onUploadFiles).not.toHaveBeenCalled()
  })

  it('allows file upload when clipboard has ONLY files (e.g. screenshot paste)', () => {
    const onUploadFiles = vi.fn()
    renderWithProviders(
      <ChatInput value="" onChange={vi.fn()} onSend={vi.fn()} onUploadFiles={onUploadFiles} />,
    )
    const textarea = screen.getByRole('textbox')
    const file = new File(['px'], 'screenshot.png', { type: 'image/png' })
    fireEvent.paste(textarea, {
      clipboardData: {
        types: ['Files'],
        items: [{ kind: 'file', type: 'image/png', getAsFile: () => file }],
        getData: () => '',
      },
    })
    expect(onUploadFiles).toHaveBeenCalledWith([file])
  })
})
