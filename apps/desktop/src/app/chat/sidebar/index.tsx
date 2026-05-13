import { useStore } from '@nanostores/react'
import type * as React from 'react'
import { useMemo } from 'react'

import { Button } from '@/components/ui/button'
import {
  Sidebar,
  SidebarContent,
  SidebarGroup,
  SidebarGroupContent,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem
} from '@/components/ui/sidebar'
import { Skeleton } from '@/components/ui/skeleton'
import type { SessionInfo } from '@/hermes'
import { Brain, ChevronDown, Clock, Layers3, MessageCircle, Plus, RefreshCw, Users } from '@/lib/icons'
import { cn } from '@/lib/utils'
import {
  $pinnedSessionIds,
  $sidebarOpen,
  $sidebarPinsOpen,
  $sidebarRecentsOpen,
  pinSession,
  setSidebarPinsOpen,
  setSidebarRecentsOpen,
  unpinSession
} from '@/store/layout'
import { $selectedStoredSessionId, $sessions, $sessionsLoading, $workingSessionIds } from '@/store/session'

import {
  type AppView,
  ARTIFACTS_ROUTE,
  CRON_ROUTE,
  MESSAGING_ROUTE,
  PROFILES_ROUTE,
  SKILLS_ROUTE
} from '../../routes'
import { SidebarPanelLabel } from '../../shell/sidebar-label'
import type { SidebarNavItem } from '../../types'

import { SidebarSessionRow } from './session-row'

const SIDEBAR_NAV: SidebarNavItem[] = [
  {
    id: 'new-session',
    label: 'New chat',
    icon: Plus,
    action: 'new-session'
  },
  { id: 'skills', label: 'Skills', icon: Brain, route: SKILLS_ROUTE },
  { id: 'messaging', label: 'Messaging', icon: MessageCircle, route: MESSAGING_ROUTE },
  { id: 'artifacts', label: 'Artifacts', icon: Layers3, route: ARTIFACTS_ROUTE },
  { id: 'cron', label: 'Cron', icon: Clock, route: CRON_ROUTE },
  { id: 'profiles', label: 'Profiles', icon: Users, route: PROFILES_ROUTE }
]

interface ChatSidebarProps extends React.ComponentProps<typeof Sidebar> {
  currentView: AppView
  onNavigate: (item: SidebarNavItem) => void
  onRefreshSessions: () => void
  onResumeSession: (sessionId: string) => void
  onDeleteSession: (sessionId: string) => void
}

