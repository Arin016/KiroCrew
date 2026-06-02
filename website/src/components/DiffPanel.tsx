import { memo, lazy, Suspense } from 'react'
import { monacoLang, useIsDark } from './MonacoCodeBlock'

const MonacoDiffEditor = lazy(() => import('@monaco-editor/react').then(m => ({ default: m.DiffEditor })))

function extOf(fp: string) {
  const i = fp.lastIndexOf('.')
  return i >= 0 ? fp.slice(i + 1).toLowerCase() : ''
}

/**
 * Side-by-side / unified Monaco diff viewer.
 * Used inside DetailPanel when a file-change chip is clicked.
 */
export default memo(function DiffPanel({ filePath, original, modified, sideBySide = true, lineNumbers = false }: {
  filePath: string
  original: string
  modified: string
  sideBySide?: boolean
  lineNumbers?: boolean
}) {
  const isDark = useIsDark()
  const lang = monacoLang(extOf(filePath)) || 'plaintext'

  return (
    <div className="relative w-full h-full flex flex-col">
      <div className="flex-1 overflow-hidden">
        <Suspense fallback={<div className="flex items-center justify-center h-full text-muted text-sm">Loading diff…</div>}>
          <MonacoDiffEditor
            original={original}
            modified={modified}
            language={lang}
            theme={isDark ? 'vs-dark' : 'vs'}
            options={{
              readOnly: true,
              renderSideBySide: sideBySide,
              renderIndicators: false,
              minimap: { enabled: false },
              scrollBeyondLastLine: false,
              fontSize: 13,
              lineNumbers: lineNumbers ? 'on' : 'off',
              scrollbar: { verticalScrollbarSize: 8, horizontalScrollbarSize: 8 },
            }}
            height="100%"
          />
        </Suspense>
      </div>
      <div
        className="shrink-0 flex items-center px-3 h-6 text-[11px] font-mono truncate"
        style={{
          background: isDark ? 'rgba(30,30,30,0.85)' : 'rgba(255,255,255,0.85)',
          backdropFilter: 'blur(8px) saturate(1.2)',
          WebkitBackdropFilter: 'blur(8px) saturate(1.2)',
          color: isDark ? '#858585' : '#6a6a6a',
          borderTop: isDark ? '1px solid #333' : '1px solid #ddd',
        }}
        title={filePath}
      >
        {filePath}
      </div>
    </div>
  )
})
