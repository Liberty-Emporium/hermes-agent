import { useStore } from '@nanostores/react'
import { useCallback, useMemo, useState } from 'react'

import type { CommandCenterSection } from '@/app/command-center'
import { GatewayMenuPanel } from '@/app/shell/gateway-menu-panel'
import { restartGateway } from '@/hermes'
import { Activity, AlertCircle, Command, Cpu, FolderOpen, GitBranch, Hash, Loader2, Sparkles } from '@/lib/icons'
import { compactPath, contextBarLabel, LiveDuration, usageContextLabel } from '@/lib/statusbar'
import { cn } from '@/lib/utils'
import { $desktopActionTasks } from '@/store/activity'
import { notify, notifyError } from '@/store/notifications'
import { $previewServerRestartStatus } from '@/store/preview'
import {
  $busy,
  $currentBranch,
  $currentCwd,
  $currentModel,
  $currentProvider,
  $currentUsage,
  $sessionStartedAt,
  $turnStartedAt,
  $workingSessionIds,
  setModelPickerOpen
} from '@/store/session'
import { $subagentsBySession, activeSubagentCount } from '@/store/subagents'
import { $desktopVersion, $updateApply, $updateStatus, setUpdateOverlayOpen } from '@/store/updates'
import type { StatusResponse } from '@/types/hermes'

import type { StatusbarItem } from '../statusbar-controls'

interface StatusbarItemsOptions {
  agentsOpen: boolean
  browseSessionCwd: () => Promise<void>
  commandCenterOpen: boolean
  extraLeftItems: readonly StatusbarItem[]
  extraRightItems: readonly StatusbarItem[]
  gatewayLogLines: readonly string[]
  openAgents: () => void
  openCommandCenterSection: (section: CommandCenterSection) => void
  statusSnapshot: StatusResponse | null
  toggleCommandCenter: () => void
}

