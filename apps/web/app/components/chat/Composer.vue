<script setup lang="ts">
/**
 * The agent panel's composer (docs/web.md § The agent panel): one rounded field holding,
 * top to bottom, what goes with the message — the context chips (the document, the
 * selection, whole sections, after the control that adds them), the reply's quote, the
 * attachments, a picked command — then the textarea, and a foot of the attach and command
 * controls with the send button at the right.
 *
 * It owns the draft and everything attached to it; the panel owns the conversation, so
 * sending is the panel's `submit`, which says whether the server took the message. Only
 * what was sent is cleared then: whatever the reader changed while it was in flight is the
 * next draft.
 */
import { deleteAttachment, listSkills, uploadAttachment, type AttachmentUpload, type UploadLimits } from '~/lib/api'
import {
  acceptList,
  builtinCommands,
  commandQuery,
  extensionOf,
  formatBytes,
  isImageFile,
  matchCommands,
  NEW_COMMAND,
  passageTooLong,
  refuseAttachment,
  sectionHeading,
  sectionText,
  type AskOutcome,
  type AskRequest,
  type AttachmentRefusal,
  type CommandOption,
  type ComposerPayload,
  type SentSection,
} from '~/lib/composer'
import { inlinePlain, type OutlineEntry } from '~/lib/markdown'
import type { MenuItem, Point } from '~/lib/menu'

const props = defineProps<{
  /** A turn is in flight: the composer waits, and Stop is the one control. */
  running: boolean
  stopping: boolean
  /** A send would be refused (no credits, a full workspace): the draft waits, Send is off. */
  blocked?: boolean
  limits: UploadLimits | null
  /** The document pane's selection, read as the reader comes to the composer. */
  documentSelection: () => string | null
  /** The document's headings, which *Add context* lists. */
  outline: OutlineEntry[]
  /** The heading of the section the document is scrolled to, read when the menu opens. */
  currentSection: () => OutlineEntry | null
  /** A heading's whole section, as its markdown. */
  sectionSource: (entry: OutlineEntry) => string | null
  /** Posts the message; true once the server accepted it. */
  submit: (payload: ComposerPayload) => Promise<boolean>
  error: string | null
  /** A conversation is open, as against the draft: `/compact` is offered (docs/web.md
   *  § The agent panel, *Commands*). */
  conversationOpen: boolean
}>()

const emit = defineEmits<{ stop: []; 'new-conversation': [] }>()

const { t } = useI18n()
const { resolve } = useApiError()

const form = ref<HTMLFormElement | null>(null)
const textarea = ref<HTMLTextAreaElement | null>(null)
const fileInput = ref<HTMLInputElement | null>(null)
const draft = ref('')
const focused = ref(false)
const sending = ref(false)
const localError = ref<string | null>(null)

function focus() {
  textarea.value?.focus()
}

// --- the selection and the quote -----------------------------------------------------

/** The document selection picked up when the reader came to the composer. */
const selection = ref<string | null>(null)
/** The passage of an answer the reader is replying to. */
const quote = ref<string | null>(null)

/** Before the composer takes focus — on the pointer's way in, and on keyboard focus — the
 *  document's selection is still the reader's, so this is when it is read. Every reading
 *  replaces the last, none included: a passage selected and then deselected is not sent.
 *  The focus that follows a pointer capture is skipped, since by then the click has
 *  collapsed what the pointer already read; so is a press or a focus that moves within
 *  the composer, where the document no longer holds a selection to read. */
let capturedByPointer = false
/** The next focus into the composer is not the reader coming to it: *Add context*'s menu
 *  handing focus back as it closes (it lives outside the field), or `ask` focusing the
 *  textarea after setting the context itself. Read on that focus, the selection would be
 *  cleared or overwritten just after the reader chose what goes. */
let focusReturning = false

/** The selection was asked for by the reader's view: chosen by blocks, it stays until the
 *  reader removes it, sends it or comes back with another passage selected — a reading that
 *  finds nothing does not clear it. */
let selectionAsked = false

function captureSelection() {
  const read = props.documentSelection() || null
  if (read !== null || !selectionAsked) {
    selection.value = read
    selectionAsked = false
  }
}

function captureOnPointer() {
  if (form.value?.contains(document.activeElement)) {
    return
  }
  captureSelection()
  capturedByPointer = true
  // Cleared once the focus this press causes (if any) has been handled.
  setTimeout(() => {
    capturedByPointer = false
  }, 0)
}

function onFocusIn(event: FocusEvent) {
  if (focusReturning) {
    focusReturning = false
    return
  }
  if (capturedByPointer || form.value?.contains(event.relatedTarget as Node | null)) {
    return
  }
  captureSelection()
}

/** Marks the focus about to come back as not the reader's; forgotten if none comes. */
function expectFocusReturn() {
  focusReturning = true
  setTimeout(() => {
    focusReturning = false
  }, 0)
}

/** The passage, as its chip shows it: its first words on one line. */
const selectionPreview = computed(() => selection.value?.replace(/\s+/g, ' ').trim() ?? '')

/** A passage past the server's cap is shown and holds Send, never cut: a cut passage would
 *  pass for the whole one. */
const selectionTooLong = computed(() => passageTooLong(selection.value, props.limits?.selection_max_chars))

// --- whole sections ----------------------------------------------------------------------

/** A section the reader added, by heading. `key` is where its heading sits, so the menu
 *  checks it rather than adding it twice; `plain` is the heading as the chip draws it. */
