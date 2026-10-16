<script lang="ts">
import {
  cancelTurn,
  createConversation,
  deleteConversation,
  getConversation,
  listConversations,
  revertRevision,
  sendMessage,
  type ChatEvent,
  type ChatMessage,
  type ChatTurn,
  type ContentBlock,
  type Conversation,
} from '~/lib/api'
import type { MenuItem, Point } from '~/lib/menu'
import type { StreamedEvent } from '~/composables/useConversationStream'

/** What the thread draws, in order (docs/web.md § The agent panel). */
type ThreadItem =
  | { kind: 'user'; key: string; text: string; selection: number }
  | { kind: 'text'; key: string; text: string }
  | { kind: 'thinking'; key: string; text: string }
  | {
      kind: 'tool'
      key: string
      name: string
      summary: string
      /** `none`: the turn ended without a result for it (a cancel) — no mark to draw. */
      status: 'pending' | 'ok' | 'error' | 'none'
    }
  | { kind: 'edit'; key: string; card: EditCard }
  | { kind: 'error'; key: string; text: string }
  | { kind: 'cancelled'; key: string }
  | { kind: 'working'; key: string }

interface DiffLine {
  kind: 'added' | 'removed' | 'context' | 'hunk'
  text: string
}

interface EditCard {
  /** The editor tool call that made it — the card's identity across reloads. */
  toolUseId: string
  path: string
  command: string
  lines: DiffLine[]
  /** The commit the `edit` event named; null for a card rebuilt from history alone. */
  commit: string | null
}

/** The chat agent's editor tool and the commands of it that write (docs/chat.md § Tools). */
const EDITOR_TOOL = 'str_replace_based_edit_tool'
const EDITOR_WRITES = new Set(['str_replace', 'insert', 'create'])
/** A turn the backend has not finished: queued, running, or cancelled and still winding
 *  down a tool call — `finished_at` says, not the status (docs/chat.md § Conversations). */
const isLive = (turn: ChatTurn): boolean => turn.finished_at === null
/** How long to wait before following a still-live turn again after the stream gave up. */
const RESUME_AFTER_MS = 10_000
/** A tool line is one line: longer command text is cut, the full one rides on `title`. */
const SUMMARY_MAX = 72

function lineOf(value: unknown): string {
  return typeof value === 'string' ? value : value === undefined ? '' : JSON.stringify(value)
}

function clip(text: string): string {
  const first = text.split('\n')[0] ?? ''
  return first.length > SUMMARY_MAX ? `${first.slice(0, SUMMARY_MAX - 1)}…` : first
}

/** `bash ls figures`, `str_replace doc.md`, `view_page 3` — the argument that names the call. */
function summarizeTool(name: string, input: Record<string, unknown>): string {
  switch (name) {
    case 'bash':
      return input.restart ? 'restart' : clip(lineOf(input.command))
    case EDITOR_TOOL: {
      const path = lineOf(input.path)
      return `${lineOf(input.command)} ${path.slice(path.lastIndexOf('/') + 1)}`.trim()
    }
    case 'view_page':
      return lineOf(input.page)
    case 'view_figure':
      return `p${lineOf(input.page)} ${lineOf(input.bbox)}`
    default:
      return clip(JSON.stringify(input))
  }
}

/**
 * A unified diff as lines. The file header and hunk markers are kept as their own kinds
 * so the card can mute them; everything else is an added, removed or context line.
 */
function parseDiff(diff: string): DiffLine[] {
  const lines: DiffLine[] = []
  for (const line of diff.replace(/\n$/, '').split('\n')) {
    if (line.startsWith('+++') || line.startsWith('---') || line.startsWith('diff ') || line.startsWith('index ')) {
      continue
    }
    if (line.startsWith('@@')) {
      lines.push({ kind: 'hunk', text: line })
    } else if (line.startsWith('+')) {
      lines.push({ kind: 'added', text: line.slice(1) })
    } else if (line.startsWith('-')) {
      lines.push({ kind: 'removed', text: line.slice(1) })
    } else {
      lines.push({ kind: 'context', text: line.startsWith(' ') ? line.slice(1) : line })
    }
  }
  return lines
}

/** The diff a stored editor call implies, for a card whose `edit` event is not at hand. */
function diffFromInput(input: Record<string, unknown>): DiffLine[] {
  const removed = lineOf(input.old_str)
  const added = lineOf(input.new_str ?? input.file_text)
  return [
    ...(removed ? removed.split('\n').map((text) => ({ kind: 'removed' as const, text })) : []),
    ...(added ? added.split('\n').map((text) => ({ kind: 'added' as const, text })) : []),
  ]
}

/** The user's own words: a text block the backend marked as context (the selected
 *  passage it appended) is scaffolding for the model, not something the reader typed. */
function textOf(blocks: ContentBlock[]): string {
  return blocks
    .filter((block): block is { type: 'text'; text: string } => block.type === 'text' && !block.context)
    .map((block) => block.text)
    .join('\n')
    .trim()
}

