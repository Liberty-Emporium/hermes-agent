import { useStore } from '@nanostores/react'
import { useMemo } from 'react'

import { Activity, AlertCircle, Layers3, Loader2, type LucideIcon, RefreshCw, Sparkles } from '@/lib/icons'
import { cn } from '@/lib/utils'
import { $desktopActionTasks, buildRailTasks, type RailTask, type RailTaskStatus } from '@/store/activity'
import { $previewServerRestart } from '@/store/preview'
import { $activeSessionId, $sessions, $workingSessionIds } from '@/store/session'
import { $subagentsBySession, buildSubagentTree, type SubagentNode, type SubagentStatus } from '@/store/subagents'

import { useRouteEnumParam } from '../hooks/use-route-enum-param'
import { OverlayCard } from '../overlays/overlay-chrome'
import { OverlayMain, OverlayNavItem, OverlaySidebar, OverlaySplitLayout } from '../overlays/overlay-split-layout'
import { OverlayView } from '../overlays/overlay-view'

type AgentsSection = 'tree' | 'activity' | 'history'

interface SectionDef {
  description: string
  icon: LucideIcon
  id: AgentsSection
  label: string
}

const SECTIONS: readonly SectionDef[] = [
  { description: 'Live subagent spawn tree for the current turn', icon: Layers3, id: 'tree', label: 'Spawn tree' },
  { description: 'Background work across sessions and the desktop', icon: Activity, id: 'activity', label: 'Activity' },
  { description: 'Past spawn snapshots, replay, and diff', icon: RefreshCw, id: 'history', label: 'History' }
]

const SECTION_IDS = SECTIONS.map(s => s.id) as readonly AgentsSection[]

const STATUS_TONE: Record<RailTaskStatus, string> = {
  error: 'text-destructive',
  running: 'text-foreground',
  success: 'text-emerald-500'
}

const STATUS_ICON: Record<RailTaskStatus, LucideIcon> = {
  error: AlertCircle,
  running: Loader2,
  success: Sparkles
}

interface AgentsViewProps {
  initialSection?: AgentsSection
  onClose: () => void
}

export function AgentsView({ initialSection = 'tree', onClose }: AgentsViewProps) {
  const [section, setSection] = useRouteEnumParam('section', SECTION_IDS, initialSection)

  const activeSessionId = useStore($activeSessionId)
  const sessions = useStore($sessions)
  const workingSessionIds = useStore($workingSessionIds)
  const previewRestart = useStore($previewServerRestart)
  const desktopActionTasks = useStore($desktopActionTasks)
  const subagentsBySession = useStore($subagentsBySession)

  const activityTasks = useMemo(
    () => buildRailTasks(workingSessionIds, sessions, previewRestart, desktopActionTasks),
    [desktopActionTasks, previewRestart, sessions, workingSessionIds]
  )

  const active = SECTIONS.find(s => s.id === section) ?? SECTIONS[0]!
  const activeSubagents = activeSessionId ? (subagentsBySession[activeSessionId] ?? []) : []
  const tree = useMemo(() => buildSubagentTree(activeSubagents), [activeSubagents])

  return (
    <OverlayView closeLabel="Close agents" onClose={onClose}>
      <OverlaySplitLayout>
        <OverlaySidebar>
          {SECTIONS.map(s => (
            <OverlayNavItem
              active={s.id === section}
              icon={s.icon}
              key={s.id}
              label={s.label}
              onClick={() => setSection(s.id)}
            />
          ))}
        </OverlaySidebar>

        <OverlayMain>
          <header className="mb-4">
            <h2 className="text-sm font-semibold text-foreground">{active.label}</h2>
            <p className="text-xs text-muted-foreground">{active.description}</p>
          </header>

          {section === 'tree' ? (
            <SubagentTree tree={tree} />
          ) : section === 'activity' ? (
            <ActivityList tasks={activityTasks} />
          ) : (
            <SectionStub label={active.label} />
          )}
        </OverlayMain>
      </OverlaySplitLayout>
    </OverlayView>
  )
}

const STATUS_CLASS: Record<SubagentStatus, string> = {
  completed: 'text-emerald-500',
  failed: 'text-destructive',
  interrupted: 'text-amber-500',
  queued: 'text-muted-foreground',
  running: 'text-primary'
}

