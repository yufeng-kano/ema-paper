/**
 * The languages the chat agent's two language choices offer on the Agent page — *Answer
 * language* and *Translate into* (docs/chat.md § Instructions, docs/web.md § Pages). A
 * fixed list of language tags, the tag being the stored value; each is named through the
 * catalog (`agents.languages.<tag>`), never here.
 *
 * The server accepts any well-formed tag, so a tag saved through the API that this list
 * does not hold is still the current choice: `languageOptions` keeps it, last, rather than
 * letting the dropdown show a value that is not the one in force.
 */

export const LANGUAGE_TAGS = ['zh-TW', 'zh-CN', 'en', 'ja', 'ko', 'fr', 'de', 'es'] as const

export type LanguageTag = (typeof LANGUAGE_TAGS)[number]

export function isListedLanguage(tag: string): tag is LanguageTag {
  return (LANGUAGE_TAGS as readonly string[]).includes(tag)
}

/** The tags a language dropdown lists: the fixed list, then the stored tag when the list
 *  lacks it. `null` (follow the interface language) is the dropdown's own first option. */
export function languageOptions(current: string | null | undefined): string[] {
  return current && !isListedLanguage(current) ? [...LANGUAGE_TAGS, current] : [...LANGUAGE_TAGS]
}
