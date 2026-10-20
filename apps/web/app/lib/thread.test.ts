import { describe, expect, it } from 'vitest'
import type { ChatEvent, ChatMessage, ChatTurn, Memory } from './api'
import { applyMemoryEvent, buildThread, memoryLineState, type MemoryLine, type ThreadItem } from './thread'

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
    expect(kinds(items)).toEqual(['user', 'text', 'steps', 'text', 'foot'])
    const steps = items[2] as Extract<ThreadItem, { kind: 'steps' }>
    expect(steps.live).toBe(false)
    expect(steps.steps.map((step) => step.type)).toEqual(['tool', 'note', 'tool'])
    const read = steps.steps[0]!
    expect(read.type === 'tool' && read.step).toMatchObject({ kind: 'read', target: 'doc.md', lines: { from: 16, to: 60 } })
    expect(read.type === 'tool' && read.output).toBe('line')
    const edit = steps.steps[2]!
    expect(edit.type === 'tool' && edit.card?.commit).toBe('c1')
    expect(edit.type === 'tool' && edit.added).toBe(1)
    // The last answer of a finished turn is the one its foot copies.
    expect(items[4]).toMatchObject({ kind: 'foot', text: 'Done.', used: null })
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
    expect(kinds(items)).toEqual(['user', 'subagent', 'text', 'foot'])
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

  it('reads the selection’s text and the sections a message carried', () => {
    const messages = [
      message('user', [
        { type: 'text', text: 'Explain' },
        { type: 'text', text: 'Instead of performing a single attention function', context: 'selection' },
        { type: 'text', text: '', context: 'section', section: '3.2 Attention' },
        { type: 'text', text: '', context: 'section', section: '3.3 Feed-Forward' },
      ]),
    ]
    const { items } = buildThread({ turns: [turn(false)], messages, groups: new Map() })
    expect(items[0]).toMatchObject({
      kind: 'user',
      text: 'Explain',
      selection: 'Instead of performing a single attention function',
      sections: ['3.2 Attention', '3.3 Feed-Forward'],
    })
  })
})

const SAVED = { id: 12, kind: 'term', text: 'query → 查詢', document_id: null, pinned: false }
const REMEMBER = { kind: 'term', text: 'query → 查詢', scope: 'all', action: 'save', id: '' }

describe('memory lines and what an answer used', () => {
  it('turns a memory call into a line under the final answer, never a step', () => {
    const messages = [
      message('user', [{ type: 'text', text: 'Translate query as 查詢 from now on' }]),
      message('assistant', [
        { type: 'text', text: 'Noted.' },
        { type: 'tool_use', id: 'm', name: 'memory', input: REMEMBER },
      ]),
      message('user', [{ type: 'tool_result', tool_use_id: 'm', content: 'Saved as m12.' }]),
      message('assistant', [{ type: 'text', text: 'I will use 查詢.' }]),
    ]
    const groups = events(
      { event: 'turn_start', model: 'm', instructions: true, memory_ids: [3, 4] },
      { event: 'tool_use', id: 'm', name: 'memory', input: REMEMBER },
      { event: 'memory', tool_use_id: 'm', action: 'saved', memory: SAVED, previous: null },
      { event: 'tool_result', id: 'm', name: 'memory', is_error: false, output: 'Saved as m12.' },
      { event: 'turn_done' },
    )
    const { items } = buildThread({ turns: [turn(true)], messages, groups })
    expect(kinds(items)).toEqual(['user', 'text', 'text', 'memory', 'foot'])
    expect(items[3]).toMatchObject({ kind: 'memory', line: { toolUseId: 'm', action: 'saved', memory: SAVED } })
    expect(items[4]).toMatchObject({ text: 'I will use 查詢.', used: { instructions: true, memoryIds: [3, 4] } })
  })

  it('hides a live memory call too, the line following the newest text', () => {
    const messages = [message('user', [{ type: 'text', text: 'Remember this' }])]
    const groups = events(
      { event: 'turn_start', model: 'm', instructions: false, memory_ids: [] },
      { event: 'text_delta', text: 'Saving it.' },
      { event: 'tool_use', id: 'm', name: 'memory', input: REMEMBER },
      { event: 'memory', tool_use_id: 'm', action: 'saved', memory: SAVED, previous: null },
    )
    const { items } = buildThread({ turns: [turn(false)], messages, groups })
    expect(kinds(items)).toEqual(['user', 'text', 'memory', 'working'])
  })

  it('says nothing of what a turn used when it had neither instructions nor memories', () => {
    const messages = [message('user', [{ type: 'text', text: 'Hi' }]), message('assistant', [{ type: 'text', text: 'Hello.' }])]
    for (const start of [{ model: 'm' }, { model: 'm', instructions: false, memory_ids: [] }]) {
      const { items } = buildThread({ turns: [turn(true)], messages, groups: events({ event: 'turn_start', ...start }) })
      expect(items.at(-1)).toMatchObject({ kind: 'foot', used: null })
    }
  })

  it('keeps the lines of a turn that ended without an answer', () => {
    const messages = [message('user', [{ type: 'text', text: 'Forget that' }])]
    const groups = events(
      { event: 'memory', tool_use_id: 'f', action: 'forgotten', memory: SAVED, previous: null },
      { event: 'turn_error', status: 'failed', error: 'boom' },
    )
    const { items } = buildThread({ turns: [turn(true, 'failed')], messages, groups })
    expect(kinds(items)).toEqual(['user', 'memory', 'error'])
  })
})

