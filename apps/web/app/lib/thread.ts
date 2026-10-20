/**
 * The agent panel's thread, built from the record and the stream (docs/web.md § The agent
 * panel). Pure, so the part that decides what the reader sees — which steps share a group,
 * where a subagent's steps come from, when a turn is done — is unit tested; the panel owns
 * only the reactive glue around it.
 *
 * The thread is `messages`, the persisted history, plus the live `chat_events` of the turn
 * in flight. A turn with its assistant messages stored renders from them (its edit cards
 * taking their commits from the replayed `edit` events); one still running renders from
 * its events as they arrive, and a stored turn still running shows the stream past the
 * last tool call the record holds. What only the events say — the memories a turn saved,
 * changed or forgot, and what it was given beyond the document — is read from them for
 * every turn, stored or live, since the panel replays a conversation's events from 0.
 */
import type { ChatEvent, ChatMessage, ChatTurn, ContentBlock, Memory, MemorySnapshot, SubagentRecord } from './api'
import { addedLines, diffFromInput, groupRuns, parseDiff, readStep, resultText, type DiffLine, type Step } from './steps'

/** The chat agent's editor tool and the commands of it that write (docs/chat.md § Tools). */
export const EDITOR_TOOL = 'str_replace_based_edit_tool'
export const EDITOR_WRITES = new Set(['str_replace', 'insert', 'create'])
export const SUBAGENT_TOOL = 'subagent'
/** A `memory` call is not a step: its `memory` event is a line under the answer instead
 *  (docs/web.md § The agent panel, *What the answer used, and memory lines*). */
export const MEMORY_TOOL = 'memory'

/** A turn the backend has not finished: queued, running, or cancelled and still winding
 *  down a tool call — `finished_at` says, not the status (docs/chat.md § Conversations). */
export const isLive = (turn: ChatTurn): boolean => turn.finished_at === null

export interface EditCard {
  /** The tool call that made it — the card's identity across reloads. */
  toolUseId: string
  path: string
  command: string
  lines: DiffLine[]
  /** The commit the `edit` event named; null for a card rebuilt from history alone. */
  commit: string | null
}

/** An edit card after the reader acted on it: reverted, and the commits that undo and redo
 *  it. Kept per tool call, which survives a reload where the item list does not. */
export interface CardState {
  reverted: boolean
  undoCommit: string | null
  redoCommit: string | null
  pending: boolean
}

/** `none`: the turn ended without a result for it (a cancel) — no mark to draw. */
export type StepStatus = 'pending' | 'ok' | 'error' | 'none'

export interface ToolStep {
  type: 'tool'
  key: string
  id: string
  name: string
  step: Step
  status: StepStatus
  /** The result's text, once it arrived. */
  output: string | null
  /** A shell step's whole command. */
  command: string | null
  card: EditCard | null
  /** The lines the step's edit added. */
  added: number | null
}

export interface NoteStep {
  type: 'note'
  key: string
  text: string
}

/** The *Use skill NAME* step a command message opens its answer with. */
export interface SkillRunStep {
  type: 'skill'
  key: string
  name: string
}

export type StepItem = ToolStep | NoteStep | SkillRunStep

export interface SubagentView {
  id: string
  name: string
  task: string
  status: 'running' | 'ok' | 'error' | 'none'
  steps: StepItem[]
  /** What it handed back: the call's result. */
  report: string | null
  /** From the stored record; null while it runs. */
  elapsedMs: number | null
  model: string | null
}

export interface UserAttachment {
  key: string
  name: string
  image: boolean
  /** The block's index in its message: what addresses an image's bytes. */
  index: number
  /** A message drawn before the record holds it shows its local preview. */
  preview: string | null
}

/** What one `memory` call did, from its event (docs/chat.md § Memory). */
export interface MemoryLine {
  toolUseId: string
  action: 'saved' | 'updated' | 'forgotten'
  /** The row as the call left it; as it was before a `forgotten`. */
  memory: MemorySnapshot
  /** The row before an `updated`. */
  previous: MemorySnapshot | null
}

