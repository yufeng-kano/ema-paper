import { describe, expect, it } from 'vitest'
import type { ChatEvent, ChatMessage, ChatTurn } from './api'
import { buildThread, type ThreadItem } from './thread'

const TURN = 'turn-1'

function turn(finished: boolean, status = finished ? 'succeeded' : 'running'): ChatTurn {
  return { id: TURN, status, error: null, finished_at: finished ? '2026-10-17T00:00:00Z' : null }
}

let nextId = 1
function message(role: 'user' | 'assistant', content: ChatMessage['content']): ChatMessage {
  nextId += 1
  return { id: nextId, role, content, turn_id: TURN, created_at: '2026-10-17T00:00:00Z' }
}

function events(...list: Array<Record<string, unknown>>): Map<string, ChatEvent[]> {
  return new Map([[TURN, list.map((event) => ({ turn_id: TURN, ...event }) as ChatEvent)]])
}

function kinds(items: ThreadItem[]): string[] {
  return items.map((item) => item.kind)
}

const SED = { command: 'cd "/workspace/files/a"; sed -n 16,60p doc.md' }
const EDIT = { command: 'str_replace', path: '/workspace/files/a/doc.md', old_str: 'x', new_str: 'x\ny' }

describe('buildThread', () => {
  it('groups a stored turn’s steps between its texts and sums its edits up', () => {
    const messages = [
      message('user', [{ type: 'text', text: 'Go' }]),
      message('assistant', [
        { type: 'text', text: 'Reading first.' },
        { type: 'tool_use', id: 'a', name: 'bash', input: SED },
      ]),
      message('user', [{ type: 'tool_result', tool_use_id: 'a', content: 'line' }]),
      message('assistant', [
        { type: 'thinking', thinking: 'Sections found.' },
        { type: 'tool_use', id: 'b', name: 'str_replace_based_edit_tool', input: EDIT },
      ]),
      message('user', [{ type: 'tool_result', tool_use_id: 'b', content: 'ok' }]),
      message('assistant', [{ type: 'text', text: 'Done.' }]),
    ]
    const groups = events(
      { event: 'edit', tool_use_id: 'b', path: 'files/a/doc.md', command: 'str_replace', commit: 'c1', diff: '@@ -1 +1,2 @@\n x\n+y\n' },
      { event: 'turn_done' },
    )
    const { items, liveTurns } = buildThread({ turns: [turn(true)], messages, groups })
    expect(liveTurns).toEqual([])
    expect(kinds(items)).toEqual(['user', 'text', 'steps', 'text'])
    const steps = items[2] as Extract<ThreadItem, { kind: 'steps' }>
    expect(steps.live).toBe(false)
    expect(steps.steps.map((step) => step.type)).toEqual(['tool', 'note', 'tool'])
    const read = steps.steps[0]!
    expect(read.type === 'tool' && read.step).toMatchObject({ kind: 'read', target: 'doc.md', lines: { from: 16, to: 60 } })
    expect(read.type === 'tool' && read.output).toBe('line')
    const edit = steps.steps[2]!
    expect(edit.type === 'tool' && edit.card?.commit).toBe('c1')
    expect(edit.type === 'tool' && edit.added).toBe(1)
    expect((items[3] as Extract<ThreadItem, { kind: 'text' }>).copy).toBe(true)
    expect((items[1] as Extract<ThreadItem, { kind: 'text' }>).copy).toBe(false)
  })

  it('shows a live turn from its events, the trailing group in flight', () => {
    const messages = [message('user', [{ type: 'text', text: 'Check page 3' }])]
    const groups = events(
      { event: 'turn_start', model: 'm' },
      { event: 'text_delta', text: 'Looking.' },
      { event: 'tool_use', id: 'p', name: 'view_page', input: { page: 3 } },
    )
    const { items, liveTurns } = buildThread({ turns: [turn(false)], messages, groups })
    expect(liveTurns).toEqual([TURN])
    expect(kinds(items)).toEqual(['user', 'text', 'steps'])
    const steps = items[2] as Extract<ThreadItem, { kind: 'steps' }>
    expect(steps.live).toBe(true)
    expect(steps.steps[0]).toMatchObject({ type: 'tool', status: 'pending', step: { kind: 'page', page: 3 } })
  })

  it('keeps a group in flight while a call of it runs beside a later subagent', () => {
    const messages = [message('user', [{ type: 'text', text: 'Read and verify' }])]
    const groups = events(
      { event: 'thinking', text: 'Both at once.' },
      { event: 'text_delta', text: 'Starting.' },
      { event: 'tool_use', id: 'p', name: 'view_page', input: { page: 3 } },
      { event: 'tool_use', id: 's', name: 'subagent', input: { name: 'reader', task: 'section 4' } },
    )
    const { items } = buildThread({ turns: [turn(false)], messages, groups })
    expect(kinds(items)).toEqual(['user', 'steps', 'text', 'steps', 'subagent'])
    expect(items.map((item) => item.kind === 'steps' && item.live)).toEqual([false, false, false, true, false])
  })

  it('waits visibly on a turn that has streamed nothing yet', () => {
    const messages = [message('user', [{ type: 'text', text: 'Hi' }])]
    const { items } = buildThread({ turns: [turn(false, 'queued')], messages, groups: new Map() })
    expect(kinds(items)).toEqual(['user', 'working'])
  })

  it('draws a running subagent from its own events and restarts only it on its retry', () => {
    const messages = [message('user', [{ type: 'text', text: 'Verify' }])]
    const groups = events(
      { event: 'tool_use', id: 's', name: 'subagent', input: { name: 'verifier', task: 'pages 1-2' } },
      { event: 'tool_use', id: 'x', name: 'view_page', input: { page: 9 }, parent: 's' },
      { event: 'retry', attempt: 2, parent: 's' },
      { event: 'thinking', text: 'Page 1 first.', parent: 's' },
      { event: 'tool_use', id: 'y', name: 'view_page', input: { page: 1 }, parent: 's' },
      { event: 'tool_result', id: 'y', name: 'view_page', is_error: false, output: '(image)', parent: 's' },
      { event: 'tool_use', id: 'z', name: 'view_page', input: { page: 2 }, parent: 's' },
    )
    const { items } = buildThread({ turns: [turn(false)], messages, groups })
    expect(kinds(items)).toEqual(['user', 'subagent'])
    const card = (items[1] as Extract<ThreadItem, { kind: 'subagent' }>).card
    expect(card).toMatchObject({ name: 'verifier', task: 'pages 1-2', status: 'running' })
    expect(card.steps.map((step) => (step.type === 'tool' ? `${step.step.page}:${step.status}` : step.type))).toEqual([
      'note',
      '1:ok',
      '2:pending',
    ])
  })

  it('draws a finished subagent from the record its result carries', () => {
    const messages = [
      message('user', [{ type: 'text', text: 'Verify' }]),
      message('assistant', [{ type: 'tool_use', id: 's', name: 'subagent', input: { name: 'reader', task: 'section 4' } }]),
      message('user', [
        {
          type: 'tool_result',
          tool_use_id: 's',
          content: 'Two mismatches.',
          subagent: {
            name: 'reader',
            model: null,
            steps: [
              { type: 'tool', name: 'view_page', input: { page: 4 }, is_error: false },
              { type: 'note', text: 'Table 1 differs.' },
            ],
            tool_calls: 1,
            elapsed_ms: 52_000,
          },
        },
      ]),
      message('assistant', [{ type: 'text', text: 'Fixed.' }]),
    ]
    const { items } = buildThread({ turns: [turn(true)], messages, groups: new Map() })
    expect(kinds(items)).toEqual(['user', 'subagent', 'text'])
    const card = (items[1] as Extract<ThreadItem, { kind: 'subagent' }>).card
    expect(card).toMatchObject({ status: 'ok', report: 'Two mismatches.', elapsedMs: 52_000 })
    expect(card.steps.map((step) => step.type)).toEqual(['tool', 'note'])
  })

  it('gives a subagent’s writes their diff cards, live and from its record', () => {
    const edit = {
      event: 'edit',
      tool_use_id: 'w',
      path: 'files/a/doc.md',
      command: 'str_replace',
      commit: 'c1',
      diff: '@@ -1 +1,2 @@\n x\n+y\n',
      parent: 's',
    }
    const live = buildThread({
      turns: [turn(false)],
      messages: [message('user', [{ type: 'text', text: '/translate' }])],
      groups: events(
        { event: 'tool_use', id: 's', name: 'subagent', input: { name: 'translator', task: 'pages 1-2' } },
        { event: 'tool_use', id: 'w', name: 'str_replace_based_edit_tool', input: EDIT, parent: 's' },
        { event: 'tool_result', id: 'w', name: 'str_replace_based_edit_tool', is_error: false, output: 'ok', parent: 's' },
        edit,
      ),
    })
    const running = (live.items[1] as Extract<ThreadItem, { kind: 'subagent' }>).card
    expect(running.steps[0]).toMatchObject({ type: 'tool', id: 'w', card: { toolUseId: 'w', commit: 'c1' }, added: 1 })

    // Stored: the record's step id joins the replayed `edit`; a step from before ids has none.
    const messages = [
      message('user', [{ type: 'text', text: '/translate' }]),
      message('assistant', [{ type: 'tool_use', id: 's', name: 'subagent', input: { name: 'translator', task: 'pages 1-2' } }]),
      message('user', [
        {
          type: 'tool_result',
          tool_use_id: 's',
          content: '1 block.',
          subagent: {
            name: 'translator',
            model: null,
            steps: [
              { type: 'tool', id: 'w', name: 'str_replace_based_edit_tool', input: EDIT, is_error: false },
              { type: 'tool', name: 'str_replace_based_edit_tool', input: EDIT, is_error: false },
            ],
            tool_calls: 2,
            elapsed_ms: 1_000,
          },
        },
      ]),
    ]
    const stored = buildThread({ turns: [turn(true)], messages, groups: events(edit) })
    const card = (stored.items[1] as Extract<ThreadItem, { kind: 'subagent' }>).card
    expect(card.steps.map((step) => (step.type === 'tool' ? step.card?.commit ?? null : step.type))).toEqual([
      'c1',
      null,
    ])
  })

  it('opens a command’s answer with the skill it ran and reads the user’s message parts', () => {
    const messages = [
      message('user', [
        { type: 'image', attachment: { name: 'shot.png' } },
        { type: 'text', text: '', context: 'attachment', attachment: { name: 'data.csv', kind: 'text', chars: 10 } },
        { type: 'text', text: '/zh-summary 3–5' },
        { type: 'text', text: 'an earlier line', context: 'quote' },
        { type: 'text', text: '', context: 'skill', skill: 'zh-summary' },
      ]),
      message('assistant', [
        { type: 'text', text: 'On it.' },
        { type: 'tool_use', id: 'a', name: 'bash', input: SED },
      ]),
    ]
    const { items } = buildThread({ turns: [turn(false)], messages, groups: new Map() })
    expect(kinds(items)).toEqual(['user', 'text', 'steps'])
    const user = items[0] as Extract<ThreadItem, { kind: 'user' }>
    expect(user).toMatchObject({ text: '/zh-summary 3–5', command: 'zh-summary', quote: 'an earlier line' })
    expect(user.attachments.map((attachment) => [attachment.name, attachment.image, attachment.index])).toEqual([
      ['shot.png', true, 0],
      ['data.csv', false, 1],
    ])
    const steps = items[2] as Extract<ThreadItem, { kind: 'steps' }>
    expect(steps.steps[0]).toMatchObject({ type: 'skill', name: 'zh-summary' })
  })

  it('reads a cancelled turn as a cancellation, not an error', () => {
    const messages = [message('user', [{ type: 'text', text: 'Go' }])]
    const groups = events({ event: 'turn_error', status: 'cancelled', error: 'cancelled' })
    const { items } = buildThread({ turns: [turn(true, 'cancelled')], messages, groups })
    expect(kinds(items)).toEqual(['user', 'cancelled'])
  })
})
