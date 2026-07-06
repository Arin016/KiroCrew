import { useState } from 'react'
import { Input } from './ui'

export interface SkillFormData {
  name: string
  category: string
  description: string
  triggers: string
  tags: string
  always: boolean
  body: string
  /** Raw markdown content (frontmatter + body). Used in raw editing mode. */
  raw?: string
}

interface SkillFormProps {
  data: SkillFormData
  onChange: (data: SkillFormData) => void
  /** Hide name/category fields (used when editing an existing skill) */
  hideIdentity?: boolean
  /** Show raw mode toggle (default: true) */
  allowRaw?: boolean
}

/** Parse YAML frontmatter from raw skill content. Shared by SkillsTab (display) and SkillForm (edit). */
export function parseFrontmatter(raw: string): { meta: Record<string, string>; body: string } {
  if (!raw.startsWith('---')) return { meta: {}, body: raw }
  const end = raw.indexOf('\n---', 3)
  if (end === -1) return { meta: {}, body: raw }
  const yamlBlock = raw.slice(4, end)
  const meta: Record<string, string> = {}
  let currentKey = ''
  for (const line of yamlBlock.split('\n')) {
    const match = line.match(/^(\w[\w-]*):\s*(.*)$/)
    if (match) {
      currentKey = match[1]
      const val = match[2].trim()
      // Handle YAML block scalar indicators (| and >)
      meta[currentKey] = (val === '|' || val === '>') ? '' : val
    } else if (currentKey && (line.startsWith('  ') || line.startsWith('\t'))) {
      meta[currentKey] += (meta[currentKey] ? '\n' : '') + line.trim()
    }
  }
  return { meta, body: raw.slice(end + 4).trim() }
}

/** Assemble YAML frontmatter + body from structured fields */
export function assembleSkillContent(data: SkillFormData): string {
  // If raw mode was used, return raw content directly
  if (data.raw !== undefined) return data.raw

  const lines = ['---']
  lines.push(`name: ${data.name}`)
  if (data.description) {
    if (data.description.includes('\n')) {
      lines.push('description: |')
      for (const l of data.description.split('\n')) lines.push(`  ${l}`)
    } else {
      lines.push(`description: ${data.description}`)
    }
  }
  if (data.always) lines.push('always: true')
  if (data.triggers) lines.push(`triggers: ${data.triggers}`)
  if (data.tags) lines.push(`tags: [${data.tags}]`)
  lines.push('---')
  lines.push('')
  lines.push(data.body || `# ${data.name}\n`)
  return lines.join('\n')
}

/** Parse raw skill content into structured form data */
export function parseSkillContent(raw: string, key: string): SkillFormData {
  const slash = key.indexOf('/')
  const name = slash > 0 ? key.slice(slash + 1) : key
  const category = slash > 0 ? key.slice(0, slash) : ''

  const { meta, body } = parseFrontmatter(raw)
  if (!Object.keys(meta).length && !raw.startsWith('---')) {
    return { name, category, description: '', triggers: '', tags: '', always: false, body: raw }
  }

  // Clean up tags — strip brackets
  const tagsRaw = meta.tags || ''
  const tags = tagsRaw.replace(/[\[\]]/g, '').trim()

  return {
    name: meta.name || name,
    category,
    description: meta.description || '',
    triggers: meta.triggers || '',
    tags,
    always: meta.always === 'true',
    body,
  }
}

