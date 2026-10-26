<script setup lang="ts">
/**
 * The composer foot's pill naming the conversation's model and effort (docs/web.md § The
 * agent panel, *The model pill and the context ring*): the catalog name of the open
 * conversation's model — of the agent's saved model on the draft — and the effort's label.
 * It opens the context menu of the offered models, the current one checked, and under a
 * separator the effort's submenu. A model picked becomes the agent's, so new conversations
 * start on it, and — with a conversation open and no turn in flight — the open
 * conversation's too; an effort picked is the agent's, read by the next turn. The pill
 * says the new name once the saves are answered, and the old one again when one is
 * refused, with the reason under the composer.
 */
import {
  AGENT_EFFORTS,
  listAgentModels,
  putAgentSettings,
  updateConversation,
  type AgentEffort,
  type AgentSettings,
  type ConnectionModel,
  type Conversation,
  type SavedAgentSettings,
} from '~/lib/api'
import type { MenuItem, Point } from '~/lib/menu'

const CHAT_AGENT = 'chat'
const DEFAULT_EFFORT: AgentEffort = 'medium'
const MODEL_KEY = 'model:'
const EFFORT_KEY = 'effort:'

const props = defineProps<{
  /** The open conversation; null on the draft. */
  conversation: Conversation | null
  /** The chat agent's settings as last read; null until they are. */
  settings: AgentSettings | null
  /** A turn is in flight: the models are listed but disabled, since a conversation
   *  cannot move mid-turn. */
  running: boolean
}>()

const emit = defineEmits<{
  saved: [settings: SavedAgentSettings]
  moved: [conversation: Conversation]
  error: [message: string]
}>()

const { t } = useI18n()
const { resolve } = useApiError()

const button = ref<HTMLButtonElement | null>(null)
const menu = ref<Point | null>(null)
const menuFromKey = ref(false)
/** The press that closed the menu: a click on the pill right after it reopens nothing. */
let closedBy: Event | null = null
let pressClosedMenu = false

// --- the offered models --------------------------------------------------------------------

const models = ref<ConnectionModel[] | null>(null)
const modelsError = ref<string | null>(null)
let modelsSeq = 0

/** Read when the pill mounts, for the name, and again whenever the menu opens. */
async function loadModels() {
  const seq = ++modelsSeq
  modelsError.value = null
  try {
    const data = (await listAgentModels(CHAT_AGENT)).data
    if (seq === modelsSeq) {
      models.value = data
    }
  } catch (error) {
    if (seq === modelsSeq) {
      modelsError.value = await resolve(error)
    }
  }
}

onMounted(loadModels)

/** The model the pill names: the conversation's, else the agent's, else the first
 *  offered — the default only the live list can name (docs/billing.md § Models). */
const currentModel = computed(
  () => props.conversation?.model ?? props.settings?.model ?? models.value?.[0]?.id ?? null,
)
const modelName = computed(() => {
  const id = currentModel.value
  if (!id) {
    return t('chat.pill.noModel')
  }
  return models.value?.find((model) => model.id === id)?.name ?? id
})
const currentEffort = computed<AgentEffort>(() => props.settings?.effort ?? DEFAULT_EFFORT)

const pending = ref(false)

const items = computed<MenuItem[]>(() => {
  const rows: MenuItem[] = []
  if (models.value === null) {
    rows.push({ key: 'loading', label: modelsError.value ?? t('common.loading'), disabled: true })
  } else if (!models.value.length) {
    rows.push({ key: 'none', label: t('chat.pill.noModel'), disabled: true })
  } else {
    for (const model of models.value) {
      rows.push({
        key: `${MODEL_KEY}${model.id}`,
        label: model.name ?? model.id,
        description: model.name ? model.id : undefined,
        checked: model.id === currentModel.value,
        disabled: props.running || pending.value,
      })
    }
  }
  rows.push({
    key: 'effort',
    label: t('agents.effortLabel'),
    separated: true,
    children: AGENT_EFFORTS.map((effort) => ({
      key: `${EFFORT_KEY}${effort}`,
      label: t(`agents.effort.${effort}`),
      checked: effort === currentEffort.value,
      disabled: pending.value,
    })),
  })
  return rows
})

// --- opening and picking -------------------------------------------------------------------

function onPointerdown(event: PointerEvent) {
  pressClosedMenu = closedBy === event
}

function toggle(event: MouseEvent) {
  const fromKey = event.detail === 0
  if (menu.value || (pressClosedMenu && !fromKey)) {
    pressClosedMenu = false
    menu.value = null
    return
  }
  const box = button.value?.getBoundingClientRect()
  menuFromKey.value = fromKey
  // Over the pill, ending just above it: the composer sits at the foot of the column.
  menu.value = box ? { x: box.left, y: box.top - 4 } : { x: 0, y: 0 }
  void loadModels()
}

function close(cause?: Event) {
  menu.value = null
  closedBy = cause ?? null
}

async function onSelect(key: string) {
  if (pending.value) {
    return
  }
  pending.value = true
  try {
    if (key.startsWith(MODEL_KEY)) {
      const id = key.slice(MODEL_KEY.length)
      // The conversation the pick was made in, decided before the save: the reader may
      // open another while it is answered, and the pick said nothing about that one.
      const target = props.conversation
      const moves = target !== null && !props.running && target.model !== id
      emit('saved', await putAgentSettings(CHAT_AGENT, { model: id }))
      if (moves && target) {
        emit('moved', await updateConversation(target.id, { model: id }))
      }
    } else if (key.startsWith(EFFORT_KEY)) {
      emit('saved', await putAgentSettings(CHAT_AGENT, { effort: key.slice(EFFORT_KEY.length) as AgentEffort }))
    }
  } catch (error) {
    emit('error', await resolve(error))
  } finally {
    pending.value = false
  }
}
</script>

<template>
  <button
    ref="button"
    type="button"
    class="pill"
    :title="`${modelName} · ${t(`agents.effort.${currentEffort}`)}`"
    aria-haspopup="menu"
    :aria-expanded="menu ? 'true' : 'false'"
    :aria-busy="pending || undefined"
    @pointerdown="onPointerdown"
    @click="toggle"
  >
    <span class="pill-model">{{ modelName }}</span>
    <span class="pill-effort">{{ t(`agents.effort.${currentEffort}`) }}</span>
    <UiSpinner v-if="pending" :size="10" />
    <UiIcon v-else name="chevron-down" class="pill-glyph" />
  </button>
  <UiContextMenu
    v-if="menu"
    :at="menu"
    :items="items"
    :label="t('chat.pill.label')"
    :focus-first="menuFromKey"
    above
    @select="onSelect"
    @close="close"
  />
</template>

<style scoped>
/* A quiet capsule in the foot's row: the muted words of a fact, ink on hover. */
.pill {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  max-width: 100%;
  height: var(--control-height-sm);
  padding: 0 var(--space-2);
  border: 0;
  border-radius: var(--radius-full);
  background: transparent;
  color: var(--muted);
  font-size: var(--text-xs);
  line-height: 1;
  cursor: pointer;
  transition:
    background var(--duration-fast) var(--ease),
    color var(--duration-fast) var(--ease);
}

.pill:hover,
.pill[aria-expanded='true'] {
  background: var(--fill);
  color: var(--ink);
}

.pill-model {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.pill-effort {
  flex-shrink: 0;
  color: var(--faint);
}

.pill:hover .pill-effort,
.pill[aria-expanded='true'] .pill-effort {
  color: var(--muted);
}

.pill-glyph {
  flex-shrink: 0;
  width: 12px;
  height: 12px;
  color: var(--faint);
}
</style>
