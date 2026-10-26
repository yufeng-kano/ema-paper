import { createPackCheckout, getBillingCatalog, type CatalogPack } from '~/lib/api'
import { formatCount, formatMinorAmount, packValidity } from '~/lib/format'

/**
 * The first top-up pack on sale (docs/billing.md § Top-up packs) — read from the public
 * catalog on `load`, never assumed — and its checkout in this tab, the return landing on the
 * Usage section as a plan checkout's does. One place, so the Usage section, the credits
 * dialog and the composer's notice name the same pack the same way: its price as Pay gives
 * it, its credits and validity as this deployment configures them.
 */
export function useTopUp() {
  const { t, locale } = useI18n()
  const { resolve } = useApiError()

  const pack = ref<CatalogPack | null>(null)
  /** Whether the catalog answered, a pack or none; the callers show the pack's figures only then. */
  const loaded = ref(false)
  const buying = ref(false)
  const error = ref<string | null>(null)

  async function load(): Promise<CatalogPack | null> {
    try {
      // A pack is for sale only while Pay's checkout is open (`available`): with it
      // closed the catalog still lists the packs, and a checkout would be refused.
      const catalog = await getBillingCatalog()
      pack.value = catalog.available ? (catalog.packs[0] ?? null) : null
    } catch {
      pack.value = null
    } finally {
      loaded.value = true
    }
    return pack.value
  }

  const price = computed(() => (pack.value ? formatMinorAmount(pack.value.amount, pack.value.currency, locale.value) : ''))
  const credits = computed(() => (pack.value ? formatCount(pack.value.credits, locale.value) : ''))
  /** `12 months`, or the days when the validity is not a whole number of months. */
  const validity = computed(() => {
    if (!pack.value) {
      return ''
    }
    const { unit, n } = packValidity(pack.value.valid_days)
    return t(`topUp.${unit}`, { n: formatCount(n, locale.value) }, n)
  })

  /** Pay's checkout for the pack; the catalog is read first where it was not yet. */
  async function buy() {
    if (buying.value) {
      return
    }
    buying.value = true
    error.value = null
    try {
      const chosen = pack.value ?? (await load())
      if (!chosen) {
        error.value = t('topUp.notOnSale')
        return
      }
      const { url } = await createPackCheckout(chosen.pack_id)
      window.location.assign(url)
    } catch (failure) {
      error.value = await resolve(failure)
    } finally {
      buying.value = false
    }
  }

  return { pack, loaded, load, price, credits, validity, buy, buying, error }
}