export default function SkillForm({ data, onChange, hideIdentity, allowRaw = true }: SkillFormProps) {
  const [rawMode, setRawMode] = useState(false)

  const set = <K extends keyof SkillFormData>(key: K, value: SkillFormData[K]) =>
    onChange({ ...data, [key]: value })

  const switchToRaw = () => {
    const assembled = assembleSkillContent({ ...data, raw: undefined })
    onChange({ ...data, raw: assembled })
    setRawMode(true)
  }

  const switchToStructured = () => {
    if (data.raw !== undefined) {
      const parsed = parseSkillContent(data.raw, data.category ? `${data.category}/${data.name}` : data.name)
      onChange({ ...parsed, raw: undefined })
    }
    setRawMode(false)
  }

  if (rawMode) {
    return (
      <div className="flex flex-col gap-2">
        <div className="flex items-center justify-between">
          <span className="text-[12px] text-muted font-mono">Raw YAML + Markdown</span>
          <button className="text-[12px] text-accent hover:text-accent-hover cursor-pointer transition-colors" onClick={switchToStructured}>Switch to structured editor</button>
        </div>
        <textarea
          aria-label="Raw YAML and Markdown"
          className="w-full bg-bg-elevated border border-border rounded-md p-3 text-text font-mono text-[13px] outline-none resize-y leading-normal transition-colors focus-ring"
          rows={20}
          value={data.raw || ''}
          onChange={e => onChange({ ...data, raw: e.target.value })}
        />
      </div>
    )
  }

  return (
    <div className="flex flex-col gap-3">
      {allowRaw && (
        <div className="flex justify-end">
          <button className="text-[12px] text-accent hover:text-accent-hover cursor-pointer transition-colors" onClick={switchToRaw}>Edit raw markdown</button>
        </div>
      )}
      {!hideIdentity && <>
        <div>
          {/* label-has-for can't resolve the control through the custom <Input>
              component; the runtime association via htmlFor + id + aria-label is correct. */}
          {/* eslint-disable-next-line jsx-a11y/label-has-for */}
          <label htmlFor="skill-name" className="text-[13px] font-semibold text-text mb-1 block">Name</label>
          <Input id="skill-name" aria-label="Name" placeholder="e.g. my-tool" value={data.name} onChange={e => set('name', e.target.value)} className="w-full" />
        </div>
        <div>
          {/* eslint-disable-next-line jsx-a11y/label-has-for -- control resolved at runtime via htmlFor + id */}
          <label htmlFor="skill-category" className="text-[13px] font-semibold text-text mb-1 block">Category <span className="text-muted font-normal">(optional)</span></label>
          <Input id="skill-category" aria-label="Category" placeholder="e.g. utils, code" value={data.category} onChange={e => set('category', e.target.value)} className="w-full" />
          <div className="text-[11px] text-muted mt-1">Groups the skill in the list. Leave empty for the general category.</div>
        </div>
      </>}
      <div>
        <label htmlFor="skill-description" className="text-[13px] font-semibold text-text mb-1 block">
          <span className="block mb-1">Description</span>
          <textarea id="skill-description" aria-label="Description" className="w-full bg-bg-elevated border border-border rounded-md px-3 py-2 text-text text-sm font-body outline-none resize-y leading-relaxed transition-colors focus-ring" rows={3} placeholder="What this skill does and when the agent should use it" value={data.description} onChange={e => set('description', e.target.value)} />
        </label>
      </div>
      <div>
        {/* label-has-for can't resolve the control through the custom <Input>
            component; the runtime association via htmlFor + id + aria-label is correct. */}
        {/* eslint-disable-next-line jsx-a11y/label-has-for */}
        <label htmlFor="skill-triggers" className="text-[13px] font-semibold text-text mb-1 block">Triggers</label>
        <Input id="skill-triggers" aria-label="Triggers" placeholder="keyword1, keyword2, keyword3" value={data.triggers} onChange={e => set('triggers', e.target.value)} className="w-full" />
        <div className="text-[11px] text-muted mt-1">Comma-separated keywords that activate this skill. Prefix with ! to exclude.</div>
      </div>
      <div>
        {/* eslint-disable-next-line jsx-a11y/label-has-for -- control resolved at runtime via htmlFor + id */}
        <label htmlFor="skill-tags" className="text-[13px] font-semibold text-text mb-1 block">Tags <span className="text-muted font-normal">(optional)</span></label>
        <Input id="skill-tags" aria-label="Tags" placeholder="skill, tool, aws" value={data.tags} onChange={e => set('tags', e.target.value)} className="w-full" />
        <div className="text-[11px] text-muted mt-1">Comma-separated labels for categorization. Metadata only — not used for matching.</div>
      </div>
      <div className="flex items-center gap-2">
        <label htmlFor="skill-always" className="flex items-center gap-2 text-[13px] text-text cursor-pointer">
          <input type="checkbox" id="skill-always" aria-label="Always loaded" checked={data.always} onChange={e => set('always', e.target.checked)} className="accent-accent" />
          <span>Always loaded <span className="text-muted">(inject full content into every session)</span></span>
        </label>
      </div>
      <div>
        <label htmlFor="skill-instructions" className="text-[13px] font-semibold text-text mb-1 block">
          <span className="block mb-1">Instructions</span>
          <textarea id="skill-instructions" aria-label="Instructions" className="w-full bg-bg-elevated border border-border rounded-md p-3 text-text font-mono text-[13px] outline-none resize-y leading-normal transition-colors focus-ring" rows={10} placeholder={"# My Skill\n\nStep-by-step instructions for the agent...\n\n## When to use\n- Scenario 1\n- Scenario 2"} value={data.body} onChange={e => set('body', e.target.value)} />
        </label>
      </div>
    </div>
  )
}