/** How many characters of the document went with the message as its selection, or 0. */
function selectionOf(blocks: ContentBlock[]): number {
  return blocks
    .filter((block): block is { type: 'text'; text: string; context: string } => block.type === 'text' && block.context === 'selection')
    .reduce((total, block) => total + block.text.length, 0)
}

function isToolResultOnly(blocks: ContentBlock[]): boolean {
  return blocks.length > 0 && blocks.every((block) => block.type === 'tool_result')
}
</script>

<script setup lang="ts">
/**
 * The `chat` agent's thread about one document (docs/web.md § The agent panel).
 *
 * The thread is `messages` — the persisted history — plus the live `chat_events` stream
 * for the turn in flight. The stream is opened from sequence 0 whenever a conversation is
 * opened, for two reasons: a turn that was already running when the viewer opened has no
 * other way back into view, and the `edit` events are the only place an edit's commit is
 * named — the history's editor calls carry the diff they asked for, not the revision they
 * made — so replaying them is what puts **Revert** back on a card after a reload. The
 * stream ends by itself once nothing is running, so the replay is one bounded read.
 *
 * Turns are matched to the stream by order: the k-th `turn_start` is the k-th turn of
 * the conversation. A turn with its assistant message stored renders from the message
 * (its edit cards taking their commits from the matching replayed group); one still
 * running renders from its events as they arrive.
 */
const props = defineProps<{
  documentId: number
  /** The document pane's current selection, read at the moment a message is composed. */
  documentSelection: () => string | null
}>()

/** The document's files changed on disk — an edit landed, or a revert — so reload them. */
const emit = defineEmits<{ changed: [] }>()

const { t, locale } = useI18n()
const { resolve } = useApiError()

// --- conversations --------------------------------------------------------------

const conversations = ref<Conversation[]>([])
const conversationsError = ref<string | null>(null)
const currentId = ref<number | null>(null)
const current = computed(
  () => conversations.value.find((row) => row.id === currentId.value) ?? null,
)
const title = computed(() => current.value?.title || t('chat.untitled'))

function byRecency(rows: Conversation[]): Conversation[] {
  return [...rows].sort((left, right) => right.updated_at.localeCompare(left.updated_at))
}

async function loadConversations() {
  conversationsError.value = null
  try {
    const rows = byRecency((await listConversations(props.documentId)).conversations)
    conversations.value = rows
    if (currentId.value === null && rows[0]) {
      await select(rows[0].id)
    }
  } catch (error) {
    conversationsError.value = await resolve(error)
  }
}

const menu = ref<Point | null>(null)
/** Opened from the keyboard, so the first row takes the highlight (docs/web.md). */
const menuFromKey = ref(false)
const titleButton = ref<HTMLButtonElement | null>(null)

const menuItems = computed<MenuItem[]>(() => [
  ...conversations.value.map((row) => ({
    key: String(row.id),
    label: row.title || t('chat.untitled'),
    checked: row.id === currentId.value,
  })),
  {
    key: 'new',
    label: t('chat.newConversation'),
    icon: 'plus' as const,
    separated: conversations.value.length > 0,
  },
  ...(current.value
    ? [
        {
          key: 'delete',
          label: t('common.delete'),
          icon: 'trash' as const,
          danger: true,
          separated: true,
        },
      ]
    : []),
])

function openMenu(event: MouseEvent) {
  const box = titleButton.value?.getBoundingClientRect()
  // A click the keyboard made carries no pointer detail.
  menuFromKey.value = event.detail === 0
  menu.value = box ? { x: box.left, y: box.bottom + 4 } : { x: 0, y: 0 }
}

async function onMenuSelect(key: string) {
  menu.value = null
  if (key === 'new') {
    await startConversation()
  } else if (key === 'delete') {
    confirmingDelete.value = true
  } else {
    await select(Number(key))
  }
}

const confirmingDelete = ref(false)
const deletePending = ref(false)
const deleteError = ref<string | null>(null)

async function removeConversation() {
  const id = currentId.value
  if (id === null) {
    return
  }
  deletePending.value = true
  deleteError.value = null
  try {
    await deleteConversation(id)
    confirmingDelete.value = false
    conversations.value = conversations.value.filter((row) => row.id !== id)
    stream.close()
    currentId.value = null
    messages.value = []
    turns.value = []
    discardFrames()
    const next = conversations.value[0]
    if (next) {
      await select(next.id)
    }
  } catch (error) {
    deleteError.value = await resolve(error)
  } finally {
    deletePending.value = false
  }
}

// --- the open conversation --------------------------------------------------------

const messages = ref<ChatMessage[]>([])
const turns = ref<ChatTurn[]>([])
const detailError = ref<string | null>(null)
const detailPending = ref(false)
/** Load generation: a conversation switched away from must not fill the thread late. */
let detailSeq = 0