/** What a turn was given beyond the document, from its `turn_start`. */
export interface UsedContext {
  instructions: boolean
  memoryIds: number[]
}

/** What the small card under an answer shows: a memory line's memory, the reader's
 *  instructions as they are now, or the memories a turn loaded. */
export type ContextCardView =
  | { kind: 'memory'; line: MemoryLine }
  | { kind: 'instructions' }
  | { kind: 'memories'; ids: number[] }

export type ThreadItem =
  | {
      kind: 'user'
      key: string
      /** The stored message the attachments belong to; null before one is known. */
      messageId: number | null
      text: string
      /** The passage of the document that went with it. */
      selection: string | null
      /** The headings of the whole sections that went with it. */
      sections: string[]
      quote: string | null
      command: string | null
      attachments: UserAttachment[]
    }
  | { kind: 'text'; key: string; text: string }
  | { kind: 'memory'; key: string; turnId: string; line: MemoryLine }
  /** Under a finished turn's last answer, after its memory lines: what the turn used and
   *  the control that copies the answer, as its markdown. */
  | { kind: 'foot'; key: string; turnId: string; text: string; used: UsedContext | null }
  | { kind: 'steps'; key: string; turnId: string; steps: StepItem[]; live: boolean }
  | { kind: 'subagent'; key: string; card: SubagentView }
  | { kind: 'error'; key: string; text: string }
  | { kind: 'cancelled'; key: string }
  | { kind: 'working'; key: string }

export interface ThreadInput {
  turns: readonly ChatTurn[]
  messages: readonly ChatMessage[]
  /** Each turn's frames, by the id every frame carries. */
  groups: ReadonlyMap<string, ChatEvent[]>
}

export interface Thread {
  items: ThreadItem[]
  /** The turns still in flight: the composer waits while there is one. */
  liveTurns: string[]
}

function lineOf(value: unknown): string {
  return typeof value === 'string' ? value : value === undefined ? '' : JSON.stringify(value)
}

function blockText(block: ContentBlock): string {
  return block.type === 'text' && typeof block.text === 'string' ? block.text : ''
}

/** The reader's own words: a block the backend marked as context is scaffolding. */
export function textOf(blocks: readonly ContentBlock[]): string {
  return blocks
    .filter((block) => block.type === 'text' && !block.context)
    .map(blockText)
    .join('\n')
    .trim()
}

/** The passage of the document that went with the message as its selection, or null. */
export function selectionOf(blocks: readonly ContentBlock[]): string | null {
  const text = blocks
    .filter((block) => block.type === 'text' && block.context === 'selection')
    .map(blockText)
    .join('\n')
    .trim()
  return text || null
}

/** The headings of the whole sections that went with the message, in the order sent. */
export function sectionsOf(blocks: readonly ContentBlock[]): string[] {
  return blocks.flatMap((block) =>
    block.type === 'text' && block.context === 'section' && block.section ? [block.section] : [],
  )
}

export function isToolResultOnly(blocks: readonly ContentBlock[]): boolean {
  return blocks.length > 0 && blocks.every((block) => block.type === 'tool_result')
}

/** The turns in order — those the record lists (oldest first, as the API sends them), then
 *  any a message or a frame names that it does not, in the order they were seen. */
export function turnOrder(input: ThreadInput): string[] {
  const ids: string[] = input.turns.map((turn) => turn.id)
  for (const message of input.messages) {
    if (message.turn_id !== null && !ids.includes(message.turn_id)) {
      ids.push(message.turn_id)
    }
  }
  for (const turnId of input.groups.keys()) {
    if (!ids.includes(turnId)) {
      ids.push(turnId)
    }
  }
  return ids
}

function isTerminal(turn: ChatTurn | undefined, group: ChatEvent[] | undefined, stored: boolean): boolean {
  if (group?.some((event) => event.event === 'turn_done' || event.event === 'turn_error')) {
    return true
  }
  if (turn) {
    return !isLive(turn)
  }
  return stored
}

