/**
 * The composer's decisions that need no DOM (docs/web.md § The agent panel): whether a
 * draft is a command being typed and which commands it matches, and whether a file may be
 * attached at all before a byte of it is sent.
 */

/** One row of the `/` menu. */
export interface CommandOption {
  name: string
  description: string
  /** What the command takes, shown at the row's end. */
  hint: string
  /** `skill` runs one of the reader's skills; `builtin` is the composer's own (`/new`,
   *  `/compact`). */
  group: 'skill' | 'builtin'
}

/** The composer's own commands (docs/web.md § The agent panel, *Commands*). */
export const NEW_COMMAND = 'new'
/** Sent as `command: compact`, its arguments the focus (docs/chat.md § Compaction). */
export const COMPACT_COMMAND = 'compact'

/** The builtin rows the menu lists under *Conversation*: `/new` always, `/compact` only with
 *  a conversation open — the draft has nothing to compact. The descriptions and hints are
 *  the caller's, read from the catalog. */
export function builtinCommands(
  conversationOpen: boolean,
  words: { newDescription: string; compactDescription: string; compactHint: string },
): CommandOption[] {
  const rows: CommandOption[] = [{ name: NEW_COMMAND, description: words.newDescription, hint: '', group: 'builtin' }]
  if (conversationOpen) {
    rows.push({ name: COMPACT_COMMAND, description: words.compactDescription, hint: words.compactHint, group: 'builtin' })
  }
  return rows
}

/** What follows the `/` while a command is being typed, lower case; null when the draft is
 *  not a command — it must start with `/` and hold no space yet. */
export function commandQuery(draft: string): string | null {
  if (!draft.startsWith('/')) {
    return null
  }
  const term = draft.slice(1)
  return /\s/.test(term) ? null : term.toLowerCase()
}

/** The commands a term matches: names that start with it first, then names or
 *  descriptions that merely contain it. */
export function matchCommands(options: readonly CommandOption[], term: string): CommandOption[] {
  if (!term) {
    return [...options]
  }
  const starts = options.filter((option) => option.name.startsWith(term))
  const contains = options.filter(
    (option) =>
      !option.name.startsWith(term) &&
      (option.name.includes(term) || option.description.toLowerCase().includes(term)),
  )
  return [...starts, ...contains]
}

/** A sent command's arguments: the reader's line with its `/name` taken off the front. */
export function commandArguments(text: string, command: string): string {
  const head = `/${command}`
  return text.startsWith(head) ? text.slice(head.length).trim() : text.trim()
}

/** The extension a file is judged by: lower case, no dot, empty when it has none. */
export function extensionOf(name: string): string {
  const dot = name.lastIndexOf('.')
  return dot <= 0 || dot === name.length - 1 ? '' : name.slice(dot + 1).toLowerCase()
}

function bare(extension: string): string {
  return extension.replace(/^\./, '').toLowerCase()
}

/** The file picker's `accept`, from the server's list; undefined accepts anything. */
export function acceptList(extensions: readonly string[] | undefined): string | undefined {
  if (!extensions?.length) {
    return undefined
  }
  return extensions.map((extension) => `.${bare(extension)}`).join(',')
}

const IMAGE_EXTENSIONS = new Set(['png', 'jpg', 'jpeg', 'webp', 'gif', 'heic', 'heif'])

/** An image is drawn as a thumbnail and sent as one; anything else is a file chip. */
export function isImageFile(file: { name: string; type: string }): boolean {
  return file.type.startsWith('image/') || IMAGE_EXTENSIONS.has(extensionOf(file.name))
}

export type AttachmentRefusal =
  | { reason: 'type' }
  | { reason: 'size'; max: number }
  | { reason: 'count'; max: number }

export interface AttachmentLimits {
  attachment_max_bytes?: number
  attachments_per_message?: number
  attachment_extensions?: string[]
}

