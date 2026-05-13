import { beforeEach, describe, expect, it } from 'vitest'

import { $subagentsBySession, activeSubagentCount, buildSubagentTree, clearSessionSubagents, upsertSubagent } from './subagents'

describe('subagent store', () => {
  beforeEach(() => {
    $subagentsBySession.set({})
  })

  it('upserts subagent progress and keeps terminal status stable', () => {
    upsertSubagent('s1', {
      goal: 'scan files',
      status: 'running',
      subagent_id: 'a1',
      task_index: 0
    })
    upsertSubagent('s1', {
      goal: 'scan files',
      status: 'completed',
      subagent_id: 'a1',
      summary: 'done',
      task_index: 0
    })
    upsertSubagent('s1', {
      goal: 'scan files',
      status: 'running',
      subagent_id: 'a1',
      task_index: 0,
      text: 'late'
    })

    const item = $subagentsBySession.get().s1?.[0]
    expect(item?.status).toBe('completed')
    expect(item?.summary).toBe('done')
  })

  it('builds parent/child trees', () => {
    upsertSubagent('s1', { goal: 'parent', status: 'running', subagent_id: 'p', task_index: 0 })
    upsertSubagent('s1', {
      goal: 'child',
      parent_id: 'p',
      status: 'queued',
      subagent_id: 'c',
      task_index: 1
    })

    const tree = buildSubagentTree($subagentsBySession.get().s1 ?? [])

    expect(tree).toHaveLength(1)
    expect(tree[0]?.children[0]?.goal).toBe('child')
    expect(activeSubagentCount($subagentsBySession.get().s1 ?? [])).toBe(2)
  })

  it('clears one session without touching another', () => {
    upsertSubagent('s1', { goal: 'one', status: 'running', subagent_id: 'a1', task_index: 0 })
    upsertSubagent('s2', { goal: 'two', status: 'running', subagent_id: 'a2', task_index: 0 })

    clearSessionSubagents('s1')

    expect($subagentsBySession.get().s1).toBeUndefined()
    expect($subagentsBySession.get().s2).toHaveLength(1)
  })
})
