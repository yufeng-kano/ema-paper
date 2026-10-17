/**
 * A tool call read as a step of the thread (docs/web.md § The agent panel).
 *
 * The thread never shows a raw tool name or a command's boilerplate: `bash cd "/workspace/
 * files/…"; sed -n 16,60p doc.md` is *Read doc.md · lines 16–60*. Reading a command is
 * guesswork by nature, so it is deliberately shallow — the first program of the first
 * command, a handful of programs read closely — and anything it does not recognise is a
 * plain *Run* naming the program. The full command is always one click away.
 *
 * Pure and framework-free, so the parsing — the part with edges — is unit tested.
 */

export type StepKind =
  | 'read'
  | 'search'
  | 'list'
  | 'count'
  | 'run'
  | 'restart'
  | 'edit'
  | 'create'
  | 'page'
  | 'figure'
  | 'skill'
  | 'subagent'
  | 'tool'

export interface Step {
  kind: StepKind
  /** What the step acted on, drawn in mono: a file, a pattern, a program, a skill. */
  target: string
  /** The file a search ran over. */
  file?: string
  /** The lines a read covered; `to` null runs to the end of the file. */
  lines?: { from: number; to: number | null }
  /** A read of the file's last lines. */
  last?: number
  page?: number
  /** Where a shell command `cd`'d first, when it said so. */
  cwd?: string
}

/** The order a group's summary names its kinds in: what was looked at, then what changed. */
export const SUMMARY_ORDER: readonly StepKind[] = [
  'read',
  'search',
  'list',
  'count',
  'page',
  'figure',
  'skill',
  'subagent',
  'run',
  'restart',
  'edit',
  'create',
  'tool',
]

const EDITOR_TOOL = 'str_replace_based_edit_tool'

function text(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

function integer(value: unknown): number | undefined {
  const number = typeof value === 'number' ? value : typeof value === 'string' ? Number(value) : Number.NaN
  return Number.isInteger(number) ? number : undefined
}

export function basename(path: string): string {
  const trimmed = path.replace(/\/+$/, '')
  return trimmed.slice(trimmed.lastIndexOf('/') + 1) || trimmed
}

// --- shell commands ------------------------------------------------------------------------

/** The `cd "<the document's path>" &&` (or `;`) the agent opens nearly every command with. */
const CD_PREFIX = /^\s*cd\s+("(?:[^"\\]|\\.)*"|'[^']*'|[^\s;&|]+)\s*(?:&&|;)\s*/

function unquote(word: string): string {
  if (word.length >= 2 && ((word.startsWith('"') && word.endsWith('"')) || (word.startsWith("'") && word.endsWith("'")))) {
    return word.slice(1, -1)
  }
  return word
}

/** The command up to the first unquoted `|`, `&`, `;` or newline: its first program's part. */
export function firstCommand(command: string): string {
  let quote: string | null = null
  for (let index = 0; index < command.length; index += 1) {
    const char = command[index]!
    if (quote) {
      if (char === '\\' && quote === '"') {
        index += 1
      } else if (char === quote) {
        quote = null
      }
      continue
    }
    if (char === '\\') {
      index += 1
    } else if (char === '"' || char === "'") {
      quote = char
    } else if (char === '|' || char === '&' || char === ';' || char === '\n') {
      return command.slice(0, index)
    }
  }
  return command
}

/** Shell words, quotes removed: enough of the grammar to read a command, not to run one. */
export function shellWords(command: string): string[] {
  const words: string[] = []
  let current = ''
  let started = false
  let quote: string | null = null
  for (let index = 0; index < command.length; index += 1) {
    const char = command[index]!
    if (quote) {
      if (char === quote) {
        quote = null
      } else if (char === '\\' && quote === '"' && index + 1 < command.length) {
        current += command[index + 1]
        index += 1
      } else {
        current += char
      }
      continue
    }
    if (char === '"' || char === "'") {
      quote = char
      started = true
    } else if (char === '\\' && index + 1 < command.length) {
      current += command[index + 1]
      index += 1
      started = true
    } else if (/\s/.test(char)) {
      if (started) {
        words.push(current)
      }
      current = ''
      started = false
    } else {
      current += char
      started = true
    }
  }
  if (started) {
    words.push(current)
  }
  return words
}

