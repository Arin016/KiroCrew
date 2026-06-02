import { useState, useRef, useEffect, useMemo, useCallback } from 'react'
import { createPortal } from 'react-dom'
import { Bot, BrainCircuit, EyeOff, FolderOpen, Ghost, RefreshCw, Undo2, VenetianMask } from 'lucide-react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useBranding } from '../hooks/useBranding'
import { api } from '../api/client'
import { useProvider } from '../providers'
import ChatInput from './ChatInput'
import { Input } from './ui'
import { useFilteredDropdown } from '../hooks/useFilteredDropdown'
import { SourceBadge } from './SourceBadge'
import AgentDropdownList from './AgentDropdownList'
import type { SendMode } from '../pages/chat/ChatSettings'
import ProjectPicker from './ProjectPicker'
import ModelDropdownList from './ModelDropdownList'
import type { PasteBlock } from '../utils/pasteTokens'

interface Agent {
  name: string
  description: string
  source: string
  kiro_agent?: string
}

interface Workspace {
  name: string
  path: string
  is_default: boolean
}

interface HistorySession {
  key: string
  title?: string
  created?: string
}

const MAX_SUGGESTIONS = 5
const DEBOUNCE_MS = 300

interface WelcomeViewProps {
  mode?: string
  input: string
  setInput: (v: string) => void
  send: () => void
  currentAgent: string
  switchAgent: (name: string) => void
  installedAgents: Agent[]
  defaultAgent: string
  currentModel?: string
  switchModel?: (name: string) => void
  availableModels?: { name: string; description: string }[]
  currentWorkspace?: string
  switchWorkspace?: (name: string) => void
  availableWorkspaces?: Workspace[]
  prefillHint?: boolean
  onDismissHint?: () => void
  onUploadFiles?: (files: File[]) => void
  uploading?: boolean
  history?: HistorySession[]
  onResumeSession?: (key: string, title: string) => void
  voiceRecording?: boolean
  voiceTranscribing?: boolean
  onVoiceToggle?: () => void
  onFileSelect?: (path: string) => void
  onFileOpen?: (path: string) => void
  pendingFiles?: string[]
  onRemoveFile?: (path: string) => void
  memoryMode?: string
  onSwitchMode?: (mode: 'persistent' | 'incognito' | 'temporary') => void
  project?: string
  onProjectChange?: (path: string) => void
  sendOnEnter?: SendMode
  pasteBlocks?: PasteBlock[]
  onPasteBlocksChange?: (blocks: PasteBlock[]) => void
  browseMode?: boolean
  onBrowseToggle?: () => void
}

const FALLBACK_SUGGESTIONS = [
  'Check my pipeline status',
  'Triage my on-call tickets',
  'Search code for usage examples',
  'Summarize this week\'s Slack activity',
  'Help me write a design doc',
  'Review my latest CR',
]

function SuggestedPills({ setInput }: { setInput: (v: string) => void }) {
  const qc = useQueryClient()
  const [refreshing, setRefreshing] = useState(false)
  const { data, isFetching } = useQuery({
    queryKey: ['suggestions'],
    queryFn: () => api.suggestions(),
    staleTime: 5 * 60_000,
    refetchInterval: 10 * 60_000,
    refetchOnWindowFocus: false,
  })

  const pills = data?.suggestions?.length ? data.suggestions : FALLBACK_SUGGESTIONS

  const handleRefresh = async () => {
    setRefreshing(true)
    try {
      const fresh = await api.suggestions(true)
      qc.setQueryData(['suggestions'], fresh)
    } catch {}
    setRefreshing(false)
  }

  const spinning = isFetching || refreshing

  return (
    <div className="flex gap-x-2 gap-y-1 flex-wrap px-5 pt-1 pb-0 -mb-1 max-w-[900px] mx-auto w-full items-center">
      {pills.map(s => (
        <button key={s} className="shrink-0 px-3 py-1.5 rounded-lg text-[13px] cursor-pointer transition-all relative border border-border text-muted hover:text-text bg-bg-elevated" onClick={() => setInput(s)}>
          {s}
        </button>
      ))}
      <button
        onClick={handleRefresh}
        disabled={spinning}
        className="p-1.5 rounded-lg text-muted hover:text-accent border border-transparent hover:border-border transition-all cursor-pointer bg-transparent"
        title="Refresh suggestions"
      >
        <RefreshCw size={13} className={spinning ? 'animate-spin' : ''} />
      </button>
    </div>
  )
}

