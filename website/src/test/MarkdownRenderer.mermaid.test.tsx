import { describe, it, expect, vi } from 'vitest'
import { render } from '@testing-library/react'

vi.mock('mermaid', () => ({
  default: {
    initialize: vi.fn(),
    render: vi.fn().mockResolvedValue({ svg: '<svg></svg>' }),
  },
}))

import mermaid from 'mermaid'
import MarkdownRenderer from '../components/MarkdownRenderer'

describe('MarkdownRenderer mermaid config', () => {
  it('initializes mermaid with suppressErrorRendering so parse errors do not leak error SVGs into the DOM', () => {
    // Regression: without suppressErrorRendering, a mermaid parse error injects a
    // temp <div id="dmermaid-*"> into document.body that render() never cleans up
    // (cleanup only runs on success), accumulating orphaned error graphics.
    render(<MarkdownRenderer content={'```mermaid\ngraph TD;A-->B\n```'} />)
    expect(mermaid.initialize).toHaveBeenCalledWith(
      expect.objectContaining({ suppressErrorRendering: true })
    )
  })
})