/** Options that take the next word as their value, per program: the word is not a file. */
const VALUE_OPTIONS: Record<string, ReadonlySet<string>> = {
  grep: new Set(['-e', '-f', '-m', '-A', '-B', '-C', '--regexp', '--file', '--max-count']),
  rg: new Set(['-e', '-f', '-m', '-A', '-B', '-C', '-g', '-t', '-T', '--glob', '--type', '--max-count', '--regexp']),
  head: new Set(['-n', '-c', '--lines', '--bytes']),
  tail: new Set(['-n', '-c', '--lines', '--bytes']),
  sed: new Set(['-e', '-f', '--expression', '--file']),
  find: new Set(),
  wc: new Set(),
}

/** A redirection (`>`, `2>>`, `<<EOF`, `&>`): not an operand, and alone it takes the next
 *  word as its file. */
const REDIRECT = /^\d*(?:<<?<?|>>?|&>>?|>&|<&)/
const REDIRECT_ALONE = /^\d*(?:<<?<?|>>?|&>>?|>&|<&)$/

/** The words that are not options, nor an option's value, nor a redirection. */
function operands(program: string, args: string[]): string[] {
  const takesValue = VALUE_OPTIONS[program] ?? new Set<string>()
  const out: string[] = []
  for (let index = 0; index < args.length; index += 1) {
    const word = args[index]!
    if (word === '--') {
      out.push(...args.slice(index + 1).filter((rest) => !REDIRECT.test(rest)))
      break
    }
    if (REDIRECT.test(word)) {
      if (REDIRECT_ALONE.test(word)) {
        index += 1
      }
      continue
    }
    if (word === '-') {
      continue // standard input
    }
    if (word.startsWith('-') && word.length > 1) {
      if (takesValue.has(word)) {
        index += 1
      }
      continue
    }
    out.push(word)
  }
  return out
}

/** The value of an option given as `-n 5`, `-n5`, `-5` or `--lines=5`. */
function optionValue(args: string[], short: string, long: string): string | undefined {
  for (let index = 0; index < args.length; index += 1) {
    const word = args[index]!
    if (word === short || word === long) {
      return args[index + 1]
    }
    if (word.startsWith(`${long}=`)) {
      return word.slice(long.length + 1)
    }
    if (word.startsWith(short) && word.length > short.length) {
      return word.slice(short.length)
    }
    if (/^-\d+$/.test(word)) {
      return word.slice(1)
    }
  }
  return undefined
}

function readSed(args: string[], cwd: string | undefined): Step {
  if (args.some((word) => word === '-i' || word.startsWith('-i') || word === '--in-place')) {
    const files = operands('sed', args).slice(1)
    return { kind: 'edit', target: files[0] ?? 'sed', cwd }
  }
  const quiet = args.includes('-n') || args.includes('--quiet')
  const expressionAt = args.findIndex((word) => word === '-e' || word === '--expression')
  const words = operands('sed', args)
  const script = expressionAt !== -1 ? (args[expressionAt + 1] ?? '') : (words[0] ?? '')
  const files = expressionAt !== -1 ? words : words.slice(1)
  const print = /^(\d+)(?:,(\d+|\$))?p$/.exec(script.split(';')[0]!.trim())
  if (quiet && print && files[0]) {
    const from = Number(print[1])
    const to = print[2] === undefined ? from : print[2] === '$' ? null : Number(print[2])
    return { kind: 'read', target: files[0], lines: { from, to }, cwd }
  }
  return { kind: 'run', target: ['sed', ...files.slice(0, 1)].join(' '), cwd }
}