interface AddedSection extends SentSection {
  key: string
  plain: string
}

const addedSections = ref<AddedSection[]>([])

function entryKey(entry: OutlineEntry): string {
  return `${entry.page}-${entry.index}`
}

/** `limits.sections_per_message` reached: no further section is offered. */
const sectionsFull = computed(() => {
  const cap = props.limits?.sections_per_message
  return cap !== undefined && addedSections.value.length >= cap
})

/** Its text is read when it is added, and past `limits.selection_max_chars` it goes as its
 *  heading alone (docs/chat.md § What a user message carries). */
function addSection(section: AddedSection) {
  if (sectionsFull.value || addedSections.value.some((added) => added.key === section.key)) {
    return
  }
  addedSections.value = [...addedSections.value, section]
}

function addEntry(entry: OutlineEntry) {
  const source = props.sectionSource(entry)
  if (source === null) {
    return
  }
  addSection({
    key: entryKey(entry),
    heading: entry.text,
    plain: entry.plain,
    text: sectionText(source, props.limits?.selection_max_chars),
  })
}

const contextRow = ref<HTMLElement | null>(null)

/** The chips' remove controls, in order: the selection's, then each section's. */
function removeControls(): HTMLElement[] {
  return [...(contextRow.value?.querySelectorAll<HTMLElement>('[data-chip-remove]') ?? [])]
}

/** A chip removed from its own control takes the focused button with it: focus goes to the
 *  remove control now in its place — the next chip's — or, past the last chip, to **+**. It
 *  stays inside the field, so it is no reader coming back to read the selection again. */
async function refocusAfterRemove(index: number) {
  await nextTick()
  const active = document.activeElement
  if (active && active !== document.body) {
    // A click that never focused the control (Safari's) left focus where it was.
    return
  }
  const next = removeControls()[index] ?? addButton.value
  if (next) {
    expectFocusReturn()
    next.focus()
  }
}

function removeSelection() {
  selection.value = null
  selectionAsked = false
  void refocusAfterRemove(0)
}

function removeSection(key: string) {
  // Counted before the chip goes: the selection's control, if any, comes first.
  const index = addedSections.value.findIndex((section) => section.key === key) + (selection.value ? 1 : 0)
  addedSections.value = addedSections.value.filter((section) => section.key !== key)
  void refocusAfterRemove(index)
}

// --- *Add context*: the current section, then every heading --------------------------------

const addButton = ref<HTMLButtonElement | null>(null)
const addMenu = ref<Point | null>(null)
const addMenuFromKey = ref(false)
/** The section the document was at when the menu opened: scrolling with the menu open
 *  closes it, so this is the one the menu offers. */
const menuCurrent = ref<OutlineEntry | null>(null)
/** The event that last closed the menu, if one did. */
let addMenuClosedBy: Event | null = null
/** The press now on **+** is the one that closed the menu (a press outside it, caught
 *  first): that press's click must not open it again. A pick, Escape or a press elsewhere
 *  leaves the next press free. */
let addPressClosedMenu = false

/** A subsection is set in by a space as wide as the type, so the outline reads as one. A
 *  heading longer than the menu is cut by the menu, with an ellipsis; the row's title and
 *  the chip it adds say it whole. */
const SUBSECTION_INDENT = ' '

function onAddPointerdown(event: PointerEvent) {
  addPressClosedMenu = addMenuClosedBy === event
}

function toggleAddMenu(event: MouseEvent) {
  // A click the keyboard made carries no pointer detail, and no press came before it.
  const fromKey = event.detail === 0
  if (addMenu.value || (addPressClosedMenu && !fromKey)) {
    addPressClosedMenu = false
    addMenu.value = null
    return
  }
  const box = addButton.value?.getBoundingClientRect()
  addMenuFromKey.value = fromKey
  menuCurrent.value = props.currentSection()
  // Over the button, ending just above it: the composer sits at the foot of the column, and
  // a menu opened down would cover the control and the draft.
  addMenu.value = box ? { x: box.left, y: box.top - 4 } : { x: 0, y: 0 }
}

function closeAddMenu(cause?: Event) {
  addMenu.value = null
  addMenuClosedBy = cause ?? null
  // The menu gives focus back to what had it, inside the field.
  expectFocusReturn()
}

const addMenuItems = computed<MenuItem[]>(() => {
  const added = new Set(addedSections.value.map((section) => section.key))
  const current = menuCurrent.value
  return [
    {
      key: 'current',
      label: current ? t('chat.context.currentSectionNamed', { heading: current.plain }) : t('chat.context.currentSection'),
      icon: 'locate' as const,
      disabled: !current || (sectionsFull.value && !added.has(entryKey(current))),
    },
    ...props.outline.map((entry, index) => ({
      key: entryKey(entry),
      label: `${entry.level >= 3 ? SUBSECTION_INDENT : ''}${entry.plain}`,
      checked: added.has(entryKey(entry)),
      disabled: sectionsFull.value && !added.has(entryKey(entry)),
      separated: index === 0,
    })),
  ]
})

function onAddSelect(key: string) {
  const entry = key === 'current' ? menuCurrent.value : props.outline.find((candidate) => entryKey(candidate) === key)
  if (entry) {
    addEntry(entry)
  }
}

function onBlur() {
  focused.value = false
  menuForced.value = false
}

async function setQuote(text: string) {
  quote.value = text
  await nextTick()
  focus()
}

// --- commands ------------------------------------------------------------------------

