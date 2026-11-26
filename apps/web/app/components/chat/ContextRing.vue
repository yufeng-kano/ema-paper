<script setup lang="ts">
/**
 * The composer foot's context ring and its card (docs/web.md § The agent panel, *The model
 * pill and the context ring*): a small ring filled to the open conversation's context over
 * its model's window — empty on the draft, and after a compaction while the size is
 * unknown — in the accent to 70 %, the warning hue to 90 % and the danger hue past it. A
 * click opens a card on the context card's surface: the tokens and the percentage, the
 * cache's state — warm with the minutes left, counted down every half minute; expired with
 * what a cold next message re-reads; no cache on this model — the **Compact idle
 * conversations** switch, and **Compact now**, which the panel sends as `/compact`.
 */
import { CHAT_AGENT, putAgentSettings, type AgentSettings, type Conversation, type SavedAgentSettings } from '~/lib/api'
import { cacheState, contextFill, contextLine, formatTokens, ringLevel } from '~/lib/context'
import { formatCount } from '~/lib/format'
import { modalPanelKey, placeCard } from '~/lib/menu'

/** The ring's circumference: a 20-unit box, a radius of 8. */
const RADIUS = 8
const CIRCUMFERENCE = 2 * Math.PI * RADIUS
/** How often the card reads the clock again. */
const TICK_MS = 30_000

const props = defineProps<{
  /** The open conversation as the panel last read it; null on the draft. */
  conversation: Conversation | null
  /** The chat agent's settings as last read; null until they are. */
  settings: AgentSettings | null
  /** Whether **Compact now** may send: a conversation open, no turn in flight, and an
   *  answer since the last summary (`compactable`). */
  canCompact: boolean
}>()

const emit = defineEmits<{
  saved: [settings: SavedAgentSettings]
  compact: []
  error: [message: string]
}>()

const { t, locale } = useI18n()
const { resolve } = useApiError()
const modalPanel = inject(modalPanelKey, null)
const titleId = useId()

const button = ref<HTMLButtonElement | null>(null)
const card = ref<HTMLElement | null>(null)
const open = ref(false)
const position = ref({ x: -9999, y: -9999 })
const now = ref(Date.now())
let ticker: ReturnType<typeof setInterval> | null = null

// --- what it reads --------------------------------------------------------------------------

const fill = computed(() => contextFill(props.conversation))
const level = computed(() => ringLevel(fill.value))
const line = computed(() => contextLine(props.conversation))
const dash = computed(() => `${fill.value * CIRCUMFERENCE} ${CIRCUMFERENCE}`)
const cache = computed(() => cacheState(props.conversation, now.value))

const cacheText = computed(() => {
  const state = cache.value
  if (!state) {
    return null
  }
  switch (state.kind) {
    case 'warm':
      return t('chat.ring.cacheWarm', { minutes: state.minutesLeft })
    case 'expired':
      return state.recacheCredits === null
        ? t('chat.ring.cacheExpiredBare')
        : t('chat.ring.cacheExpired', { n: formatCount(state.recacheCredits, locale.value) }, state.recacheCredits)
    default:
      return t('chat.ring.cacheNone')
  }
})

const ringLabel = computed(() => (line.value ? t('chat.ring.labelFilled', { fill: line.value }) : t('chat.ring.label')))

// --- the idle switch -----------------------------------------------------------------------

const idleOn = computed(() => props.settings?.idle_compact ?? true)
const idlePending = ref(false)

async function toggleIdle(value: boolean) {
  if (idlePending.value) {
    return
  }
  idlePending.value = true
  try {
    emit('saved', await putAgentSettings(CHAT_AGENT, { idle_compact: value }))
  } catch (error) {
    emit('error', await resolve(error))
  } finally {
    idlePending.value = false
  }
}

// --- the card ------------------------------------------------------------------------------

/** Over the ring where there is room — the composer sits at the foot of the column — else
 *  under it; kept inside the window. */
function place() {
  const element = card.value
  const anchor = button.value
  if (!element || !anchor) {
    return
  }
  position.value = placeCard(
    anchor.getBoundingClientRect(),
    { width: element.offsetWidth, height: element.offsetHeight },
    { width: window.innerWidth, height: window.innerHeight },
    { prefer: 'above' },
  )
}

const floating = useFloatingCard({ card, anchor: () => button.value, place, close })

function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') {
    // The card, not the dialog it floats in.
    event.stopPropagation()
    close()
    button.value?.focus({ preventScroll: true })
  }
}

async function show() {
  open.value = true
  now.value = Date.now()
  ticker = setInterval(() => {
    now.value = Date.now()
  }, TICK_MS)
  await nextTick()
  place()
  card.value?.focus()
  floating.start()
}

function close() {
  if (!open.value) {
    return
  }
  open.value = false
  if (ticker !== null) {
    clearInterval(ticker)
    ticker = null
  }
  floating.stop()
}