async function loadDetail(id: number) {
  const seq = ++detailSeq
  detailError.value = null
  detailPending.value = true
  try {
    const detail = await getConversation(id)
    if (seq !== detailSeq) {
      return
    }
    messages.value = [...detail.messages].sort((left, right) => left.id - right.id)
    turns.value = [...detail.turns]
    if (turns.value.some(isLive)) {
      // A turn still unfinished is followed from here: a stream dropped before
      // its turn_start would otherwise never be reopened (docs/web.md).
      stream.expectTurn()
    }
    conversations.value = conversations.value.map((row) =>
      row.id === id ? { ...row, title: detail.conversation.title, updated_at: detail.conversation.updated_at } : row,
    )
  } catch (error) {
    if (seq === detailSeq) {
      detailError.value = await resolve(error)
    }
  } finally {
    if (seq === detailSeq) {
      detailPending.value = false
    }
  }
}

async function select(id: number) {
  if (id === currentId.value) {
    return
  }
  currentId.value = id
  messages.value = []
  turns.value = []
  discardFrames()
  cards.clear()
  stream.open(id, 0)
  await loadDetail(id)
}

async function startConversation() {
  composerError.value = null
  try {
    const created = await createConversation(props.documentId)
    conversations.value = [created, ...conversations.value]
    await select(created.id)
    return created.id
  } catch (error) {
    composerError.value = await resolve(error)
    return null
  }
}

onMounted(loadConversations)

// --- the stream --------------------------------------------------------------------

/** Every frame of the open conversation, replayed and live. `shallowRef`: frames are
 *  appended in batches and never edited, and a deep proxy over thousands of deltas would
 *  cost more than it reads. */
const events = shallowRef<StreamedEvent[]>([])
let pendingFrames: StreamedEvent[] = []
let flushTimer: ReturnType<typeof setTimeout> | null = null
/** An `edit` of a turn the record still lists as unfinished arrived since the last flush:
 *  the viewer must reload. Decided as the frame arrives — an `edit` and the `turn_done`
 *  behind it can share a batch, by which time the stream no longer counts as active. */
let liveEdit = false

/** Forget every frame, shown or still queued: the thread they belong to is gone. */
function discardFrames() {
  if (flushTimer !== null) {
    clearTimeout(flushTimer)
    flushTimer = null
  }
  pendingFrames = []
  liveEdit = false
  events.value = []
}

/** Frames land in bursts — a replay is thousands of them — so they are folded into the
 *  thread once per tick rather than once per frame. */
function flushFrames() {
  flushTimer = null
  if (!pendingFrames.length) {
    return
  }
  const batch = pendingFrames
  pendingFrames = []
  events.value = events.value.concat(batch)
  // A replayed edit of a finished turn changed nothing since the viewer loaded; one of a
  // turn still in flight did. One reload covers however many landed in this batch.
  if (liveEdit) {
    liveEdit = false
    emit('changed')
  }
  const ended = batch.some(
    (frame) => frame.event.event === 'turn_done' || frame.event.event === 'turn_error',
  )
  if (ended && currentId.value !== null) {
    // The runner has now stored the assistant message and the turn's outcome; the thread
    // switches from the events to the record.
    void loadDetail(currentId.value)
  }
}

const stream = useConversationStream(
  (frame) => {
    if (frame.event.event === 'edit') {
      // Every edit frame reloads the document, replayed ones included: the content was
      // fetched on its own, and nothing says the edit came before it (docs/web.md).
      liveEdit = true
    }
    pendingFrames.push(frame)
    if (flushTimer === null) {
      flushTimer = setTimeout(flushFrames, 16)
    }
  },
  () => {
    // Six silent drops: the record decides. A turn it still lists as live is waited on
    // again — a fresh retry budget after a pause, until the record says it ended.
    const id = currentId.value
    if (id === null) {
      return
    }
    void loadDetail(id).then(() => {
      if (currentId.value === id && turns.value.some(isLive)) {
        setTimeout(() => {
          if (currentId.value === id) {
            stream.resume()
          }
        }, RESUME_AFTER_MS)
      }
    })
  },
)

onBeforeUnmount(() => {
  if (flushTimer !== null) {
    clearTimeout(flushTimer)
  }
})

/** The frames of each turn, by the id every frame carries — never by position, since a
 *  turn whose load failed emits only its `turn_error`. */
const groups = computed(() => {
  const out = new Map<string, ChatEvent[]>()
  for (const frame of events.value) {
    const list = out.get(frame.event.turn_id)
    if (list) {
      list.push(frame.event)
    } else {
      out.set(frame.event.turn_id, [frame.event])
    }
  }
  return out
})

// --- the thread --------------------------------------------------------------------

/** Each card's state after the reader acted on it: reverted, and the commit that undoes
 *  the undo. Keyed by the tool call, which survives a reload where the item list does not. */
const cards = reactive(
  new Map<string, { reverted: boolean; undoCommit: string | null; redoCommit: string | null; pending: boolean }>(),
)

/** The turns in order — those the record lists (oldest first, as the API sends them), then
 *  any a message or a frame names that it does not, in the order they were seen. */
