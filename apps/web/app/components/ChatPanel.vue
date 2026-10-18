<script setup lang="ts">
/**
 * The `chat` agent's thread about one document (docs/web.md § The agent panel).
 *
 * The thread is `messages` — the persisted history — plus the live `chat_events` stream
 * for the turn in flight; what that reads as (which steps share a group, where a
 * subagent's steps come from, when a turn is done) is `lib/thread.ts`, and this component
 * is the reactive glue around it: the conversation list, the stream, what the reader has
 * opened, the edit cards' reverts, the reply toolbar and the drop zone.
 *
 * The stream is opened from sequence 0 whenever a conversation is opened, for two reasons:
 * a turn that was already running when the viewer opened has no other way back into view,
 * and the `edit` events are the only place an edit's commit is named — the history's
 * editor calls carry the diff they asked for, not the revision they made — so replaying
 * them is what puts **Revert** back on a card after a reload. The stream ends by itself
 * once nothing is running, so the replay is one bounded read.
 */
import {
  attachmentUrl,
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
import { commandArguments, formatBytes, type ComposerPayload } from '~/lib/composer'
import { nodesToMarkdown } from '~/lib/copy'
import { parseResultMarkdown, type ResultBlock } from '~/lib/markdown'
import type { MenuItem, Point } from '~/lib/menu'
import {
  buildThread,
  isLive,
  type CardState,
  type EditCard,
  type ThreadItem,
  type UserAttachment,
} from '~/lib/thread'
import type { StreamedEvent } from '~/composables/useConversationStream'

/** How long to wait before following a still-live turn again after the stream gave up. */
const RESUME_AFTER_MS = 10_000

/** How close to the thread's end counts as at it: the thread follows a live turn only
 *  from there (docs/web.md § The agent panel). */
const FOLLOW_SLACK_PX = 24

const props = defineProps<{
  documentId: number
  /** The document pane's current selection, read at the moment a message is composed. */
  documentSelection: () => string | null
}>()

/** The document's files changed on disk — an edit landed, or a revert — so reload them. */
const emit = defineEmits<{ changed: [] }>()

const { t, locale } = useI18n()
const { resolve } = useApiError()
const auth = useAuth()

/** The server's own numbers: the composer checks attachments against them. */
const limits = computed(() => auth.me.value?.limits ?? null)

/** What the composer exposes to the panel: the drop zone and the reply toolbar feed it. */
interface ComposerControls {
  addFiles: (files: readonly File[]) => void
  setQuote: (text: string) => Promise<void>
  focus: () => void
}

const composer = ref<ComposerControls | null>(null)
const composerError = ref<string | null>(null)

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
    const live = turns.value.find(isLive)
    if (live) {
      // A turn still unfinished is followed from here, by its id: a stream dropped
      // before its turn_start would otherwise never be reopened, and the replayed
      // terminal events of earlier turns must not end the wait (docs/web.md).
      stream.expectTurn(live.id)
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
  following = true
  // The cards' state stays: keyed by tool-call id, it belongs to the card, not to the
  // conversation now open, and a thread switched away from and back must not offer to
  // revert a commit the reader already reverted (docs/web.md § Agent panel).
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

const thread = computed(() => buildThread({ turns: turns.value, messages: messages.value, groups: groups.value }))
const items = computed(() => thread.value.items)

/** A turn is in flight: the composer waits, and Stop is the one control. */
const running = computed(() => thread.value.liveTurns.length > 0 || stream.active.value)

const scroller = ref<HTMLElement | null>(null)

/** Whether the thread follows what arrives: only while the reader is at its end. One who
 *  scrolled up to read, or opened something that runs past the bottom, is left where they
 *  are until they scroll back down. */
let following = true
let lastScrollTop = 0

function onThreadScroll() {
  const box = scroller.value
  if (box) {
    const fromEnd = box.scrollHeight - box.scrollTop - box.clientHeight
    // Up is reading back — unless the thread got shorter under a reader at its end, which
    // moves the scroll up without the reader leaving the end.
    const up = box.scrollTop < lastScrollTop
    if (up && fromEnd > 1) {
      following = false
    } else if (!up && fromEnd <= FOLLOW_SLACK_PX) {
      following = true
    }
    lastScrollTop = box.scrollTop
  }
  scheduleToolbar()
}

/** A wheel turned up stops the following at once: a frame folded in before its scroll
 *  lands would otherwise pull the thread back to its end. */
function onThreadWheel(event: WheelEvent) {
  if (event.deltaY < 0) {
    following = false
  }
}

/** After the reader opens or closes something, the thread follows only if its end is
 *  still in view — a card opened at the bottom is being read, not watched. */
async function settleFollowing() {
  await nextTick()
  const box = scroller.value
  if (box) {
    following = box.scrollHeight - box.scrollTop - box.clientHeight <= FOLLOW_SLACK_PX
  }
}

/** Keeps the end in view as the thread grows, while it is followed. */
watch(
  () => [items.value.length, events.value.length],
  async () => {
    if (!following) {
      return
    }
    await nextTick()
    const box = scroller.value
    if (box) {
      box.scrollTop = box.scrollHeight
    }
  },
)

/** Parsed messages by text: the panel re-renders on every streamed delta, and re-parsing
 *  every answer in a long thread each time would make typing stutter. Streaming makes many
 *  short-lived texts, so the cache is simply emptied once it grows. */
const parsedMessages = new Map<string, ResultBlock[]>()

/** A message as the blocks the viewer's parser reads in it — markdown and TeX drawn, not
 *  shown as source. */
function messageBlocks(text: string): ResultBlock[] {
  let blocks = parsedMessages.get(text)
  if (!blocks) {
    if (parsedMessages.size > 200) {
      parsedMessages.clear()
    }
    blocks = parseResultMarkdown(text).flatMap((page) => page.blocks)
    parsedMessages.set(text, blocks)
  }
  return blocks
}

type UserItem = Extract<ThreadItem, { kind: 'user' }>
type StepsItem = Extract<ThreadItem, { kind: 'steps' }>
type SubagentItem = Extract<ThreadItem, { kind: 'subagent' }>

/** A command message reads as its chip and then its arguments. */
function userText(item: UserItem): string {
  return item.command ? commandArguments(item.text, item.command) : item.text
}

/** An attached image's thumbnail: its local preview until the record holds the message,
 *  then the stored block's bytes. */
function imageSource(item: UserItem, attachment: UserAttachment): string | null {
  if (attachment.preview) {
    return attachment.preview
  }
  if (item.messageId === null || item.messageId < 0 || currentId.value === null) {
    return null
  }
  return attachmentUrl(currentId.value, item.messageId, attachment.index)
}

// --- what the reader opened ------------------------------------------------------------

/** Kept by key, since the items are rebuilt on every frame: a group the reader toggled
 *  stays as they left it, one they never touched is open while its turn is live. A
 *  subagent card starts closed, running or not. */
const openGroups = reactive(new Map<string, boolean>())
const openSteps = reactive(new Set<string>())
const openCards = reactive(new Map<string, boolean>())

function groupOpen(item: StepsItem): boolean {
  return openGroups.get(item.key) ?? item.live
}

function toggleGroup(item: StepsItem) {
  openGroups.set(item.key, !groupOpen(item))
  void settleFollowing()
}

function toggleStep(key: string) {
  if (openSteps.has(key)) {
    openSteps.delete(key)
  } else {
    openSteps.add(key)
  }
  void settleFollowing()
}

function cardOpen(item: SubagentItem): boolean {
  return openCards.get(item.card.id) ?? false
}

function toggleCard(item: SubagentItem) {
  openCards.set(item.card.id, !cardOpen(item))
  void settleFollowing()
}

/** When the panel first saw each subagent running: what its live duration counts from. */
const subagentSeen = reactive(new Map<string, number>())
const now = ref(Date.now())
let ticker: ReturnType<typeof setInterval> | null = null

watch(
  items,
  (list) => {
    for (const item of list) {
      if (item.kind === 'subagent' && item.card.status === 'running' && !subagentSeen.has(item.card.id)) {
        subagentSeen.set(item.card.id, Date.now())
      }
    }
  },
  { immediate: true },
)

// A clock for the running subagents' durations, stopped while none runs.
watch(
  () => items.value.some((item) => item.kind === 'subagent' && item.card.status === 'running'),
  (anyRunning) => {
    if (anyRunning && ticker === null) {
      now.value = Date.now()
      ticker = setInterval(() => {
        now.value = Date.now()
      }, 1000)
    } else if (!anyRunning && ticker !== null) {
      clearInterval(ticker)
      ticker = null
    }
  },
  { immediate: true },
)

// --- edit cards: revert and apply again ---------------------------------------------

/** Each card's state after the reader acted on it: reverted, and the commit that undoes
 *  the undo. Keyed by the tool call, which survives a reload where the item list does not. */
const cards = reactive(new Map<string, CardState>())

function cardState(card: EditCard): CardState {
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
    return false
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

// --- sending -------------------------------------------------------------------------

/** The message as the record will serve it — its blocks in their stored order, the
 *  scaffolding marked and empty — so the thread draws it the same before the reload. */
function sentContent(payload: ComposerPayload): ContentBlock[] {
  const blocks: ContentBlock[] = []
  for (const attachment of payload.attachments.filter((entry) => entry.image)) {
    blocks.push({
      type: 'image',
      attachment: { name: attachment.name, kind: 'image' },
      ...(attachment.preview ? { preview: attachment.preview } : {}),
    })
  }
  for (const attachment of payload.attachments.filter((entry) => !entry.image)) {
    blocks.push({ type: 'text', text: '', context: 'attachment', attachment: { name: attachment.name, kind: 'text' } })
  }
  const line = payload.command ? `/${payload.command}${payload.text ? ` ${payload.text}` : ''}` : payload.text
  if (line) {
    blocks.push({ type: 'text', text: line })
  }
  if (payload.selection) {
    blocks.push({ type: 'text', text: payload.selection, context: 'selection' })
  }
  if (payload.quote) {
    blocks.push({ type: 'text', text: payload.quote, context: 'quote' })
  }
  if (payload.command) {
    blocks.push({ type: 'text', text: '', context: 'skill', skill: payload.command })
  }
  return blocks
}

/** Posts the composer's message, opening a conversation first if none is. True once the
 *  server accepted it: the composer then clears what it sent. */
async function submit(payload: ComposerPayload): Promise<boolean> {
  if (running.value) {
    return false
  }
  composerError.value = null
  let id = currentId.value
  if (id === null) {
    id = await startConversation()
    if (id === null) {
      return false
    }
  }
  try {
    const { turn_id, message_id } = await sendMessage(id, {
      text: payload.text,
      ...(payload.selection ? { selection: payload.selection } : {}),
      ...(payload.quote ? { quote: payload.quote } : {}),
      ...(payload.command ? { command: payload.command } : {}),
      ...(payload.attachments.length ? { attachments: payload.attachments.map((entry) => entry.id) } : {}),
      locale: locale.value,
    })
    if (currentId.value !== id) {
      // The reader switched threads while the send was in flight: the turn belongs to
      // the thread it was sent to, which shows it when it is opened again. What was sent
      // was accepted there, so it is no draft of this thread.
      return true
    }
    // A record reload still in flight (a terminal event's, a dropped stream's) fetched the
    // thread before this message was accepted: dropped, or it would replace the message
    // and the turn below with a record that lacks them (docs/web.md).
    detailSeq++
    detailPending.value = false
    // The reader's own message is followed into view, wherever they had scrolled.
    following = true
    messages.value = [
      ...messages.value,
      {
        // The stored id, so an attached image's bytes can be asked for once its preview
        // is gone; negative only if the server named none, so it never meets a stored one.
        id: message_id ?? -(messages.value.length + 1),
        role: 'user',
        content: sentContent(payload),
        turn_id,
        created_at: new Date().toISOString(),
      },
    ]
    turns.value = [...turns.value, { id: turn_id, status: 'queued', error: null, finished_at: null }]
    stream.expectTurn(turn_id)
    return true
  } catch (error) {
    composerError.value = await resolve(error)
    return false
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

// --- reply: the toolbar over a selection in an answer ------------------------------------

/** Where the toolbar stands (viewport coordinates) and the passage it would quote. */
const toolbar = ref<{ x: number; y: number; below: boolean; text: string } | null>(null)
/** A pointer is down in the thread: the selection is still being made. */
let selecting = false
let toolbarFrame = 0

/** The answer a node sits in, if it is one of this thread's. */
function answerOf(node: Node | null): Element | null {
  const element = node instanceof Element ? node : (node?.parentElement ?? null)
  const answer = element?.closest('.assistant') ?? null
  return answer && scroller.value?.contains(answer) ? answer : null
}

/** The passage as markdown — math back as `$…$`, as the viewer's own copy does — so the
 *  quote reads as the answer was written. */
function passageOf(selection: Selection): string {
  const range = selection.getRangeAt(0)
  return (nodesToMarkdown(range.cloneContents().childNodes) || selection.toString()).trim()
}

function readAnswerSelection() {
  toolbarFrame = 0
  const selection = window.getSelection()
  if (selecting || !selection || selection.isCollapsed || selection.rangeCount === 0) {
    toolbar.value = null
    return
  }
  const answer = answerOf(selection.anchorNode)
  if (!answer || answer !== answerOf(selection.focusNode)) {
    toolbar.value = null
    return
  }
  const text = passageOf(selection)
  if (!text) {
    toolbar.value = null
    return
  }
  const rect = selection.getRangeAt(0).getBoundingClientRect()
  // Under the passage when there is no room over it.
  const below = rect.top < 48
  toolbar.value = {
    x: Math.min(Math.max(rect.left + rect.width / 2, 96), window.innerWidth - 96),
    y: below ? rect.bottom + 6 : rect.top - 6,
    below,
    text,
  }
}

function scheduleToolbar() {
  if (!toolbarFrame) {
    toolbarFrame = requestAnimationFrame(readAnswerSelection)
  }
}

function onThreadPointerDown() {
  selecting = true
  toolbar.value = null
}

function onPointerUp() {
  if (selecting) {
    selecting = false
    scheduleToolbar()
  }
}

/** *Reply* puts the passage in the composer as the quote and focuses the textarea. */
function replyToSelection() {
  const text = toolbar.value?.text
  toolbar.value = null
  if (!text) {
    return
  }
  window.getSelection()?.removeAllRanges()
  void composer.value?.setQuote(text)
}

async function copySelection() {
  const text = toolbar.value?.text
  toolbar.value = null
  if (!text) {
    return
  }
  try {
    await navigator.clipboard.writeText(text)
  } catch {
    // Clipboard access can be denied; the passage is still selected for a manual copy.
  }
}

// --- attachments dropped anywhere on the panel -------------------------------------------

const dragging = ref(false)
/** Enter and leave fire for every child the drag crosses: the zone stays while any holds it. */
let dragDepth = 0

function carriesFiles(event: DragEvent): boolean {
  return Array.from(event.dataTransfer?.types ?? []).includes('Files')
}

function onDragEnter(event: DragEvent) {
  if (!carriesFiles(event)) {
    return
  }
  event.preventDefault()
  dragDepth += 1
  // The composer takes nothing while a turn runs, so neither does the panel.
  dragging.value = !running.value
}

function onDragOver(event: DragEvent) {
  if (!carriesFiles(event)) {
    return
  }
  // Taken here, never handed to the browser — which would open the file in the tab.
  event.preventDefault()
  if (event.dataTransfer) {
    event.dataTransfer.dropEffect = running.value ? 'none' : 'copy'
  }
}

function onDragLeave(event: DragEvent) {
  if (!carriesFiles(event)) {
    return
  }
  dragDepth = Math.max(0, dragDepth - 1)
  if (!dragDepth) {
    dragging.value = false
  }
}

function endDrag() {
  dragDepth = 0
  dragging.value = false
}

function onDrop(event: DragEvent) {
  if (!carriesFiles(event)) {
    return
  }
  event.preventDefault()
  endDrag()
  if (!running.value) {
    composer.value?.addFiles(Array.from(event.dataTransfer?.files ?? []))
  }
}

const maxAttachmentBytes = computed(() => limits.value?.attachment_max_bytes ?? null)

onMounted(() => {
  document.addEventListener('selectionchange', scheduleToolbar)
  window.addEventListener('pointerup', onPointerUp)
  // A drag that ends anywhere — dropped elsewhere, or cancelled — takes the zone with it.
  window.addEventListener('dragend', endDrag)
  window.addEventListener('drop', endDrag)
})

onBeforeUnmount(() => {
  if (flushTimer !== null) {
    clearTimeout(flushTimer)
  }
  if (ticker !== null) {
    clearInterval(ticker)
  }
  if (toolbarFrame) {
    cancelAnimationFrame(toolbarFrame)
  }
  document.removeEventListener('selectionchange', scheduleToolbar)
  window.removeEventListener('pointerup', onPointerUp)
  window.removeEventListener('dragend', endDrag)
  window.removeEventListener('drop', endDrag)
})
</script>

<template>
  <div class="chat" @dragenter="onDragEnter" @dragover="onDragOver" @dragleave="onDragLeave" @drop="onDrop">
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

    <div
      ref="scroller"
      class="thread"
      @pointerdown="onThreadPointerDown"
      @scroll="onThreadScroll"
      @wheel.passive="onThreadWheel"
    >
      <UiBanner v-if="conversationsError" tone="error">{{ conversationsError }}</UiBanner>
      <UiBanner v-else-if="detailError" tone="error">{{ detailError }}</UiBanner>
      <UiBanner v-if="cardError" tone="error">{{ cardError }}</UiBanner>

      <UiSkeleton v-if="detailPending && !items.length" :rows="3" />
      <p v-else-if="!items.length" class="empty">{{ t('chat.empty') }}</p>

      <template v-for="item in items" :key="item.key">
        <!-- The reader's turn: right-aligned, what went with it above the bubble. -->
        <div v-if="item.kind === 'user'" class="user-row">
          <div v-if="item.attachments.length" class="user-attachments">
            <template v-for="attachment in item.attachments" :key="attachment.key">
              <span
                v-if="attachment.image"
                class="user-image"
                role="img"
                :aria-label="attachment.name"
                :title="attachment.name"
              >
                <img v-if="imageSource(item, attachment)" :src="imageSource(item, attachment) ?? undefined" alt="" >
                <UiIcon v-else name="image" />
              </span>
              <span v-else class="user-file" :title="attachment.name">
                <UiIcon name="file-text" />
                <span class="user-file-name">{{ attachment.name }}</span>
              </span>
            </template>
          </div>
          <p v-if="item.quote" class="user-quote">
            <UiIcon name="reply" />
            <span class="user-quote-text">{{ item.quote }}</span>
          </p>
          <p v-if="item.text || item.command" class="user" :class="{ command: item.command }">
            <span v-if="item.command" class="user-command mono">/{{ item.command }}</span>
            <span v-if="userText(item)" class="user-text">{{ userText(item) }}</span>
          </p>
          <p v-if="item.selection" class="user-selection">{{ t('chat.selection', { count: item.selection }) }}</p>
        </div>

        <div v-else-if="item.kind === 'text'" class="answer">
          <div class="assistant md-message">
            <MdBlock v-for="(block, index) in messageBlocks(item.text)" :key="index" :block="block" :page="1" />
          </div>
          <!-- The last answer of a finished turn copies whole, as its markdown. -->
          <div v-if="item.copy" class="answer-tools">
            <UiCopyButton :text="item.text" size="xs" :label="t('chat.copyAnswer')" />
          </div>
        </div>

        <ChatStepGroup
          v-else-if="item.kind === 'steps'"
          :steps="item.steps"
          :live="item.live"
          :open="groupOpen(item)"
          :open-steps="openSteps"
          :card-state="cardState"
          :locked="running"
          @toggle="toggleGroup(item)"
          @toggle-step="toggleStep"
          @toggle-edit="toggleEdit"
        />

        <ChatSubagentCard
          v-else-if="item.kind === 'subagent'"
          :card="item.card"
          :open="cardOpen(item)"
          :started-at="subagentSeen.get(item.card.id) ?? null"
          :now="now"
          :open-steps="openSteps"
          :card-state="cardState"
          :locked="running"
          @toggle="toggleCard(item)"
          @toggle-step="toggleStep"
          @toggle-edit="toggleEdit"
        />

        <p v-else-if="item.kind === 'error'" class="turn-error" role="alert">
          {{ t('chat.turnError', { error: item.text }) }}
        </p>
        <p v-else-if="item.kind === 'cancelled'" class="turn-note">{{ t('chat.turnCancelled') }}</p>
        <p v-else-if="item.kind === 'working'" class="working">
          <UiSpinner :size="12" />
          <span>{{ t('chat.working') }}</span>
        </p>
      </template>
    </div>

    <ChatComposer
      ref="composer"
      :running="running"
      :stopping="stopping"
      :limits="limits"
      :document-selection="documentSelection"
      :submit="submit"
      :error="composerError"
      @stop="stop"
      @new-conversation="startConversation"
    />

    <!-- A file dragged over the panel: the whole column is the target. -->
    <div v-if="dragging" class="drop-zone" aria-hidden="true">
      <p class="drop-title">{{ t('chat.dropToAttach') }}</p>
      <p v-if="maxAttachmentBytes" class="drop-kinds">
        {{ t('chat.dropKinds', { size: formatBytes(maxAttachmentBytes) }) }}
      </p>
    </div>

    <!-- Selecting inside an answer: Reply and Copy, built like the context menu. -->
    <Teleport to="body">
      <div
        v-if="toolbar"
        class="reply-toolbar"
        :class="{ below: toolbar.below }"
        role="toolbar"
        :aria-label="t('chat.selectionTools')"
        :style="{ left: `${toolbar.x}px`, top: `${toolbar.y}px` }"
      >
        <button type="button" class="reply-item" @mousedown.prevent @click="replyToSelection">
          <UiIcon name="reply" />
          <span>{{ t('chat.reply') }}</span>
        </button>
        <button type="button" class="reply-item" @mousedown.prevent @click="copySelection">
          <UiIcon name="copy" />
          <span>{{ t('common.copy') }}</span>
        </button>
      </div>
    </Teleport>

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
  position: relative;
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

/* The thread scrolls rather than squeezing its items: a card that clips its own overflow
   would otherwise shrink to nothing once the thread outgrows the column. */
.thread > * {
  flex-shrink: 0;
}

.empty {
  margin: auto 0;
  color: var(--faint);
  font-size: var(--text-xs);
  text-align: center;
}

/* --- the reader's turn ------------------------------------------------------------ */

.user-row {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 6px;
}

.user {
  max-width: 88%;
  margin: 0;
  padding: var(--space-2) var(--space-3);
  border-radius: var(--radius);
  background: var(--rail);
  color: var(--ink);
  font-size: var(--text-sm);
  line-height: 1.55;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.user.command {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 6px;
}

.user-command {
  padding: 1px 6px;
  border-radius: var(--radius);
  background: var(--rail-active);
  color: var(--ink);
}

.user-text {
  white-space: pre-wrap;
}

.user-quote {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  max-width: 88%;
  margin: 0;
  color: var(--muted);
  font-size: var(--text-xs);
  line-height: 1.5;
}

.user-quote :deep(svg) {
  flex-shrink: 0;
  width: 12px;
  height: 12px;
  margin-top: 3px;
}

.user-quote-text {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.user-attachments {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 6px;
  max-width: 88%;
}

.user-image {
  display: grid;
  place-items: center;
  width: 72px;
  height: 72px;
  overflow: hidden;
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: var(--rail);
  color: var(--faint);
}

.user-image img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.user-image :deep(svg) {
  width: 18px;
  height: 18px;
}

.user-file {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  max-width: 100%;
  height: 28px;
  padding: 0 10px 0 var(--space-2);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: var(--paper);
  color: var(--ink-soft);
  font-size: var(--text-xs);
}

.user-file :deep(svg) {
  flex-shrink: 0;
  width: 14px;
  height: 14px;
  color: var(--faint);
}

.user-file-name {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.user-selection {
  margin: 0;
  font-size: var(--text-xs);
  color: var(--muted);
  text-align: right;
}

/* --- the agent's answer --------------------------------------------------------------- */

.answer {
  display: flex;
  flex-direction: column;
  gap: var(--space-1);
  min-width: 0;
}

/* The assistant's markdown at the panel's UI scale: the document's blocks, drawn smaller. */
.assistant {
  --block-heading: var(--display-xs);
  --block-subheading: var(--text-base);
  --block-text: var(--text-sm);
  --block-text-sm: var(--text-xs);

  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  min-width: 0;
  color: var(--ink-soft);
  overflow-wrap: anywhere;
}

.assistant :deep(.md-p),
.assistant :deep(.md-list) {
  line-height: 1.6;
}

.answer-tools {
  display: flex;
  gap: 2px;
  margin-left: -5px;
}

.turn-error {
  color: var(--danger);
  font-size: var(--text-xs);
  line-height: 1.5;
  overflow-wrap: anywhere;
}

.turn-note {
  color: var(--faint);
  font-size: var(--text-xs);
  line-height: 1.5;
}

.working {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  color: var(--faint);
  font-size: var(--text-xs);
}

/* --- the drop zone: a dashed accent outline over the whole column ---------------------- */

.drop-zone {
  position: absolute;
  inset: var(--space-2);
  z-index: 3;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: var(--space-2);
  border: 2px dashed var(--accent);
  border-radius: var(--radius-menu);
  background: color-mix(in srgb, var(--paper) 94%, transparent);
  pointer-events: none;
}

.drop-title {
  margin: 0;
  color: var(--ink);
  font-size: var(--text-base);
  font-weight: var(--weight-medium);
}

.drop-kinds {
  margin: 0;
  color: var(--muted);
  font-size: var(--text-xs);
}

/* --- the reply toolbar: the context menu's radius, surface and shadow ------------------ */

.reply-toolbar {
  position: fixed;
  z-index: 90;
  display: flex;
  gap: 2px;
  padding: var(--space-1);
  border: 1px solid var(--menu-edge);
  border-radius: var(--radius-menu);
  background: var(--menu-surface);
  box-shadow: var(--shadow-menu);
  transform: translate(-50%, -100%);
  backdrop-filter: blur(24px) saturate(180%);
  -webkit-backdrop-filter: blur(24px) saturate(180%);
}

.reply-toolbar.below {
  transform: translate(-50%, 0);
}

.reply-item {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 28px;
  padding: 0 10px 0 var(--space-2);
  border: 0;
  border-radius: var(--radius-menu-item);
  background: transparent;
  color: var(--ink);
  font-size: var(--text-xs);
  cursor: pointer;
}

/* The accent fills the highlighted row, as in the context menu. */
.reply-item:hover,
.reply-item:focus-visible {
  background: var(--accent);
  color: var(--accent-fg);
}

.reply-item :deep(svg) {
  width: 13px;
  height: 13px;
}
</style>
