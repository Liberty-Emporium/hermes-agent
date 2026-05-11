import { describe, expect, it } from 'vitest'

import {
  appendAssistantTextPart,
  chatMessageText,
  renderMediaTags,
  toChatMessages,
  upsertToolPart
} from './chat-messages'

describe('toChatMessages', () => {
  it('keeps a turn with interleaved tool-only rows in a single bubble', () => {
    const messages = toChatMessages([
      { role: 'assistant', content: 'Planning.', timestamp: 1 },
      {
        role: 'assistant',
        content: '',
        timestamp: 2,
        tool_calls: [{ id: 'tc', function: { name: 'terminal', arguments: '{}' } }]
      },
      { role: 'assistant', content: 'Done.', timestamp: 3 }
    ])

    expect(messages).toHaveLength(1)
    expect(messages[0].parts.map(p => p.type)).toEqual(['text', 'tool-call', 'text'])
    expect(chatMessageText(messages[0])).toBe('Planning.Done.')
  })

  it('keeps assistant tool-call iterations in one loaded assistant bubble', () => {
    const messages = toChatMessages([
      { role: 'user', content: 'check this repo', timestamp: 1 },
      {
        role: 'assistant',
        content: "Let me also check if there's a top-level lint workflow.",
        timestamp: 2,
        tool_calls: [{ id: 'tc-1', function: { name: 'search_files', arguments: '{"path":".github"}' } }]
      },
      {
        role: 'tool',
        tool_call_id: 'tc-1',
        tool_name: 'search_files',
        content: '{"error":"Path not found: /repo/.github"}',
        timestamp: 3
      },
      {
        role: 'assistant',
        content: 'No CI in this repo. Build is enough.',
        timestamp: 4,
        tool_calls: [{ id: 'tc-2', function: { name: 'terminal', arguments: '{"command":"git status --short"}' } }]
      },
      {
        role: 'tool',
        tool_call_id: 'tc-2',
        tool_name: 'terminal',
        content: '{"output":"M src/ui/components/image-distortion.tsx\\n","exit_code":0}',
        timestamp: 5
      },
      { role: 'assistant', content: 'Now let me check git status and commit.', timestamp: 6 }
    ])

    const assistantMessages = messages.filter(message => message.role === 'assistant')

    expect(assistantMessages).toHaveLength(1)
    expect(assistantMessages[0].parts.filter(part => part.type === 'tool-call')).toHaveLength(2)
    expect(chatMessageText(assistantMessages[0])).toContain("Let me also check if there's a top-level lint workflow.")
    expect(chatMessageText(assistantMessages[0])).toContain('Now let me check git status and commit.')
  })

  it('hides attached context payloads from user message display', () => {
    const [message] = toChatMessages([
      {
        role: 'user',
        content:
          'what is this file\n\n--- Attached Context ---\n\n📄 @file:tsconfig.tsbuildinfo (981 tokens)\n```json\n{"root":["./src/main.tsx"]}\n```',
        timestamp: 1
      }
    ])

    expect(chatMessageText(message)).toBe('@file:tsconfig.tsbuildinfo\n\nwhat is this file')
  })

  it('renders MEDIA tags as assistant attachment links', () => {
    const [message] = toChatMessages([
      {
        role: 'assistant',
        content: "MEDIA:/Users/brooklyn/.hermes/cache/audio/tts_20260501_222725.mp3\n\nhow's that sound?",
        timestamp: 1
      }
    ])

    expect(chatMessageText(message)).toBe(
      "[Audio: tts_20260501_222725.mp3](#media:%2FUsers%2Fbrooklyn%2F.hermes%2Fcache%2Faudio%2Ftts_20260501_222725.mp3)\n\nhow's that sound?"
    )
  })

  it('coerces non-string message content without throwing', () => {
    const [message] = toChatMessages([
      {
        content: {
          text: 'hello from object content'
        },
        role: 'assistant',
        timestamp: 1
      }
    ])

    expect(chatMessageText(message)).toBe('hello from object content')
  })

  it('applies attached-context filtering when user content is object-shaped', () => {
    const [message] = toChatMessages([
      {
        content: {
          text: 'look\n\n--- Attached Context ---\n\n📄 @file:foo.ts (10 tokens)\n```ts\nconst x = 1\n```'
        },
        role: 'user',
        timestamp: 1
      }
    ])

    expect(chatMessageText(message)).toBe('@file:foo.ts\n\nlook')
  })
})