const turnOrder = computed(() => {
  const ids: string[] = turns.value.map((turn) => turn.id)
  for (const message of messages.value) {
    if (message.turn_id !== null && !ids.includes(message.turn_id)) {
      ids.push(message.turn_id)
    }
  }
  for (const turnId of groups.value.keys()) {
    if (!ids.includes(turnId)) {
      ids.push(turnId)
    }
  }
  return ids
})

function isTerminal(turn: ChatTurn | undefined, group: ChatEvent[] | undefined, stored: boolean): boolean {
  if (group?.some((event) => event.event === 'turn_done' || event.event === 'turn_error')) {
    return true
  }
  if (turn) {
    return !isLive(turn)
  }
  return stored
}

const thread = computed<ThreadItem[]>(() => {
  const items: ThreadItem[] = []
  const turnById = new Map(turns.value.map((turn) => [turn.id, turn]))

  turnOrder.value.forEach((turnId) => {
    const turn = turnById.get(turnId)
    const group = groups.value.get(turnId)
    const own = messages.value.filter((message) => message.turn_id === turnId)
    // One assistant message per model round: a tool-using turn has several, and the
    // final answer is the last of them.
    const assistants = own.filter((message) => message.role === 'assistant')
    const prefix = `t${turnId}`

    const typed = own.filter((message) => message.role === 'user' && !isToolResultOnly(message.content))
    const userText = typed
      .map((message) => textOf(message.content))
      .filter(Boolean)
      .join('\n')
    if (userText) {
      items.push({
        kind: 'user',
        key: `${prefix}-user`,
        text: userText,
        selection: typed.reduce((total, message) => total + selectionOf(message.content), 0),
      })
    }

    // Tool outcomes, from the record and from the stream — whichever has them.
    const results = new Map<string, boolean>()
    for (const message of own) {
      for (const block of message.content) {
        if (block.type === 'tool_result') {
          results.set(block.tool_use_id, block.is_error === true)
        }
      }
    }
    const edits = new Map<string, Extract<ChatEvent, { event: 'edit' }>>()
    for (const event of group ?? []) {
      if (event.event === 'tool_result') {
        results.set(event.id, event.is_error)
      } else if (event.event === 'edit') {
        edits.set(event.tool_use_id, event)
      }
    }

    const done = isTerminal(turn, group, assistants.length > 0)

    const toolItem = (id: string, name: string, input: Record<string, unknown>): ThreadItem => {
      const outcome = results.get(id)
      return {
        kind: 'tool',
        key: `${prefix}-tool-${id}`,
        name,
        summary: summarizeTool(name, input),
        status: outcome === undefined ? (done ? 'none' : 'pending') : outcome ? 'error' : 'ok',
      }
    }

    const editItem = (id: string, input: Record<string, unknown>): ThreadItem | null => {
      const command = lineOf(input.command)
      const event = edits.get(id)
      if (!event && !EDITOR_WRITES.has(command)) {
        return null
      }
      return {
        kind: 'edit',
        key: `${prefix}-edit-${id}`,
        card: {
          toolUseId: id,
          path: event?.path ?? lineOf(input.path),
          command: event?.command ?? command,
          lines: event ? parseDiff(event.diff) : diffFromInput(input),
          commit: event?.commit ?? null,
        },
      }
    }

    // The live events a persisted round does not cover: what streamed after the last
    // tool call the record holds (its results and the next round), or the whole group
    // when nothing of the turn is stored yet. A retried model call starts over, so what
    // the abandoned attempt streamed is noise.
    const renderLive = (events: ChatEvent[], coveredEdits: Set<string>) => {
      const restart = events.map((event) => event.event).lastIndexOf('retry')
      const live = restart === -1 ? events : events.slice(restart + 1)
      let text = ''
      let textKey = 0
      const flushText = () => {
        if (text.trim()) {
          items.push({ kind: 'text', key: `${prefix}-live-${textKey}`, text: text.trim() })
        }
        text = ''
        textKey += 1
      }
      for (const event of live) {
        switch (event.event) {
          case 'text_delta':
            text += event.text
            break
          case 'thinking':
            flushText()
            if (event.text.trim()) {
              items.push({ kind: 'thinking', key: `${prefix}-live-think-${items.length}`, text: event.text.trim() })
            }
            break
          case 'tool_use':
            flushText()
            items.push(toolItem(event.id, event.name, event.input))
            break
          case 'edit': {
            if (coveredEdits.has(event.tool_use_id)) {
              break // drawn with the stored round's tool call
            }
            flushText()
            const card = editItem(event.tool_use_id, {})
            if (card) {
              items.push(card)
            }
            break
          }
          default:
            break
        }
      }
      flushText()
    }

    if (assistants.length > 0) {
      const storedTools = new Set<string>()
      assistants.forEach((message, round) => {
        message.content.forEach((block, position) => {
          const at = `${round}-${position}`
          if (block.type === 'text' && block.text.trim()) {
            items.push({ kind: 'text', key: `${prefix}-text-${at}`, text: block.text.trim() })
          } else if (block.type === 'thinking' && block.thinking.trim()) {
            items.push({ kind: 'thinking', key: `${prefix}-think-${at}`, text: block.thinking.trim() })
          } else if (block.type === 'tool_use') {
            storedTools.add(block.id)
            items.push(toolItem(block.id, block.name, block.input))
            // Any tool whose `edit` event is on record — a shell command that changed the
            // tree has a revision to show and revert too — or an editor write the record
            // says succeeded; a failed one has nothing to show.
            if (edits.has(block.id) || (block.name === EDITOR_TOOL && results.get(block.id) === false)) {
              const card = editItem(block.id, block.input)
              if (card) {
                items.push(card)
              }
            }
          }
        })
      })
      if (group && !done) {
        // The stream past the last stored round: a tool-using turn goes on after the
        // panel opened or the record was reloaded, and its later rounds show live
        // until the next reload stores them (docs/web.md § The agent panel).
        const cut = group.map((event) => event.event === 'tool_use' && storedTools.has(event.id)).lastIndexOf(true)
        if (cut !== -1) {
          renderLive(group.slice(cut + 1), storedTools)
        }
      }
    } else if (group) {
      renderLive(group, new Set())
    }

    const streamError = group?.find(
      (event): event is Extract<ChatEvent, { event: 'turn_error' }> => event.event === 'turn_error',
    )
    // A cancelled turn ends in a `turn_error` whose status says so: a cancellation, not an
    // error with the backend's word in it.
    if (streamError?.status === 'cancelled' || turn?.status === 'cancelled') {
      items.push({ kind: 'cancelled', key: `${prefix}-cancelled` })
    } else if (streamError) {
      items.push({ kind: 'error', key: `${prefix}-error`, text: streamError.error })
    } else if (turn?.error) {
      items.push({ kind: 'error', key: `${prefix}-error`, text: turn.error })
    }

    if (!done) {
      items.push({ kind: 'working', key: `${prefix}-working` })
    }
  })

  return items
})