/** The skill picked from the menu: a chip at the start of the field. */
const command = ref<CommandOption | null>(null)
const skills = ref<CommandOption[]>([])
/** The first list of skills has come back, or failed to: until then the menu says it is
 *  loading rather than listing nothing. */
const skillsLoaded = ref(false)
/** A fetch of the list is in flight. Enter and Tab then pick only a skill already listed
 *  and otherwise wait, so a command typed meanwhile is not sent as text. */
const skillsLoading = ref(false)
/** Numbers the fetches: only the latest may fill the list. */
let skillsRequest = 0
/** The command control opened the menu over a draft that is not a command. */
const menuForced = ref(false)
/** Escape closed it for the command being typed; the next one opens it again. */
const menuDismissed = ref(false)
const active = ref(0)

/** The reader's commands: their skills that can be run as `/name`, fetched each time the
 *  menu opens, since the Agent page may have changed them since. */
async function loadSkills() {
  const request = ++skillsRequest
  skillsLoading.value = true
  try {
    const listed: CommandOption[] = (await listSkills()).skills
      .filter((skill) => skill.user_invocable)
      .map((skill) => ({ name: skill.name, description: skill.description, hint: skill.argument_hint, group: 'skill' }))
    if (request === skillsRequest) {
      skills.value = listed
    }
  } catch (error) {
    if (request === skillsRequest) {
      localError.value = await resolve(error)
    }
  } finally {
    if (request === skillsRequest) {
      skillsLoading.value = false
      skillsLoaded.value = true
    }
  }
}

const query = computed(() => commandQuery(draft.value))
const menuOpen = computed(
  () => focused.value && !props.running && !command.value && !menuDismissed.value && (menuForced.value || query.value !== null),
)
const matches = computed(() => {
  const found = matchCommands(
    [
      ...skills.value,
      ...builtinCommands(props.conversationOpen, {
        newDescription: t('chat.commandNew'),
        compactDescription: t('chat.commandCompact'),
        compactHint: t('chat.commandCompactHint'),
      }),
    ],
    query.value ?? '',
  )
  // Grouped as the menu draws them: the skills, then the conversation's own.
  return [...found.filter((option) => option.group === 'skill'), ...found.filter((option) => option.group === 'builtin')]
})
/** The menu's sections, each under its label: the skills, then the conversation's own. */
const sections = computed(() =>
  (
    [
      { group: 'skill', label: t('chat.commandsSkills') },
      { group: 'builtin', label: t('chat.commandsConversation') },
    ] as const
  )
    .map((section) => ({ ...section, options: matches.value.filter((option) => option.group === section.group) }))
    .filter((section) => section.options.length),
)

function optionId(option: CommandOption): string {
  return `chat-command-${option.group}-${option.name}`
}

const activeId = computed(() => {
  const option = matches.value[active.value]
  return menuOpen.value && skillsLoaded.value && option ? optionId(option) : undefined
})

watch(menuOpen, (open) => {
  if (open) {
    active.value = 0
    void loadSkills()
  }
})

// The highlight starts on the first row whenever the list changes under it.
watch(matches, () => {
  active.value = 0
})

watch(draft, (value) => {
  if (commandQuery(value) === null) {
    menuDismissed.value = false
  }
})

function openCommands() {
  if (props.running) {
    return
  }
  if (!command.value) {
    if (!draft.value) {
      draft.value = '/'
    } else if (query.value === null) {
      menuForced.value = true
    }
    menuDismissed.value = false
  }
  focus()
}

function pick(option: CommandOption) {
  menuForced.value = false
  if (option.group === 'builtin' && option.name === NEW_COMMAND) {
    draft.value = ''
    emit('new-conversation')
    return
  }
  // A skill, or `/compact`: a chip, the textarea taking its arguments.
  command.value = option
  // The `/term` typed was the command; a draft the command control opened over stays as
  // its arguments.
  if (query.value !== null) {
    draft.value = ''
  }
  focus()
}

/** Enter sends and Shift+Enter breaks the line, as in Claude's own chat; an Enter that
 *  confirms an input method's composition only confirms it, and never picks a command. */
function onKeydown(event: KeyboardEvent) {
  if (event.isComposing || event.keyCode === 229) {
    return
  }
  if (menuOpen.value) {
    const count = matches.value.length
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault()
      if (count) {
        active.value = (active.value + (event.key === 'ArrowDown' ? 1 : -1) + count) % count
      }
      return
    }
    if ((event.key === 'Enter' && !event.shiftKey) || event.key === 'Tab') {
      const option = matches.value[active.value]
      // Anything but a listed skill may be standing in for one not listed yet.
      if (skillsLoading.value && option?.group !== 'skill') {
        event.preventDefault()
        return
      }
      if (option) {
        event.preventDefault()
        pick(option)
        return
      }
    }
    if (event.key === 'Escape') {
      // The menu's, not the viewer dialog's around the panel.
      event.preventDefault()
      event.stopPropagation()
      menuForced.value = false
      menuDismissed.value = true
      return
    }
  }
  if (event.key === 'Backspace' && command.value && !draft.value) {
    event.preventDefault()
    command.value = null
    return
  }
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault()
    void send()
  }
}

const placeholder = computed(() =>
  command.value ? command.value.hint || t('chat.argumentsPlaceholder') : t('chat.placeholder'),
)

// --- attachments -----------------------------------------------------------------------

