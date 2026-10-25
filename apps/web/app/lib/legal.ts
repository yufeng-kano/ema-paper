/**
 * The public policy pages (docs/web.md § Pages): which there are, and when each text last
 * changed — the date under its title, so a reader can tell a revision. The text itself is the
 * catalog's, numbered sections `s1Title`/`s1Body` on, read until the next number is missing.
 */
export const LEGAL_DOCS = ['terms', 'privacy', 'refund'] as const

export type LegalDoc = (typeof LEGAL_DOCS)[number]

export const LEGAL_UPDATED: Record<LegalDoc, string> = {
  terms: '2026-10-25',
  privacy: '2026-10-25',
  refund: '2026-10-23',
}

/** The numbered section keys a catalog holds for a document, in order. */
export function legalSections(catalog: Record<string, unknown>): number[] {
  const found: number[] = []
  for (let n = 1; `s${n}Title` in catalog && `s${n}Body` in catalog; n += 1) {
    found.push(n)
  }
  return found
}