/** A turn is in flight: the composer waits, and Stop is the one control. */
const running = computed(
  () => thread.value.some((item) => item.kind === 'working') || stream.active.value,
)

const scroller = ref<HTMLElement | null>(null)

/** Follows the thread as it grows — a reader watching a turn is reading its tail. */
watch(
  () => [thread.value.length, events.value.length],
  async () => {
    await nextTick()
    const box = scroller.value
    if (box) {
      box.scrollTop = box.scrollHeight
    }
  },
)

// --- edit cards: revert and apply again ---------------------------------------------

function cardState(card: EditCard) {
  return cards.get(card.toolUseId) ?? { reverted: false, undoCommit: null, redoCommit: null, pending: false }
}

const cardError = ref<string | null>(null)

/**
 * Revert writes the file as it was before the edit's commit, as a new commit; that new
 * commit is what *Apply again* reverts in turn, and so on — undo is itself a revision
 * (docs/workspace.md § Revisions). The revision list, newest first, names each one.
 */
async function toggleEdit(card: EditCard) {
  const state = cardState(card)
  const target = state.reverted ? state.undoCommit : (state.redoCommit ?? card.commit)
  if (!target || state.pending) {
    return
  }
  cards.set(card.toolUseId, { ...state, pending: true })
  cardError.value = null
  try {
    // The commit the revert made, from the response: the newest revision could by now
    // be someone else's.
    const { commit: made } = await revertRevision(props.documentId, target)
    cards.set(card.toolUseId, {
      reverted: !state.reverted,
      undoCommit: state.reverted ? state.undoCommit : made,
      redoCommit: state.reverted ? made : state.redoCommit,
      pending: false,
    })
    emit('changed')
  } catch (error) {
    cardError.value = await resolve(error)
    cards.set(card.toolUseId, { ...state, pending: false })
  }
}

// --- the composer --------------------------------------------------------------------

const draft = ref('')
const composer = ref<HTMLTextAreaElement | null>(null)
const composerError = ref<string | null>(null)
const sending = ref(false)
/** The document selection picked up when the reader came to the composer. */
const selection = ref<string | null>(null)

/** Before the composer takes focus — on the pointer's way in, and on keyboard focus — the
 *  document's selection is still the reader's, so this is when it is read. Every reading
 *  replaces the last, none included: a passage selected and then deselected is not sent.
 *  The focus that follows a pointer capture is skipped, since by then the click has
 *  collapsed what the pointer already read. */
let capturedByPointer = false

function captureSelection() {
  selection.value = props.documentSelection() || null
}

function captureOnPointer() {
  captureSelection()
  capturedByPointer = true
  // Cleared once the focus this press causes (if any) has been handled.
  setTimeout(() => {
    capturedByPointer = false
  }, 0)
}

function captureOnFocus() {
  if (capturedByPointer) {
    return
  }
  captureSelection()
}

