import { FileText, AlertTriangle, FileQuestion, RefreshCw, Download, Copy, ShieldAlert } from 'lucide-react'
import MarkdownRenderer from '../../components/MarkdownRenderer'
import { EmptyState, Skeleton } from '../../components/ui'
import { IMAGE_EXTS, LANG_BY_EXT } from './constants'
import { extOf, basename, formatBytes, formatTime, isSensitivePath } from './utils'
import type { FileMeta } from './types'

interface FileViewerProps {
  filePath: string | null
  fileMeta: FileMeta | null
  content: string
  loading: boolean
  error: string | null
  onReload: () => void
  onDownload: () => void
}

function renderViewerBody({ ext, fileMeta, content, openFile }: { ext: string; fileMeta: FileMeta; content: string; openFile: string }) {
  if (fileMeta.binary && fileMeta.encoding !== 'base64') {
    return <EmptyState icon={<FileQuestion size={22} />} title="Binary file" subtitle={`${formatBytes(fileMeta.size)} · ${fileMeta.mime || 'unknown'}`} />
  }
  if (IMAGE_EXTS.has(ext) && fileMeta.encoding === 'base64') {
    const src = `data:${fileMeta.mime || 'image/png'};base64,${content}`
    return <div className="mc-fe-img-wrap"><img src={src} alt={openFile} style={{ maxWidth: '100%', maxHeight: '100%' }} /></div>
  }
  if (ext === '.md' || ext === '.markdown') {
    return <MarkdownRenderer content={content || ''} />
  }
  const lang = LANG_BY_EXT[ext] || 'plaintext'
  const maxRun = (content || '').match(/`{3,}/g)?.reduce((max, s) => Math.max(max, s.length), 0) ?? 0
  const fence = '`'.repeat(Math.max(3, maxRun + 1))
  const wrapped = fence + lang + '\n' + (content || '') + '\n' + fence
  return <MarkdownRenderer content={wrapped} />
}

export default function FileViewer({ filePath, fileMeta, content, loading, error, onReload, onDownload }: FileViewerProps) {
  if (!filePath) {
    return <EmptyState icon={<FileText size={28} />} title="Select a file to view" subtitle="Tip: Ctrl/Cmd+F to search" />
  }
  if (loading) return <Skeleton className="h-full w-full" />
  if (error) {
    return <EmptyState icon={<AlertTriangle size={22} style={{ color: 'var(--danger)' }} />} title={error} />
  }
  if (!fileMeta) return null

  const ext = extOf(filePath)
  const fileName = basename(filePath)
  const copyPath = () => { navigator.clipboard?.writeText(filePath) }

  return (
    <>
      <div className="mc-fe-viewer-bar">
        <div className="mc-fe-viewer-title">
          <FileText size={14} style={{ marginRight: 6, opacity: 0.6 }} />
          <span className="mc-fe-viewer-filename">{fileName}</span>
          <button className="mc-fe-iconbtn" title={`Copy path: ${filePath}`} onClick={copyPath} aria-label="Copy path">
            <Copy size={11} />
          </button>
        </div>
        <div className="mc-fe-viewer-actions">
          <span className="mc-fe-viewer-meta">{formatBytes(fileMeta.size)}</span>
          {fileMeta.mtime && <span className="mc-fe-viewer-meta"> · {formatTime(fileMeta.mtime)}</span>}
          {fileMeta.truncated && <span style={{ color: 'var(--warn)', fontSize: 11 }}> · truncated</span>}
          <button className="mc-fe-iconbtn" title="Reload" onClick={onReload} aria-label="Reload"><RefreshCw size={12} /></button>
          <button className="mc-fe-iconbtn" title="Download" onClick={onDownload} aria-label="Download"><Download size={12} /></button>
        </div>
      </div>
      {isSensitivePath(filePath) && (
        <div style={{ padding: '6px 12px', background: 'color-mix(in srgb, var(--warn) 12%, transparent)', borderBottom: '1px solid var(--border)', display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, color: 'var(--warn)' }}>
          <ShieldAlert size={13} /> Sensitive file — avoid sharing your screen while viewing credentials
        </div>
      )}
      <div className="mc-fe-viewer-body">
        {renderViewerBody({ ext, fileMeta, content, openFile: filePath })}
      </div>
    </>
  )
}