interface PendingAttachment {
  key: number
  name: string
  size: number
  image: boolean
  /** The image's local preview: the thumbnail here, and in the thread until it reloads. */
  preview: string | null
  state: 'uploading' | 'ready' | 'refused'
  /** 0–100 while it uploads. */
  progress: number
  /** Why it was refused, as the reader reads it. */
  reason: string | null
  id: string | null
  /** A send in flight carries it: the server is claiming it, so it is neither removed
   *  nor deleted with a panel that goes meanwhile. */
  claimed: boolean
}

const pending = ref<PendingAttachment[]>([])
/** The requests in flight, by key — kept out of reactive state, they are not drawn. */
const uploads = new Map<number, AttachmentUpload>()
/** Every preview made, released when the panel goes: a sent one is still on screen. */
const previews = new Set<string>()
let unmounted = false
let nextKey = 0

const accept = computed(() => acceptList(props.limits?.attachment_extensions))

function find(key: number): PendingAttachment | undefined {
  return pending.value.find((item) => item.key === key)
}

function refusalText(refusal: AttachmentRefusal): string {
  switch (refusal.reason) {
    case 'type':
      return t('chat.attachmentUnsupported')
    case 'size':
      return t('chat.attachmentTooLarge', { size: formatBytes(refusal.max) })
    case 'count':
      return t('chat.attachmentTooMany', { count: refusal.max })
  }
}

/** Checks each file against the server's numbers, then uploads it at once, so a refusal
 *  shows before the message is sent (docs/chat.md § What a user message carries). */
function addFiles(files: readonly File[]) {
  if (props.running) {
    return
  }
  for (const file of files) {
    const attached = pending.value.filter((item) => item.state !== 'refused').length
    const refusal = refuseAttachment(file, props.limits, attached)
    const image = isImageFile(file)
    const preview = image && !refusal ? URL.createObjectURL(file) : null
    if (preview) {
      previews.add(preview)
    }
    const key = nextKey++
    pending.value.push({
      key,
      name: file.name,
      size: file.size,
      image,
      preview,
      state: refusal ? 'refused' : 'uploading',
      progress: 0,
      reason: refusal ? refusalText(refusal) : null,
      id: null,
      claimed: false,
    })
    if (!refusal) {
      upload(key, file)
    }
  }
}

function upload(key: number, file: File) {
  const request = uploadAttachment(file, (loaded, total) => {
    const item = find(key)
    if (item) {
      item.progress = Math.round((loaded / total) * 100)
    }
  })
  uploads.set(key, request)
  request.promise
    .then((attachment) => {
      const item = find(key)
      if (item) {
        // The server's reading of the file decides how it is sent and drawn.
        Object.assign(item, { state: 'ready', id: attachment.id, progress: 100, image: attachment.kind === 'image' })
      } else {
        // Removed while it uploaded, after the request could still be stopped.
        void deleteAttachment(attachment.id).catch(() => {})
      }
    })
    .catch(async (error: unknown) => {
      const item = find(key)
      // A removed one's abort is not a refusal.
      if (item) {
        Object.assign(item, { state: 'refused', reason: await resolve(error) })
      }
    })
    .finally(() => uploads.delete(key))
}

function remove(key: number) {
  const item = find(key)
  if (!item || item.claimed) {
    return
  }
  pending.value = pending.value.filter((entry) => entry.key !== key)
  uploads.get(key)?.abort()
  if (item.id) {
    // One left behind is swept with the other unsent uploads after a day.
    void deleteAttachment(item.id).catch(() => {})
  }
  if (item.preview) {
    URL.revokeObjectURL(item.preview)
    previews.delete(item.preview)
  }
}

function onPicked(event: Event) {
  const input = event.target as HTMLInputElement
  addFiles(Array.from(input.files ?? []))
  // The same file picked again is a new attachment, not "no change".
  input.value = ''
}

/** A pasted image is attached like a picked one; pasted text stays text. */
function onPaste(event: ClipboardEvent) {
  const images = Array.from(event.clipboardData?.files ?? []).filter((file) => isImageFile(file))
  if (!images.length) {
    return
  }
  if (!event.clipboardData?.types.includes('text/plain')) {
    event.preventDefault()
  }
  addFiles(images)
}

function fileMeta(item: PendingAttachment): string {
  if (item.state === 'refused') {
    return item.reason ?? ''
  }
  const extension = extensionOf(item.name).toUpperCase()
  const kind = extension.length <= 4 ? extension : ''
  const detail =
    item.state === 'uploading' ? t('chat.attachmentUploading', { percent: item.progress }) : formatBytes(item.size)
  return kind ? `${kind} · ${detail}` : detail
}

onBeforeUnmount(() => {
  unmounted = true
  for (const request of uploads.values()) {
    request.abort()
  }
  for (const item of pending.value) {
    if (item.id && !item.claimed) {
      void deleteAttachment(item.id).catch(() => {})
    }
  }
  for (const preview of previews) {
    URL.revokeObjectURL(preview)
  }
})

// --- sending ---------------------------------------------------------------------------

const canSend = computed(
  () =>
    !props.running &&
    !props.blocked &&
    !sending.value &&
    !selectionTooLong.value &&
    (draft.value.trim() !== '' || command.value !== null || pending.value.some((item) => item.state !== 'refused')),
)

/** Sends what the composer holds; whether the message was taken, so an ask that sends can
 *  tell a refusal, which leaves everything in place, from a send. */
