import { createPackCheckout, getBillingCatalog, type CatalogPack } from '~/lib/api'
import { formatCount, formatMinorAmount, packValidity } from '~/lib/format'

/**
 * The first top-up pack the public catalog lists (docs/billing.md § Top-up packs) — read on
 * `load`, never assumed — and its checkout in this tab, the return landing on the Usage
 * section as a plan checkout's does. One place, so the Usage section and its top-up dialog,
 * the credits dialog and the composer's notice name the same pack the same way: its price
 * as Pay gives it, its credits and validity as this deployment configures them.
 */
export function useTopUp() {
  const { t, locale } = useI18n()
  const { resolve } = useApiError()

  /** The pack as the catalog describes it, on sale or not: the Usage section says what a
   *  pack is while Pay's checkout is still closed. */
  const listed = ref<CatalogPack | null>(null)
  const onSale = ref(false)
  /** The pack a checkout can be started for: listed while Pay's checkout is open
   *  (`available`) — with it closed the catalog still lists the packs, and a checkout
   *  would be refused. */
  const pack = computed(() => (onSale.value ? listed.value : null))
  /** Whether the catalog answered, a pack or none; the callers show the pack's figures only then. */
  const loaded = ref(false)
  const buying = ref(false)
  const error = ref<string | null>(null)

  async function load(): Promise<CatalogPack | null> {
    try {
      const catalog = await getBillingCatalog()
      listed.value = catalog.packs[0] ?? null
      onSale.value = catalog.available
    } catch {
      listed.value = null
      onSale.value = false
    } finally {
      loaded.value = true
    }
    return pack.value
  }

  const price = computed(() =>
    listed.value ? formatMinorAmount(listed.value.amount, listed.value.currency, locale.value) : '',
  )
  const credits = computed(() => (listed.value ? formatCount(listed.value.credits, locale.value) : ''))
  /** `12 months`, or the days when the validity is not a whole number of months. */
  const validity = computed(() => {
    if (!listed.value) {
      return ''
    }
    const { unit, n } = packValidity(listed.value.valid_days)
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

  return { listed, pack, loaded, load, price, credits, validity, buy, buying, error }
}