describe('renderMediaTags', () => {
  it('renders standalone and inline MEDIA tags as links', () => {
    expect(renderMediaTags('here\nMEDIA:/tmp/voice.mp3\nthere')).toBe(
      'here\n[Audio: voice.mp3](#media:%2Ftmp%2Fvoice.mp3)\nthere'
    )
    expect(renderMediaTags('audio: MEDIA:/tmp/voice.mp3 done')).toBe(
      'audio: [Audio: voice.mp3](#media:%2Ftmp%2Fvoice.mp3) done'
    )
    expect(renderMediaTags('MEDIA:/tmp/demo.mp4')).toBe('[Video: demo.mp4](#media:%2Ftmp%2Fdemo.mp4)')
  })

  it('renders streamed assistant media once the tag is complete', () => {
    const parts = appendAssistantTextPart(appendAssistantTextPart([], 'ok\nMEDIA:'), '/tmp/voice.mp3')
    const text = chatMessageText({ id: 'a', role: 'assistant', parts })

    expect(text).toBe('ok\n[Audio: voice.mp3](#media:%2Ftmp%2Fvoice.mp3)')
  })
})

describe('upsertToolPart', () => {
  it('preserves inline diffs from tool completion events', () => {
    const parts = upsertToolPart(
      [],
      {
        inline_diff: '--- a/foo.ts\n+++ b/foo.ts\n@@\n-old\n+new',
        name: 'patch',
        tool_id: 'tool-1'
      },
      'complete'
    )

    const [part] = parts

    expect(part?.type).toBe('tool-call')
    expect(part && 'result' in part ? part.result : undefined).toMatchObject({
      inline_diff: '--- a/foo.ts\n+++ b/foo.ts\n@@\n-old\n+new'
    })
  })

  it('keeps live todo rows stable across sparse progress payloads', () => {
    const first = upsertToolPart(
      [],
      {
        name: 'todo',
        todos: [{ content: 'Boil water', id: 'boil', status: 'in_progress' }],
        tool_id: 'todo-1'
      },
      'running'
    )

    const progressed = upsertToolPart(
      first,
      {
        name: 'todo',
        preview: 'updating plan',
        tool_id: 'todo-1'
      },
      'running'
    )

    const [part] = progressed
    const args = part && 'args' in part ? (part.args as Record<string, unknown>) : {}

    expect(args.todos).toEqual([{ content: 'Boil water', id: 'boil', status: 'in_progress' }])
  })

  it('archives todo state on completion and accepts explicit empty clears', () => {
    const started = upsertToolPart(
      [],
      {
        name: 'todo',
        todos: [{ content: 'Boil water', id: 'boil', status: 'in_progress' }],
        tool_id: 'todo-1'
      },
      'running'
    )

    const completed = upsertToolPart(
      started,
      {
        name: 'todo',
        tool_id: 'todo-1'
      },
      'complete'
    )

    const cleared = upsertToolPart(
      completed,
      {
        name: 'todo',
        todos: [],
        tool_id: 'todo-1'
      },
      'complete'
    )

    const completedResult = completed[0] && 'result' in completed[0] ? (completed[0].result as Record<string, unknown>) : {}
    const clearedResult = cleared[0] && 'result' in cleared[0] ? (cleared[0].result as Record<string, unknown>) : {}

    expect(completedResult.todos).toEqual([{ content: 'Boil water', id: 'boil', status: 'in_progress' }])
    expect(clearedResult.todos).toEqual([])
  })
})