function readHead(args: string[], cwd: string | undefined): Step {
  const count = Number(optionValue(args, '-n', '--lines') ?? '10')
  const file = operands('head', args)[0]
  if (!file) {
    return { kind: 'run', target: 'head', cwd }
  }
  return { kind: 'read', target: file, lines: Number.isInteger(count) && count > 0 ? { from: 1, to: count } : undefined, cwd }
}

function readTail(args: string[], cwd: string | undefined): Step {
  const raw = optionValue(args, '-n', '--lines') ?? '10'
  const file = operands('tail', args)[0]
  if (!file) {
    return { kind: 'run', target: 'tail', cwd }
  }
  if (raw.startsWith('+')) {
    const from = Number(raw.slice(1))
    return { kind: 'read', target: file, lines: Number.isInteger(from) ? { from, to: null } : undefined, cwd }
  }
  const count = Number(raw)
  return { kind: 'read', target: file, last: Number.isInteger(count) && count > 0 ? count : undefined, cwd }
}

function readSearch(program: string, args: string[], cwd: string | undefined): Step {
  const key = program === 'rg' ? 'rg' : 'grep'
  const explicit = args.findIndex((word) => word === '-e' || word === '--regexp')
  const words = operands(key, args)
  const pattern = explicit !== -1 ? (args[explicit + 1] ?? '') : (words[0] ?? '')
  const files = explicit !== -1 ? words : words.slice(1)
  // A basic regular expression's alternation, `\|`, reads as the `|` it means.
  return { kind: 'search', target: pattern.replace(/\\\|/g, '|'), file: files[0], cwd }
}

/** A shell command as a step. */
export function readShell(command: string): Step {
  let rest = command.trim()
  let cwd: string | undefined
  const cd = CD_PREFIX.exec(rest)
  if (cd) {
    cwd = unquote(cd[1]!)
    rest = rest.slice(cd[0].length)
  }
  let words = shellWords(firstCommand(rest))
  // `LC_ALL=C grep …`: the assignments are not the program.
  while (words[0] && /^[A-Za-z_][A-Za-z0-9_]*=/.test(words[0])) {
    words = words.slice(1)
  }
  const program = basename(words[0] ?? '')
  const args = words.slice(1)
  switch (program) {
    case '':
      return { kind: 'run', target: rest.split('\n')[0] ?? '', cwd }
    case 'sed':
      return readSed(args, cwd)
    case 'head':
      return readHead(args, cwd)
    case 'tail':
      return readTail(args, cwd)
    case 'cat':
    case 'less':
    case 'more':
    case 'nl':
    case 'bat': {
      const file = operands(program, args)[0]
      return file ? { kind: 'read', target: file, cwd } : { kind: 'run', target: program, cwd }
    }
    case 'grep':
    case 'egrep':
    case 'fgrep':
    case 'rg':
      return readSearch(program, args, cwd)
    case 'ls':
    case 'tree':
    case 'find':
      return { kind: 'list', target: operands(program, args)[0] ?? '.', cwd }
    case 'wc': {
      const file = operands('wc', args)[0]
      return file ? { kind: 'count', target: file, cwd } : { kind: 'run', target: 'wc', cwd }
    }
    default: {
      // `python3 -c "…"`: the code is no name for the step; the program is.
      const operand = args.includes('-c') ? undefined : operands(program, args)[0]
      return { kind: 'run', target: operand ? `${program} ${operand}` : program, cwd }
    }
  }
}

// --- any tool call --------------------------------------------------------------------------

function readEditor(input: Record<string, unknown>): Step {
  const target = basename(text(input.path))
  switch (text(input.command)) {
    case 'view': {
      const range = Array.isArray(input.view_range) ? input.view_range : null
      const from = range ? integer(range[0]) : undefined
      const to = range ? integer(range[1]) : undefined
      return {
        kind: 'read',
        target,
        lines: from !== undefined ? { from, to: to === undefined || to === -1 ? null : to } : undefined,
      }
    }
    case 'create':
      return { kind: 'create', target }
    case 'str_replace':
    case 'insert':
    case 'undo_edit':
      return { kind: 'edit', target }
    default:
      return { kind: 'tool', target: EDITOR_TOOL }
  }
}

