import type * as React from 'react'
import { useCallback, useEffect, useMemo, useState } from 'react'

import { PageLoader } from '@/components/page-loader'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { Textarea } from '@/components/ui/textarea'
import {
  createCronJob,
  type CronJob,
  deleteCronJob,
  getCronJobs,
  pauseCronJob,
  resumeCronJob,
  triggerCronJob,
  updateCronJob
} from '@/hermes'
import { AlertTriangle, Clock, Pause, Pencil, Play, Plus, RefreshCw, Search, Trash2, X, Zap } from '@/lib/icons'
import { cn } from '@/lib/utils'
import { notify, notifyError } from '@/store/notifications'

import type { SetStatusbarItemGroup } from '../shell/statusbar-controls'
import { titlebarHeaderBaseClass } from '../shell/titlebar'
import type { SetTitlebarToolGroup } from '../shell/titlebar-controls'

const DEFAULT_DELIVER = 'local'

const DELIVERY_OPTIONS: ReadonlyArray<{ label: string; value: string }> = [
  { label: 'This desktop', value: 'local' },
  { label: 'Telegram', value: 'telegram' },
  { label: 'Discord', value: 'discord' },
  { label: 'Slack', value: 'slack' },
  { label: 'Email', value: 'email' }
]

const STATE_TONE: Record<string, 'good' | 'muted' | 'warn' | 'bad'> = {
  enabled: 'good',
  scheduled: 'good',
  running: 'good',
  paused: 'warn',
  disabled: 'muted',
  error: 'bad',
  completed: 'muted'
}

const PILL_TONE: Record<'good' | 'muted' | 'warn' | 'bad', string> = {
  good: 'bg-primary/10 text-primary',
  muted: 'bg-muted text-muted-foreground',
  warn: 'bg-amber-500/10 text-amber-600 dark:text-amber-300',
  bad: 'bg-destructive/10 text-destructive'
}

const asText = (value: unknown): string => (typeof value === 'string' ? value : '')

const truncate = (value: string, max = 80): string => (value.length > max ? `${value.slice(0, max)}…` : value)

function jobName(job: CronJob): string {
  return asText(job.name).trim()
}

function jobPrompt(job: CronJob): string {
  return asText(job.prompt)
}

function jobTitle(job: CronJob): string {
  const name = jobName(job)

  if (name) {
    return name
  }

  const prompt = jobPrompt(job)

  if (prompt) {
    return truncate(prompt, 60)
  }

  const script = asText(job.script)

  if (script) {
    return truncate(script, 60)
  }

  return job.id || 'Cron job'
}

function jobScheduleDisplay(job: CronJob): string {
  return asText(job.schedule_display) || asText(job.schedule?.display) || asText(job.schedule?.expr) || '—'
}

function jobScheduleExpr(job: CronJob): string {
  return asText(job.schedule?.expr) || asText(job.schedule_display) || ''
}

function jobState(job: CronJob): string {
  return asText(job.state) || (job.enabled === false ? 'disabled' : 'scheduled')
}

function jobDeliver(job: CronJob): string {
  return asText(job.deliver) || DEFAULT_DELIVER
}

function formatTime(iso?: null | string): string {
  if (!iso) {
    return '—'
  }

  const date = new Date(iso)

  if (Number.isNaN(date.valueOf())) {
    return iso
  }

  return date.toLocaleString()
}

function matchesQuery(job: CronJob, q: string): boolean {
  if (!q) {
    return true
  }

  const needle = q.toLowerCase()

  return [jobTitle(job), jobPrompt(job), jobScheduleDisplay(job), jobScheduleExpr(job), jobDeliver(job)].some(value =>
    value.toLowerCase().includes(needle)
  )
}

interface CronViewProps extends React.ComponentProps<'section'> {
  setStatusbarItemGroup?: SetStatusbarItemGroup
  setTitlebarToolGroup?: SetTitlebarToolGroup
}