function SubagentTree({ tree }: { tree: SubagentNode[] }) {
  if (tree.length === 0) {
    return (
      <OverlayCard className="grid place-items-center gap-3 px-6 py-12 text-center">
        <Sparkles className="size-6 text-muted-foreground/70" />
        <div className="grid gap-1">
          <p className="text-sm font-medium text-foreground">No live subagents</p>
          <p className="max-w-md text-xs leading-relaxed text-muted-foreground">
            When a turn delegates work, child agents appear here as a live spawn tree.
          </p>
        </div>
      </OverlayCard>
    )
  }

  return (
    <div className="grid gap-2 overflow-y-auto pr-1">
      {tree.map(node => (
        <SubagentRow key={node.id} node={node} />
      ))}
    </div>
  )
}

function SubagentRow({ node, depth = 0 }: { node: SubagentNode; depth?: number }) {
  const running = node.status === 'running' || node.status === 'queued'

  return (
    <OverlayCard className="px-3 py-2" style={{ marginLeft: depth ? `${Math.min(depth, 4) * 1.25}rem` : undefined }}>
      <div className="flex items-start gap-2">
        {running ? (
          <Loader2 className="mt-0.5 size-3.5 shrink-0 animate-spin text-primary" />
        ) : (
          <Sparkles className={cn('mt-0.5 size-3.5 shrink-0', STATUS_CLASS[node.status])} />
        )}
        <div className="min-w-0 flex-1">
          <div className="flex min-w-0 items-center gap-2">
            <div className="truncate text-sm font-medium text-foreground">{node.goal}</div>
            <span className={cn('shrink-0 text-[0.65rem]', STATUS_CLASS[node.status])}>{node.status}</span>
          </div>
          <div className="mt-0.5 flex flex-wrap gap-x-3 gap-y-0.5 text-[0.68rem] text-muted-foreground">
            {node.model && <span>{node.model}</span>}
            {typeof node.durationSeconds === 'number' && <span>{node.durationSeconds.toFixed(1)}s</span>}
            {typeof node.costUsd === 'number' && <span>${node.costUsd.toFixed(4)}</span>}
            {typeof node.apiCalls === 'number' && <span>{node.apiCalls} calls</span>}
          </div>
          {(node.toolName || node.toolPreview || node.summary) && (
            <div className="mt-1 line-clamp-2 text-xs leading-relaxed text-muted-foreground">
              {node.summary || [node.toolName, node.toolPreview].filter(Boolean).join(' · ')}
            </div>
          )}
        </div>
      </div>
      {node.children.length > 0 && (
        <div className="mt-2 grid gap-2">
          {node.children.map(child => (
            <SubagentRow depth={depth + 1} key={child.id} node={child} />
          ))}
        </div>
      )}
    </OverlayCard>
  )
}

function ActivityList({ tasks }: { tasks: readonly RailTask[] }) {
  if (tasks.length === 0) {
    return (
      <OverlayCard className="px-3 py-4 text-sm text-muted-foreground">
        No background activity. Long-running tools, preview restarts, and parallel sessions surface here.
      </OverlayCard>
    )
  }

  return (
    <div className="grid min-h-0 gap-1.5 overflow-y-auto pr-1">
      {tasks.map(task => {
        const Icon = STATUS_ICON[task.status]

        return (
          <OverlayCard className="flex items-start gap-2.5 px-3 py-2" key={task.id}>
            <Icon
              className={cn(
                'mt-0.5 size-3.5 shrink-0',
                STATUS_TONE[task.status],
                task.status === 'running' && 'animate-spin'
              )}
            />
            <div className="min-w-0 flex-1">
              <div className="truncate text-sm font-medium text-foreground">{task.label}</div>
              {task.detail && <div className="truncate text-xs text-muted-foreground">{task.detail}</div>}
            </div>
          </OverlayCard>
        )
      })}
    </div>
  )
}

function SectionStub({ label }: { label: string }) {
  return (
    <OverlayCard className="grid place-items-center gap-3 px-6 py-12 text-center">
      <Sparkles className="size-6 text-muted-foreground/70" />
      <div className="grid gap-1">
        <p className="text-sm font-medium text-foreground">{label} — coming soon</p>
        <p className="max-w-md text-xs leading-relaxed text-muted-foreground">
          Subagent stores aren&apos;t wired into the desktop yet. Once gateway events for{' '}
          <code className="rounded bg-muted/60 px-1 py-0.5 font-mono text-[0.65rem]">
            subagent.spawn / progress / complete
          </code>{' '}
          land here, this view shows the live spawn tree, replay history, and pause/kill controls — modelled on the
          TUI&apos;s <code className="rounded bg-muted/60 px-1 py-0.5 font-mono text-[0.65rem]">/agents</code> overlay.
        </p>
      </div>
    </OverlayCard>
  )
}