function userItem(prefix: string, typed: ChatMessage[]): ThreadItem | null {
  const text = typed
    .map((message) => textOf(message.content))
    .filter(Boolean)
    .join('\n')
  const first = typed[0]
  if (!first) {
    return null
  }
  let command: string | null = null
  const quotes: string[] = []
  const attachments: UserAttachment[] = []
  first.content.forEach((block, index) => {
    if (block.type === 'text' && block.context === 'skill' && block.skill) {
      command = block.skill
    } else if (block.type === 'text' && block.context === 'quote') {
      quotes.push(blockText(block))
    } else if (block.type === 'text' && block.context === 'attachment' && block.attachment) {
      attachments.push({ key: `${prefix}-file-${index}`, name: block.attachment.name, image: false, index, preview: null })
    } else if (block.type === 'image' && block.attachment) {
      attachments.push({
        key: `${prefix}-image-${index}`,
        name: block.attachment.name,
        image: true,
        index,
        preview: block.preview ?? null,
      })
    }
  })
  if (!text && !command && !attachments.length) {
    return null
  }
  return {
    kind: 'user',
    key: `${prefix}-user`,
    messageId: first.id,
    text,
    selection:
      typed
        .map((message) => selectionOf(message.content))
        .filter(Boolean)
        .join('\n') || null,
    sections: typed.flatMap((message) => sectionsOf(message.content)),
    quote: quotes.filter(Boolean).join('\n') || null,
    command,
    attachments,
  }
}

interface Outcome {
  isError: boolean
  output: string
  subagent?: SubagentRecord
}

/** One flat entry of a turn before its steps are grouped. */
type Flat = ThreadItem | { kind: 'step'; item: StepItem }

/** The events of a stream past its last restart: a retried call starts over, so what the
 *  abandoned attempt streamed is noise. A subagent's retry restarts only that subagent. */
function sinceRestart(events: ChatEvent[], parent: string | undefined): ChatEvent[] {
  let restart = -1
  events.forEach((event, index) => {
    if (event.event === 'retry' && event.parent === parent) {
      restart = index
    }
  })
  return restart === -1 ? events : events.slice(restart + 1)
}

function parentOf(event: ChatEvent): string | undefined {
  return 'parent' in event ? event.parent : undefined
}

/** The turn's `memory` events, one line per call, in the order they arrived. */
function memoryLines(group: readonly ChatEvent[] | undefined): MemoryLine[] {
  const lines = new Map<string, MemoryLine>()
  for (const event of group ?? []) {
    if (event.event === 'memory') {
      lines.set(event.tool_use_id, {
        toolUseId: event.tool_use_id,
        action: event.action,
        memory: event.memory,
        previous: event.previous,
      })
    }
  }
  return [...lines.values()]
}

/** What the turn was given beyond the document, or null when it was given neither the
 *  reader's instructions nor a memory (or ran before either existed). */
function usedContext(group: readonly ChatEvent[] | undefined): UsedContext | null {
  let start: Extract<ChatEvent, { event: 'turn_start' }> | undefined
  for (const event of group ?? []) {
    if (event.event === 'turn_start') {
      start = event
    }
  }
  const instructions = start?.instructions === true
  const memoryIds = start?.memory_ids ?? []
  return instructions || memoryIds.length ? { instructions, memoryIds } : null
}