function row(fields: Partial<Memory> = {}): Memory {
  return {
    ...SAVED,
    kind: 'term',
    document_name: null,
    conversation_id: 1,
    conversation_title: null,
    source_ref: null,
    created_at: '2026-10-20T00:00:00Z',
    updated_at: '2026-10-20T00:00:00Z',
    ...fields,
  }
}

describe('memoryLineState', () => {
  const saved: MemoryLine = { toolUseId: 'm', action: 'saved', memory: { ...SAVED, kind: 'term' }, previous: null }

  it('waits for the memories, then finds the saved row by the call', () => {
    expect(memoryLineState(saved, null)).toEqual({ state: 'loading' })
    expect(memoryLineState(saved, [row({ source_ref: 'm' })])).toMatchObject({ state: 'saved', row: { id: 12 } })
    // Saved again after an Undo: a new row, found by the call it carries.
    expect(memoryLineState(saved, [row({ id: 40, source_ref: 'm' })])).toMatchObject({ state: 'saved', row: { id: 40 } })
    // Gone: *Undone* only right after this line's own Undo, else neutrally gone.
    const other = row({ id: 7, text: 'key → 鍵', source_ref: 'other' })
    expect(memoryLineState(saved, [other], true)).toEqual({ state: 'unsaved' })
    expect(memoryLineState(saved, [other])).toEqual({ state: 'removed' })
  })

  it('reads a save a later turn forgot as gone, not as undone', () => {
    const forgot: MemoryLine = { toolUseId: 'f', action: 'forgotten', memory: { ...SAVED, kind: 'term' }, previous: null }
    expect(memoryLineState(saved, [])).toEqual({ state: 'removed' })
    expect(memoryLineState(forgot, [])).toEqual({ state: 'forgotten' })
  })

  it('never offers to keep a memory twice once a forget is undone', () => {
    const forgot: MemoryLine = { toolUseId: 'f', action: 'forgotten', memory: { ...SAVED, kind: 'term' }, previous: null }
    // The forget's Undo saved it again as a new row carrying the forget's call.
    const back = [row({ id: 41, source_ref: 'f' })]
    expect(memoryLineState(forgot, back)).toMatchObject({ state: 'restored', row: { id: 41 } })
    // The save's line finds the memory held by that row: View only, no Save again.
    expect(memoryLineState(saved, back)).toEqual({ state: 'elsewhere', row: back[0] })
    expect(memoryLineState(saved, back, true)).toMatchObject({ state: 'elsewhere' })
    // And the other way round: saved again from the save's line, the forget's Undo is moot.
    const savedAgain = [row({ id: 42, source_ref: 'm' })]
    expect(memoryLineState(forgot, savedAgain)).toEqual({ state: 'elsewhere', row: savedAgain[0] })
  })

  it('reads every line after Clear all as gone, none of them as undone', () => {
    const updated: MemoryLine = {
      toolUseId: 'u',
      action: 'updated',
      memory: { ...SAVED, kind: 'term', text: 'query → 查詢語句' },
      previous: { ...SAVED, kind: 'term' },
    }
    const forgot: MemoryLine = { toolUseId: 'f', action: 'forgotten', memory: { ...SAVED, kind: 'term' }, previous: null }
    expect(memoryLineState(saved, [])).toEqual({ state: 'removed' })
    expect(memoryLineState(updated, [])).toEqual({ state: 'changed', row: null })
    expect(memoryLineState(forgot, [])).toEqual({ state: 'forgotten' })
  })

  it('reads an update as applied, undone, or changed since', () => {
    const updated: MemoryLine = {
      toolUseId: 'u',
      action: 'updated',
      memory: { ...SAVED, kind: 'term', text: 'query → 查詢語句' },
      previous: { ...SAVED, kind: 'term' },
    }
    expect(memoryLineState(updated, [row({ text: 'query → 查詢語句', pinned: true })])).toMatchObject({ state: 'updated' })
    expect(memoryLineState(updated, [row()])).toMatchObject({ state: 'reverted', row: { id: 12 } })
    expect(memoryLineState(updated, [row({ text: 'edited on the Agent page' })])).toMatchObject({ state: 'changed' })
    expect(memoryLineState(updated, [])).toEqual({ state: 'changed', row: null })
  })

  it('reads a forget as forgotten until Undo puts a row back for the call', () => {
    const forgotten: MemoryLine = { toolUseId: 'f', action: 'forgotten', memory: { ...SAVED, kind: 'term' }, previous: null }
    expect(memoryLineState(forgotten, [])).toEqual({ state: 'forgotten' })
    expect(memoryLineState(forgotten, [row({ id: 41, source_ref: 'f' })])).toMatchObject({ state: 'restored', row: { id: 41 } })
  })
})

