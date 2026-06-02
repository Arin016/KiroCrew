import React from 'react'
import { twMerge } from 'tailwind-merge'
import InfoTip from './InfoTip'

/* ── Shared UI primitives ── */

export function Card({ children, className = '' }: { children: React.ReactNode; className?: string }) {
  return (
    <div className={`card-glow border border-border bg-card rounded-lg p-5 mb-4 animate-rise shadow-sm hover:border-border-strong hover:shadow-md transition-all ${className}`}>
      {children}
    </div>
  )
}

export function CardTitle({ children }: { children: React.ReactNode }) {
  return <h3 className="text-sm font-semibold tracking-tight text-text-strong mb-3.5 flex items-center gap-2">{children}</h3>
}

export const Btn = React.forwardRef<HTMLButtonElement, React.ButtonHTMLAttributes<HTMLButtonElement> & { danger?: boolean; primary?: boolean }>(
  ({ children, danger, primary, className, ...rest }, ref) => (
    <button
      ref={ref}
      className={twMerge(`inline-flex items-center gap-1.5 px-2.5 py-1 rounded-md border text-[13px] cursor-pointer font-body transition-all disabled:opacity-30 disabled:cursor-not-allowed ${
        primary
          ? 'bg-accent text-accent-fg border-accent hover:bg-accent-hover hover:shadow-[0_0_12px_var(--accent-glow)]'
          : danger
            ? 'border-border bg-transparent text-muted hover:text-danger hover:border-danger'
            : 'border-border bg-transparent text-muted hover:text-text hover:border-border-strong hover:bg-bg-hover'
      }`, className)}
      {...rest}
    >
      {children}
    </button>
  )
)

export function SendBtn({ children, onClick, disabled, style }: { children: React.ReactNode; onClick: () => void; disabled?: boolean; style?: React.CSSProperties }) {
  return (
    <button
      className="btn-sweep bg-accent text-accent-fg border-none rounded-lg px-4 h-9 text-sm font-semibold cursor-pointer hover:bg-accent-hover hover:shadow-[0_0_20px_var(--accent-glow)] disabled:opacity-30 disabled:cursor-not-allowed transition-all font-body"
      onClick={onClick}
      disabled={disabled}
      style={style}
    >
      {children}
    </button>
  )
}

export const Input = React.forwardRef<HTMLInputElement, React.InputHTMLAttributes<HTMLInputElement>>(
  ({ className = '', ...props }, ref) => (
    <input
      ref={ref}
      className={twMerge('bg-bg-elevated border border-border rounded-md px-3 py-2 text-text text-sm font-body outline-none flex-1 transition-colors focus-ring', className)}
      {...props}
    />
  )
)

export function SearchInput({ className = '', ...props }: React.InputHTMLAttributes<HTMLInputElement>) {
  return (
    <div className={`relative ${className}`}>
      <svg className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-muted pointer-events-none stroke-current fill-none" viewBox="0 0 24 24" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
      <input
        className="w-full bg-bg-elevated border border-border rounded-md pl-7 pr-3 py-1.5 text-text text-[13px] font-body outline-none transition-all focus-ring placeholder:text-muted/50"
        {...props}
      />
    </div>
  )
}

export function Badge({ variant, children }: { variant: 'ok' | 'err' | 'warn' | 'aim'; children: React.ReactNode }) {
  const cls =
    variant === 'ok' ? 'bg-ok-subtle text-ok'
    : variant === 'err' ? 'bg-danger-subtle text-danger'
    : variant === 'aim' ? 'bg-aim-subtle text-aim'
    : 'bg-warn-subtle text-warn'
  return (
    <span className={`inline-flex items-center gap-1 px-2 py-[2px] rounded-full text-[13px] font-medium font-mono whitespace-nowrap hover:scale-105 transition-transform ${cls}`}>
      {children}
    </span>
  )
}

export function AimBadge({ source }: { source: string }) {
  const cls =
    source === 'aim' ? 'bg-aim-subtle text-aim border-aim/30'
    : source === 'kiroclaw' ? 'bg-accent-subtle text-accent border-accent/30'
    : 'bg-bg-elevated text-muted border-border'
  return <span className={`px-1.5 py-[2px] rounded-full text-[11px] font-bold border shrink-0 ${cls}`}>{source}</span>
}