async function send() {
  const text = draft.value.trim()
  if (!text || sending.value || running.value) {
    return
  }
  sending.value = true
  composerError.value = null
  try {
    let id = currentId.value
    if (id === null) {
      id = await startConversation()
      if (id === null) {
        return
      }
    }
    const picked = selection.value
    const { turn_id } = await sendMessage(id, {
      text,
      ...(picked ? { selection: picked } : {}),
      locale: locale.value,
    })
    if (currentId.value !== id) {
      // The reader switched threads while the send was in flight: the turn belongs to
      // the thread it was sent to, which shows it when it is opened again.
      return
    }
    messages.value = [
      ...messages.value,
      {
        // Shown until the record is reloaded; negative so it never meets a stored id.
        id: -(messages.value.length + 1),
        role: 'user',
        content: [{ type: 'text', text }],
        turn_id,
        created_at: new Date().toISOString(),
      },
    ]
    turns.value = [...turns.value, { id: turn_id, status: 'queued', error: null, finished_at: null }]
    draft.value = ''
    selection.value = null
    stream.expectTurn()
  } catch (error) {
    composerError.value = await resolve(error)
  } finally {
    sending.value = false
  }
}

function onComposerKey(event: KeyboardEvent) {
  if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
    event.preventDefault()
    void send()
  }
}

const stopping = ref(false)

async function stop() {
  const id = currentId.value
  if (id === null || stopping.value) {
    return
  }
  stopping.value = true
  try {
    await cancelTurn(id)
  } catch (error) {
    composerError.value = await resolve(error)
  } finally {
    stopping.value = false
  }
}
</script>

<template>
  <div class="chat">
    <!-- The conversation's name opens the document's list of them. -->
    <div class="head">
      <button
        ref="titleButton"
        type="button"
        class="title"
        aria-haspopup="menu"
        :aria-expanded="menu ? 'true' : 'false'"
        :aria-label="t('chat.pick')"
        @click="menu ? (menu = null) : openMenu($event)"
      >
        <span class="title-text">{{ current ? title : t('chat.newConversation') }}</span>
        <UiIcon name="chevron-down" />
      </button>
    </div>

    <UiContextMenu
      v-if="menu"
      :at="menu"
      :items="menuItems"
      :label="t('chat.conversations')"
      :focus-first="menuFromKey"
      @select="onMenuSelect"
      @close="menu = null"
    />

    <div ref="scroller" class="thread">
      <UiBanner v-if="conversationsError" tone="error">{{ conversationsError }}</UiBanner>
      <UiBanner v-else-if="detailError" tone="error">{{ detailError }}</UiBanner>
      <UiBanner v-if="cardError" tone="error">{{ cardError }}</UiBanner>

      <UiSkeleton v-if="detailPending && !thread.length" :rows="3" />
      <p v-else-if="!thread.length" class="empty">{{ t('chat.empty') }}</p>

      <template v-for="item in thread" :key="item.key">
        <!-- The reader's turn: right-aligned on the sunken surface. -->
        <div v-if="item.kind === 'user'" class="user-row">
          <p class="user">{{ item.text }}</p>
          <p v-if="item.selection" class="user-selection">{{ t('chat.selection', { count: item.selection }) }}</p>
        </div>

        <p v-else-if="item.kind === 'text'" class="assistant">{{ item.text }}</p>

        <!-- The model's note between tool calls: muted, one line. -->
        <p v-else-if="item.kind === 'thinking'" class="thinking">{{ item.text }}</p>

        <!-- One tool call, one mono line, marked when its result arrives. -->
        <p v-else-if="item.kind === 'tool'" class="tool mono" :title="item.summary">
          <span class="tool-mark" :class="item.status">
            <UiIcon v-if="item.status === 'ok'" name="check" />
            <UiIcon v-else-if="item.status === 'error'" name="x" />
            <UiSpinner v-else-if="item.status === 'pending'" :size="11" />
          </span>
          <span class="tool-name">{{ item.name }}</span>
          <span class="tool-args">{{ item.summary }}</span>
          <span class="sr-only">{{ item.status === 'ok' ? t('chat.toolOk') : item.status === 'error' ? t('chat.toolFailed') : '' }}</span>
        </p>

        <!-- An edit as a diff card: applied as made, undone from its footer. -->
        <div v-else-if="item.kind === 'edit'" class="card">
          <div class="diff mono">
            <div
              v-for="(line, lineIndex) in item.card.lines"
              :key="lineIndex"
              class="diff-line"
              :class="line.kind"
            >
              <span class="diff-sign" aria-hidden="true">{{
                line.kind === 'added' ? '+' : line.kind === 'removed' ? '−' : ' '
              }}</span>
              <span class="diff-text">{{ line.text }}</span>
            </div>
          </div>
          <div class="card-foot">
            <span class="card-state">
              {{ cardState(item.card).reverted ? t('chat.reverted') : t('chat.applied') }}
              <span class="mono card-path">· {{ item.card.path.slice(item.card.path.lastIndexOf('/') + 1) }}</span>
            </span>
            <button
              v-if="item.card.commit"
              type="button"
              class="text-link"
              :disabled="cardState(item.card).pending || running"
              @click="toggleEdit(item.card)"
            >
              {{ cardState(item.card).reverted ? t('chat.applyAgain') : t('chat.revert') }}
            </button>
          </div>
        </div>

        <p v-else-if="item.kind === 'error'" class="turn-error" role="alert">
          {{ t('chat.turnError', { error: item.text }) }}
        </p>
        <p v-else-if="item.kind === 'cancelled'" class="thinking">{{ t('chat.turnCancelled') }}</p>
        <p v-else-if="item.kind === 'working'" class="working">
          <UiSpinner :size="12" />
          <span>{{ t('chat.working') }}</span>
        </p>
      </template>
    </div>

    <!-- One rounded field: the text, the attach control and the send control. -->
    <form class="composer" @submit.prevent="send" @mousedown.capture="captureOnPointer">
      <div class="field" :class="{ disabled: running }">
        <p v-if="selection" class="selection">
          <span class="selection-text">{{ t('chat.selection', { count: selection.length }) }}</span>
          <button type="button" class="selection-drop" :aria-label="t('chat.dropSelection')" @click="selection = null">
            <UiIcon name="x" />
          </button>
        </p>
        <label for="chat-composer" class="sr-only">{{ t('chat.composer') }}</label>
        <textarea
          id="chat-composer"
          ref="composer"
          v-model="draft"
          class="input"
          rows="2"
          :placeholder="t('chat.placeholder')"
          :disabled="running"
          @focus="captureOnFocus"
          @keydown="onComposerKey"
        />
        <div class="field-foot">
          <!-- Attachments are not wired yet; the control is where they will go. -->
          <button type="button" class="round ghost" :aria-label="t('chat.attach')" :title="t('chat.attach')" disabled>
            <UiIcon name="paperclip" />
          </button>
          <button
            v-if="running"
            type="button"
            class="round stop"
            :aria-label="t('chat.stop')"
            :title="t('chat.stop')"
            :disabled="stopping"
            @click="stop"
          >
            <UiIcon name="square" />
          </button>
          <button
            v-else
            type="submit"
            class="round send"
            :aria-label="t('chat.send')"
            :title="t('chat.send')"
            :disabled="sending || !draft.trim()"
          >
            <UiSpinner v-if="sending" :size="13" />
            <UiIcon v-else name="arrow-up" />
          </button>
        </div>
      </div>
      <p v-if="composerError" class="composer-error" role="alert">{{ composerError }}</p>
    </form>

    <UiConfirmDialog
      v-if="confirmingDelete"
      :title="t('chat.deleteConversation')"
      :message="t('chat.deleteConfirm', { title })"
      :confirm-label="t('common.delete')"
      :pending="deletePending"
      :error="deleteError"
      @confirm="removeConversation"
      @cancel="confirmingDelete = false"
    />
  </div>