function toggle() {
  if (open.value) {
    close()
  } else {
    void show()
  }
}

/** The thread switched under it: the card is about another conversation now. */
watch(
  () => props.conversation?.id ?? null,
  () => close(),
)

onBeforeUnmount(close)
</script>

<template>
  <button
    ref="button"
    type="button"
    class="ring"
    :class="level"
    :title="line ?? t('chat.ring.empty')"
    :aria-label="ringLabel"
    aria-haspopup="dialog"
    :aria-expanded="open ? 'true' : 'false'"
    @click="toggle"
  >
    <svg viewBox="0 0 20 20" aria-hidden="true">
      <circle class="track" cx="10" cy="10" :r="RADIUS" />
      <circle class="fill" cx="10" cy="10" :r="RADIUS" :stroke-dasharray="dash" transform="rotate(-90 10 10)" />
    </svg>
  </button>
  <Teleport :to="modalPanel ?? 'body'">
    <div
      v-if="open"
      ref="card"
      class="card"
      role="dialog"
      tabindex="-1"
      :aria-labelledby="titleId"
      :style="{ left: `${position.x}px`, top: `${position.y}px` }"
      @keydown="onKeydown"
      @focusout="floating.onFocusout"
    >
      <p :id="titleId" class="card-title">{{ t('chat.ring.title') }}</p>
      <p class="card-line tabular" :class="level">{{ line ?? t('chat.ring.empty') }}</p>
      <p v-if="conversation?.context_window && !line" class="card-note">
        {{ t('chat.ring.window', { window: formatTokens(conversation.context_window) }) }}
      </p>
      <p v-if="cacheText" class="card-line" :class="{ danger: cache?.kind === 'expired' }">{{ cacheText }}</p>
      <UiSwitch class="card-switch" :model-value="idleOn" :busy="idlePending" @update:model-value="toggleIdle">
        {{ t('agents.idleCompact.label') }}
      </UiSwitch>
      <p class="card-note">{{ t('agents.idleCompact.note') }}</p>
      <UiButton size="sm" variant="secondary" :disabled="!canCompact" @click="emit('compact')">
        {{ t('chat.ring.compactNow') }}
      </UiButton>
    </div>
  </Teleport>
</template>

<style scoped>
/* A round control the size of the foot's others, with the ring drawn inside it. */
.ring {
  display: inline-flex;
  flex-shrink: 0;
  align-items: center;
  justify-content: center;
  width: var(--control-height-sm);
  height: var(--control-height-sm);
  padding: 0;
  border: 0;
  border-radius: var(--radius-full);
  background: transparent;
  color: var(--accent);
  cursor: pointer;
  transition: background var(--duration-fast) var(--ease);
}

.ring:hover,
.ring[aria-expanded='true'] {
  background: var(--fill);
}

.ring svg {
  width: 16px;
  height: 16px;
}

.ring .track {
  fill: none;
  stroke: var(--fill-strong);
  stroke-width: 2.5;
}

.ring .fill {
  fill: none;
  stroke: currentcolor;
  stroke-width: 2.5;
  stroke-linecap: round;
  transition: stroke-dasharray var(--duration-slow) var(--ease);
}

/* The warning and danger hues: the fill's, and the card's figure. */
.ring.warning,
.card-line.warning {
  color: var(--tint-orange);
}

.ring.danger,
.card-line.danger {
  color: var(--danger);
}

/* The context card's surface (chat/ContextCard.vue), drawn here in this card's own scoped
   rules: a little narrower, and short enough never to scroll. */
.card {
  position: fixed;
  z-index: 90;
  display: flex;
  flex-direction: column;
  gap: var(--space-2);
  width: max-content;
  min-width: 220px;
  max-width: min(300px, calc(100vw - 16px));
  padding: var(--space-3);
  border: 1px solid var(--menu-edge);
  border-radius: var(--radius-menu);
  background: var(--menu-surface);
  box-shadow: var(--shadow-menu);
  color: var(--ink-soft);
  font-size: var(--text-xs);
  line-height: 1.5;
  outline: none;
  animation: card-in var(--duration-fast) var(--ease-enter);
}

@keyframes card-in {
  from {
    opacity: 0;
    transform: scale(0.96);
  }
}

@media (prefers-reduced-motion: reduce) {
  .card {
    animation: none;
  }
}

.card p {
  margin: 0;
}

.card-title {
  color: var(--faint);
}

.card-line {
  color: var(--ink);
}

.card-note {
  color: var(--muted);
}

/* The capsule's words line up with the card's text. */
.card-switch {
  align-self: flex-start;
  margin-top: var(--space-1);
  margin-left: calc(-1 * var(--space-3));
}

.card > :last-child {
  align-self: flex-start;
  margin-top: var(--space-1);
}
</style>