export function useStatusbarItems({
  agentsOpen,
  browseSessionCwd,
  commandCenterOpen,
  extraLeftItems,
  extraRightItems,
  gatewayLogLines,
  openAgents,
  openCommandCenterSection,
  statusSnapshot,
  toggleCommandCenter
}: StatusbarItemsOptions) {
  const busy = useStore($busy)
  const currentBranch = useStore($currentBranch)
  const currentCwd = useStore($currentCwd)
  const currentModel = useStore($currentModel)
  const currentProvider = useStore($currentProvider)
  const currentUsage = useStore($currentUsage)
  const desktopActionTasks = useStore($desktopActionTasks)
  const previewServerRestartStatus = useStore($previewServerRestartStatus)
  const sessionStartedAt = useStore($sessionStartedAt)
  const turnStartedAt = useStore($turnStartedAt)
  const workingSessionIds = useStore($workingSessionIds)
  const subagentsBySession = useStore($subagentsBySession)
  const updateStatus = useStore($updateStatus)
  const updateApply = useStore($updateApply)
  const desktopVersion = useStore($desktopVersion)

  const contextUsage = useMemo(() => usageContextLabel(currentUsage), [currentUsage])
  const contextBar = useMemo(() => contextBarLabel(currentUsage), [currentUsage])

  const [restartingGateway, setRestartingGateway] = useState(false)

  const handleRestartGateway = useCallback(async () => {
    if (restartingGateway) {
      return
    }

    setRestartingGateway(true)

    try {
      await restartGateway()
      notify({
        kind: 'success',
        title: 'Gateway restart requested',
        message: 'Status will update once the gateway reconnects.'
      })
    } catch (err) {
      notifyError(err, 'Failed to restart gateway')
    } finally {
      setRestartingGateway(false)
    }
  }, [restartingGateway])

  const gatewayMenuContent = useMemo(
    () => (
      <GatewayMenuPanel
        logLines={gatewayLogLines}
        onOpenSystem={() => openCommandCenterSection('system')}
        onRestart={() => void handleRestartGateway()}
        restarting={restartingGateway}
        statusSnapshot={statusSnapshot}
      />
    ),
    [gatewayLogLines, handleRestartGateway, openCommandCenterSection, restartingGateway, statusSnapshot]
  )

  const { bgFailed, bgRunning, subagentsRunning } = useMemo(() => {
    const actions = Object.values(desktopActionTasks)
    const running = actions.filter(t => t.status.running).length
    const failed = actions.filter(t => !t.status.running && (t.status.exit_code ?? 0) !== 0).length
    const previewRunning = previewServerRestartStatus === 'running' ? 1 : 0
    const previewFailed = previewServerRestartStatus === 'error' ? 1 : 0
    const subagentsRunning = Object.values(subagentsBySession).reduce((sum, items) => sum + activeSubagentCount(items), 0)

    return {
      bgFailed: failed + previewFailed,
      bgRunning: workingSessionIds.length + running + previewRunning,
      subagentsRunning
    }
  }, [desktopActionTasks, previewServerRestartStatus, subagentsBySession, workingSessionIds])

  const gatewayUp = Boolean(statusSnapshot?.gateway_running)

  const versionItem = useMemo<StatusbarItem>(() => {
    const appVersion = desktopVersion?.appVersion
    const sha = updateStatus?.currentSha?.slice(0, 7) ?? null
    const behind = updateStatus?.behind ?? 0
    const applying = updateApply.applying || updateApply.stage === 'restart'
    const base = appVersion ? `v${appVersion}` : (sha ?? 'unknown')
    const behindHint = !applying && behind > 0 ? ` (+${behind})` : ''

    const label = applying
      ? updateApply.stage === 'restart'
        ? `${base} · restart`
        : `${base} · update`
      : `${base}${behindHint}`

    const tooltip = [
      applying ? updateApply.message || 'Update in progress' : null,
      !applying && behind > 0 && `${behind} commit${behind === 1 ? '' : 's'} behind ${updateStatus?.branch ?? '…'}`,
      appVersion && `Hermes Desktop v${appVersion}`,
      sha && `commit ${sha}`,
      updateStatus?.branch && `branch ${updateStatus.branch}`
    ]
      .filter(Boolean)
      .join(' · ')

    return {
      className: !applying && behind > 0 ? 'text-primary hover:text-primary' : undefined,
      detail: appVersion && sha && !applying ? sha : undefined,
      hidden: !appVersion && !sha,
      icon: applying ? <Loader2 className="size-3 animate-spin" /> : <Hash className="size-3" />,
      id: 'version',
      label,
      onSelect: () => setUpdateOverlayOpen(true),
      title: tooltip || undefined,
      variant: 'action'
    }
  }, [
    desktopVersion?.appVersion,
    updateApply.applying,
    updateApply.message,
    updateApply.stage,
    updateStatus?.behind,
    updateStatus?.branch,
    updateStatus?.currentSha
  ])

  const coreLeftStatusbarItems = useMemo<readonly StatusbarItem[]>(
    () => [
      {
        className: `w-7 justify-center px-0${commandCenterOpen ? ' bg-accent/55 text-foreground' : ''}`,
        icon: <Command className="size-3.5" />,
        id: 'command-center',
        onSelect: toggleCommandCenter,
        title: commandCenterOpen ? 'Close Command Center' : 'Open Command Center',
        variant: 'action'
      },
      {
        className: gatewayUp ? undefined : 'text-destructive hover:text-destructive',
        detail: gatewayUp ? statusSnapshot?.gateway_state || 'online' : 'offline',
        icon: gatewayUp ? <Activity className="size-3" /> : <AlertCircle className="size-3" />,
        id: 'gateway-health',
        label: 'Gateway',
        menuClassName: 'w-72',
        menuContent: gatewayMenuContent,
        title: 'Gateway and platform health',
        variant: 'menu'
      },
      {
        className: cn(
          agentsOpen && 'bg-accent/55 text-foreground',
          bgFailed > 0 && 'text-destructive hover:text-destructive'
        ),
        detail:
          subagentsRunning > 0
            ? `${subagentsRunning} subagent${subagentsRunning === 1 ? '' : 's'}`
            : bgFailed > 0
              ? `${bgFailed} failed`
              : bgRunning > 0
                ? `${bgRunning} running`
                : undefined,
        icon:
          bgFailed > 0 ? (
            <AlertCircle className="size-3" />
          ) : bgRunning > 0 || subagentsRunning > 0 ? (
            <Loader2 className="size-3 animate-spin" />
          ) : (
            <Sparkles className="size-3" />
          ),
        id: 'agents',
        label: 'Agents',
        onSelect: openAgents,
        title: agentsOpen ? 'Close agents' : 'Open agents',
        variant: 'action'
      }
    ],
    [
      agentsOpen,
      bgFailed,
      bgRunning,
      commandCenterOpen,
      gatewayMenuContent,
      gatewayUp,
      openAgents,
      statusSnapshot?.gateway_state,
      subagentsRunning,
      toggleCommandCenter
    ]
  )

  const coreRightStatusbarItems = useMemo<readonly StatusbarItem[]>(
    () => [
      {
        detail: <LiveDuration since={turnStartedAt} />,
        hidden: !busy || !turnStartedAt,
        icon: <Loader2 className="size-3 animate-spin" />,
        id: 'running-timer',
        label: 'Running',
        title: 'Current turn elapsed',
        variant: 'text'
      },
      {
        detail: contextBar || undefined,
        hidden: !contextUsage,
        id: 'context-usage',
        label: contextUsage,
        title: 'Context usage',
        variant: 'text'
      },
      {
        detail: <LiveDuration since={sessionStartedAt} />,
        hidden: !sessionStartedAt,
        id: 'session-timer',
        label: 'Session',
        title: 'Runtime session elapsed',
        variant: 'text'
      },
      {
        detail: currentProvider || '',
        icon: <Cpu className="size-3" />,
        id: 'model-summary',
        label: currentModel || 'No model selected',
        onSelect: () => setModelPickerOpen(true),
        title: currentProvider ? `Switch model · ${currentProvider}: ${currentModel || ''}` : 'Open model picker',
        variant: 'action'
      },
      {
        icon: <FolderOpen className="size-3" />,
        id: 'cwd',
        label: currentCwd ? compactPath(currentCwd) : 'No project cwd',
        onSelect: () => void browseSessionCwd(),
        title: currentCwd ? `Change working directory · ${currentCwd}` : 'Choose working directory',
        variant: 'action'
      },
      {
        hidden: !currentBranch,
        icon: <GitBranch className="size-3" />,
        id: 'branch',
        label: currentBranch,
        title: currentBranch ? `Current branch: ${currentBranch}` : undefined,
        variant: 'text'
      },
      versionItem
    ],
    [
      browseSessionCwd,
      busy,
      contextBar,
      contextUsage,
      currentBranch,
      currentCwd,
      currentModel,
      currentProvider,
      sessionStartedAt,
      turnStartedAt,
      versionItem
    ]
  )

  const leftStatusbarItems = useMemo(
    () => [...coreLeftStatusbarItems, ...extraLeftItems],
    [coreLeftStatusbarItems, extraLeftItems]
  )

  const statusbarItems = useMemo(
    () => [...extraRightItems, ...coreRightStatusbarItems],
    [coreRightStatusbarItems, extraRightItems]
  )

  return { leftStatusbarItems, statusbarItems }
}
