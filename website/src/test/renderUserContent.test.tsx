import { describe, it, expect, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import { renderUserContent } from '../pages/ChatPage'

const noop = () => {}

describe('renderUserContent — markdown rendering', () => {
  it('renders bold text via markdown', () => {
    const { container } = render(<>{renderUserContent('hello **bold** world', undefined, noop)}</>)
    expect(container.querySelector('strong')).toHaveTextContent('bold')
  })

  it('renders italic text via markdown', () => {
    const { container } = render(<>{renderUserContent('hello *italic* world', undefined, noop)}</>)
    expect(container.querySelector('em')).toHaveTextContent('italic')
  })

  it('renders inline code via markdown', () => {
    const { container } = render(<>{renderUserContent('use `npm install`', undefined, noop)}</>)
    expect(container.querySelector('code')).toHaveTextContent('npm install')
  })

  it('renders links via markdown', () => {
    const { container } = render(<>{renderUserContent('see [docs](https://example.com)', undefined, noop)}</>)
    const link = container.querySelector('a')
    expect(link).toHaveTextContent('docs')
    expect(link).toHaveAttribute('href', 'https://example.com')
  })

  it('renders unordered lists via markdown', () => {
    const { container } = render(<>{renderUserContent('- item one\n- item two', undefined, noop)}</>)
    const items = container.querySelectorAll('li')
    expect(items).toHaveLength(2)
    expect(items[0]).toHaveTextContent('item one')
  })

  it('renders code blocks via markdown', () => {
    const { container } = render(<>{renderUserContent('```js\nconst x = 1\n```', undefined, noop)}</>)
    expect(container.querySelector('pre')).toBeInTheDocument()
    expect(container.querySelector('code')).toHaveTextContent('const x = 1')
  })

  it('renders plain text without extra wrapping issues', () => {
    const { container } = render(<>{renderUserContent('just plain text', undefined, noop)}</>)
    expect(container).toHaveTextContent('just plain text')
  })

  it('renders image markdown', () => {
    const { container } = render(<>{renderUserContent('![alt](/path/to/img.png)', undefined, noop)}</>)
    const img = container.querySelector('img')
    expect(img).toBeInTheDocument()
    // MarkdownRenderer rewrites local paths to /api/file-raw?path=<encoded>
    expect(decodeURIComponent(img?.getAttribute('src') || '')).toContain('/path/to/img.png')
  })

  it('renders file chips for attached files with markdown in surrounding text', () => {
    const content = '[attached_file 1] /home/user/file.ts\ncheck this **bold** text'
    const { container } = render(<>{renderUserContent(content, undefined, noop)}</>)
    // File chip rendered
    expect(container.querySelector('[title="/home/user/file.ts"]')).toBeInTheDocument()
    // Markdown in remaining text
    expect(container.querySelector('strong')).toHaveTextContent('bold')
  })
})
