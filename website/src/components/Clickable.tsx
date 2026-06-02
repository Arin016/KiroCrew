import { forwardRef } from 'react'

type ClickableProps = Omit<React.HTMLAttributes<HTMLDivElement>, 'role' | 'onClick'> & {
  onClick: (e?: React.MouseEvent | React.KeyboardEvent) => void
  disabled?: boolean
}

/** Accessible clickable div — use instead of `<div onClick>` to satisfy a11y requirements.
 *  Adds role="button", tabIndex, keyboard Enter/Space handling automatically. */
const Clickable = forwardRef<HTMLDivElement, ClickableProps>(
  ({ onClick, disabled, children, className, onKeyDown, tabIndex, ...props }, ref) => (
    <div
      ref={ref}
      role="button"
      tabIndex={disabled ? -1 : tabIndex ?? 0}
      onClick={disabled ? undefined : onClick}
      onKeyDown={e => {
        onKeyDown?.(e)
        if (!disabled && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); onClick(e) }
      }}
      aria-disabled={disabled || undefined}
      className={className}
      {...props}
    >
      {children}
    </div>
  )
)

Clickable.displayName = 'Clickable'
export default Clickable