export function StatCard({ label, value, accent, colorClass, delay, onClick, active, title }: { label: string; value?: string | number | null; accent?: boolean; colorClass?: string; delay?: number; onClick?: () => void; active?: boolean; title?: string }) {
  const loading = value === undefined || value === null
  return (
    <div
      className={`stat-accent relative overflow-hidden bg-card rounded-md px-4 py-3.5 border shadow-[inset_0_1px_0_var(--card-hl)] animate-rise hover:border-border-strong hover:-translate-y-0.5 hover:shadow-md transition-all ${active ? 'border-accent ring-1 ring-accent/40' : 'border-border'} ${onClick ? 'cursor-pointer' : ''}`}
      style={delay ? { animationDelay: `${delay}ms` } : undefined}
      onClick={onClick}
      role={onClick ? 'button' : undefined}
      tabIndex={onClick ? 0 : undefined}
      onKeyDown={onClick ? (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onClick() } } : undefined}
    >
      <div className="text-muted text-[13px] font-medium uppercase tracking-[.04em] flex items-center gap-1">{label}{title && <InfoTip text={title} />}</div>
      {loading
        ? <div className="skeleton h-7 w-16 mt-1.5 rounded" />
        : <div className={`text-2xl font-bold mt-1.5 tracking-tight leading-none ${accent ? 'text-accent' : colorClass || ''}`}>{value ?? '—'}</div>
      }
    </div>
  )
}

export function Skeleton({ className = '' }: { className?: string }) {
  return <div className={`skeleton ${className}`} />
}

export function ContentSkeleton({ rows = 5 }: { rows?: number }) {
  return (
    <div className="space-y-4 animate-pulse">
      <div className="skeleton h-5 w-48 rounded" />
      <div className="skeleton h-3 w-72 rounded" />
      <div className="space-y-2 mt-4">
        {Array.from({ length: rows }).map((_, i) => (
          <div key={i} className="flex items-center gap-3">
            <div className="skeleton h-4 w-4 rounded" />
            <div className="skeleton h-4 rounded flex-1" style={{ maxWidth: `${60 + (i * 17) % 30}%` }} />
          </div>
        ))}
      </div>
    </div>
  )
}

export function EmptyState({ icon, title, subtitle }: { icon: React.ReactNode; title: string; subtitle?: string }) {
  return (
    <div className="flex flex-col items-center justify-center py-12 gap-2 animate-rise">
      <div className="text-[40px] opacity-[.12] select-none">{icon}</div>
      <div className="text-muted text-sm font-medium">{title}</div>
      {subtitle && <div className="text-muted/60 text-[13px]">{subtitle}</div>}
    </div>
  )
}

export function PageHeader({ title, subtitle }: { title: string; subtitle: string }) {
  return (
    <div className="flex items-end justify-between gap-4 px-6 pt-4 pb-3">
      <div>
        <div className="text-2xl font-bold tracking-tight text-text-strong">{title}</div>
        <div className="text-muted text-sm mt-1">{subtitle}</div>
      </div>
    </div>
  )
}

export function Toggle({ checked, onChange, disabled, label }: { checked: boolean; onChange: (v: boolean) => void; disabled?: boolean; label?: string }) {
  return (
    <div
      role="switch"
      aria-checked={checked}
      aria-label={label}
      tabIndex={disabled ? -1 : 0}
      onClick={() => !disabled && onChange(!checked)}
      onKeyDown={e => { if (!disabled && (e.key === ' ' || e.key === 'Enter')) { e.preventDefault(); onChange(!checked) } }}
      className={`w-9 h-5 rounded-full relative transition-colors shrink-0 cursor-pointer ${disabled ? 'opacity-40 cursor-not-allowed' : ''} ${checked ? 'bg-accent' : 'bg-border'}`}
    >
      <div className={`absolute top-0.5 w-4 h-4 rounded-full bg-white shadow transition-transform ${checked ? 'translate-x-[18px]' : 'translate-x-0.5'}`} />
    </div>
  )
}