async function send(): Promise<boolean> {
  if (!canSend.value) {
    return false
  }
  sending.value = true
  localError.value = null
  try {
    // Send waits for the uploads in flight; one that fails stays as a refused chip.
    await Promise.allSettled([...uploads.values()].map((request) => request.promise))
    if (unmounted) {
      // The panel went before the message did: the draft, its attachments with it, went too.
      return false
    }
    const ready = pending.value.filter((item) => item.state === 'ready')
    const text = draft.value.trim()
    const picked = command.value
    if (!text && !picked && !ready.length) {
      return false
    }
    const sentSections = addedSections.value
    const headingMax = props.limits?.section_heading_max_chars
    const payload: ComposerPayload = {
      text,
      selection: selection.value,
      // Added from the menu or asked for by the reader's view, a heading goes within the cap.
      sections: sentSections.map(({ heading, text: source }) => ({ heading: sectionHeading(heading, headingMax), text: source })),
      quote: quote.value,
      command: picked?.name ?? null,
      attachments: ready.map((item) => ({ id: item.id!, name: item.name, image: item.image, preview: item.preview })),
    }
    for (const item of ready) {
      item.claimed = true
    }
    let accepted = false
    try {
      accepted = await props.submit(payload)
    } finally {
      for (const item of ready) {
        item.claimed = false
      }
    }
    if (!accepted) {
      if (unmounted) {
        // Refused after the panel went: no draft holds them any more.
        for (const item of ready) {
          void deleteAttachment(item.id!).catch(() => {})
        }
      }
      return false
    }
    if (draft.value.trim() === text) {
      draft.value = ''
    }
    // By name: `setCommand` swaps a command for its listed entry once the list arrives, which
    // can land while the send is out.
    if (command.value?.name === picked?.name) {
      command.value = null
    }
    if (selection.value === payload.selection) {
      selection.value = null
      selectionAsked = false
    }
    addedSections.value = addedSections.value.filter((section) => !sentSections.includes(section))
    if (quote.value === payload.quote) {
      quote.value = null
    }
    const sent = new Set(ready.map((item) => item.key))
    pending.value = pending.value.filter((item) => !sent.has(item.key))
    return true
  } finally {
    sending.value = false
  }
}

// --- asked for by the reader's view --------------------------------------------------------

/** A command named from outside the menu: the listed skill once the list is in, so its
 *  chip carries its description and hint. */
function setCommand(name: string) {
  menuForced.value = false
  if (query.value !== null) {
    draft.value = ''
  }
  const listed = skills.value.find((option) => option.name === name)
  command.value = listed ?? { name, description: '', hint: '', group: 'skill' }
  if (!listed) {
    void loadSkills().then(() => {
      const found = skills.value.find((option) => option.name === name)
      if (found && command.value?.name === name) {
        command.value = found
      }
    })
  }
}

/**
 * Puts what the reader's view asks for into the composer — a passage as the selection, a
 * whole section, a command (docs/web.md § The agent panel, *Asked from the document*). With
 * `send` it goes at once only while nothing runs and the composer held nothing before it —
 * no draft, no command, no chip, quote or attachment — so a command acts on what the reader
 * just pointed at alone; otherwise, or when the send is refused, it waits for Send, with
 * focus, unless a turn is running.
 * A section the composer has no room for is not added, and then nothing is.
 */
async function ask(request: AskRequest): Promise<AskOutcome> {
  const clean =
    draft.value.trim() === '' &&
    command.value === null &&
    selection.value === null &&
    !addedSections.value.length &&
    quote.value === null &&
    !pending.value.length
  if (request.section) {
    const { heading, text } = request.section
    const entry = props.outline.find((candidate) => candidate.text === heading)
    const key = entry ? entryKey(entry) : `heading:${heading}`
    if (!addedSections.value.some((added) => added.key === key)) {
      if (sectionsFull.value) {
        return 'full'
      }
      addSection({
        key,
        heading,
        plain: entry?.plain ?? inlinePlain(heading),
        text: text === null ? null : sectionText(text, props.limits?.selection_max_chars),
      })
    }
  }
  if (request.selection !== undefined) {
    selection.value = request.selection.trim() || null
    selectionAsked = selection.value !== null
  }
  if (request.command) {
    setCommand(request.command)
  }
  await nextTick()
  if (request.send && clean && !props.running && !sending.value && !selectionTooLong.value && (await send())) {
    return 'sent'
  }
  if (props.running) {
    return 'kept'
  }
  expectFocusReturn()
  focus()
  return 'focused'
}

defineExpose({ addFiles, setQuote, focus, ask })
</script>