export function ChatSidebar({
  currentView,
  onNavigate,
  onRefreshSessions,
  onResumeSession,
  onDeleteSession
}: ChatSidebarProps) {
  const sidebarOpen = useStore($sidebarOpen)
  const pinnedSessionIds = useStore($pinnedSessionIds)
  const pinsOpen = useStore($sidebarPinsOpen)
  const recentsOpen = useStore($sidebarRecentsOpen)
  const selectedSessionId = useStore($selectedStoredSessionId)
  const activeSidebarSessionId = currentView === 'chat' ? selectedSessionId : null
  const sessions = useStore($sessions)
  const sessionsLoading = useStore($sessionsLoading)
  const workingSessionIds = useStore($workingSessionIds)

  const sortedSessions = useMemo(
    () =>
      [...sessions].sort((a, b) => {
        const aTime = a.last_active || a.started_at || 0
        const bTime = b.last_active || b.started_at || 0

        return bTime - aTime
      }),
    [sessions]
  )

  const sessionsById = useMemo(() => new Map(sessions.map(session => [session.id, session])), [sessions])
  const workingSessionIdSet = useMemo(() => new Set(workingSessionIds), [workingSessionIds])
  const visiblePinnedIds = pinnedSessionIds.filter(id => sessionsById.has(id))
  const visiblePinnedIdSet = new Set(visiblePinnedIds)

  const pinnedSessions = visiblePinnedIds
    .map(id => sessionsById.get(id))
    .filter((session): session is SessionInfo => Boolean(session))

  const recentSessions = sortedSessions.filter(session => !visiblePinnedIdSet.has(session.id))

  const showSessionSkeletons = sessionsLoading && sortedSessions.length === 0
  const showSessionSections = showSessionSkeletons || sortedSessions.length > 0

  return (
    <Sidebar
      className={cn(
        'relative h-full min-w-0 overflow-hidden border-r border-t-0 border-b-0 border-l-0 text-foreground transition-none [backdrop-filter:blur(1.5rem)_saturate(1.08)]',
        sidebarOpen
          ? 'border-(--sidebar-edge-border) bg-[color-mix(in_srgb,var(--dt-sidebar-bg)_97%,transparent)] opacity-100'
          : 'pointer-events-none border-transparent bg-transparent opacity-0'
      )}
      collapsible="none"
    >
      <SidebarContent className="gap-0 overflow-hidden bg-transparent px-(--sidebar-content-inline-padding)">
        <SidebarGroup className="shrink-0 p-0 pb-2 pt-[calc(var(--titlebar-height)+0.25rem)]">
          <SidebarPanelLabel className="pb-1 pt-1">Workspace</SidebarPanelLabel>
          <SidebarGroupContent>
            <SidebarMenu className="gap-px">
              {SIDEBAR_NAV.map(item => {
                const isInteractive = Boolean(item.action) || Boolean(item.route)

                const active =
                  (item.id === 'skills' && currentView === 'skills') ||
                  (item.id === 'messaging' && currentView === 'messaging') ||
                  (item.id === 'artifacts' && currentView === 'artifacts') ||
                  (item.id === 'cron' && currentView === 'cron') ||
                  (item.id === 'profiles' && currentView === 'profiles')

                return (
                  <SidebarMenuItem key={item.id}>
                    <SidebarMenuButton
                      aria-disabled={!isInteractive}
                      className={cn(
                        'flex h-7 w-full justify-start gap-2 rounded-md border border-transparent px-2 text-left text-sm font-medium text-sidebar-foreground/78 transition-colors duration-300 ease-out hover:border-[color-mix(in_srgb,var(--dt-border)_60%,transparent)] hover:bg-(--chrome-action-hover) hover:text-foreground hover:transition-none',
                        active &&
                          'border-[color-mix(in_srgb,var(--dt-midground)_34%,var(--dt-border))] bg-[color-mix(in_srgb,var(--dt-midground)_10%,var(--dt-card))] text-foreground shadow-[inset_0_0.0625rem_0_color-mix(in_srgb,white_40%,transparent)] hover:border-[color-mix(in_srgb,var(--dt-midground)_34%,var(--dt-border))]!',
                        !isInteractive &&
                          'cursor-default hover:border-transparent hover:bg-transparent hover:text-inherit'
                      )}
                      onClick={() => onNavigate(item)}
                      tooltip={item.label}
                      type="button"
                    >
                      <item.icon className="size-4 shrink-0 text-[color-mix(in_srgb,currentColor_72%,transparent)]" />
                      {sidebarOpen && <span className="max-[46.25rem]:hidden">{item.label}</span>}
                    </SidebarMenuButton>
                  </SidebarMenuItem>
                )
              })}
            </SidebarMenu>
          </SidebarGroupContent>
        </SidebarGroup>

        {sidebarOpen && showSessionSections && (
          <SidebarGroup className="shrink-0 p-0 pb-1">
            <SidebarSectionHeader label="Pinned" onToggle={() => setSidebarPinsOpen(!pinsOpen)} open={pinsOpen} />
            {pinsOpen && (
              <SidebarGroupContent className="flex min-h-10 shrink-0 flex-col gap-px rounded-lg pb-2 pt-1">
                {pinnedSessions.length === 0 && (
                  <div className="italic flex min-h-7 items-center gap-2 rounded-lg pl-2 text-xs text-muted-foreground/80">
                    <span>Shift click to pin a chat</span>
                  </div>
                )}
                {pinnedSessions.map(session => (
                  <SidebarSessionRow
                    isPinned
                    isSelected={session.id === activeSidebarSessionId}
                    isWorking={workingSessionIdSet.has(session.id)}
                    key={session.id}
                    onDelete={() => onDeleteSession(session.id)}
                    onPin={() => unpinSession(session.id)}
                    onResume={() => onResumeSession(session.id)}
                    session={session}
                  />
                ))}
              </SidebarGroupContent>
            )}
          </SidebarGroup>
        )}

        {sidebarOpen && showSessionSections && (
          <SidebarGroup className="min-h-0 flex-1 p-0">
            <SidebarSectionHeader
              action={
                <Button
                  aria-label={sessionsLoading ? 'Refreshing sessions' : 'Refresh sessions'}
                  className="size-4 rounded-sm p-0 text-muted-foreground opacity-10 hover:bg-accent hover:text-foreground hover:opacity-100 focus-visible:opacity-100 disabled:opacity-35 [&_svg]:size-3!"
                  disabled={sessionsLoading}
                  onClick={event => {
                    event.stopPropagation()
                    setSidebarRecentsOpen(true)
                    onRefreshSessions()
                  }}
                  size="icon-xs"
                  variant="ghost"
                >
                  <RefreshCw className={cn(sessionsLoading && 'animate-spin')} />
                </Button>
              }
              label="Recent chats"
              onToggle={() => setSidebarRecentsOpen(!recentsOpen)}
              open={recentsOpen}
            />

            {recentsOpen && (
              <SidebarGroupContent className="flex min-h-0 flex-1 flex-col gap-px overflow-y-auto overscroll-contain pb-1.75">
                {showSessionSkeletons && <SidebarSessionSkeletons />}
                {!showSessionSkeletons && recentSessions.length === 0 && <SidebarAllPinnedState />}
                {recentSessions.map(session => (
                  <SidebarSessionRow
                    isPinned={false}
                    isSelected={session.id === activeSidebarSessionId}
                    isWorking={workingSessionIdSet.has(session.id)}
                    key={session.id}
                    onDelete={() => onDeleteSession(session.id)}
                    onPin={() => pinSession(session.id)}
                    onResume={() => onResumeSession(session.id)}
                    session={session}
                  />
                ))}
              </SidebarGroupContent>
            )}
          </SidebarGroup>
        )}
      </SidebarContent>
    </Sidebar>
  )
}