</template>

<style scoped>
.chat {
  display: flex;
  flex-direction: column;
  min-height: 0;
  height: 100%;
}

.head {
  flex-shrink: 0;
  padding: var(--space-3) var(--space-3) 0;
}

.title {
  display: inline-flex;
  align-items: center;
  gap: var(--space-2);
  max-width: 100%;
  height: var(--control-height-sm);
  padding: 0 var(--space-3);
  border: 0;
  border-radius: var(--radius);
  background: transparent;
  color: var(--ink);
  font-size: var(--text-sm);
  font-weight: var(--weight-medium);
  cursor: pointer;
  transition: background var(--duration-fast) var(--ease);
}

.title:hover {
  background: var(--rail);
}

.title-text {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.title :deep(svg) {
  flex-shrink: 0;
  width: 14px;
  height: 14px;
  color: var(--muted);
}

/* The thread scrolls on its own, scrollbar hidden (docs/web.md § The agent panel). */
.thread {
  display: flex;
  flex: 1 1 auto;
  flex-direction: column;
  gap: var(--space-4);
  min-height: 0;
  overflow-y: auto;
  overscroll-behavior: contain;
  padding: var(--space-3) var(--space-5) var(--space-2);
  scrollbar-width: none;
}

.thread::-webkit-scrollbar {
  display: none;
}

.empty {
  margin: auto 0;
  color: var(--faint);
  font-size: var(--text-xs);
  text-align: center;
}

.user-row {
  display: flex;
  justify-content: flex-end;
}

.user {
  max-width: 88%;
  padding: var(--space-2) var(--space-3);
  border-radius: var(--radius);
  background: var(--rail);
  color: var(--ink);
  font-size: var(--text-sm);
  line-height: 1.55;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.assistant {
  color: var(--ink-soft);
  font-size: var(--text-sm);
  line-height: 1.55;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.thinking {
  color: var(--faint);
  font-size: var(--text-xs);
  line-height: 1.5;
}

.tool {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  min-width: 0;
  color: var(--faint);
  font-size: var(--text-2xs);
}

.tool-mark {
  display: inline-flex;
  flex-shrink: 0;
  width: 12px;
  height: 12px;
}

.tool-mark :deep(svg) {
  width: 12px;
  height: 12px;
}

/* A finished call takes the accent, a failed one the danger hue — the mark is the data. */
.tool-mark.ok {
  color: var(--accent);
}

.tool-mark.error {
  color: var(--danger);
}

.tool-name {
  flex-shrink: 0;
  color: var(--muted);
}

.tool-args {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* --- the edit card ------------------------------------------------------------ */

.card {
  border: 1px solid var(--edge);
  border-radius: var(--radius);
  overflow: hidden;
}

.diff {
  display: flex;
  flex-direction: column;
  max-height: 240px;
  overflow: auto;
  padding: var(--space-2) 0;
  font-size: var(--text-2xs);
  line-height: 1.6;
}

.diff-line {
  display: flex;
  gap: var(--space-2);
  padding: 0 var(--space-3);
  white-space: pre;
}

.diff-line.removed {
  background: var(--diff-removed);
  color: var(--danger);
}

.diff-line.added {
  background: var(--diff-added);
  color: var(--accent);
}

.diff-line.hunk {
  color: var(--faint);
}

.diff-line.context {
  color: var(--muted);
}

.diff-sign {
  flex-shrink: 0;
  width: 1ch;
}

.card-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3);
  padding: var(--space-1) var(--space-2) var(--space-1) var(--space-3);
  border-top: 1px solid var(--line);
  font-size: var(--text-xs);
  color: var(--faint);
}

.card-state {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.card-path {
  font-size: var(--text-2xs);
}

.text-link {
  flex-shrink: 0;
  height: var(--control-height-xs);
  padding: 0 var(--space-2);
  border: 0;
  border-radius: var(--radius);
  background: transparent;
  color: var(--accent);
  font-size: var(--text-xs);
  cursor: pointer;
  transition: background var(--duration-fast) var(--ease);
}

.text-link:hover {
  background: var(--rail);
}

.text-link:disabled {
  cursor: not-allowed;
  opacity: 0.5;
}

.turn-error {
  color: var(--danger);
  font-size: var(--text-xs);
  line-height: 1.5;
  overflow-wrap: anywhere;
}

.working {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  color: var(--faint);
  font-size: var(--text-xs);
}

/* --- the composer ---------------------------------------------------------------- */

.composer {
  flex-shrink: 0;
  margin: 0;
  padding: var(--space-2) var(--space-4) var(--space-4);
}

/* The one rounded control in the app besides the context menu: a field a message is
   typed into (docs/web.md § The agent panel). */
.field {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  padding: var(--space-3) var(--space-2) var(--space-2) var(--space-4);
  border: 1px solid var(--edge);
  border-radius: var(--radius-composer);
  background: var(--paper);
  transition: border-color var(--duration-fast) var(--ease);
}

.field:focus-within {
  border-color: var(--accent);
}

.field.disabled {
  background: var(--paper-sunken);
}

.user-selection {
  margin: 2px 0 0;
  font-size: 12px;
  color: var(--muted);
  text-align: right;
}

.selection {
  display: inline-flex;
  align-items: center;
  gap: var(--space-1);
  align-self: flex-start;
  height: var(--control-height-xs);
  padding: 0 var(--space-1) 0 var(--space-2);
  border-radius: var(--radius-full);
  background: var(--rail);
  color: var(--muted);
  font-size: var(--text-2xs);
}

.selection-drop {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 18px;
  height: 18px;
  padding: 0;
  border: 0;
  border-radius: var(--radius-full);
  background: transparent;
  color: var(--muted);
  cursor: pointer;
}

.selection-drop :deep(svg) {
  width: 11px;
  height: 11px;
}

.input {
  display: block;
  width: 100%;
  min-width: 0;
  margin: 0;
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--ink);
  font-size: var(--text-sm);
  line-height: 1.5;
  resize: none;
}

/* The field carries the focus ring; the textarea inside it must not draw a second one. */
.input:focus-visible {
  outline: none;
}

.input::placeholder {
  color: var(--faint);
}

.input:disabled {
  color: var(--muted);
}

.field-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.round {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: var(--control-height-sm);
  height: var(--control-height-sm);
  padding: 0;
  border: 0;
  border-radius: var(--radius-full);
  cursor: pointer;
  transition:
    background var(--duration-fast) var(--ease),
    color var(--duration-fast) var(--ease);
}

.round :deep(svg) {
  width: 15px;
  height: 15px;
}

.round:disabled {
  cursor: not-allowed;
  opacity: 0.5;
}

.round.ghost {
  background: transparent;
  color: var(--muted);
}

.round.ghost:hover:not(:disabled) {
  background: var(--rail);
  color: var(--ink);
}

/* The accent's one non-data use: the control that commits the message. */
.round.send {
  background: var(--accent);
  color: var(--accent-fg);
}

.round.send:hover:not(:disabled) {
  background: var(--accent-hover);
}

.round.stop {
  background: var(--rail-active);
  color: var(--ink);
}

.round.stop:hover:not(:disabled) {
  background: var(--edge);
}

.composer-error {
  margin-top: var(--space-2);
  color: var(--danger);
  font-size: var(--text-xs);
}

@media (pointer: coarse) {
  .input {
    font-size: 16px;
  }
}
</style>
