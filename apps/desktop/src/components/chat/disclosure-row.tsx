import { ChevronRight } from 'lucide-react'
import type { ReactNode } from 'react'

import { cn } from '@/lib/utils'

// Shared header row for any collapsible block (thinking, tool group, single
// tool). Owns the grid indent (chevron column = --message-text-indent), the
// hover surface, and the trailing-slot anchor used for copy buttons / running
// timers. Each parent supplies its own outer wrapper (with the data-slot CSS
// uses to escape the message padding) and its own expanded body.
//
// Passing `onToggle` makes the row expandable (chevron + hover + click).
// Omitting it renders a static row that still reserves the chevron column so
// nested rows stay vertically aligned with their group header.
export function DisclosureRow({
  children,
  onToggle,
  open,
  trailing
}: {
  children: ReactNode
  onToggle?: () => void
  open: boolean
  trailing?: ReactNode
}) {
  return (
    <div
      className={cn(
        'group/disclosure-row relative flex w-full max-w-full min-w-0 items-start rounded-md text-muted-foreground transition-colors',
        onToggle && 'hover:bg-[color-mix(in_srgb,var(--dt-midground)_8%,transparent)] hover:text-foreground'
      )}
    >
      <button
        aria-expanded={onToggle ? open : undefined}
        className={cn(
          'grid w-full min-w-0 grid-cols-[var(--message-text-indent)_minmax(0,1fr)] items-start py-0.5 pr-2 text-left',
          onToggle ? 'cursor-pointer' : 'cursor-default'
        )}
        disabled={!onToggle}
        onClick={onToggle}
        type="button"
      >
        <span className="flex h-[1.1rem] items-center justify-center">
          {onToggle ? (
            <ChevronRight
              aria-hidden
              className={cn(
                'size-3 text-midground/55 transition-transform group-hover/disclosure-row:text-midground',
                open && 'rotate-90'
              )}
            />
          ) : (
            <span aria-hidden className="size-3" />
          )}
        </span>
        <span className="min-w-0">{children}</span>
      </button>
      {trailing && <span className="absolute right-1 top-0.5 flex h-[1.1rem] items-center">{trailing}</span>}
    </div>
  )
}