export default function WelcomeView({
  mode,
  input, setInput, send,
  currentAgent, switchAgent, installedAgents,
  defaultAgent,
  currentModel = '', switchModel, availableModels = [],
  prefillHint, onDismissHint, onUploadFiles, uploading,
  history = [], onResumeSession,
  voiceRecording, voiceTranscribing, onVoiceToggle,
  onFileSelect, onFileOpen, pendingFiles = [], onRemoveFile,
  memoryMode,
  onSwitchMode,
  project,
  onProjectChange,
  sendOnEnter,
  pasteBlocks,
  onPasteBlocksChange,
  browseMode,
  onBrowseToggle,
}: WelcomeViewProps) {
  const provider = useProvider()
  const agentItems = installedAgents.filter(a => a.name !== 'kiroclaw-lite')
  const { open: agentOpen, setOpen: setAgentOpen, filter: agentFilter, setFilter: setAgentFilter, dropdownRef: agentDropdownRef, inputRef: agentInputRef, filtered: filteredAgents } = useFilteredDropdown(agentItems)
  const agentBtnRef = useRef<HTMLButtonElement>(null)
  const { open: modelOpen, setOpen: setModelOpen, filter: modelFilter, setFilter: setModelFilter, dropdownRef: modelDropdownRef, inputRef: modelInputRef, filtered: filteredModels } = useFilteredDropdown(availableModels)
  const modelBtnRef = useRef<HTMLButtonElement>(null)
  const projBtnRef = useRef<HTMLButtonElement>(null)
  const { botName, avatar } = useBranding()
  const [dragOver, setDragOver] = useState(false)
  const [showProjPicker, setShowProjPicker] = useState(false)
  const [anonOpen, setAnonOpen] = useState(false)
  const anonBtnRef = useRef<HTMLButtonElement>(null)
  const anonPopRef = useRef<HTMLDivElement>(null)
  // Close anon popover on outside click
  useEffect(() => {
    if (!anonOpen) return
    const handler = (e: MouseEvent) => {
      const t = e.target as Node
      if (anonPopRef.current?.contains(t) || anonBtnRef.current?.contains(t)) return
      setAnonOpen(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [anonOpen])
  const currentMode = (memoryMode ?? 'persistent') as 'persistent' | 'incognito' | 'temporary'
  const handleDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault(); e.stopPropagation(); setDragOver(false)
    const files = Array.from(e.dataTransfer.files)
    if (files.length && onUploadFiles) onUploadFiles(files)
  }, [onUploadFiles])

  // Resolve agent's model and sync to parent
  const currentModelRef = useRef(currentModel)
  currentModelRef.current = currentModel
  const resolvedTemplateName = (() => { const mc = installedAgents.find(a => a.name === currentAgent); return provider.resolveAgentTemplate(mc || { name: currentAgent }) })()
  const { data: resolvedAgentModel } = useQuery({
    queryKey: ['resolved-model', resolvedTemplateName, provider.id],
    queryFn: () => provider.resolveModel(resolvedTemplateName),
    enabled: !!resolvedTemplateName,
  })
  const switchModelRef = useRef(switchModel)
  switchModelRef.current = switchModel
  useEffect(() => { if (resolvedAgentModel && switchModelRef.current && resolvedAgentModel !== currentModelRef.current) switchModelRef.current(resolvedAgentModel) }, [resolvedAgentModel]) // eslint-disable-line react-hooks/exhaustive-deps
  const displayModel = currentModel || 'auto'

  // Debounced search query for history suggestions
  const [debouncedQuery, setDebouncedQuery] = useState('')
  const [dismissed, setDismissed] = useState(false)

  useEffect(() => {
    if (!input.trim()) { setDebouncedQuery(''); setDismissed(false); return }
    setDismissed(false)
    const t = setTimeout(() => {
      setDebouncedQuery(input.trim().toLowerCase())
    }, DEBOUNCE_MS)
    return () => clearTimeout(t)
  }, [input])

  const suggestions = useMemo(() => {
    if (!debouncedQuery || !history.length) return []
    return history
      .filter(s =>
        (s.title || '').toLowerCase().includes(debouncedQuery) ||
        s.key.toLowerCase().includes(debouncedQuery)
      )
      .slice(0, MAX_SUGGESTIONS)
  }, [debouncedQuery, history])

  const showSuggestions = suggestions.length > 0 && !dismissed

  // Dismiss on Escape
  useEffect(() => {
    if (!showSuggestions) return
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setDismissed(true) }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [showSuggestions])

  const currentAgentInfo = installedAgents.find(a => a.name === currentAgent)

  return (
    <div className="flex-1 flex flex-col">
      {/* Centered branding + pickers */}
      <div className="flex-1 flex flex-col items-center justify-center gap-6 px-8 pb-16">
        {mode === 'orchestrator' && <img src={avatar} alt={botName} className="w-16 h-16 rounded-xl object-cover" />}
        <div className="text-center">
          <h2 className="text-5xl font-light text-text-strong mb-1 tracking-tight">{mode === 'orchestrator' ? 'Autopilot' : 'What can I do for you?'}</h2>
          <p className="text-[13px] text-muted">{mode === 'orchestrator' ? 'Simple tasks run instantly. Complex ones get a plan you approve first.' : ''}</p>
        </div>
        {mode === 'orchestrator' && (
          <button
            className="px-4 py-2 rounded-lg text-[13px] text-muted border border-border bg-card hover:border-accent hover:text-text transition-all cursor-pointer"
            onClick={() => setInput('Create a plan to analyze KiroClaw code package and report file count by major components')}
          >
            Try: &ldquo;Create a plan to analyze KiroClaw code package and report file count by major components&rdquo;
          </button>
        )}
        {onSwitchMode && (
          <>
            <button
              ref={anonBtnRef}
              className="flex items-center gap-1.5 text-[12px] text-muted hover:text-warn transition-colors"
              onClick={() => {
                if (currentMode === 'persistent') setAnonOpen(!anonOpen)
                else onSwitchMode('persistent')
              }}
            >
              {currentMode === 'persistent' ? <Ghost size={13} /> : <Undo2 size={13} />}
              <span>{currentMode === 'persistent' ? 'Switch to ephemeral mode' : 'Switch back to default mode'}</span>
            </button>
            {anonOpen && createPortal(
              <div
                ref={anonPopRef}
                className="fixed z-[9999] bg-bg-elevated border border-border rounded-xl shadow-xl p-2 flex gap-2"
                style={(() => { const r = anonBtnRef.current?.getBoundingClientRect(); return { top: r ? r.bottom + 6 : '50%', left: r ? r.left + r.width / 2 : '50%', transform: 'translateX(-50%)' } })()}
              >
                {([
                  { key: 'incognito' as const, Icon: EyeOff, label: 'Incognito', desc: 'Memory reads enabled, writes disabled', color: 'text-warn' },
                  { key: 'temporary' as const, Icon: VenetianMask, label: 'Temporary', desc: 'Blank slate — no memory reads or writes', color: 'text-aim' },
                ] as const).map(t => (
                  <button
                    key={t.key}
                    className="w-[220px] p-3 rounded-lg border border-border hover:border-accent hover:bg-bg-hover transition-all text-left flex flex-col gap-1.5"
                    onClick={() => { onSwitchMode(t.key); setAnonOpen(false) }}
                  >
                    <div className="flex items-center gap-1.5 text-[13px] font-semibold text-text">
                      <t.Icon size={14} className={t.color} />
                      <span>{t.label}</span>
                    </div>
                    <div className="text-[11px] text-muted leading-snug">{t.desc}</div>
                  </button>
                ))}
              </div>,
              document.body
            )}
          </>
        )}
        <div className="flex gap-4 flex-wrap justify-center">
          {/* Agent picker — custom dropdown matching in-chat style */}
          <div className="flex flex-col gap-1.5 w-[340px] min-w-[260px]">
            <label className="text-[12px] text-muted font-medium uppercase tracking-wider">Agent</label>
            <div className="relative">
              <button ref={agentBtnRef} className="flex items-center gap-2 bg-bg-elevated border border-border rounded-lg px-3 py-2 h-10 text-sm font-mono min-w-[260px] w-full cursor-pointer focus-ring hover:border-border-strong transition-all" onClick={() => setAgentOpen(!agentOpen)}>
                <span className="text-accent"><Bot size={14} /></span>
                <span className="text-text font-semibold truncate min-w-0">{currentAgent}</span>
                {currentAgentInfo?.source && <SourceBadge source={currentAgentInfo.source} />}
                <span className="ml-auto text-muted text-[12px] shrink-0">▾</span>
              </button>
              {agentOpen && agentBtnRef.current && createPortal(
                <div ref={agentDropdownRef} className="fixed z-[9999] bg-card border border-border rounded-lg shadow-lg min-w-[260px] max-w-[340px] max-h-[320px] flex flex-col overflow-hidden animate-slide-up" style={(() => { const r = agentBtnRef.current!.getBoundingClientRect(); const dropH = 320; const top = r.bottom + 4 + dropH > window.innerHeight ? r.top - dropH - 4 : r.bottom + 4; const left = Math.max(8, Math.min(r.left, window.innerWidth - 348)); return { top, left } })()}>
                  <div className="p-2 border-b border-border">
                    <Input ref={agentInputRef} type="text" placeholder="Type to filter…" value={agentFilter} onChange={e => setAgentFilter(e.target.value)} onKeyDown={e => { if (e.key === 'Escape') setAgentOpen(false); if (e.key === 'Enter' && filteredAgents.length === 1) { switchAgent(filteredAgents[0].name); setAgentOpen(false) } }} className="w-full px-2 py-1 text-[13px] font-mono" />
                  </div>
                  <AgentDropdownList agents={filteredAgents} activeAgent={currentAgent} defaultAgent={defaultAgent} onSelect={name => { switchAgent(name); setAgentOpen(false) }} />
                </div>,
                document.body
              )}
            </div>
          </div>
          {/* Model picker */}
          {switchModel && availableModels.length > 0 && (
          <div className="flex flex-col gap-1.5 w-[340px] min-w-[260px]">
            <label className="text-[12px] text-muted font-medium uppercase tracking-wider">Model</label>
            <div className="relative">
              <button ref={modelBtnRef} className="flex items-center gap-2 bg-bg-elevated border border-border rounded-lg px-3 py-2 h-10 text-sm font-mono min-w-[260px] w-full cursor-pointer focus-ring hover:border-border-strong transition-all" onClick={() => setModelOpen(!modelOpen)}>
                <span className="text-muted"><BrainCircuit size={14} /></span>
                <span className="text-text font-semibold truncate min-w-0">{displayModel}</span>
                <span className="ml-auto text-muted text-[12px] shrink-0">▾</span>
              </button>
              {modelOpen && modelBtnRef.current && createPortal(
                <div ref={modelDropdownRef} className="fixed z-[9999] bg-card border border-border rounded-lg shadow-lg min-w-[260px] max-w-[340px] max-h-[320px] flex flex-col overflow-hidden animate-slide-up" style={(() => { const r = modelBtnRef.current!.getBoundingClientRect(); const dropH = 320; const top = r.bottom + 4 + dropH > window.innerHeight ? r.top - dropH - 4 : r.bottom + 4; const left = Math.max(8, Math.min(r.left, window.innerWidth - 348)); return { top, left } })()}>
                  <div className="p-2 border-b border-border">
                    <Input ref={modelInputRef} type="text" placeholder="Type to filter…" value={modelFilter} onChange={e => setModelFilter(e.target.value)} onKeyDown={e => { if (e.key === 'Escape') setModelOpen(false); if (e.key === 'Enter' && filteredModels.length === 1) { switchModel(filteredModels[0].name); setModelOpen(false) } }} className="w-full px-2 py-1 text-[13px] font-mono" />
                  </div>
                  <ModelDropdownList models={filteredModels} activeModel={displayModel} onSelect={name => { switchModel(name); setModelOpen(false) }} />
                </div>,
                document.body
              )}
            </div>
          </div>
          )}
          {/* Project picker */}
          {onProjectChange && (
          <div className="flex flex-col gap-1.5 w-[340px] min-w-[260px]">
            <label className="text-[12px] text-muted font-medium uppercase tracking-wider">Project</label>
            <div className="relative">
              <button ref={projBtnRef} className="flex items-center gap-2 bg-bg-elevated border border-border rounded-lg px-3 py-2 h-10 text-sm font-mono min-w-[260px] w-full cursor-pointer focus-ring hover:border-border-strong transition-all" onClick={() => setShowProjPicker(!showProjPicker)}>
                <span className="text-muted"><FolderOpen size={14} /></span>
                <span className={`truncate min-w-0 ${project ? 'text-text font-semibold' : 'text-muted'}`}>{project || 'Select project…'}</span>
                <span className="ml-auto text-muted text-[12px] shrink-0">▾</span>
              </button>
              <ProjectPicker open={showProjPicker} onOpenChange={setShowProjPicker} anchorRef={projBtnRef} onSelect={path => { onProjectChange(path) }} />
            </div>
          </div>
          )}
        </div>
      </div>
      {/* Input pinned to bottom with suggestions floating above */}
      <div className="relative">
        {showSuggestions && (
          <div className="absolute left-0 right-0 bottom-full mb-1 mx-4 border border-border rounded-lg bg-card overflow-hidden animate-scale-in z-50 shadow-lg flex flex-col max-h-[min(300px,40vh)]">
            <div className="px-3.5 py-2.5 border-b border-border shrink-0">
              <span className="text-[12px] font-semibold text-muted tracking-[.02em]">Continue a previous chat?</span>
            </div>
            <div className="overflow-y-auto flex-1 min-h-0" role="listbox" aria-label="Previous chats">
              {suggestions.map((s) => (
                <div
                  key={s.key}
                  role="option"
                  tabIndex={0}
                  aria-selected={false}
                  className="w-full text-left px-3.5 py-2.5 flex items-center gap-3 cursor-pointer transition-all border-b border-border last:border-0 hover:bg-bg-hover"
                  onMouseDown={(e) => { e.preventDefault(); onResumeSession?.(s.key, s.title || s.key) }}
                  onKeyDown={(e) => { if (e.key === 'Enter') onResumeSession?.(s.key, s.title || s.key) }}
                >
                  <div className="flex-1 min-w-0">
                    <div className="font-mono text-[13px] text-text truncate">{s.title || s.key}</div>
                    {s.created && <div className="text-[11px] text-muted font-mono mt-0.5">{new Date(s.created).toLocaleDateString([], { year: 'numeric', month: 'short', day: 'numeric' })}</div>}
                  </div>
                  <span className="text-accent text-[14px] shrink-0">↩</span>
                </div>
              ))}
            </div>
            <div className="px-3.5 py-2 border-t border-border flex justify-end shrink-0">
              <span className="text-[11px] text-muted-strong">Esc to dismiss</span>
            </div>
          </div>
        )}
        {mode !== 'orchestrator' && (
          <SuggestedPills setInput={setInput} />
        )}
        <ChatInput
          value={input}
          onChange={setInput}
          onSend={send}
          prefillHint={prefillHint}
          onDismissHint={onDismissHint}
          onUploadFiles={onUploadFiles}
          uploading={uploading}
          onDrop={handleDrop}
          dragOver={dragOver}
          onFileSelect={onFileSelect}
          onFileOpen={onFileOpen}
          pendingFiles={pendingFiles}
          onRemoveFile={onRemoveFile}
          project={project}
          onDragOver={e => { e.preventDefault(); e.stopPropagation(); setDragOver(true) }}
          onDragLeave={e => { if (e.currentTarget === e.target) setDragOver(false) }}
          voiceRecording={voiceRecording}
          voiceTranscribing={voiceTranscribing}
          onVoiceToggle={onVoiceToggle}
          memoryMode={currentMode}
          sendOnEnter={sendOnEnter}
          pasteBlocks={pasteBlocks}
          onPasteBlocksChange={onPasteBlocksChange}
          browseMode={browseMode}
          onBrowseToggle={onBrowseToggle}
        />
      </div>
    </div>
  )
}