/**
 * Why a file cannot be attached, or null when it can — checked against the server's own
 * numbers before it is uploaded. `attached` is how many the message already carries. A
 * limit the server did not send is not checked here; the upload's answer decides.
 */
export function refuseAttachment(
  file: { name: string; size: number },
  limits: AttachmentLimits | null | undefined,
  attached: number,
): AttachmentRefusal | null {
  const perMessage = limits?.attachments_per_message
  if (perMessage !== undefined && attached >= perMessage) {
    return { reason: 'count', max: perMessage }
  }
  const extensions = limits?.attachment_extensions
  if (extensions?.length && !extensions.map(bare).includes(extensionOf(file.name))) {
    return { reason: 'type' }
  }
  const max = limits?.attachment_max_bytes
  if (max !== undefined && file.size > max) {
    return { reason: 'size', max }
  }
  return null
}

/** Bytes in the unit a person reads a file's size in: KB under a megabyte, else MB. */
export function formatBytes(bytes: number): string {
  if (bytes >= 1024 * 1024) {
    const megabytes = bytes / 1024 / 1024
    return `${Number.isInteger(megabytes) ? megabytes : megabytes.toFixed(1)} MB`
  }
  return `${Math.max(1, Math.round(bytes / 1024))} KB`
}

/** An attachment as the message carries it: the id the upload returned, and what the
 *  thread draws for it until the record holds the message. */
export interface SentAttachment {
  id: string
  name: string
  image: boolean
  /** The local preview a thumbnail shows before the record's bytes can be asked for. */
  preview: string | null
}

/** A whole section as a message carries it: its heading as the document has it, and its
 *  markdown — null past the cap, so the agent reads it from the document instead. */
export interface SentSection {
  heading: string
  text: string | null
}

/** What the composer hands the panel to send (docs/chat.md § What a user message carries). */
export interface ComposerPayload {
  /** As typed; a command's arguments when `command` is set. */
  text: string
  selection: string | null
  sections: SentSection[]
  quote: string | null
  command: string | null
  attachments: SentAttachment[]
}

/**
 * A section's text as it is sent: whole, or null when it is longer than the server takes
 * (`limits.selection_max_chars`, counted in code points as the server counts) — a cut
 * section must never pass for a whole one (docs/chat.md § What a user message carries).
 * No cap from the server sends it whole and leaves the answer to the server.
 */
export function sectionText(source: string, max: number | undefined): string | null {
  return max !== undefined && [...source].length > max ? null : source
}

/**
 * A section's heading as it is sent: cut with an ellipsis to `limits.section_heading_max_chars`
 * code points, as the server counts — a paragraph a parse took for a heading must not make
 * the server refuse the whole message. It only names the section; its text goes whole or
 * not at all. No cap from the server sends it as it is.
 */
export function sectionHeading(heading: string, max: number | undefined): string {
  if (max === undefined || max < 1) {
    return heading
  }
  const chars = [...heading]
  return chars.length > max ? `${chars.slice(0, max - 1).join('')}…` : heading
}

/**
 * What the reader's view asks the agent panel to put in the composer: a passage, a whole
 * section, a command — and whether to send at once, which happens only while nothing is
 * running and nothing was being typed (the reader's draft is never sent for them).
 */
export interface AskRequest {
  selection?: string
  section?: SentSection
  command?: string
  send?: boolean
}

/**
 * What an ask did (docs/web.md § The agent panel, *Asked from the document*): went at once;
 * waits in the composer, which took focus; waits there without it, while a turn runs; or
 * nothing at all, because the composer already holds as many sections as a message takes.
 */
export type AskOutcome = 'sent' | 'focused' | 'kept' | 'full'

/** Whether a passage is past the server's cap, counted in code points as the server counts:
 *  shown, never cut and never sent (docs/web.md § The agent panel, *Asked from the document*). */
export function passageTooLong(passage: string | null, max: number | undefined): boolean {
  return passage !== null && max !== undefined && [...passage].length > max
}
