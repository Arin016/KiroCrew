import { memo, useEffect, useRef, useState } from 'react'
import { Copy, Check } from 'lucide-react'
import DOMPurify from 'dompurify'
import hljs from '../utils/hljs'
import { copyToClipboard } from '../utils/clipboard'

export function HighlightedCode({ code, lang, className }: { code: string; lang: string | undefined; className: string }) {
  const [html, setHtml] = useState('')
  const prevRef = useRef('')

  useEffect(() => {
    if (prevRef.current === code) return
    prevRef.current = code
    let highlighted = code
    if (lang && hljs.getLanguage(lang)) {
      try { highlighted = hljs.highlight(code, { language: lang }).value } catch { /* fallback */ }
    } else if (!lang) {
      try { highlighted = hljs.highlightAuto(code).value } catch { /* fallback */ }
    }
    setHtml(DOMPurify.sanitize(highlighted))
  }, [code, lang])

  return (
    <code
      className={`hljs text-[13px] font-mono leading-relaxed ${className}`}
      dangerouslySetInnerHTML={{ __html: html }}
    />
  )
}

export const CodeBlock = memo(function CodeBlock(
  { code, lang, complete, headerActions }: {
    code: string; lang?: string; complete: boolean; headerActions?: React.ReactNode
  },
) {
  const [copied, setCopied] = useState(false)
  const copy = () => { copyToClipboard(code); setCopied(true); setTimeout(() => setCopied(false), 1500) }

  return (
    <div className="code-block group/code rounded-xl border border-border bg-bg-elevated overflow-hidden">
      <div className="flex items-center justify-between px-3 py-1">
        <span className="text-muted text-[13px] font-mono">{lang || 'code'}</span>
        <div className="flex items-center gap-1 opacity-0 group-hover/code:opacity-100 group-focus-within/code:opacity-100 transition-opacity">
          {headerActions}
          <button className="p-1 rounded text-muted hover:text-text hover:bg-bg-hover cursor-pointer" onClick={copy} title={copied ? 'Copied!' : 'Copy'} aria-label={copied ? 'Copied!' : 'Copy'}>
            {copied ? <Check size={13} /> : <Copy size={13} />}
          </button>
        </div>
      </div>
      <pre className="overflow-x-auto scroll-fade px-3 py-2">
        <HighlightedCode code={code} lang={lang} className={lang ? `language-${lang}` : ''} />
        {!complete && <span className="text-muted text-[12px] italic animate-pulse ml-2">generating…</span>}
      </pre>
    </div>
  )
})