<template>
  <form
    ref="form"
    class="composer"
    @submit.prevent="send"
    @mousedown.capture="captureOnPointer"
    @focusin="onFocusIn"
  >
    <!-- The commands, over the field: the context menu's surface. A press inside it keeps
         the focus in the textarea, which is what keeps it open; the pointer takes the
         highlight only by moving, so a menu that opens under a resting cursor keeps its
         first row. -->
    <div v-if="menuOpen" class="menu" @mousedown.prevent>
      <p v-if="!skillsLoaded" class="menu-empty" role="status">{{ t('common.loading') }}</p>
      <div v-else id="chat-commands" class="menu-list" role="listbox" :aria-label="t('chat.commands')">
        <div
          v-for="section in sections"
          :key="section.group"
          class="menu-group"
          role="group"
          :aria-labelledby="`chat-commands-${section.group}`"
        >
          <p :id="`chat-commands-${section.group}`" class="menu-label">{{ section.label }}</p>
          <button
            v-for="option in section.options"
            :id="optionId(option)"
            :key="option.name"
            type="button"
            role="option"
            class="option"
            :class="{ active: matches[active] === option }"
            :aria-selected="matches[active] === option"
            tabindex="-1"
            @mousemove="active = matches.indexOf(option)"
            @click="pick(option)"
          >
            <span class="option-name mono">/{{ option.name }}</span>
            <span class="option-description">{{ option.description }}</span>
            <span v-if="option.hint" class="option-hint mono">{{ option.hint }}</span>
          </button>
        </div>
      </div>
      <p v-if="skillsLoaded && !matches.length" class="menu-empty" role="status">
        {{ skillsLoading ? t('common.loading') : t('chat.commandNoMatch') }}
      </p>
      <p class="menu-keys">{{ t('chat.commandKeys') }}</p>
    </div>

    <!-- The message field: what goes with the message, then its text, then the send. -->
    <div class="field" :class="{ disabled: running }">
      <!-- What goes with the message besides its text (docs/web.md § The agent panel,
           *Context chips*): the control that adds a section, the document the
           conversation is about, the selection read on the way in, the sections added. -->
      <div ref="contextRow" class="context" role="group" :aria-label="t('chat.context.label')">
        <button
          ref="addButton"
          type="button"
          class="context-add"
          aria-haspopup="menu"
          :aria-expanded="addMenu ? 'true' : 'false'"
          :aria-label="t('chat.context.add')"
          :title="t('chat.context.add')"
          :disabled="running"
          @pointerdown="onAddPointerdown"
          @click="toggleAddMenu"
        >
          <UiIcon name="plus" />
        </button>
        <ChatContextChip icon="file-text" :text="t('chat.context.document')" faint />
        <ChatContextChip
          v-if="selection"
          icon="text-quote"
          :text="selectionPreview"
          :refusal="
            selectionTooLong ? t('chat.context.selectionTooLong', { max: limits?.selection_max_chars }) : undefined
          "
          :remove-label="t('chat.context.removeSelection')"
          @remove="removeSelection"
        />
        <ChatContextChip
          v-for="section in addedSections"
          :key="section.key"
          icon="heading"
          :text="t('chat.context.section', { heading: section.plain })"
          :tooltip="section.plain"
          :remove-label="t('chat.context.removeSection', { heading: section.plain })"
          @remove="removeSection(section.key)"
        />
      </div>
      <UiContextMenu
        v-if="addMenu"
        :at="addMenu"
        :items="addMenuItems"
        :label="t('chat.context.add')"
        :focus-first="addMenuFromKey"
        above
        @select="onAddSelect"
        @close="closeAddMenu"
      />

      <p v-if="quote" class="chip">
        <UiIcon name="reply" class="chip-glyph" />
        <span class="chip-label">{{ t('chat.reply') }}</span>
        <span class="chip-text">{{ quote }}</span>
        <button type="button" class="chip-drop" :aria-label="t('chat.quoteDrop')" :title="t('chat.quoteDrop')" @click="quote = null">
          <UiIcon name="x" />
        </button>
      </p>

      <ul v-if="pending.length" class="attachments" :aria-label="t('chat.attachments')">
        <li
          v-for="item in pending"
          :key="item.key"
          :class="[item.image && item.state !== 'refused' ? 'thumb' : 'file', { refused: item.state === 'refused' }]"
        >
          <template v-if="item.image && item.state !== 'refused'">
            <span class="thumb-box" role="img" :aria-label="item.name" :title="item.name">
              <img v-if="item.preview" :src="item.preview" alt="" >
              <UiIcon v-else name="image" />
            </span>
            <span
              v-if="item.state === 'uploading'"
              class="progress over"
              role="progressbar"
              :aria-label="t('chat.attachmentProgress', { name: item.name })"
              aria-valuemin="0"
              aria-valuemax="100"
              :aria-valuenow="item.progress"
            >
              <span class="progress-fill" :style="{ width: `${item.progress}%` }" />
            </span>
            <button
              type="button"
              class="thumb-drop"
              :aria-label="t('chat.attachmentRemove', { name: item.name })"
              :title="t('chat.attachmentRemove', { name: item.name })"
              :disabled="item.claimed"
              @click="remove(item.key)"
            >
              <UiIcon name="x" />
            </button>
          </template>
          <template v-else>
            <span class="file-glyph" aria-hidden="true"><UiIcon :name="item.image ? 'image' : 'file-text'" /></span>
            <span class="file-body">
              <span class="file-name" :title="item.name">{{ item.name }}</span>
              <span class="file-meta tabular">{{ fileMeta(item) }}</span>
              <span
                v-if="item.state === 'uploading'"
                class="progress"
                role="progressbar"
                :aria-label="t('chat.attachmentProgress', { name: item.name })"
                aria-valuemin="0"
                aria-valuemax="100"
                :aria-valuenow="item.progress"
              >
                <span class="progress-fill" :style="{ width: `${item.progress}%` }" />
              </span>
            </span>
            <button
              type="button"
              class="file-drop"
              :aria-label="t('chat.attachmentRemove', { name: item.name })"
              :title="t('chat.attachmentRemove', { name: item.name })"
              :disabled="item.claimed"
              @click="remove(item.key)"
            >
              <UiIcon name="x" />
            </button>
          </template>
        </li>
      </ul>

      <div v-if="command" class="command">
        <span class="command-chip mono">
          /{{ command.name }}
          <button
            type="button"
            class="command-drop"
            :aria-label="t('chat.commandRemove')"
            :title="t('chat.commandRemove')"
            @click="command = null"
          >
            <UiIcon name="x" />
          </button>
        </span>
        <span class="command-description">{{ command.description }}</span>
      </div>

      <label for="chat-composer" class="sr-only">{{ t('chat.composer') }}</label>
      <textarea
        id="chat-composer"
        ref="textarea"
        v-model="draft"
        class="input"
        rows="2"
        :placeholder="placeholder"
        :disabled="running"
        aria-controls="chat-commands"
        :aria-activedescendant="activeId"
        @focus="focused = true"
        @blur="onBlur"
        @keydown="onKeydown"
        @paste="onPaste"
      />

      <div class="field-foot">
        <div class="field-tools">
          <button
            type="button"
            class="round ghost"
            :aria-label="t('chat.attachPicker')"
            :title="t('chat.attach')"
            :disabled="running"
            @click="fileInput?.click()"
          >
            <UiIcon name="paperclip" />
          </button>
          <input ref="fileInput" type="file" multiple hidden :accept="accept" @change="onPicked" >
          <button
            type="button"
            class="round ghost"
            :aria-label="t('chat.commandControl')"
            :title="t('chat.commandControl')"
            :disabled="running"
            @mousedown.prevent
            @click="openCommands"
          >
            <UiIcon name="square-slash" />
          </button>
        </div>
        <!-- The model pill and the context ring: the panel's, drawn here in the foot. -->
        <div class="field-extras">
          <slot name="foot" />
        </div>
        <button
          v-if="running"
          type="button"
          class="round stop"
          :aria-label="t('chat.stop')"
          :title="t('chat.stop')"
          :disabled="stopping"
          @click="emit('stop')"
        >
          <UiIcon name="square" />
        </button>
        <button
          v-else
          type="submit"
          class="round send"
          :aria-label="t('chat.send')"
          :title="t('chat.send')"
          :disabled="!canSend"
        >
          <UiSpinner v-if="sending" :size="13" />
          <UiIcon v-else name="arrow-up" />
        </button>
      </div>
    </div>
    <p v-if="error || localError" class="composer-error" role="alert">{{ error ?? localError }}</p>
  </form>
