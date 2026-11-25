/**
 * The public policy pages (docs/web.md § Pages): which there are, and when each text last
 * changed — the date under its title, so a reader can tell a revision. The text itself is the
 * catalog's, numbered sections `s1Title`/`s1Body` on, read until the next number is missing.
 */
export const LEGAL_UPDATED = {
  terms: '2026-10-26',
  privacy: '2026-10-27',
  refund: '2026-10-26',
} satisfies Record<string, string>

export type LegalDoc = keyof typeof LEGAL_UPDATED

/** The numbered sections a document's catalog holds, in order, read through `has`, which says
 *  whether a key (`s1Title`, `s1Body`, …) is there: the walk stops at the first number
 *  missing its title or its body. */
export function legalSections(has: (key: string) => boolean): number[] {
  const found: number[] = []
  for (let n = 1; has(`s${n}Title`) && has(`s${n}Body`); n += 1) {
    found.push(n)
  }
  return found
}
