<script setup lang="ts">
/**
 * The agent panel's composer (docs/web.md § The agent panel): one rounded field holding,
 * top to bottom, what goes with the message — the reply's quote, the document selection,
 * the attachments, a picked command — then the textarea, and a foot of the attach and
 * command controls with the send button at the right.
 *
 * It owns the draft and everything attached to it; the panel owns the conversation, so
 * sending is the panel's `submit`, which says whether the server took the message. Only
 * what was sent is cleared then: whatever the reader changed while it was in flight is the
 * next draft.
 */
import { deleteAttachment, listSkills, uploadAttachment, type AttachmentUpload, type UploadLimits } from '~/lib/api'
import {
  acceptList,
  commandQuery,
  extensionOf,
  formatBytes,
  isImageFile,
  matchCommands,
  refuseAttachment,
  type AttachmentRefusal,
  type CommandOption,
  type ComposerPayload,
} from '~/lib/composer'

const props = defineProps<{
  /** A turn is in flight: the composer waits, and Stop is the one control. */
  running: boolean
  stopping: boolean
  limits: UploadLimits | null
  /** The document pane's selection, read as the reader comes to the composer. */
  documentSelection: () => string | null
  /** Posts the message; true once the server accepted it. */
  submit: (payload: ComposerPayload) => Promise<boolean>
  error: string | null
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

function captureSelection() {
  selection.value = props.documentSelection() || null
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
  if (capturedByPointer || form.value?.contains(event.relatedTarget as Node | null)) {
    return
  }
  captureSelection()
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
/** The command control opened the menu over a draft that is not a command. */
const menuForced = ref(false)
/** Escape closed it for the command being typed; the next one opens it again. */
const menuDismissed = ref(false)
const active = ref(0)

/** The reader's commands: their skills that can be run as `/name`, fetched each time the
 *  menu opens, since the Agent page may have changed them since. */
async function loadSkills() {
  try {
    skills.value = (await listSkills()).skills
      .filter((skill) => skill.user_invocable)
      .map((skill) => ({ name: skill.name, description: skill.description, hint: skill.argument_hint, group: 'skill' }))
  } catch (error) {
    localError.value = await resolve(error)
  }
}

const query = computed(() => commandQuery(draft.value))
const menuOpen = computed(
  () => focused.value && !props.running && !command.value && !menuDismissed.value && (menuForced.value || query.value !== null),
)
const matches = computed(() => {
  const found = matchCommands(
    [...skills.value, { name: 'new', description: t('chat.commandNew'), hint: '', group: 'builtin' }],
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
  return menuOpen.value && option ? optionId(option) : undefined
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
  if (option.group === 'builtin') {
    draft.value = ''
    emit('new-conversation')
    return
  }
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
}

const pending = ref<PendingAttachment[]>([])
/** The requests in flight, by key — kept out of reactive state, they are not drawn. */
const uploads = new Map<number, AttachmentUpload>()
/** Every preview made, released when the panel goes: a sent one is still on screen. */
const previews = new Set<string>()
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
  if (!item) {
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
  for (const request of uploads.values()) {
    request.abort()
  }
  for (const item of pending.value) {
    if (item.id) {
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
    !sending.value &&
    (draft.value.trim() !== '' || command.value !== null || pending.value.some((item) => item.state !== 'refused')),
)

async function send() {
  if (!canSend.value) {
    return
  }
  sending.value = true
  localError.value = null
  try {
    // Send waits for the uploads in flight; one that fails stays as a refused chip.
    await Promise.allSettled([...uploads.values()].map((request) => request.promise))
    const ready = pending.value.filter((item) => item.state === 'ready')
    const text = draft.value.trim()
    const picked = command.value
    if (!text && !picked && !ready.length) {
      return
    }
    const payload: ComposerPayload = {
      text,
      selection: selection.value,
      quote: quote.value,
      command: picked?.name ?? null,
      attachments: ready.map((item) => ({ id: item.id!, name: item.name, image: item.image, preview: item.preview })),
    }
    if (!(await props.submit(payload))) {
      return
    }
    if (draft.value.trim() === text) {
      draft.value = ''
    }
    if (command.value === picked) {
      command.value = null
    }
    if (selection.value === payload.selection) {
      selection.value = null
    }
    if (quote.value === payload.quote) {
      quote.value = null
    }
    const sent = new Set(ready.map((item) => item.key))
    pending.value = pending.value.filter((item) => !sent.has(item.key))
  } finally {
    sending.value = false
  }
}

defineExpose({ addFiles, setQuote, focus })
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
      <div id="chat-commands" class="menu-list" role="listbox" :aria-label="t('chat.commands')">
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
      <p v-if="!matches.length" class="menu-empty">{{ t('chat.commandNoMatch') }}</p>
      <p class="menu-keys">{{ t('chat.commandKeys') }}</p>
    </div>

    <!-- The one rounded control in the app besides the context menu. -->
    <div class="field" :class="{ disabled: running }">
      <p v-if="quote" class="chip">
        <UiIcon name="reply" class="chip-glyph" />
        <span class="chip-label">{{ t('chat.reply') }}</span>
        <span class="chip-text">{{ quote }}</span>
        <button type="button" class="chip-drop" :aria-label="t('chat.quoteDrop')" :title="t('chat.quoteDrop')" @click="quote = null">
          <UiIcon name="x" />
        </button>
      </p>
      <p v-if="selection" class="chip">
        <span class="chip-text">{{ t('chat.selection', { count: selection.length }) }}</span>
        <button
          type="button"
          class="chip-drop"
          :aria-label="t('chat.dropSelection')"
          :title="t('chat.dropSelection')"
          @click="selection = null"
        >
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
  backdrop-filter: blur(24px) saturate(180%);
  -webkit-backdrop-filter: blur(24px) saturate(180%);
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

.field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: var(--space-3) var(--space-2) var(--space-2) var(--space-3);
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

/* What goes with the message as a pill: the quote, the selection. */
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
  background: var(--rail);
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
  background: var(--rail-active);
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
  border: 1px solid var(--line);
  border-radius: var(--radius);
  background: var(--rail);
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

.file {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  min-width: 0;
  max-width: 100%;
  height: 44px;
  padding: 0 var(--space-1) 0 6px;
  border: 1px solid var(--edge);
  border-radius: var(--radius);
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
  border-radius: var(--radius);
  background: var(--rail);
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
  border-radius: var(--radius);
}

.progress {
  display: block;
  height: 3px;
  margin-top: 3px;
  background: var(--rail-active);
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
  border-radius: var(--radius);
  background: var(--rail-active);
  color: var(--ink);
}

.command-drop {
  width: 20px;
  height: 20px;
  border-radius: var(--radius);
}

.command-drop:hover {
  background: var(--edge);
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
  justify-content: space-between;
}

.field-tools {
  display: flex;
  gap: 2px;
  margin-left: calc(var(--space-1) * -1);
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