</template>

<style scoped>
.composer {
  position: relative;
  flex-shrink: 0;
  margin: 0;
  padding: var(--space-2) var(--space-4) var(--space-4);
}

/* --- the command menu: the context menu's surface, over the field ------------------- */

.menu {
  position: absolute;
  right: var(--space-4);
  bottom: calc(100% - var(--space-1));
  left: var(--space-4);
  z-index: 2;
  display: flex;
  flex-direction: column;
  max-height: 22rem;
  overflow-y: auto;
  padding: var(--space-1);
  border: 1px solid var(--menu-edge);
  border-radius: var(--radius-menu);
  background: var(--menu-surface);
  box-shadow: var(--shadow-menu);
}

.menu-label {
  margin: 0;
  padding: 6px var(--space-2) var(--space-1);
  color: var(--faint);
  font-size: var(--text-2xs);
  font-weight: var(--weight-semibold);
}

.menu-list,
.menu-group {
  display: flex;
  flex-direction: column;
}

.menu-group + .menu-group .menu-label {
  padding-top: var(--space-2);
}

.option {
  display: flex;
  flex-shrink: 0;
  align-items: center;
  gap: 10px;
  width: 100%;
  min-height: 32px;
  padding: var(--space-1) var(--space-2);
  border: 0;
  border-radius: var(--radius-menu-item);
  background: transparent;
  color: var(--muted);
  text-align: left;
  cursor: pointer;
}

/* The accent fills the highlighted row, as in the context menu. */
.option.active {
  background: var(--accent);
  color: var(--accent-fg);
}

.option-name {
  flex-shrink: 0;
  color: var(--ink);
}

.option.active .option-name {
  color: var(--accent-fg);
}

.option-description {
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: var(--text-xs);
}

.option-hint {
  flex-shrink: 0;
  font-size: var(--text-2xs);
}

.menu-empty {
  margin: 0;
  padding: var(--space-2);
  color: var(--faint);
  font-size: var(--text-xs);
}

.menu-keys {
  margin: var(--space-1) 0 0;
  padding: 6px var(--space-2) 2px;
  border-top: 1px solid var(--line);
  color: var(--faint);
  font-size: var(--text-2xs);
}

/* --- the field ------------------------------------------------------------------- */

/* Paper on the agent's glass card, as the system's message field: a hairline, a faint
   lift, the accent's ring while it has the caret. */
.field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: var(--space-3) var(--space-2) var(--space-2) var(--space-3);
  border: 1px solid var(--line-strong);
  border-radius: var(--radius-composer);
  background: var(--paper);
  box-shadow: 0 1px 2px var(--glass-shadow);
  transition:
    border-color var(--duration-fast) var(--ease),
    box-shadow var(--duration-fast) var(--ease);
}

.field:focus-within {
  border-color: var(--accent);
  box-shadow: 0 0 0 3px var(--accent-tint);
}

.field.disabled {
  background: var(--paper-sunken);
}

/* --- the context chips: a row at the top of the field --------------------------------- */

/* Set out a little past the textarea's text, so the chips' edges, not their glyphs, line
   up with it. */
.context {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  margin: -2px 0 var(--space-1) -2px;
}

.context-add {
  display: inline-flex;
  flex: none;
  align-items: center;
  justify-content: center;
  width: 24px;
  height: 24px;
  padding: 0;
  border: 0;
  border-radius: var(--radius-full);
  background: var(--fill);
  color: var(--muted);
  cursor: pointer;
  transition:
    background var(--duration-fast) var(--ease),
    color var(--duration-fast) var(--ease);
}