/** One tool call as a step: what the thread draws instead of the raw call. */
export function readStep(name: string, input: Record<string, unknown>): Step {
  switch (name) {
    case 'bash':
      return input.restart ? { kind: 'restart', target: '' } : readShell(text(input.command))
    case EDITOR_TOOL:
      return readEditor(input)
    case 'view_page':
      return { kind: 'page', target: '', page: integer(input.page) }
    case 'view_figure':
      return { kind: 'figure', target: '', page: integer(input.page) }
    case 'skill':
      return { kind: 'skill', target: text(input.name) }
    case 'subagent':
      return { kind: 'subagent', target: text(input.name) }
    default:
      return { kind: 'tool', target: name }
  }
}

// --- groups ------------------------------------------------------------------------------------

/**
 * Folds every run of consecutive items `inRun` accepts into one value `group` makes of it,
 * leaving the rest where they stand: the steps between two texts of an answer become one
 * step group.
 */
export function groupRuns<T, G>(
  items: readonly T[],
  inRun: (item: T) => boolean,
  group: (run: T[]) => G,
): Array<T | G> {
  const out: Array<T | G> = []
  let run: T[] = []
  for (const item of items) {
    if (inRun(item)) {
      run.push(item)
      continue
    }
    if (run.length) {
      out.push(group(run))
      run = []
    }
    out.push(item)
  }
  if (run.length) {
    out.push(group(run))
  }
  return out
}

/** How many steps of each kind, in the summary's order, kinds with none left out. */
export function countKinds(kinds: readonly StepKind[]): Array<{ kind: StepKind; count: number }> {
  const counts = new Map<StepKind, number>()
  for (const kind of kinds) {
    counts.set(kind, (counts.get(kind) ?? 0) + 1)
  }
  return SUMMARY_ORDER.filter((kind) => counts.has(kind)).map((kind) => ({ kind, count: counts.get(kind)! }))
}

// --- diffs -------------------------------------------------------------------------------------

export interface DiffLine {
  kind: 'added' | 'removed' | 'context' | 'hunk'
  text: string
}

/**
 * A unified diff as lines. The file header is dropped and the hunk markers kept as their
 * own kind so the card can mute them; everything else is an added, removed or context line.
 */
export function parseDiff(diff: string): DiffLine[] {
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
export function diffFromInput(input: Record<string, unknown>): DiffLine[] {
  const removed = text(input.old_str)
  const added = text(input.new_str ?? input.file_text)
  return [
    ...(removed ? removed.split('\n').map((line) => ({ kind: 'removed' as const, text: line })) : []),
    ...(added ? added.split('\n').map((line) => ({ kind: 'added' as const, text: line })) : []),
  ]
}

export function addedLines(lines: readonly DiffLine[]): number {
  return lines.filter((line) => line.kind === 'added').length
}

// --- outputs and durations ---------------------------------------------------------------------

/** A tool result's text, whatever shape it was stored in: images stand as a marker. */
export function resultText(content: unknown): string {
  if (typeof content === 'string') {
    return content
  }
  if (!Array.isArray(content)) {
    return ''
  }
  return content
    .map((block) => {
      if (block && typeof block === 'object' && 'type' in block) {
        if (block.type === 'text' && typeof (block as { text?: unknown }).text === 'string') {
          return (block as { text: string }).text
        }
        if (block.type === 'image') {
          return '[image]'
        }
      }
      return ''
    })
    .filter(Boolean)
    .join('\n')
}

/** Whole minutes and the seconds left, for "1 min 48 s". */
export function splitDuration(ms: number): { minutes: number; seconds: number } {
  const total = Math.max(0, Math.round(ms / 1000))
  return { minutes: Math.floor(total / 60), seconds: total % 60 }
}