export function buildThread(input: ThreadInput): Thread {
  const items: ThreadItem[] = []
  const liveTurns: string[] = []
  const turnById = new Map(input.turns.map((turn) => [turn.id, turn]))

  for (const turnId of turnOrder(input)) {
    const turn = turnById.get(turnId)
    const group = input.groups.get(turnId)
    const own = input.messages.filter((message) => message.turn_id === turnId)
    // One assistant message per model round: a tool-using turn has several, and the
    // final answer is the last of them.
    const assistants = own.filter((message) => message.role === 'assistant')
    const prefix = `t${turnId}`
    const flat: Flat[] = []

    const typed = own.filter((message) => message.role === 'user' && !isToolResultOnly(message.content))
    const user = userItem(prefix, typed)
    if (user) {
      flat.push(user)
    }

    // Tool outcomes, from the record and from the stream — whichever has them.
    const results = new Map<string, Outcome>()
    for (const message of own) {
      for (const block of message.content) {
        if (block.type === 'tool_result') {
          results.set(block.tool_use_id, {
            isError: block.is_error === true,
            output: resultText(block.content),
            subagent: block.subagent,
          })
        }
      }
    }
    const edits = new Map<string, Extract<ChatEvent, { event: 'edit' }>>()
    for (const event of group ?? []) {
      if (event.event === 'tool_result') {
        const known = results.get(event.id)
        results.set(event.id, {
          isError: event.is_error,
          // The record's text is the whole result; the event's is what the stream carried.
          output: known?.output || event.output || '',
          subagent: event.subagent ?? known?.subagent,
        })
      } else if (event.event === 'edit') {
        edits.set(event.tool_use_id, event)
      }
    }

    const done = isTerminal(turn, group, assistants.length > 0)
    if (!done) {
      liveTurns.push(turnId)
    }

    const statusOf = (id: string): StepStatus => {
      const outcome = results.get(id)
      return outcome === undefined ? (done ? 'none' : 'pending') : outcome.isError ? 'error' : 'ok'
    }

    const cardFor = (id: string, name: string, input: Record<string, unknown>): EditCard | null => {
      const event = edits.get(id)
      const command = lineOf(input.command)
      // Any tool whose `edit` event is on record — a shell command that changed the tree
      // has a revision to show and revert too — or an editor write the record says
      // succeeded; a failed one has nothing to show.
      if (!event && !(name === EDITOR_TOOL && EDITOR_WRITES.has(command) && results.get(id)?.isError === false)) {
        return null
      }
      return {
        toolUseId: id,
        path: event?.path ?? lineOf(input.path),
        command: event?.command ?? command,
        lines: event ? parseDiff(event.diff) : diffFromInput(input),
        commit: event?.commit ?? null,
      }
    }

    const toolStep = (id: string, name: string, input: Record<string, unknown>, key: string): ToolStep => {
      const card = cardFor(id, name, input)
      return {
        type: 'tool',
        key,
        id,
        name,
        step: readStep(name, input),
        status: statusOf(id),
        output: results.get(id)?.output ?? null,
        command: name === 'bash' && !input.restart ? lineOf(input.command) : null,
        card,
        added: card ? addedLines(card.lines) : null,
      }
    }

    const subagentView = (id: string, input: Record<string, unknown>): SubagentView => {
      const outcome = results.get(id)
      const record = outcome?.subagent
      const steps: StepItem[] = []
      if (record) {
        record.steps.forEach((step, index) => {
          const key = `${prefix}-sub-${id}-${index}`
          if (step.type === 'note') {
            steps.push({ type: 'note', key, text: step.text })
          } else {
            // The record's status is the run's own; a step of a record from before ids has
            // no edit to join (docs/chat.md § Subagents).
            const status = step.is_error ? 'error' : 'ok'
            steps.push({ ...toolStep(step.id ?? key, step.name, step.input, key), status })
          }
        })
      } else if (group) {
        const own = sinceRestart(
          group.filter((event) => parentOf(event) === id),
          id,
        )
        own.forEach((event, index) => {
          if (event.event === 'tool_use') {
            steps.push(toolStep(event.id, event.name, event.input, `${prefix}-sub-${id}-${event.id}`))
          } else if (event.event === 'thinking' && event.text.trim()) {
            steps.push({ type: 'note', key: `${prefix}-sub-${id}-note-${index}`, text: event.text.trim() })
          }
        })
      }
      return {
        id,
        name: lineOf(input.name),
        task: lineOf(input.task),
        status: outcome ? (outcome.isError ? 'error' : 'ok') : done ? 'none' : 'running',
        steps,
        report: outcome ? outcome.output : null,
        elapsedMs: record?.elapsed_ms ?? null,
        model: record?.model ?? null,
      }
    }

    const callItem = (id: string, name: string, input: Record<string, unknown>, key: string): Flat[] =>
      name === MEMORY_TOOL
        ? []
        : name === SUBAGENT_TOOL
          ? [{ kind: 'subagent', key, card: subagentView(id, input) }]
          : [{ kind: 'step', item: toolStep(id, name, input, key) }]

    // The live events a persisted round does not cover: what streamed after the last
    // tool call the record holds (its results and the next round), or the whole group
    // when nothing of the turn is stored yet. A subagent's own events are its card's.
    const renderLive = (events: ChatEvent[]) => {
      const live = sinceRestart(
        events.filter((event) => parentOf(event) === undefined),
        undefined,
      )
      let text = ''
      let textKey = 0
      const flushText = () => {
        if (text.trim()) {
          flat.push({ kind: 'text', key: `${prefix}-live-${textKey}`, text: text.trim() })
        }
        text = ''
        textKey += 1
      }
      live.forEach((event, index) => {
        switch (event.event) {
          case 'text_delta':
            text += event.text
            break
          case 'thinking':
            flushText()
            if (event.text.trim()) {
              flat.push({ kind: 'step', item: { type: 'note', key: `${prefix}-live-think-${index}`, text: event.text.trim() } })
            }
            break
          case 'tool_use':
            flushText()
            flat.push(...callItem(event.id, event.name, event.input, `${prefix}-tool-${event.id}`))
            break
          default:
            break
        }
      })
      flushText()
    }

    if (assistants.length > 0) {
      const storedTools = new Set<string>()
      assistants.forEach((message, round) => {
        message.content.forEach((block, position) => {
          const at = `${round}-${position}`
          if (block.type === 'text' && blockText(block).trim()) {
            flat.push({ kind: 'text', key: `${prefix}-text-${at}`, text: blockText(block).trim() })
          } else if (block.type === 'thinking' && block.thinking.trim()) {
            flat.push({ kind: 'step', item: { type: 'note', key: `${prefix}-think-${at}`, text: block.thinking.trim() } })
          } else if (block.type === 'tool_use') {
            storedTools.add(block.id)
            flat.push(...callItem(block.id, block.name, block.input, `${prefix}-tool-${block.id}`))
          }
        })
      })
      if (group && !done) {
        // The stream past the last stored round: a tool-using turn goes on after the
        // panel opened or the record was reloaded, and its later rounds show live until
        // the next reload stores them (docs/web.md § The agent panel).
        const cut = group.map((event) => event.event === 'tool_use' && storedTools.has(event.id)).lastIndexOf(true)
        if (cut !== -1) {
          renderLive(group.slice(cut + 1))
        }
      }
    } else if (group) {
      renderLive(group)
    }

    // A command opens its answer's first step group with the skill it ran.
    if (user?.kind === 'user' && user.command) {
      const skillStep: Flat = { kind: 'step', item: { type: 'skill', key: `${prefix}-skill`, name: user.command } }
      const first = flat.findIndex((entry) => entry.kind === 'step')
      if (first === -1) {
        flat.push(skillStep)
      } else {
        flat.splice(first, 0, skillStep)
      }
    }

    // Under the last answer: the turn's memory lines, then — once it is finished — what it
    // used and the control that copies the answer. Live, the lines follow the newest text and
    // so land under the final one when the turn ends; a turn with no text keeps them at its
    // end.
    let lastText = -1
    flat.forEach((entry, index) => {
      if (entry.kind === 'text') {
        lastText = index
      }
    })
    const under: Flat[] = memoryLines(group).map((line) => ({
      kind: 'memory',
      key: `${prefix}-memory-${line.toolUseId}`,
      turnId,
      line,
    }))
    const answer = flat[lastText]
    if (done && answer?.kind === 'text') {
      under.push({ kind: 'foot', key: `${prefix}-foot`, turnId, text: answer.text, used: usedContext(group) })
    }
    flat.splice(lastText === -1 ? flat.length : lastText + 1, 0, ...under)

    const streamError = group?.find(
      (event): event is Extract<ChatEvent, { event: 'turn_error' }> => event.event === 'turn_error',
    )
    // A cancelled turn ends in a `turn_error` whose status says so: a cancellation, not an
    // error with the backend's word in it.
    if (streamError?.status === 'cancelled' || turn?.status === 'cancelled') {
      flat.push({ kind: 'cancelled', key: `${prefix}-cancelled` })
    } else if (streamError) {
      flat.push({ kind: 'error', key: `${prefix}-error`, text: streamError.error })
    } else if (turn?.error) {
      flat.push({ kind: 'error', key: `${prefix}-error`, text: turn.error })
    }

    const grouped = groupRuns<Flat, ThreadItem>(
      flat,
      (entry) => entry.kind === 'step',
      (run) => ({
        kind: 'steps',
        key: `${(run[0] as { item: StepItem }).item.key}-group`,
        turnId,
        steps: run.map((entry) => (entry as { item: StepItem }).item),
        live: false,
      }),
    ) as ThreadItem[]

    if (!done) {
      // A live turn's group is in flight while it is the trailing one, or while a call of
      // it still runs beside a later one (reads of one round run together): open, its
      // summary the step it is on.
      grouped.forEach((entry, index) => {
        if (
          entry.kind === 'steps' &&
          (index === grouped.length - 1 || entry.steps.some((step) => step.type === 'tool' && step.status === 'pending'))
        ) {
          grouped[index] = { ...entry, live: true }
        }
      })
      const last = grouped[grouped.length - 1]
      if (last?.kind !== 'steps' && !(last?.kind === 'subagent' && last.card.status === 'running')) {
        grouped.push({ kind: 'working', key: `${prefix}-working` })
      }
    }
    items.push(...grouped)
  }

  return { items, liveTurns }
}

