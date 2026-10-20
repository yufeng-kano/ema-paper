<script setup lang="ts">
/**
 * **What the agent receives** (docs/web.md § Pages, docs/api.md `GET
 * /api/agents/chat/context`): a non-modal drawer over the panel's right edge listing, in the
 * order a turn sends them, the system blocks the chat agent is given — rendered by the
 * runner's own code, so the reader sees the text the model sees rather than a description
 * of it — then the context block every message carries, then the document's facts. Each row
 * opens onto its text.
 *
 * No document is open on the Agent page, so the facts are listed by name, each filled in
 * per document, and the prompt shows its tokens as they are. Sizes are characters: counting
 * tokens would be an upstream call.
 */
import { getAgentContext, type AgentContext, type AgentContextFacts, type AgentContextPart } from '~/lib/api'

defineProps<{
  /** The drawer's element id, which the button that opens it names in `aria-controls`. */
  id: string
}>()

const emit = defineEmits<{ close: [] }>()

const { t, locale } = useI18n()
const { resolve } = useApiError()

/** The facts the prompt is rendered with, in the order the API lists them. */
const FACT_NAMES: readonly (keyof AgentContextFacts)[] = [
  'document_path',
  'page_count',
  'bbox_format',
  'parse_model',
  'network',
]

const headingId = useId()
const heading = ref<HTMLElement | null>(null)
const context = ref<AgentContext | null>(null)
const error = ref<string | null>(null)
/** The rows opened onto their text. */
const opened = reactive(new Set<string>())
/** Request generation: only the newest load may fill the drawer. */
let loads = 0

async function load() {
  const seq = ++loads
  context.value = null
  error.value = null
  try {
    const answer = await getAgentContext(locale.value)
    if (seq === loads) {
      context.value = answer
    }
  } catch (failure) {
    if (seq === loads) {
      error.value = await resolve(failure)
    }
  }
}

interface Row {
  key: string
  label: string
  /** The text the row opens onto; null for the facts, which are a list of their own. */
  text: string | null
  /** Its size in characters; null where there is no one text to measure. */
  chars: number | null
}

function partLabel(part: AgentContextPart): string {
  switch (part.kind) {
    case 'prompt':
      return part.own ? t('agents.receives.ownPrompt') : t('agents.receives.defaultPrompt')
    case 'preset':
      return t('agents.receives.preset')
    case 'instructions':
      return t('agents.receives.instructions')
    case 'catalogue':
      return part.subagents
        ? t('agents.receives.catalogueSubagents', { skills: part.skills ?? 0, subagents: part.subagents })
        : t('agents.receives.catalogue', { skills: part.skills ?? 0 })
    case 'memory':
      return part.paused ? t('agents.receives.memoryPaused') : t('agents.receives.memory', { count: part.count ?? 0 })
  }
  // A block a newer API sends that this page does not know yet: listed by its kind.
  return String((part as { kind: string }).kind)
}

/** The system blocks as sent, then the turn block, then the facts — a part that is not
 *  sent is not in the answer, so it is not listed. */
const rows = computed<Row[]>(() => {
  const answer = context.value
  if (!answer) {
    return []
  }
  return [
    ...answer.parts.map((part, index) => ({
      key: `${part.kind}-${index}`,
      label: partLabel(part),
      text: part.text,
      chars: part.chars,
    })),
    { key: 'turn', label: t('agents.receives.turn'), text: answer.turn.text, chars: answer.turn.chars },
    { key: 'facts', label: t('agents.receives.facts'), text: null, chars: null },
  ]
})

function toggle(key: string) {
  if (opened.has(key)) {
    opened.delete(key)
  } else {
    opened.add(key)
  }
}

/** A fact's value, or — on the Agent page, where no document is open — its token. */
function factValue(name: keyof AgentContextFacts): string {
  return context.value?.facts?.[name] ?? `{${name}}`
}

function size(chars: number): string {
  return t('agents.receives.chars', { n: chars.toLocaleString(locale.value) }, chars)
}

/** The drawer's own Escape: it closes the drawer and goes no further. */
function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') {
    event.stopPropagation()
    emit('close')
  }
}

watch(locale, () => void load())

onMounted(async () => {
  void load()
  await nextTick()
  heading.value?.focus()
})

onBeforeUnmount(() => {
  loads += 1
})
</script>

