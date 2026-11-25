/**
 * What a block is called, in the reader's language (docs/web.md § Editing, *Moving blocks*):
 * the words `blockName` builds a handle's name from, and a block's kind as a menu's header
 * says it. The document's handles, the notes' and the agent panel's name blocks alike.
 */
import type { NameText } from '~/lib/blocks'
import { isHeading, type ResultBlock } from '~/lib/markdown'

export function useBlockNames() {
  const { t } = useI18n()

  /** The words a block's name is built from (`blockName`). */
  const nameText = computed<NameText>(() => ({
    formula: t('viewer.names.formula'),
    formulaTag: (tag) => t('viewer.names.formulaTag', { tag }),
    quoted: (words) => t('viewer.names.quoted', { words }),
    figure: (words) => t('viewer.names.figure', { name: t('viewer.names.quoted', { words }) }),
    figureBare: t('viewer.names.figureBare'),
    image: (words) => t('viewer.names.image', { name: t('viewer.names.quoted', { words }) }),
    imageBare: t('viewer.names.imageBare'),
  }))

  /** Block kinds as the menu's header and the handle's name say them. */
  function kindLabel(block: ResultBlock): string {
    return t(`viewer.block.kinds.${isHeading(block) ? 'h' : block.kind}`)
  }

  return { nameText, kindLabel }
}