interface SidebarSectionHeaderProps extends React.ComponentProps<'div'> {
  label: string
  open: boolean
  onToggle: () => void
  action?: React.ReactNode
}

function SidebarSectionHeader({ label, open, onToggle, action }: SidebarSectionHeaderProps) {
  return (
    <div className="flex shrink-0 items-center justify-between pb-1 pt-1.5">
      <button
        className="group/section-label flex w-fit items-center gap-2 bg-transparent text-left leading-none"
        onClick={onToggle}
        type="button"
      >
        <SidebarPanelLabel>{label}</SidebarPanelLabel>
        <ChevronDown
          className={cn(
            'size-3 text-muted-foreground/70 opacity-0 transition group-hover/section-label:opacity-100',
            !open && '-rotate-90'
          )}
        />
      </button>
      {action}
    </div>
  )
}

function SidebarSessionSkeletons() {
  return (
    <div aria-hidden="true" className="grid gap-px">
      {['w-32', 'w-40', 'w-28', 'w-36', 'w-24'].map((width, i) => (
        <div className="grid min-h-7 grid-cols-[minmax(0,1fr)_1.5rem] items-center rounded-lg" key={`${width}-${i}`}>
          <Skeleton className={cn('h-3.5 rounded-full', width)} />
          <Skeleton className="mx-auto size-4 rounded-md opacity-60" />
        </div>
      ))}
    </div>
  )
}

const SidebarAllPinnedState = () => (
  <div className="grid min-h-24 place-items-center rounded-lg text-center text-xs text-muted-foreground">
    Everything here is pinned. Unpin a chat to show it in recents.
  </div>
)