export function CronView({
  setStatusbarItemGroup: _setStatusbarItemGroup,
  setTitlebarToolGroup,
  ...props
}: CronViewProps) {
  const [jobs, setJobs] = useState<CronJob[] | null>(null)
  const [query, setQuery] = useState('')
  const [refreshing, setRefreshing] = useState(false)
  const [busyJobId, setBusyJobId] = useState<null | string>(null)

  const [editor, setEditor] = useState<EditorState>({ mode: 'closed' })
  const [pendingDelete, setPendingDelete] = useState<CronJob | null>(null)
  const [deleting, setDeleting] = useState(false)

  const refresh = useCallback(async () => {
    setRefreshing(true)

    try {
      const result = await getCronJobs()
      setJobs(result)
    } catch (err) {
      notifyError(err, 'Failed to load cron jobs')
    } finally {
      setRefreshing(false)
    }
  }, [])

  useEffect(() => {
    void refresh()
  }, [refresh])

  useEffect(() => {
    if (!setTitlebarToolGroup) {
      return
    }

    setTitlebarToolGroup('cron', [
      {
        disabled: refreshing,
        icon: <RefreshCw className={cn(refreshing && 'animate-spin')} />,
        id: 'refresh-cron',
        label: refreshing ? 'Refreshing cron jobs' : 'Refresh cron jobs',
        onSelect: () => void refresh()
      }
    ])

    return () => setTitlebarToolGroup('cron', [])
  }, [refresh, refreshing, setTitlebarToolGroup])

  const visibleJobs = useMemo(() => {
    if (!jobs) {
      return []
    }

    return jobs.filter(job => matchesQuery(job, query.trim())).sort((a, b) => jobTitle(a).localeCompare(jobTitle(b)))
  }, [jobs, query])

  const enabledCount = jobs?.filter(job => job.enabled).length ?? 0
  const totalCount = jobs?.length ?? 0

  async function handlePauseResume(job: CronJob) {
    setBusyJobId(job.id)

    try {
      const isPaused = jobState(job) === 'paused'
      const updated = isPaused ? await resumeCronJob(job.id) : await pauseCronJob(job.id)
      setJobs(current => (current ? current.map(row => (row.id === job.id ? updated : row)) : current))
      notify({
        kind: 'success',
        title: isPaused ? 'Cron resumed' : 'Cron paused',
        message: truncate(jobTitle(job), 60)
      })
    } catch (err) {
      notifyError(err, 'Failed to update cron job')
    } finally {
      setBusyJobId(null)
    }
  }

  async function handleTrigger(job: CronJob) {
    setBusyJobId(job.id)

    try {
      const updated = await triggerCronJob(job.id)
      setJobs(current => (current ? current.map(row => (row.id === job.id ? updated : row)) : current))
      notify({ kind: 'success', title: 'Cron triggered', message: truncate(jobTitle(job), 60) })
    } catch (err) {
      notifyError(err, 'Failed to trigger cron job')
    } finally {
      setBusyJobId(null)
    }
  }

  async function handleConfirmDelete() {
    if (!pendingDelete) {
      return
    }

    setDeleting(true)

    try {
      await deleteCronJob(pendingDelete.id)
      setJobs(current => (current ? current.filter(row => row.id !== pendingDelete.id) : current))
      notify({ kind: 'success', title: 'Cron deleted', message: truncate(jobTitle(pendingDelete), 60) })
      setPendingDelete(null)
    } catch (err) {
      notifyError(err, 'Failed to delete cron job')
    } finally {
      setDeleting(false)
    }
  }

  async function handleEditorSave(values: EditorValues) {
    if (editor.mode === 'create') {
      const created = await createCronJob({
        prompt: values.prompt,
        schedule: values.schedule,
        name: values.name || undefined,
        deliver: values.deliver || DEFAULT_DELIVER
      })

      setJobs(current => (current ? [...current, created] : [created]))
      notify({ kind: 'success', title: 'Cron created', message: truncate(jobTitle(created), 60) })
    } else if (editor.mode === 'edit') {
      const updated = await updateCronJob(editor.job.id, {
        prompt: values.prompt,
        schedule: values.schedule,
        name: values.name,
        deliver: values.deliver
      })

      setJobs(current => (current ? current.map(row => (row.id === updated.id ? updated : row)) : current))
      notify({ kind: 'success', title: 'Cron updated', message: truncate(jobTitle(updated), 60) })
    }

    setEditor({ mode: 'closed' })
  }

  return (
    <section {...props} className="flex h-full min-w-0 flex-col overflow-hidden rounded-b-[0.9375rem] bg-background">
      <header className={titlebarHeaderBaseClass}>
        <h2 className="pointer-events-auto text-base font-semibold leading-none tracking-tight">Cron</h2>
        <span className="pointer-events-auto text-xs text-muted-foreground">
          {totalCount === 0 ? 'No scheduled jobs' : `${enabledCount}/${totalCount} active`}
        </span>
      </header>

      <div className="min-h-0 flex-1 overflow-hidden rounded-b-[1.0625rem] border border-border/50 bg-background/85">
        <div className="border-b border-border/50 px-4 py-3">
          <div className="flex flex-wrap items-center gap-2">
            <Button onClick={() => setEditor({ mode: 'create' })} size="sm">
              <Plus />
              New cron
            </Button>

            <div className="ml-auto w-full max-w-sm min-w-64">
              <div className="relative">
                <Search className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground" />
                <Input
                  className="h-8 rounded-lg pl-8 pr-8 text-sm"
                  onChange={event => setQuery(event.target.value)}
                  placeholder="Search cron jobs..."
                  value={query}
                />
                {query && (
                  <Button
                    aria-label="Clear search"
                    className="absolute right-1 top-1/2 h-6 w-6 -translate-y-1/2 text-muted-foreground hover:text-foreground"
                    onClick={() => setQuery('')}
                    size="icon"
                    type="button"
                    variant="ghost"
                  >
                    <X className="size-3.5" />
                  </Button>
                )}
              </div>
            </div>
          </div>
        </div>

        {!jobs ? (
          <PageLoader label="Loading cron jobs..." />
        ) : visibleJobs.length === 0 ? (
          <EmptyState
            actionLabel={totalCount === 0 ? 'Create first cron' : undefined}
            description={
              totalCount === 0
                ? 'Schedule a prompt to run on a cron expression. Hermes will run it and deliver results to the destination you pick.'
                : 'Try a broader search query.'
            }
            onAction={totalCount === 0 ? () => setEditor({ mode: 'create' }) : undefined}
            title={totalCount === 0 ? 'No scheduled jobs yet' : 'No matches'}
          />
        ) : (
          <div className="h-full overflow-y-auto px-4 py-3">
            <div className="divide-y divide-border/40 rounded-lg border border-border/40 bg-background/70">
              {visibleJobs.map(job => (
                <CronJobRow
                  busy={busyJobId === job.id}
                  job={job}
                  key={job.id}
                  onDelete={() => setPendingDelete(job)}
                  onEdit={() => setEditor({ mode: 'edit', job })}
                  onPauseResume={() => void handlePauseResume(job)}
                  onTrigger={() => void handleTrigger(job)}
                />
              ))}
            </div>
          </div>
        )}
      </div>

      <CronEditorDialog editor={editor} onClose={() => setEditor({ mode: 'closed' })} onSave={handleEditorSave} />

      <Dialog onOpenChange={open => !open && !deleting && setPendingDelete(null)} open={pendingDelete !== null}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle>Delete cron job?</DialogTitle>
            <DialogDescription>
              {pendingDelete ? (
                <>
                  This will remove <span className="font-medium text-foreground">{truncate(jobTitle(pendingDelete), 60)}</span>{' '}
                  permanently. It will stop firing immediately.
                </>
              ) : null}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button disabled={deleting} onClick={() => setPendingDelete(null)} variant="outline">
              Cancel
            </Button>
            <Button disabled={deleting} onClick={() => void handleConfirmDelete()} variant="destructive">
              {deleting ? 'Deleting...' : 'Delete'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </section>
  )
}

function CronJobRow({
  busy,
  job,
  onDelete,
  onEdit,
  onPauseResume,
  onTrigger
}: {
  busy: boolean
  job: CronJob
  onDelete: () => void
  onEdit: () => void
  onPauseResume: () => void
  onTrigger: () => void
}) {
  const state = jobState(job)
  const isPaused = state === 'paused'
  const hasName = Boolean(jobName(job))
  const prompt = jobPrompt(job)
  const deliver = jobDeliver(job)

  return (
    <div className="grid gap-3 px-3 py-2.5 sm:grid-cols-[minmax(0,1fr)_auto] sm:items-start">
      <button
        className="min-w-0 cursor-pointer rounded-md text-left transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/40"
        onClick={onEdit}
        type="button"
      >
        <div className="flex flex-wrap items-center gap-2">
          <span className="truncate text-sm font-medium">{jobTitle(job)}</span>
          <StatePill tone={STATE_TONE[state] ?? 'muted'}>{state}</StatePill>
          {deliver && deliver !== DEFAULT_DELIVER && <StatePill tone="muted">{deliver}</StatePill>}
        </div>
        {hasName && prompt && <p className="mt-1 truncate text-xs text-muted-foreground">{truncate(prompt, 120)}</p>}
        <div className="mt-1 flex flex-wrap items-center gap-x-4 gap-y-1 text-[0.68rem] text-muted-foreground">
          <span className="inline-flex items-center gap-1 font-mono">
            <Clock className="size-3" />
            {jobScheduleDisplay(job)}
          </span>
          <span>Last: {formatTime(job.last_run_at)}</span>
          <span>Next: {formatTime(job.next_run_at)}</span>
        </div>
        {job.last_error && (
          <p className="mt-1 inline-flex items-start gap-1 text-[0.68rem] text-destructive">
            <AlertTriangle className="mt-px size-3 shrink-0" />
            <span className="line-clamp-2">{job.last_error}</span>
          </p>
        )}
      </button>

      <div className="flex shrink-0 items-center gap-0.5">
        <IconAction
          aria-label={isPaused ? 'Resume cron' : 'Pause cron'}
          disabled={busy}
          onClick={onPauseResume}
          title={isPaused ? 'Resume' : 'Pause'}
        >
          {isPaused ? <Play className="size-3.5" /> : <Pause className="size-3.5" />}
        </IconAction>
        <IconAction aria-label="Trigger now" disabled={busy} onClick={onTrigger} title="Trigger now">
          <Zap className="size-3.5" />
        </IconAction>
        <IconAction aria-label="Edit cron" onClick={onEdit} title="Edit">
          <Pencil className="size-3.5" />
        </IconAction>
        <IconAction
          aria-label="Delete cron"
          className="text-muted-foreground hover:bg-destructive/10 hover:text-destructive"
          onClick={onDelete}
          title="Delete"
        >
          <Trash2 className="size-3.5" />
        </IconAction>
      </div>
    </div>
  )
}

function IconAction({
  children,
  className,
  ...props
}: Omit<React.ComponentProps<typeof Button>, 'size' | 'variant'>) {
  return (
    <Button className={cn('size-7 text-muted-foreground hover:text-foreground', className)} size="icon" variant="ghost" {...props}>
      {children}
    </Button>
  )
}

function StatePill({ children, tone }: { children: string; tone: keyof typeof PILL_TONE }) {
  return (
    <span className={cn('inline-flex items-center rounded-full px-1.5 py-0.5 text-[0.64rem] capitalize', PILL_TONE[tone])}>
      {children}
    </span>
  )
}

function EmptyState({
  actionLabel,
  description,
  onAction,
  title
}: {
  actionLabel?: string
  description: string
  onAction?: () => void
  title: string
}) {
  return (
    <div className="grid h-full place-items-center px-6 py-12 text-center">
      <div className="max-w-sm space-y-2">
        <div className="text-sm font-medium">{title}</div>
        <p className="text-xs text-muted-foreground">{description}</p>
        {actionLabel && onAction && (
          <Button className="mt-2" onClick={onAction} size="sm">
            <Plus />
            {actionLabel}
          </Button>
        )}
      </div>
    </div>
  )
}

function CronEditorDialog({
  editor,
  onClose,
  onSave
}: {
  editor: EditorState
  onClose: () => void
  onSave: (values: EditorValues) => Promise<void>
}) {
  const open = editor.mode !== 'closed'
  const isEdit = editor.mode === 'edit'
  const initial = isEdit ? editor.job : null

  const [name, setName] = useState('')
  const [prompt, setPrompt] = useState('')
  const [schedule, setSchedule] = useState('')
  const [deliver, setDeliver] = useState(DEFAULT_DELIVER)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<null | string>(null)

  useEffect(() => {
    if (!open) {
      return
    }

    setName(initial ? jobName(initial) : '')
    setPrompt(initial ? jobPrompt(initial) : '')
    setSchedule(initial ? jobScheduleExpr(initial) : '')
    setDeliver(initial ? jobDeliver(initial) : DEFAULT_DELIVER)
    setError(null)
    setSaving(false)
  }, [initial, open])

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault()
    const trimmedPrompt = prompt.trim()
    const trimmedSchedule = schedule.trim()

    if (!trimmedPrompt || !trimmedSchedule) {
      setError('Prompt and schedule are required.')

      return
    }

    setSaving(true)
    setError(null)

    try {
      await onSave({
        deliver,
        name: name.trim(),
        prompt: trimmedPrompt,
        schedule: trimmedSchedule
      })
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to save cron job')
    } finally {
      setSaving(false)
    }
  }

  return (
    <Dialog onOpenChange={value => !value && !saving && onClose()} open={open}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>{isEdit ? 'Edit cron job' : 'New cron job'}</DialogTitle>
          <DialogDescription>
            {isEdit
              ? 'Update the schedule, prompt, or delivery target. Changes apply on next run.'
              : 'Schedule a prompt to run automatically. Use cron syntax or a natural phrase like "every 15 minutes".'}
          </DialogDescription>
        </DialogHeader>

        <form className="grid gap-4" onSubmit={handleSubmit}>
          <Field htmlFor="cron-name" label="Name" optional>
            <Input
              autoFocus
              id="cron-name"
              onChange={event => setName(event.target.value)}
              placeholder="Morning briefing"
              value={name}
            />
          </Field>

          <Field htmlFor="cron-prompt" label="Prompt">
            <Textarea
              className="min-h-24 font-mono"
              id="cron-prompt"
              onChange={event => setPrompt(event.target.value)}
              placeholder="Summarize my unread Slack threads and email me the top 5..."
              value={prompt}
            />
          </Field>

          <div className="grid gap-4 sm:grid-cols-2">
            <Field htmlFor="cron-schedule" label="Schedule">
              <Input
                className="font-mono"
                id="cron-schedule"
                onChange={event => setSchedule(event.target.value)}
                placeholder="0 9 * * *"
                value={schedule}
              />
              <FieldHint>Cron expression, or phrases like "every hour" or "weekdays at 9am".</FieldHint>
            </Field>

            <Field htmlFor="cron-deliver" label="Deliver to">
              <Select onValueChange={setDeliver} value={deliver}>
                <SelectTrigger id="cron-deliver">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {DELIVERY_OPTIONS.map(option => (
                    <SelectItem key={option.value} value={option.value}>
                      {option.label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </Field>
          </div>

          {error && (
            <div className="flex items-start gap-2 rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-xs text-destructive">
              <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />
              <span>{error}</span>
            </div>
          )}

          <DialogFooter>
            <Button disabled={saving} onClick={onClose} type="button" variant="outline">
              Cancel
            </Button>
            <Button disabled={saving} type="submit">
              {saving ? 'Saving...' : isEdit ? 'Save changes' : 'Create cron'}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

function Field({
  children,
  htmlFor,
  label,
  optional
}: {
  children: React.ReactNode
  htmlFor: string
  label: string
  optional?: boolean
}) {
  return (
    <div className="grid gap-1.5">
      <label className="flex items-baseline gap-2 text-xs font-medium text-foreground" htmlFor={htmlFor}>
        {label}
        {optional && <span className="text-[0.65rem] font-normal text-muted-foreground">Optional</span>}
      </label>
      {children}
    </div>
  )
}

function FieldHint({ children }: { children: React.ReactNode }) {
  return <p className="text-[0.66rem] leading-4 text-muted-foreground">{children}</p>
}

type EditorState = { mode: 'closed' } | { mode: 'create' } | { job: CronJob; mode: 'edit' }

interface EditorValues {
  deliver: string
  name: string
  prompt: string
  schedule: string
}