describe('applyMemoryEvent', () => {
  const context = { conversationId: 3, conversationTitle: 'Attention', now: '2026-10-20T12:00:00Z' }
  const saved: MemoryLine = { toolUseId: 'm', action: 'saved', memory: { ...SAVED, kind: 'term' }, previous: null }
  function frame(action: 'saved' | 'updated' | 'forgotten', memory: MemoryLine['memory'] = saved.memory) {
    return { event: 'memory', turn_id: TURN, tool_use_id: 'm', action, memory, previous: null } as const
  }

  it('reads a fresh save as saved before the list is read again', () => {
    const before = [row({ id: 3, text: 'key → 鍵' })]
    expect(memoryLineState(saved, before)).toEqual({ state: 'removed' })
    const after = applyMemoryEvent(before, frame('saved'), context)
    expect(memoryLineState(saved, after)).toMatchObject({
      state: 'saved',
      row: { id: 12, source_ref: 'm', conversation_id: 3 },
    })
    // A frame for a row already listed adds nothing.
    expect(applyMemoryEvent(after, frame('saved'), context)).toHaveLength(2)
  })

  it('rewrites a changed row and drops a forgotten one', () => {
    const before = [row({ pinned: true }), row({ id: 3, text: 'key → 鍵' })]
    const updated: MemoryLine = {
      toolUseId: 'm',
      action: 'updated',
      memory: { ...SAVED, kind: 'term', text: 'query → 查詢語句' },
      previous: { ...SAVED, kind: 'term' },
    }
    const after = applyMemoryEvent(before, frame('updated', updated.memory), context)
    // The reader's pin stays theirs.
    expect(memoryLineState(updated, after)).toMatchObject({ state: 'updated', row: { pinned: true } })
    expect(applyMemoryEvent(before, frame('forgotten'), context).map((memory) => memory.id)).toEqual([3])
  })
})
