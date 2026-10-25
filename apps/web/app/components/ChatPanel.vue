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
 * once nothing is running, so the replay is one bounded read. The same replay brings back
 * each turn's `turn_start` and `memory` events, which are all a reloaded thread has of what
 * an answer used and what it saved to memory.
 *
 * The document is the viewer's: the panel reads its headings to offer whole sections and
 * to tell which of an answer's section links still lead somewhere, and asks the viewer to
 * scroll to one (`locate`).
 */
import {
  attachmentUrl,
  cancelTurn,
  createConversation,
  deleteConversation,
  ApiRequestError,
  getMe,
  getSandbox,
  getConversation,
  listConversations,
  listMemories,
  renameConversation,
  revertRevision,
  sendMessage,
  creditsRefusalOf,
  type CreditsRefusal,
  type ChatEvent,
  type ChatMessage,
  type ChatTurn,
  type ContentBlock,
  type Conversation,
  type Memory,
} from '~/lib/api'
import { blockName, type NameText } from '~/lib/blocks'
import { commandArguments, formatBytes, type AskOutcome, type AskRequest, type ComposerPayload } from '~/lib/composer'
import { byRecency } from '~/lib/conversations'
import { nodesToMarkdown } from '~/lib/copy'
import { formatCount } from '~/lib/format'
import { turnErrorKey, turnErrorNeedsPlan } from '~/lib/turnError'
import { ANSWER_DRAG_TYPE, BLOCK_DRAG_TYPE } from '~/lib/dnd'
import {
  blockPlain,
  documentOutline,
  findHeading,
  inlinePlain,
  parseResultMarkdown,
  sectionSource,
  type CiteResolver,
  type OutlineEntry,
  type ResultBlock,
  type ResultPageBlocks,
} from '~/lib/markdown'
import type { MenuItem, Point } from '~/lib/menu'
import {
  applyMemoryEvent,
  buildThread,
  isLive,
  type CardState,
  type ContextCardView,
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

/** A press on a handle that closed that handle's own menu: its click must not open it again
 *  (the document handle's guard, ResultViewer). */
const REOPEN_GUARD_MS = 300

const props = defineProps<{
  documentId: number
  /** The document pane's current selection, read at the moment a message is composed. */
  documentSelection: () => string | null
  /** The document as the viewer renders it: its headings are what sections are added by
   *  and what an answer's section links lead to. */
  documentPages: () => ResultPageBlocks[]
  /** Where the heading of the section the document is scrolled to sits, if any. */
  currentSection: () => { page: number; index: number } | null
}>()

const emit = defineEmits<{
  /** The document's files changed on disk — an edit landed, or a revert — so reload them. */
  changed: []
  /** An answer's section link was clicked: scroll the document to that heading. */
  locate: [target: { label: string; anchor: string }]
  /** An answer block's *Add to notes*: its markdown, for the end of the notes. */
  'to-notes': [markdown: string]
  /** The head's close: the viewer hides the card. */
  close: []
}>()

const { t, locale } = useI18n()
const { resolve } = useApiError()
const auth = useAuth()

/** The server's own numbers: the composer checks attachments and sections against them. */
const limits = computed(() => auth.me.value?.limits ?? null)

/** What the composer exposes to the panel: the drop zone, the reply toolbar and the
 *  reader's view (`ask`) feed it. */
interface ComposerControls {
  addFiles: (files: readonly File[]) => void
  setQuote: (text: string) => Promise<void>
  focus: () => void
  ask: (request: AskRequest) => Promise<AskOutcome>
}

const composer = ref<ComposerControls | null>(null)
const composerError = ref<string | null>(null)
/** The 402 of a send: the dialog that says what a turn needs (docs/web.md § The agent panel). */
const creditsRefusal = ref<CreditsRefusal | null>(null)

// --- conversations --------------------------------------------------------------

/** The document's conversations that hold a message, the one continued last first: the
 *  server lists no empty one (docs/web.md § The agent panel, *Conversations*). */
const conversations = ref<Conversation[]>([])
const listError = ref<string | null>(null)
const listPending = ref(false)
/** Numbers the reads: only the latest may fill the list, and a change made here since — a
 *  first send accepted, a rename, a delete — drops every read begun before it. */
let listRequest = 0
/** The open conversation; null is the draft, a new conversation nothing was sent to yet. */
const currentId = ref<number | null>(null)
const current = computed(
  () => conversations.value.find((row) => row.id === currentId.value) ?? null,
)
const title = computed(() => current.value?.title || t('chat.untitled'))

async function readConversations() {
  const request = ++listRequest
  listPending.value = true
  try {
    const { conversations: rows } = await listConversations(props.documentId)
    if (request === listRequest) {
      conversations.value = byRecency(rows)
      listError.value = null
    }
  } catch (error) {
    if (request === listRequest) {
      listError.value = await resolve(error)
    }
  } finally {
    if (request === listRequest) {
      listPending.value = false
    }
  }
}

/** The list was changed here: a read already on its way predates the change. */
function listChanged() {
  listRequest++
  listPending.value = false
}

/** The panel opens on the conversation continued last, or on the draft when there is none —
 *  unless the reader has sent from the draft meanwhile: a send on its way, or one refused
 *  whose text waits in the composer for the draft's retry. */
async function loadConversations() {
  await readConversations()
  const latest = conversations.value[0]
  if (latest && currentId.value === null && !draftSending && draft === null) {
    await select(latest.id)
  }
}

const chatRoot = ref<HTMLElement | null>(null)
const titleButton = ref<HTMLButtonElement | null>(null)
/** The list open under the title: where it stands in the card, how far down it may reach,
 *  and whether the keyboard opened it (its current row then takes focus). */
const list = ref<{ top: number; room: number; fromKey: boolean } | null>(null)
const renameError = ref<string | null>(null)

/** Measured from the card each time it opens: under the title, and down to the thread's
 *  foot, which leaves the composer's own top padding — 8px — above its field. The list is
 *  read again as it opens. */
function toggleList(event: MouseEvent) {
  if (list.value) {
    list.value = null
    return
  }
  const card = chatRoot.value?.getBoundingClientRect()
  const under = titleButton.value?.getBoundingClientRect().bottom
  const foot = scroller.value?.getBoundingClientRect().bottom
  if (!card || under === undefined || foot === undefined) {
    return
  }
  const top = under - card.top + 4
  // A click the keyboard made carries no pointer detail.
  list.value = { top, room: foot - card.top - top, fromKey: event.detail === 0 }
  renameError.value = null
  void readConversations()
}

function closeList(refocus: boolean) {
  list.value = null
  if (refocus) {
    titleButton.value?.focus()
  }
}

async function pickConversation(id: number) {
  closeList(true)
  await select(id)
}

function newFromList() {
  list.value = null
  newConversation()
}

/** Shown at once, then as the server cleaned it; a refusal puts the old title back and says
 *  why in the list. The row keeps its place: a rename is not a thread continued. */
async function renameRow(id: number, name: string) {
  const before = conversations.value.find((row) => row.id === id)?.title ?? null
  renameError.value = null
  listChanged()
  conversations.value = conversations.value.map((row) => (row.id === id ? { ...row, title: name } : row))
  try {
    const saved = await renameConversation(id, name)
    listChanged()
    conversations.value = conversations.value.map((row) => (row.id === id ? { ...row, title: saved.title } : row))
  } catch (error) {
    conversations.value = conversations.value.map((row) =>
      row.id === id && row.title === name ? { ...row, title: before } : row,
    )
    renameError.value = await resolve(error)
  }
}

/** The conversation the delete dialog asks about: any row's, from its menu. */
const deleting = ref<Conversation | null>(null)
const deletePending = ref(false)
const deleteError = ref<string | null>(null)

/** The list closes and focus waits on the title, where the dialog gives it back. */
async function askDelete(row: Conversation) {
  list.value = null
  await nextTick()
  titleButton.value?.focus()
  deleteError.value = null
  deleting.value = row
}

async function removeConversation() {
  const target = deleting.value
  if (!target) {
    return
  }
  deletePending.value = true
  deleteError.value = null
  try {
    await deleteConversation(target.id)
    deleting.value = null
    listChanged()
    conversations.value = conversations.value.filter((row) => row.id !== target.id)
    if (currentId.value === target.id) {
      openDraft()
      const next = conversations.value[0]
      if (next) {
        await select(next.id)
      }
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
  card.value = null
  following = true
  // The cards' state stays: keyed by tool-call id, it belongs to the card, not to the
  // conversation now open, and a thread switched away from and back must not offer to
  // revert a commit the reader already reverted (docs/web.md § Agent panel).
  stream.open(id, 0)
  await loadDetail(id)
}

/** The conversation a refused first send made: empty, so never listed, and where the next
 *  send from the draft goes rather than making another (docs/web.md § The agent panel). */
let draft: Conversation | null = null
/** A first send from the draft is on its way: the list read as the panel mounts must not
 *  open another conversation over it. */
let draftSending = false
/** Counts the moves back to the draft: a first send accepted after one is not opened over
 *  the draft the reader moved to. */
let draftGeneration = 0

/** Closes the open thread onto the draft. */
function openDraft() {
  draftGeneration++
  stream.close()
  currentId.value = null
  detailSeq++
  detailPending.value = false
  detailError.value = null
  messages.value = []
  turns.value = []
  discardFrames()
  card.value = null
  following = true
}

/** *New conversation* — the head's button, the list's foot, `/new` — makes nothing on the
 *  server: it opens the draft, keeping whatever the composer holds. On the draft already, it
 *  only focuses the composer. */
function newConversation() {
  if (currentId.value !== null) {
    openDraft()
    composerError.value = null
  }
  composer.value?.focus()
}

onMounted(loadConversations)

// --- the document's sections ----------------------------------------------------------

const outline = computed(() => documentOutline(props.documentPages()))

/** Whether an answer's section link leads to a heading the document still has: one that
 *  does is a chip, one that does not reads as its plain text (docs/web.md § The agent
 *  panel, *Section links*). Read as the answer renders, so it follows the document. */
const resolveCite: CiteResolver = (label, anchor) => findHeading(outline.value, label, anchor) !== null

function currentSectionEntry(): OutlineEntry | null {
  const at = props.currentSection()
  return at ? (outline.value.find((entry) => entry.page === at.page && entry.index === at.index) ?? null) : null
}

function sectionSourceOf(entry: OutlineEntry): string | null {
  return sectionSource(props.documentPages(), entry.page, entry.index)
}

/** A click on a section link in an answer: the viewer scrolls there. Taken here, from the
 *  whole thread, since the links are drawn deep inside the answers' markdown. */
function onThreadClick(event: MouseEvent) {
  const link = event.target instanceof Element ? event.target.closest<HTMLElement>('.md-cite') : null
  if (link && answerOf(link)) {
    emit('locate', { label: link.dataset.citeLabel ?? '', anchor: link.dataset.citeAnchor ?? '' })
  }
}

// --- memory -------------------------------------------------------------------------------

/** The reader's memories as they are now: what each memory line's state is read from
 *  (docs/web.md § The agent panel). Null until first read. Read when the panel opens, when
 *  a `memory` event arrives — replayed ones included, since a line may be from another
 *  tab's turn — and after each Undo. */
const memories = ref<Memory[] | null>(null)
const memoriesError = ref<string | null>(null)
/** Numbers the reads: only the latest may fill the list. */
let memoriesRequest = 0
/** The memories changed — a `memory` event, a line's Undo — and no read has come back since:
 *  the list is the one from before, or the panel's guess at what the change made of it. */
const memoriesStale = ref(false)

async function loadMemories() {
  const request = ++memoriesRequest
  try {
    const { memories: rows } = await listMemories()
    if (request === memoriesRequest) {
      memories.value = rows
      memoriesError.value = null
      // The latest read began after every change so far.
      memoriesStale.value = false
    }
  } catch (error) {
    if (request === memoriesRequest) {
      memoriesError.value = await resolve(error)
    }
  }
}

/** The memories changed under the list: read them again, and until that read succeeds take
 *  the list as unconfirmed. */
function memoriesChanged(): Promise<void> {
  memoriesStale.value = true
  return loadMemories()
}

/** The list the memory lines and the card read. One the read after a change failed to
 *  confirm is withheld: the lines then wait, saying why, rather than offer an action on a
 *  state that may be wrong. */
const shownMemories = computed(() => (memoriesStale.value && memoriesError.value !== null ? null : memories.value))

/** Turns the panel knows are over: the record says so, or their terminal frame has come. */
function endedTurns(): Set<string> {
  const ended = new Set(turns.value.filter((turn) => !isLive(turn)).map((turn) => turn.id))
  for (const frame of events.value) {
    if (frame.event.event === 'turn_done' || frame.event.event === 'turn_error') {
      ended.add(frame.event.turn_id)
    }
  }
  return ended
}

/**
 * A memory a running turn saved, changed or forgot is folded into the list at once: its line
 * renders with the frame, and read against the list from before the call a fresh save would
 * read as undone for a round trip. A replayed frame of a turn already over is left to the read
 * that follows — the list may well be newer than it. `live` says which turns are running.
 */
function applyMemoryFrames(frames: readonly StreamedEvent[], live: (turnId: string) => boolean) {
  if (memories.value === null) {
    return
  }
  const context = {
    conversationId: currentId.value,
    conversationTitle: current.value?.title ?? null,
    now: new Date().toISOString(),
  }
  let rows = memories.value
  for (const { event } of frames) {
    if (event.event === 'memory' && live(event.turn_id)) {
      rows = applyMemoryEvent(rows, event, context)
    }
  }
  memories.value = rows
}

onMounted(loadMemories)

/** A memory pinned, edited or deleted in the settings dialog over this panel: the dialog
 *  keeps the panel mounted, so the change is read here rather than on a remount. */
const { version: contextVersion } = useAgentContextVersion()
watch(contextVersion, () => void memoriesChanged())

/** The floating card a memory line's **View** or a part of the *Used* line opened, and the
 *  control that opened it — where it stands, and where focus goes back to. */
const card = ref<{ key: string; anchor: HTMLElement; view: ContextCardView } | null>(null)

/** The same control again closes it; another opens its own. A card open is being read, so
 *  the thread stops following under it — the card stays with its control, which a thread
 *  kept at its end would carry off — and once it closes, follows again only from its end. */
function openCard(key: string, anchor: HTMLElement, view: ContextCardView) {
  if (card.value?.key === key) {
    closeCard()
    return
  }
  card.value = { key, anchor, view }
  following = false
}

function closeCard() {
  card.value = null
  void settleFollowing()
}

type FootItem = Extract<ThreadItem, { kind: 'foot' }>
type UsedPart = 'instructions' | 'memories'

/** *Used: your instructions · 3 memories* — each part opens the card of what it names. */
function openUsed(item: FootItem, part: UsedPart, anchor: HTMLElement) {
  const view: ContextCardView =
    part === 'instructions' ? { kind: 'instructions' } : { kind: 'memories', ids: item.used?.memoryIds ?? [] }
  openCard(`${item.key}:${part}`, anchor, view)
}

/** Which part of this answer's *Used* line has its card open. */
function usedExpanded(item: FootItem): UsedPart | null {
  const key = card.value?.key ?? ''
  return key === `${item.key}:instructions` ? 'instructions' : key === `${item.key}:memories` ? 'memories' : null
}

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
  const memoryFrames = batch.filter((frame) => frame.event.event === 'memory')
  // Which turns were running is read before this batch joins the thread: a turn's terminal
  // frame follows its memory frames, often in the same batch (a Stop during a save), and must
  // not make that save read as a replay. A turn the panel does not know yet — a replay racing
  // the record — counts as running only while this batch does not end it.
  const endedBefore = memoryFrames.length ? endedTurns() : null
  const liveBefore = new Set(turns.value.filter(isLive).map((turn) => turn.id))
  events.value = events.value.concat(batch)
  // A replayed edit of a finished turn changed nothing since the viewer loaded; one of a
  // turn still in flight did. One reload covers however many landed in this batch.
  if (liveEdit) {
    liveEdit = false
    emit('changed')
  }
  // A memory saved, changed or forgotten: its line's state is read from the list.
  if (endedBefore) {
    const endedNow = endedTurns()
    applyMemoryFrames(
      memoryFrames,
      (turnId) => !endedBefore.has(turnId) && (liveBefore.has(turnId) || !endedNow.has(turnId)),
    )
    void memoriesChanged()
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

// --- what would refuse a send (docs/web.md § The agent panel) -------------------------

/** The workspace against the plan's storage, from `GET /api/sandbox`; null until read, or
 *  when it cannot be — the server's 409 is the authority either way. */
const storage = ref<{ bytes: number; quota: number } | null>(null)
/** A send the server refused for storage (409 `storage_full`) while the figures read
 *  showed room, or could not be read: the notice stands until a read shows room again. */
const storageRefused = ref(false)

async function readStorage() {
  try {
    const { workspace } = await getSandbox()
    storage.value = { bytes: workspace.bytes, quota: workspace.quota_bytes }
    storageRefused.value = false
  } catch {
    storage.value = null
  }
}

const storageFull = computed(
  () => storageRefused.value || (storage.value !== null && storage.value.bytes >= storage.value.quota),
)

/** The balance, read again and handed to the shell. A failed read keeps the figures there
 *  were, where `auth.refresh` would sign the panel out on a network blip. */
async function readBalance() {
  try {
    auth.me.value = await getMe()
  } catch {
    // The cached figures stand; the server's 402 is the authority either way.
  }
}

/** Both figures, when the panel opens, when the window comes back — a period may have
 *  reset or files been deleted meanwhile — and once a turn, which spends and writes, ends. */
function readLimits() {
  void readStorage()
  void readBalance()
}

onMounted(() => {
  readLimits()
  window.addEventListener('focus', readLimits)
})
onBeforeUnmount(() => window.removeEventListener('focus', readLimits))
watch(running, (now, before) => {
  if (before && !now) {
    readLimits()
  }
})

const sendBlock = computed<'credits' | 'storage' | null>(() => {
  const me = auth.me.value
  if (me && me.credits_remaining < me.chat_min_credits) {
    return 'credits'
  }
  if (storageFull.value) {
    return 'storage'
  }
  return null
})

const localePath = useLocalePath()

/** The pricing page, where the plans are presented and the checkout starts. */
function seePlans() {
  void navigateTo(localePath('/pricing'))
}

/** A failed turn's *See plans*, unless the notice above the composer stands for the same
 *  cause and carries it already: one cause, one See plans (docs/web.md § The agent panel). */
function errorNeedsPlan(error: string): boolean {
  if (!turnErrorNeedsPlan(error)) {
    return false
  }
  return sendBlock.value !== (error === 'out_of_credits' ? 'credits' : 'storage')
}

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

/** A sent passage as its chip shows it: its first words on one line. */
function selectionPreview(text: string): string {
  return text.replace(/\s+/g, ' ').trim()
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
  // Served without their text, as the record serves them.
  for (const section of payload.sections) {
    blocks.push({ type: 'text', text: '', context: 'section', section: section.heading })
  }
  if (payload.quote) {
    blocks.push({ type: 'text', text: payload.quote, context: 'quote' })
  }
  if (payload.command) {
    blocks.push({ type: 'text', text: '', context: 'skill', skill: payload.command })
  }
  return blocks
}

/** Posts the composer's message. From the draft, the conversation is made first — or is the
 *  one a refused first send made — and joins the list once its message is accepted. True once
 *  the server accepted it: the composer then clears what it sent. */
async function submit(payload: ComposerPayload): Promise<boolean> {
  if (running.value) {
    return false
  }
  composerError.value = null
  const fromDraft = currentId.value === null
  const generation = draftGeneration
  let id = currentId.value
  let made: Conversation | null = null
  if (fromDraft) {
    draftSending = true
  }
  try {
    if (id === null) {
      made = draft ?? (await createConversation(props.documentId))
      draft = made
      id = made.id
    }
    const { turn_id, message_id } = await sendMessage(id, {
      text: payload.text,
      ...(payload.selection ? { selection: payload.selection } : {}),
      ...(payload.sections.length ? { sections: payload.sections } : {}),
      ...(payload.quote ? { quote: payload.quote } : {}),
      ...(payload.command ? { command: payload.command } : {}),
      ...(payload.attachments.length ? { attachments: payload.attachments.map((entry) => entry.id) } : {}),
      locale: locale.value,
    })
    if (made) {
      // It holds a message now, so it is listed — first, as the thread continued last — and
      // the list is read again for the title its first message gave it.
      const accepted = { ...made, updated_at: new Date().toISOString() }
      draft = null
      listChanged()
      conversations.value = [accepted, ...conversations.value.filter((row) => row.id !== accepted.id)]
      void readConversations()
      if (currentId.value !== null || generation !== draftGeneration) {
        // The reader opened another thread, or a new draft, while the send was in flight:
        // the turn is this conversation's, shown when it is opened.
        return true
      }
      currentId.value = id
      stream.open(id, 0)
    } else if (currentId.value !== id) {
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
    const refusal = creditsRefusalOf(error)
    if (refusal) {
      creditsRefusal.value = refusal
      return false
    }
    if (error instanceof ApiRequestError && error.type === 'storage_full') {
      // The notice says it, with the figures read again, rather than an error line.
      await readStorage()
      storageRefused.value = true
      return false
    }
    composerError.value = await resolve(error)
    return false
  } finally {
    if (fromDraft) {
      draftSending = false
    }
  }
}

/** What a turn cost, from the record (`credits` is settled once the turn ends and the
 *  record is read again); null until then (docs/billing.md § Routes). */
function turnCredits(turnId: string): number | null {
  return turns.value.find((turn) => turn.id === turnId)?.credits ?? null
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

function onThreadPointerDown(event: PointerEvent) {
  selecting = true
  toolbar.value = null
  // A touch screen has no hover: the block tapped shows its handle until another tap.
  if (event.pointerType === 'touch') {
    hoveredBlock.value = answerBlockOf(event.target)
  }
}

function onPointerUp() {
  pressedBlock.value = null
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

// --- answer blocks: the handle, its drag and its menu ------------------------------------

/** Answer blocks by `{item key}:{index}`: a handle is drawn only on the block the pointer is
 *  over or level with, the one last tapped, the one holding keyboard focus, the one whose menu
 *  is open and the one being dragged (docs/web.md § The agent panel, *An answer is blocks*). A
 *  pressed handle stays until the press ends: a drag starts only once the pointer has moved,
 *  by then often off the block, and a handle gone by then starts none. */
const hoveredBlock = ref<string | null>(null)
const focusedBlock = ref<string | null>(null)
const pressedBlock = ref<string | null>(null)
const draggedBlock = ref<string | null>(null)
/** The open menu's block by where it is, not as it read then: a live answer grows under it. */
const blockMenu = ref<{ at: Point; key: string; itemKey: string; index: number; fromKey: boolean } | null>(null)
const dragCard = ref<HTMLElement | null>(null)
let blockMenuClosedAt = 0
let blockMenuClosedBy: EventTarget | null = null
let blockMenuClosedKey: string | null = null

/** What a handle calls its block, as the document's handles do. */
const nameText = computed<NameText>(() => ({
  formula: t('viewer.names.formula'),
  formulaTag: (tag) => t('viewer.names.formulaTag', { tag }),
  quoted: (words) => t('viewer.names.quoted', { words }),
  figure: (words) => t('viewer.names.figure', { name: t('viewer.names.quoted', { words }) }),
  figureBare: t('viewer.names.figureBare'),
  image: (words) => t('viewer.names.image', { name: t('viewer.names.quoted', { words }) }),
  imageBare: t('viewer.names.imageBare'),
}))

function handleShown(key: string): boolean {
  return (
    hoveredBlock.value === key ||
    focusedBlock.value === key ||
    pressedBlock.value === key ||
    draggedBlock.value === key ||
    blockMenu.value?.key === key
  )
}

function answerBlockOf(target: EventTarget | null): string | null {
  return target instanceof Element ? (target.closest<HTMLElement>('[data-answer-block]')?.dataset.answerBlock ?? null) : null
}

/** The block the pointer is over — or level with, in the gutter — and in the gap between two
 *  blocks of an answer the last one, so the pointer on its way to a handle keeps it: the
 *  document's rule (ResultViewer's `readHover`). */
function onThreadPointerMove(event: PointerEvent) {
  if (event.pointerType === 'touch') {
    return
  }
  let probe = event.target instanceof Element ? event.target : null
  if (probe && !probe.closest('[data-answer-block]')) {
    const column = scroller.value?.querySelector('.assistant')?.getBoundingClientRect()
    if (column && event.clientX < column.left) {
      probe = document.elementFromPoint(column.left + 1, event.clientY)
    }
  }
  const key = answerBlockOf(probe)
  if (key !== null) {
    hoveredBlock.value = key
  } else if (!probe?.closest('.assistant')) {
    hoveredBlock.value = null
  }
}

/** A finger lifted leaves too: a tapped block keeps its handle. */
function onThreadPointerLeave(event: PointerEvent) {
  if (event.pointerType !== 'touch') {
    hoveredBlock.value = null
  }
}

/** Keyboard focus only: a handle pressed with the pointer takes focus too, and would keep its
 *  handle drawn once the pointer has gone. */
function onThreadFocusIn(event: FocusEvent) {
  focusedBlock.value = event.target instanceof Element && event.target.matches(':focus-visible') ? answerBlockOf(event.target) : null
}

/** The window losing focus is not focus leaving: the handle stays to be given it back. */
function onThreadFocusOut(event: FocusEvent) {
  if (event.relatedTarget === null && (!document.hasFocus() || document.activeElement === event.target)) {
    return
  }
  if (answerBlockOf(event.relatedTarget) !== focusedBlock.value) {
    focusedBlock.value = null
  }
}

/** The drag is a copy: the document's block type with no run of the document's, the
 *  answer's own type, and the block's markdown as text — what the notes, the document and any
 *  text field land. */
function onBlockDragStart(event: DragEvent, key: string, block: ResultBlock) {
  const transfer = event.dataTransfer
  if (!transfer) {
    event.preventDefault()
    return
  }
  draggedBlock.value = key
  toolbar.value = null
  transfer.effectAllowed = 'copy'
  transfer.setData(BLOCK_DRAG_TYPE, '[]')
  transfer.setData(ANSWER_DRAG_TYPE, '')
  transfer.setData('text/plain', block.src)
  const card = dragCard.value
  const text = card?.querySelector('.drag-card-text')
  if (card && text) {
    text.textContent = blockPlain(block) || blockName(block, nameText.value)
    transfer.setDragImage(card, 12, 14)
  }
}

/** The menu's block as it reads now, or null when the answer it was in is gone or stored
 *  under another key. */
function blockOfMenu(): ResultBlock | null {
  const menu = blockMenu.value
  const item = menu ? items.value.find((entry) => entry.key === menu.itemKey) : undefined
  return menu && item?.kind === 'text' ? (messageBlocks(item.text)[menu.index] ?? null) : null
}

/** A menu open is being read: the thread stops following under it, since its scroll would
 *  close the menu, as a card does (`openCard`). */
function openBlockMenu(event: MouseEvent | KeyboardEvent, itemKey: string, index: number) {
  const handle = event.currentTarget as HTMLElement
  const key = `${itemKey}:${index}`
  // The press that closed this block's own menu is not a press to open it again; one that
  // closed another block's menu is.
  if (
    performance.now() - blockMenuClosedAt < REOPEN_GUARD_MS &&
    blockMenuClosedKey === key &&
    blockMenuClosedBy instanceof Node &&
    handle.contains(blockMenuClosedBy)
  ) {
    return
  }
  if (blockMenu.value?.key === key) {
    closeBlockMenu()
    return
  }
  const rect = handle.getBoundingClientRect()
  blockMenu.value = {
    at: { x: rect.right + 4, y: rect.top },
    key,
    itemKey,
    index,
    fromKey: event instanceof KeyboardEvent || event.detail === 0,
  }
  following = false
}

function closeBlockMenu(cause?: Event) {
  const menu = blockMenu.value
  // The menu gives focus back to its handle as it closes, and the handle stays drawn to take
  // it — unless a press elsewhere closed it, which puts focus where it pressed.
  if (menu && (menu.fromKey || !(cause instanceof PointerEvent))) {
    focusedBlock.value = menu.key
  }
  blockMenu.value = null
  blockMenuClosedAt = performance.now()
  blockMenuClosedBy = cause?.target ?? null
  blockMenuClosedKey = menu?.key ?? null
  void settleFollowing()
}

watch(items, () => {
  if (blockMenu.value && !blockOfMenu()) {
    closeBlockMenu()
  }
})

const blockMenuItems = computed<MenuItem[]>(() => [
  { key: 'notes', label: t('chat.block.toNotes'), icon: 'notebook-pen' },
  { key: 'copy', label: t('chat.block.copy'), icon: 'copy' },
])

async function onBlockMenuSelect(key: string) {
  const block = blockOfMenu()
  if (!block) {
    return
  }
  if (key === 'notes') {
    emit('to-notes', block.src)
  } else if (key === 'copy') {
    try {
      await navigator.clipboard.writeText(block.src)
    } catch {
      // Clipboard access can be denied; the answer's own copy control is still there.
    }
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

/**
 * For the reader's view: put a passage, a whole section or a command in the composer —
 * sent at once with `send` while nothing runs and the composer held nothing else, otherwise
 * left there for Send (docs/web.md § The agent panel, *Asked from the document*).
 */
function ask(request: AskRequest): Promise<AskOutcome> {
  return composer.value?.ask(request) ?? Promise.resolve('kept')
}

defineExpose({ ask })

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
  <div
    ref="chatRoot"
    class="chat"
    @dragenter="onDragEnter"
    @dragover="onDragOver"
    @dragleave="onDragLeave"
    @drop="onDrop"
  >
    <!-- The conversation's name opens the document's list of them; at the right a new
         conversation and the close (docs/web.md § The agent panel). -->
    <div class="head">
      <button
        ref="titleButton"
        type="button"
        class="title"
        aria-haspopup="dialog"
        :aria-expanded="list ? 'true' : 'false'"
        :aria-label="t('chat.pick')"
        @click="toggleList"
      >
        <span class="title-text">{{ current ? title : t('chat.newConversation') }}</span>
        <UiIcon name="chevron-down" />
      </button>
      <UiButton class="new" variant="ghost" icon-only :label="t('chat.newConversation')" @click="newConversation()">
        <template #icon><UiIcon name="square-pen" /></template>
      </UiButton>
      <UiButton variant="ghost" icon-only :label="t('chat.closePanel')" @click="emit('close')">
        <template #icon><UiIcon name="panel-right-close" /></template>
      </UiButton>
    </div>

    <ChatConversationList
      v-if="list"
      :conversations="conversations"
      :current-id="currentId"
      :pending="listPending"
      :error="listError"
      :rename-error="renameError"
      :top="list.top"
      :room="list.room"
      :title-max="limits?.conversation_title_max_chars"
      :from-key="list.fromKey"
      :anchor="titleButton"
      @select="pickConversation"
      @new="newFromList"
      @rename="renameRow"
      @delete="askDelete"
      @retry="readConversations"
      @close="closeList"
    />

    <UiContextMenu
      v-if="blockMenu"
      :at="blockMenu.at"
      :items="blockMenuItems"
      :label="t('chat.block.menu')"
      :focus-first="blockMenu.fromKey"
      @select="onBlockMenuSelect"
      @close="closeBlockMenu"
    />

    <!-- An answer block's drag picture, written as the drag starts; never seen in place. -->
    <div ref="dragCard" class="drag-card" aria-hidden="true">
      <UiIcon name="grip-vertical" />
      <span class="drag-card-text" />
    </div>

    <div
      ref="scroller"
      class="thread"
      @pointerdown="onThreadPointerDown"
      @pointermove="onThreadPointerMove"
      @pointerleave="onThreadPointerLeave"
      @focusin="onThreadFocusIn"
      @focusout="onThreadFocusOut"
      @click="onThreadClick"
      @scroll="onThreadScroll"
      @wheel.passive="onThreadWheel"
    >
      <UiBanner v-if="listError" tone="error">{{ listError }}</UiBanner>
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
          <!-- What went with it, as the composer's chips showed it, without remove controls. -->
          <div v-if="item.selection || item.sections.length" class="user-context">
            <ChatContextChip v-if="item.selection" icon="text-quote" :text="selectionPreview(item.selection)" />
            <ChatContextChip
              v-for="(heading, index) in item.sections"
              :key="index"
              icon="heading"
              :text="t('chat.context.section', { heading: inlinePlain(heading) })"
              :tooltip="inlinePlain(heading)"
            />
          </div>
        </div>

        <!-- An answer: its section links are chips that scroll the document there. -->
        <div v-else-if="item.kind === 'text'" class="assistant md-message md-side">
          <template v-for="(block, index) in messageBlocks(item.text)" :key="index">
            <!-- A comment draws nothing, and takes no wrapper that would count as a block. -->
            <div v-if="block.kind !== 'comment'" class="answer-block" :data-answer-block="`${item.key}:${index}`">
              <MdBlock :block="block" :cite="resolveCite" />
              <!-- After the block, so a heading stays its wrapper's first child. -->
              <div
                v-if="handleShown(`${item.key}:${index}`)"
                role="button"
                class="answer-handle"
                :class="{ on: blockMenu?.key === `${item.key}:${index}` }"
                data-chrome
                tabindex="0"
                draggable="true"
                :aria-label="t('chat.block.handle', { name: blockName(block, nameText) })"
                aria-haspopup="menu"
                :aria-expanded="blockMenu?.key === `${item.key}:${index}` ? 'true' : 'false'"
                @pointerdown.stop="pressedBlock = `${item.key}:${index}`"
                @click.stop="openBlockMenu($event, item.key, index)"
                @keydown.enter.space.prevent="openBlockMenu($event, item.key, index)"
                @dragstart="onBlockDragStart($event, `${item.key}:${index}`, block)"
                @dragend="draggedBlock = null; pressedBlock = null"
              >
                <UiIcon name="grip-vertical" />
              </div>
            </div>
          </template>
        </div>

        <ChatMemoryLine
          v-else-if="item.kind === 'memory'"
          :line="item.line"
          :memories="shownMemories"
          :conversation-id="currentId"
          :load-error="memoriesError"
          :reload="memoriesChanged"
          :viewing="card?.key === item.key"
          @view="(anchor) => openCard(item.key, anchor, { kind: 'memory', line: item.line })"
        />

        <!-- Under a finished turn's last answer: what it used, and the control that copies
             it whole, as its markdown. -->
        <div v-else-if="item.kind === 'foot'" class="answer-foot">
          <UiCopyButton :text="item.text" size="xs" :label="t('chat.copyAnswer')" />
          <!-- The turn's credits: a cost figure, so in the accent (docs/web.md § The agent panel). -->
          <span v-if="turnCredits(item.turnId) !== null" class="answer-credits tabular">
            {{ t('credits.n', { n: formatCount(turnCredits(item.turnId)!, locale) }, turnCredits(item.turnId)!) }}
          </span>
          <ChatUsedLine
            v-if="item.used"
            :used="item.used"
            :expanded="usedExpanded(item)"
            @open="(part, anchor) => openUsed(item, part, anchor)"
          />
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

        <!-- The worker's text is for the log: the line says it in the reader's words and
             keeps the original in the tooltip (docs/web.md § The agent panel). -->
        <div v-else-if="item.kind === 'error'" class="turn-error-row">
          <p class="turn-error" role="alert" :title="item.text">{{ t(turnErrorKey(item.text)) }}</p>
          <UiButton v-if="errorNeedsPlan(item.text)" size="sm" variant="secondary" @click="seePlans">
            {{ t('credits.seePlans') }}
          </UiButton>
        </div>
        <p v-else-if="item.kind === 'cancelled'" class="turn-note">{{ t('chat.turnCancelled') }}</p>
        <p v-else-if="item.kind === 'working'" class="working">
          <UiSpinner :size="12" />
          <span>{{ t('chat.working') }}</span>
        </p>
      </template>
    </div>

    <div v-if="sendBlock" class="send-block" role="status">
      <p class="send-block-text">
        {{
          sendBlock === 'credits'
            ? t('chat.blockedCredits')
            : storage
              ? t('chat.blockedStorage', { used: formatBytes(storage.bytes), quota: formatBytes(storage.quota) })
              : t('chat.blockedStorageBare')
        }}
      </p>
      <UiButton size="sm" @click="seePlans">{{ t('credits.seePlans') }}</UiButton>
    </div>

    <ChatComposer
      ref="composer"
      :blocked="sendBlock !== null"
      :running="running"
      :stopping="stopping"
      :limits="limits"
      :document-selection="documentSelection"
      :outline="outline"
      :current-section="currentSectionEntry"
      :section-source="sectionSourceOf"
      :submit="submit"
      :error="composerError"
      @stop="stop"
      @new-conversation="newConversation"
    />

    <ChatContextCard
      v-if="card"
      :anchor="card.anchor"
      :view="card.view"
      :memories="shownMemories"
      :load-error="memoriesError"
      @close="closeCard"
    />

    <CreditsDialog
      v-if="creditsRefusal"
      :refusal="creditsRefusal"
      action="chat"
      @close="creditsRefusal = null"
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
      v-if="deleting"
      :title="t('chat.deleteConversation')"
      :message="t('chat.deleteConfirm', { title: deleting.title || t('chat.untitled') })"
      :confirm-label="t('common.delete')"
      :pending="deletePending"
      :error="deleteError"
      @confirm="removeConversation"
      @cancel="deleting = null"
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

/* The card's head, as tall as the notes card's: the title is the column's heading, in the
   card's title size, and the two buttons sit at its right (docs/web.md § The agent panel). */
.head {
  display: flex;
  align-items: center;
  gap: 2px;
  flex-shrink: 0;
  height: 56px;
  padding: 0 10px 0 var(--space-2);
}

.title {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
  max-width: 100%;
  height: var(--control-height);
  margin-right: auto;
  padding: 0 var(--space-3);
  border: 0;
  border-radius: var(--radius-full);
  background: transparent;
  color: var(--ink);
  font-size: var(--display-xs);
  font-weight: var(--weight-semibold);
  letter-spacing: -0.01em;
  cursor: pointer;
  transition: background var(--duration-fast) var(--ease);
}

.title:hover,
.title[aria-expanded='true'] {
  background: var(--fill);
}

/* A new conversation is the head's one action in the accent; the close stays quiet. */
.head .new,
.head .new:hover {
  color: var(--accent);
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
/* Positioned, so what sits absolutely inside it — each copy button's live region — scrolls
   and clips with it. Placed against the column instead, those reach past its foot, and the
   viewer's body, which hides its overflow but can still be scrolled, has room to slide up. */
.thread {
  position: relative;
  display: flex;
  flex: 1 1 auto;
  flex-direction: column;
  gap: var(--space-4);
  min-height: 0;
  overflow-y: auto;
  overscroll-behavior: contain;
  padding: var(--space-1) var(--space-5) var(--space-3);
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

/* The reader's words in the accent bubble, as a message the reader sent. */
.user {
  max-width: 78%;
  margin: 0;
  padding: 9px 15px;
  border-radius: var(--radius-xl);
  background: var(--accent);
  color: var(--accent-fg);
  font-size: var(--text-sm);
  line-height: 1.5;
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
  padding: 1px 8px;
  border-radius: var(--radius-full);
  background: var(--accent-highlight);
  font-weight: var(--weight-medium);
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
  border-radius: var(--radius-lg);
  background: var(--fill);
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
  padding: 0 12px 0 10px;
  border-radius: var(--radius-full);
  background: var(--fill);
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

/* What went with the message, as chips under it: a passage and whole sections. */
.user-context {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 6px;
  max-width: 88%;
}

/* --- the agent's answer --------------------------------------------------------------- */

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

/* Each block of an answer, its handle placed against it in the thread's left gutter. */
.answer-block {
  position: relative;
  min-width: 0;
}

/* Each block is its wrapper's first child, which markdown.css reads as the column's top: a
   `##` past an answer's first block keeps the rule over it — the block's own heading only,
   never one inside a quote. */
.answer-block + .answer-block > :deep(.md-h2:not([data-level='1'])) {
  padding-top: var(--space-4);
  border-top: 1px solid var(--line);
}

/* The document's handle (ResultViewer's .block-handle), narrowed to the 20px gutter. */
.answer-handle {
  position: absolute;
  top: 0;
  left: calc(-1 * var(--space-5));
  z-index: 1;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 20px;
  height: 24px;
  border-radius: var(--radius);
  color: var(--faint);
  cursor: grab;
  transition:
    background var(--duration-fast) var(--ease),
    color var(--duration-fast) var(--ease);
}

.answer-handle:hover {
  background: var(--fill);
}

.answer-handle.on {
  background: var(--fill);
  color: var(--ink);
}

/* Beside a ruled `##`'s text, not on its rule. */
.answer-block + .answer-block:has(> .md-h2:not([data-level='1'])) > .answer-handle {
  top: calc(var(--space-4) + 1px);
}

.answer-handle > :deep(svg) {
  width: 16px;
  height: 16px;
}

/* The drag's picture, as the document's: off screen until the browser takes it. */
.drag-card {
  position: fixed;
  top: -1000px;
  left: -1000px;
  display: flex;
  align-items: center;
  gap: var(--space-2);
  max-width: 280px;
  padding: var(--space-1) var(--space-3) var(--space-1) var(--space-2);
  border: 1px solid var(--edge);
  border-radius: var(--radius);
  background: var(--paper);
  box-shadow: var(--shadow-menu);
  color: var(--ink);
  font-size: var(--text-sm);
  white-space: nowrap;
  pointer-events: none;
}

.drag-card > :deep(svg) {
  flex: none;
  width: 16px;
  height: 16px;
  color: var(--faint);
}

.drag-card-text {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* The memory lines and the foot belong to the answer above them, not to the thread's rhythm:
   they sit closer to it than one item to the next. */
.thread > .assistant + .memory-line,
.thread > .memory-line + .memory-line {
  margin-top: calc(-1 * var(--space-2));
}

.answer-foot {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 2px var(--space-3);
  margin-top: calc(-1 * var(--space-3));
}

/* The copy control's glyph lines up with the answer's edge, past its own padding. */
.answer-foot > :first-child {
  margin-left: -5px;
}

.answer-credits {
  color: var(--accent);
  font-size: var(--text-xs);
}

.turn-error-row {
  display: flex;
  align-items: center;
  gap: var(--space-3);
}

.turn-error {
  flex: 1 1 auto;
  min-width: 0;
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

/* What would refuse a send, over the composer: a fact with its way out, not an error. */
.send-block {
  display: flex;
  flex-shrink: 0;
  align-items: center;
  gap: var(--space-3);
  margin: var(--space-2) var(--space-4) 0;
  padding: var(--space-3) var(--space-3) var(--space-3) var(--space-4);
  border-radius: var(--radius-lg);
  background: var(--fill-weak);
}

.send-block-text {
  flex: 1 1 auto;
  min-width: 0;
  color: var(--ink);
  font-size: var(--text-sm);
  line-height: 1.5;
}

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

/* --- the reply toolbar: the document's selection capsule, on the menu's surface ---------- */

/* Over the agent's glass card, so not glass itself: the menu's surface in the capsule's
   shape (docs/web.md § Design system, *Glass is for what floats*). */
.reply-toolbar {
  position: fixed;
  z-index: 90;
  display: flex;
  gap: 2px;
  padding: var(--space-1);
  border: 1px solid var(--menu-edge);
  border-radius: var(--radius-full);
  background: var(--menu-surface);
  box-shadow: var(--shadow-menu);
  transform: translate(-50%, -100%);
}

.reply-toolbar.below {
  transform: translate(-50%, 0);
}

.reply-item {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 30px;
  padding: 0 12px 0 10px;
  border: 0;
  border-radius: var(--radius-full);
  background: transparent;
  color: var(--ink);
  font-size: var(--text-xs);
  cursor: pointer;
}

.reply-item:hover,
.reply-item:focus-visible {
  background: var(--fill);
}

.reply-item :deep(svg) {
  width: 13px;
  height: 13px;
}
</style>