.context-add:hover:not(:disabled),
.context-add[aria-expanded='true'] {
  background: var(--fill-strong);
  color: var(--ink);
}

.context-add:disabled {
  cursor: not-allowed;
  opacity: 0.5;
}

.context-add :deep(svg) {
  width: 14px;
  height: 14px;
}

.context-add:focus-visible {
  outline-offset: 1px;
}

/* The reply's quote as a pill. */
.chip {
  display: flex;
  align-items: center;
  gap: 6px;
  align-self: flex-start;
  max-width: 100%;
  min-height: var(--control-height-xs);
  margin: 0;
  padding: 0 var(--space-1) 0 var(--space-2);
  border-radius: var(--radius-full);
  background: var(--fill);
  color: var(--muted);
  font-size: var(--text-2xs);
}

.chip-glyph {
  flex-shrink: 0;
  width: 12px;
  height: 12px;
}

.chip-label {
  flex-shrink: 0;
}

.chip-text {
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--ink-soft);
}

.chip-drop,
.command-drop,
.file-drop {
  display: inline-flex;
  flex-shrink: 0;
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
  transition: background var(--duration-fast) var(--ease);
}

.chip-drop:hover,
.command-drop:hover,
.file-drop:hover {
  background: var(--fill-strong);
}

.chip-drop :deep(svg),
.command-drop :deep(svg),
.file-drop :deep(svg) {
  width: 11px;
  height: 11px;
}

/* --- attachments ------------------------------------------------------------------- */

.attachments {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.thumb {
  position: relative;
  width: 56px;
  height: 56px;
}

.thumb-box {
  display: grid;
  place-items: center;
  width: 56px;
  height: 56px;
  overflow: hidden;
  border-radius: var(--radius-md);
  background: var(--fill);
  color: var(--faint);
}

.thumb-box img {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.thumb-box :deep(svg) {
  width: 18px;
  height: 18px;
}

.thumb-drop {
  position: absolute;
  top: -6px;
  right: -6px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 20px;
  height: 20px;
  padding: 0;
  border: 2px solid var(--paper);
  border-radius: var(--radius-full);
  background: var(--ink-soft);
  color: var(--paper);
  cursor: pointer;
}

.thumb-drop :deep(svg) {
  width: 10px;
  height: 10px;
}

/* While a send carries the attachment, it cannot be taken back. */
.thumb-drop:disabled,
.file-drop:disabled {
  cursor: not-allowed;
  opacity: 0.5;
}

.file {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  min-width: 0;
  max-width: 100%;
  height: 44px;
  padding: 0 var(--space-1) 0 6px;
  border: 1px solid var(--line);
  border-radius: var(--radius-md);
  background: var(--paper);
}

.file.refused {
  border-color: var(--danger-edge);
}

.file-glyph {
  display: grid;
  flex-shrink: 0;
  place-items: center;
  width: 30px;
  height: 30px;
  border-radius: var(--radius-sm);
  background: var(--fill);
  color: var(--faint);
}

.file-glyph :deep(svg) {
  width: 15px;
  height: 15px;
}

.file-body {
  display: flex;
  flex-direction: column;
  min-width: 0;
  max-width: 180px;
}

.file-name,
.file-meta {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  line-height: 1.35;
}

.file-name {
  color: var(--ink);
  font-size: var(--text-xs);
}

.file-meta {
  color: var(--faint);
  font-size: var(--text-2xs);
}

.refused .file-meta {
  color: var(--danger);
}

.file-drop {
  width: 24px;
  height: 24px;
}

.progress {
  display: block;
  height: 3px;
  margin-top: 3px;
  overflow: hidden;
  border-radius: var(--radius-full);
  background: var(--fill);
}

.progress.over {
  position: absolute;
  right: var(--space-1);
  bottom: var(--space-1);
  left: var(--space-1);
  margin: 0;
}

.progress-fill {
  display: block;
  height: 3px;
  background: var(--accent);
  transition: width var(--duration-fast) var(--ease);
}

/* --- the command chip and the textarea -------------------------------------------- */

.command {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  min-width: 0;
  padding-left: var(--space-1);
}

.command-chip {
  display: inline-flex;
  flex-shrink: 0;
  align-items: center;
  gap: var(--space-1);
  height: 24px;
  padding: 0 2px 0 var(--space-2);
  border-radius: var(--radius-full);
  background: var(--accent-tint);
  color: var(--accent);
}

.command-drop {
  width: 20px;
  height: 20px;
  color: currentcolor;
}

.command-drop:hover {
  background: var(--accent-tint);
}

.command-description {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--faint);
  font-size: var(--text-2xs);
}

.input {
  display: block;
  width: 100%;
  min-width: 0;
  margin: 0;
  padding: 0 0 0 var(--space-1);
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
  gap: var(--space-2);
}

.field-tools {
  display: flex;
  flex-shrink: 0;
  gap: 2px;
  margin-left: calc(var(--space-1) * -1);
}

/* Between the tools and the send button, taking what room the row has: the pill cuts its
   name rather than pushing the send button out. */
.field-extras {
  display: flex;
  flex: 1 1 auto;
  align-items: center;
  gap: var(--space-2);
  min-width: 0;
}

.round.send,
.round.stop {
  flex-shrink: 0;
  margin-left: auto;
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
  background: var(--fill);
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
  background: var(--fill-strong);
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