<template>
  <aside :id="id" class="drawer" :aria-labelledby="headingId" @keydown="onKeydown">
    <div class="head">
      <h2 :id="headingId" ref="heading" class="title" tabindex="-1">{{ t('agents.receives.title') }}</h2>
      <UiButton class="close" variant="ghost" size="xs" icon-only :label="t('common.close')" @click="emit('close')">
        <template #icon><UiIcon name="close" /></template>
      </UiButton>
    </div>

    <div class="body">
      <UiBanner v-if="error" tone="error">{{ error }}</UiBanner>
      <UiSkeleton v-else-if="!context" :rows="5" />
      <ol v-else class="rows">
        <li v-for="(row, index) in rows" :key="row.key">
          <button
            type="button"
            class="row"
            :aria-expanded="opened.has(row.key)"
            :aria-controls="`${id}-${row.key}`"
            @click="toggle(row.key)"
          >
            <span class="number tabular" aria-hidden="true">{{ index + 1 }}</span>
            <span class="label">{{ row.label }}</span>
            <span v-if="row.chars !== null" class="size tabular">{{ size(row.chars) }}</span>
            <UiIcon :name="opened.has(row.key) ? 'chevron-down' : 'chevron-right'" class="chevron" />
          </button>
          <div v-show="opened.has(row.key)" :id="`${id}-${row.key}`" class="detail">
            <pre v-if="row.text !== null" class="text mono">{{ row.text }}</pre>
            <template v-else>
              <dl class="facts">
                <div v-for="name in FACT_NAMES" :key="name" class="fact">
                  <dt>{{ t(`agents.receives.factNames.${name}`) }}</dt>
                  <dd class="mono">{{ factValue(name) }}</dd>
                </div>
              </dl>
              <p v-if="!context?.facts" class="facts-note">{{ t('agents.receives.factsNote') }}</p>
            </template>
          </div>
        </li>
      </ol>
    </div>
  </aside>
</template>

<style scoped>
/* Over the panel's right edge, not beside it: the page behind stays where it was, and the
   drawer reads as a look at what the controls produce rather than a step away from them. */
.drawer {
  position: absolute;
  inset: 0 0 0 auto;
  z-index: 5;
  display: flex;
  flex-direction: column;
  width: 320px;
  max-width: 100%;
  background: var(--paper);
  border-left: 1px solid var(--hair);
  box-shadow: var(--shadow-dialog);
}

/* Below the breakpoint the region is no longer a bounded box but part of one long scroll,
   so the drawer holds to the viewport instead — under the sidebar's own drawer and any
   dialog. */
@media (max-width: 900px) {
  .drawer {
    position: fixed;
    z-index: 30;
  }
}

.head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
  flex-shrink: 0;
  padding: var(--space-5) var(--space-4) var(--space-3) var(--space-6);
  border-bottom: 1px solid var(--hair);
}

.title {
  min-width: 0;
  font-size: var(--display-xs);
  line-height: 1.3;
  letter-spacing: normal;
}

.close :deep(svg) {
  width: 15px;
  height: 15px;
}

.body {
  flex: 1 1 auto;
  min-height: 0;
  overflow: auto;
  overscroll-behavior: contain;
  padding: var(--space-2) var(--space-2) var(--space-2) var(--space-4);
}

.rows {
  margin: 0;
  padding: 0;
  list-style: none;
}

/* A list row that opens: the rail's hover, a number, the label, its size, a chevron. */
.row {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  width: 100%;
  min-height: var(--control-height);
  padding: var(--space-1) var(--space-2);
  border: 0;
  border-radius: var(--radius);
  background: transparent;
  color: var(--ink);
  font-size: var(--text-sm);
  text-align: left;
  cursor: pointer;
  transition: background var(--duration-fast) var(--ease);
}

.row:hover {
  background: var(--rail);
}

.number {
  flex-shrink: 0;
  min-width: var(--space-4);
  color: var(--faint);
  font-size: var(--text-xs);
}

.label {
  flex: 1 1 auto;
  min-width: 0;
}

.size {
  flex-shrink: 0;
  color: var(--faint);
  font-size: var(--text-xs);
}

.chevron {
  flex-shrink: 0;
  width: 14px;
  height: 14px;
  color: var(--faint);
}

.detail {
  padding: var(--space-1) var(--space-2) var(--space-3);
}

/* The text as the model gets it: kept line for line, wrapped rather than scrolled sideways,
   and scrolling inside its own box past a screenful. */
.text {
  max-height: 20rem;
  margin: 0;
  padding: var(--space-3);
  overflow: auto;
  overscroll-behavior: contain;
  border: 1px solid var(--line-soft);
  border-radius: var(--radius);
  background: var(--paper-sunken);
  color: var(--ink-soft);
  font-size: var(--text-2xs);
  line-height: 1.6;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.facts {
  display: grid;
  gap: var(--space-2);
  margin: 0;
}

.fact {
  display: grid;
  gap: 2px;
}

.fact dt {
  color: var(--muted);
  font-size: var(--text-xs);
}

.fact dd {
  margin: 0;
  color: var(--ink-soft);
  font-size: var(--text-2xs);
  overflow-wrap: anywhere;
}

.facts-note {
  margin-top: var(--space-3);
  color: var(--faint);
  font-size: var(--text-xs);
}
</style>
