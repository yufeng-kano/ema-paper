/** Locale-aware display formatting. The locale comes from the i18n catalog in use. */

export function formatDateTime(iso: string, locale: string): string {
  return new Intl.DateTimeFormat(locale, { dateStyle: 'medium', timeStyle: 'short' }).format(
    new Date(iso),
  )
}

/**
 * `MMM d, h:mm AM` — the compact stamp both list screens use in a column of rows.
 *
 * The year is dropped for anything from this year: it is redundant there and was the widest
 * thing in the column. It comes back for older rows, because a list is a count and not a
 * window — two rows a year apart must not read as the same day.
 */
export function formatShortDateTime(iso: string, locale: string): string {
  const at = new Date(iso)
  return new Intl.DateTimeFormat(locale, {
    year: at.getFullYear() === new Date().getFullYear() ? undefined : 'numeric',
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  }).format(at)
}

/** A date alone — the day a period resets — in the reader's own timezone. */
export function formatDate(iso: string, locale: string): string {
  return new Intl.DateTimeFormat(locale, { dateStyle: 'medium' }).format(new Date(iso))
}

/**
 * Pay's amount is in the currency's minor unit (`"1500"` of `USD` is $15.00), as Dodo and
 * Paddle both report it; `Intl` wants major units and knows each currency's exponent.
 */
export function formatMinorAmount(amount: string, currency: string, locale: string): string {
  const format = new Intl.NumberFormat(locale, { style: 'currency', currency })
  const digits = format.resolvedOptions().maximumFractionDigits ?? 2
  return format.format(Number(amount) / 10 ** digits)
}

export function formatCount(value: number, locale: string): string {
  return new Intl.NumberFormat(locale).format(value)
}

/**
 * A file's size in the reader's own locale. 1000-based, because the unit names `Intl`
 * prints ("kB", "MB") are the 1000-based ones — pairing them with 1024 steps would label
 * 1024 bytes "1 kB" and be wrong by the width of the mismatch.
 */
const BYTE_UNITS = ['byte', 'kilobyte', 'megabyte', 'gigabyte'] as const

export function formatBytes(bytes: number, locale: string): string {
  let value = Math.max(0, bytes)
  let step = 0
  while (value >= 1000 && step < BYTE_UNITS.length - 1) {
    value /= 1000
    step += 1
  }
  return new Intl.NumberFormat(locale, {
    style: 'unit',
    unit: BYTE_UNITS[step],
    unitDisplay: 'short',
    // Whole bytes, and one decimal only where it says something: 1.4 MB, but 240 MB.
    maximumFractionDigits: step === 0 || value >= 100 ? 0 : 1,
  }).format(value)
}
