import { atom } from 'nanostores'

export type SubagentStatus = 'completed' | 'failed' | 'interrupted' | 'queued' | 'running'

export interface SubagentProgress {
  id: string
  apiCalls?: number
  costUsd?: number
  depth: number
  durationSeconds?: number
  filesRead: string[]
  filesWritten: string[]
  goal: string
  inputTokens?: number
  model?: string
  outputTail: { isError?: boolean; preview?: string; tool?: string }[]
  outputTokens?: number
  parentId: null | string
  reasoningTokens?: number
  sessionId: string
  status: SubagentStatus
  summary?: string
  taskCount: number
  taskIndex: number
  toolName?: string
  toolPreview?: string
  toolsets: string[]
  updatedAt: number
}

export interface SubagentNode extends SubagentProgress {
  children: SubagentNode[]
}

export type SubagentPayload = Record<string, unknown>

export const $subagentsBySession = atom<Record<string, SubagentProgress[]>>({})

const TERMINAL = new Set<SubagentStatus>(['completed', 'failed', 'interrupted'])

const asString = (value: unknown) => (typeof value === 'string' ? value : '')
const asNumber = (value: unknown) => (typeof value === 'number' && Number.isFinite(value) ? value : undefined)
const asStatus = (value: unknown): SubagentStatus =>
  value === 'completed' || value === 'failed' || value === 'interrupted' || value === 'queued' ? value : 'running'

const asStringList = (value: unknown) => (Array.isArray(value) ? value.map(asString).filter(Boolean) : [])

const asOutputTail = (value: unknown): SubagentProgress['outputTail'] =>
  Array.isArray(value)
    ? value
        .map(item => (item && typeof item === 'object' ? (item as Record<string, unknown>) : null))
        .filter((item): item is Record<string, unknown> => Boolean(item))
        .map(item => ({
          isError: item.is_error === true,
          preview: asString(item.preview) || undefined,
          tool: asString(item.tool) || undefined
        }))
    : []

function idFor(payload: SubagentPayload) {
  return (
    asString(payload.subagent_id) ||
    `${asString(payload.parent_id) || 'root'}:${asNumber(payload.task_index) ?? 0}:${asString(payload.goal)}`
  )
}

function toProgress(sessionId: string, payload: SubagentPayload, previous?: SubagentProgress): SubagentProgress {
  return {
    apiCalls: asNumber(payload.api_calls) ?? previous?.apiCalls,
    costUsd: asNumber(payload.cost_usd) ?? previous?.costUsd,
    depth: asNumber(payload.depth) ?? previous?.depth ?? 0,
    durationSeconds: asNumber(payload.duration_seconds) ?? previous?.durationSeconds,
    filesRead: asStringList(payload.files_read).length ? asStringList(payload.files_read) : (previous?.filesRead ?? []),
    filesWritten: asStringList(payload.files_written).length
      ? asStringList(payload.files_written)
      : (previous?.filesWritten ?? []),
    goal: asString(payload.goal) || previous?.goal || 'Subagent',
    id: previous?.id || idFor(payload),
    inputTokens: asNumber(payload.input_tokens) ?? previous?.inputTokens,
    model: asString(payload.model) || previous?.model,
    outputTail: asOutputTail(payload.output_tail).length ? asOutputTail(payload.output_tail) : (previous?.outputTail ?? []),
    outputTokens: asNumber(payload.output_tokens) ?? previous?.outputTokens,
    parentId: asString(payload.parent_id) || previous?.parentId || null,
    reasoningTokens: asNumber(payload.reasoning_tokens) ?? previous?.reasoningTokens,
    sessionId,
    status: asStatus(payload.status),
    summary: asString(payload.summary) || previous?.summary,
    taskCount: asNumber(payload.task_count) ?? previous?.taskCount ?? 1,
    taskIndex: asNumber(payload.task_index) ?? previous?.taskIndex ?? 0,
    toolName: asString(payload.tool_name) || previous?.toolName,
    toolPreview: asString(payload.tool_preview) || asString(payload.text) || previous?.toolPreview,
    toolsets: asStringList(payload.toolsets).length ? asStringList(payload.toolsets) : (previous?.toolsets ?? []),
    updatedAt: Date.now()
  }
}

export function clearSessionSubagents(sessionId: string) {
  const current = $subagentsBySession.get()

  if (!(sessionId in current)) {
    return
  }

  const next = { ...current }
  delete next[sessionId]
  $subagentsBySession.set(next)
}

export function upsertSubagent(sessionId: string, payload: SubagentPayload, createIfMissing = true) {
  const current = $subagentsBySession.get()
  const list = current[sessionId] ?? []
  const id = idFor(payload)
  const index = list.findIndex(item => item.id === id)

  if (index < 0 && !createIfMissing) {
    return
  }

  const previous = index >= 0 ? list[index] : undefined

  if (previous && TERMINAL.has(previous.status)) {
    return
  }

  const nextItem = toProgress(sessionId, payload, previous)
  const nextList = index >= 0 ? list.map(item => (item.id === id ? nextItem : item)) : [...list, nextItem]

  $subagentsBySession.set({ ...current, [sessionId]: nextList })
}

export function buildSubagentTree(items: readonly SubagentProgress[]): SubagentNode[] {
  const nodes = new Map<string, SubagentNode>()

  for (const item of items) {
    nodes.set(item.id, { ...item, children: [] })
  }

  const roots: SubagentNode[] = []

  for (const node of nodes.values()) {
    const parent = node.parentId ? nodes.get(node.parentId) : null

    if (parent) {
      parent.children.push(node)
    } else {
      roots.push(node)
    }
  }

  const sort = (a: SubagentNode, b: SubagentNode) => a.taskIndex - b.taskIndex || a.goal.localeCompare(b.goal)
  const walk = (node: SubagentNode) => node.children.sort(sort).forEach(walk)

  roots.sort(sort).forEach(walk)

  return roots
}

export const activeSubagentCount = (items: readonly SubagentProgress[]) =>
  items.filter(item => item.status === 'queued' || item.status === 'running').length