/**
 * Where a memory line stands, read from the memories as they are now rather than from what
 * was clicked, so a reload or another tab shows what is true (docs/web.md § The agent
 * panel). The row a call saved is found by the call's id (`source_ref`), which Undo and
 * Save again carry too; a row a call changed, by its own id.
 *
 * - `saved`: the row is there — **Undo** deletes it. `unsaved`: it is not — **Save again**.
 * - `updated`: the row reads as the call left it — **Undo** writes back what it was.
 *   `reverted`: it reads as it was before — **Apply again**. `changed`: neither, edited or
 *   deleted since — only **View**.
 * - `forgotten`: nothing holds it — **Undo** saves it again. `restored`: Undo put it back —
 *   **Remove again** deletes that row.
 *
 * `loading` until the memories have been read. Only the fields the tool writes are compared
 * (kind, text, scope): a pin the reader added since is theirs, not the call's.
 */
export type MemoryLineState =
  | { state: 'loading' }
  | { state: 'saved'; row: Memory }
  | { state: 'unsaved' }
  | { state: 'updated'; row: Memory }
  | { state: 'reverted'; row: Memory }
  | { state: 'changed'; row: Memory | null }
  | { state: 'forgotten' }
  | { state: 'restored'; row: Memory }

/** Whether a row holds what a snapshot says, in the fields the `memory` tool writes. */
export function sameMemory(row: Pick<Memory, 'kind' | 'text' | 'document_id'>, snapshot: MemorySnapshot): boolean {
  return row.kind === snapshot.kind && row.text === snapshot.text && row.document_id === snapshot.document_id
}

export function memoryLineState(line: MemoryLine, memories: readonly Memory[] | null): MemoryLineState {
  if (memories === null) {
    return { state: 'loading' }
  }
  const bySource = memories.find((row) => row.source_ref === line.toolUseId)
  const byId = memories.find((row) => row.id === line.memory.id)
  switch (line.action) {
    case 'saved': {
      const row = bySource ?? byId
      return row ? { state: 'saved', row } : { state: 'unsaved' }
    }
    case 'updated':
      if (byId && sameMemory(byId, line.memory)) {
        return { state: 'updated', row: byId }
      }
      if (byId && line.previous && sameMemory(byId, line.previous)) {
        return { state: 'reverted', row: byId }
      }
      return { state: 'changed', row: byId ?? null }
    case 'forgotten':
      return bySource ? { state: 'restored', row: bySource } : { state: 'forgotten' }
  }
}
