/**
 * The slash menu's rules (docs/web.md § Editing, *The slash menu*): when a typed `/` opens it,
 * what the text after it matches, and the glyph each row draws. The labels come from the
 * catalog; the keywords here are matching data, in English and Chinese alike whatever the
 * interface language, so `/標` finds the headings and `/quote` the quote in either.
 */
import type { IconName } from './icons'
import type { BlockKindName } from './richdoc'

/** What a conversion row turns the empty paragraph into. */
export type SlashConversion = BlockKindName | 'table'

export interface SlashItem {
  key: SlashConversion
  icon: IconName
  /** The markdown it stands for, drawn in mono where a shortcut sits; empty for a paragraph. */
  markdown: string
  keywords: string[]
}

export const SLASH_CONVERSIONS: readonly SlashItem[] = [
  { key: 'paragraph', icon: 'type', markdown: '', keywords: ['paragraph', 'text', 'plain', '段落', '文字', '內文'] },
  { key: 'heading', icon: 'heading-2', markdown: '##', keywords: ['heading', 'h2', 'title', '標題', '大標題'] },
  { key: 'subheading', icon: 'heading-3', markdown: '###', keywords: ['subheading', 'heading', 'h3', '小標題', '標題', '子標題'] },
  { key: 'bulleted', icon: 'list', markdown: '-', keywords: ['bulleted', 'bullet', 'list', 'ul', '項目清單', '清單', '列表'] },
  { key: 'numbered', icon: 'list-ordered', markdown: '1.', keywords: ['numbered', 'ordered', 'list', 'ol', '編號清單', '清單', '編號'] },
  { key: 'quote', icon: 'text-quote', markdown: '>', keywords: ['quote', 'blockquote', 'citation', '引用', '引言'] },
  { key: 'formula', icon: 'sigma', markdown: '$$', keywords: ['formula', 'math', 'equation', 'latex', 'tex', '公式', '數學', '方程式'] },
  { key: 'code', icon: 'code', markdown: '```', keywords: ['code', 'codeblock', 'pre', '程式碼', '程式', '代碼'] },
  { key: 'table', icon: 'table', markdown: '', keywords: ['table', 'grid', '表格', '表'] },
]

/** The slash a reader types: ASCII, or the full-width one an input method commits. */
const SLASHES = ['/', '／']

/**
 * The filter a paragraph's text is while the menu is open: what follows its opening slash,
 * lower case. Null when the text does not start with a slash — the menu then closes.
 */
export function slashQuery(text: string): string | null {
  const first = text.charAt(0)
  return SLASHES.includes(first) ? text.slice(1).toLowerCase() : null
}

/** Whether a paragraph's text is a slash and nothing else: what opens the menu. */
export function opensSlash(text: string): boolean {
  return SLASHES.includes(text)
}

/** The conversions a filter matches: any keyword starting with it. */
export function matchSlash(items: readonly SlashItem[], query: string): SlashItem[] {
  const term = query.trim().toLowerCase()
  return term ? items.filter((item) => item.keywords.some((keyword) => keyword.startsWith(term))) : [...items]
}

/** A skill's glyph: the action's own where the viewer has one — the same glyph on every
 *  surface — and the composer's command glyph for any other skill. */
export function skillIcon(name: string): IconName {
  switch (name) {
    case 'translate':
      return 'languages'
    case 'fix-math':
      return 'square-function'
    case 'check-figures':
      return 'image'
    default:
      return 'square-slash'
  }
}
