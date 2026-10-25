<script setup lang="ts">
/**
 * The toolbar a text selection in the document pane gets (docs/web.md § Editing, *The
 * selection toolbar*): Ask the agent, Quote in notes, Copy and Skills, over the selection's
 * first line and centered on it, held `--space-2` inside the pane. Flipped below where it
 * would leave the pane's top; under the selection on a coarse pointer, the side the
 * platform's own selection callout leaves free. Where it is still wider than the pane its
 * buttons drop their words, which stay their names.
 *
 * A press on it never moves focus, so the selection — in the open editor or over the rendered
 * blocks — is still there for the action to read. Its buttons are not tab stops: the keyboard
 * reaches the same actions through the block menu.
 *
 * Skills opens the skill list under the toolbar's right edge, part of the toolbar: it takes
 * no focus either, so the selection, an open editor and the toolbar stay as they were. A
 * second press closes it, and so does anything that hides the toolbar.
 */
import type { IconName } from '~/lib/icons'

/** A box on screen, in viewport coordinates. */
export interface ToolbarRect {
  left: number
  top: number
  right: number
  bottom: number
}

/** A skill the list offers, by its slash name. */
export interface ToolbarSkill {
  name: string
  description: string
  icon: IconName
}

const props = defineProps<{
  /** The selection's first line and last line. */
  first: ToolbarRect
  last: ToolbarRect
  /** The pane the toolbar is drawn in. */
  bounds: ToolbarRect
  coarse: boolean
  /** The skills the selection can be sent to; none hides the Skills button. */
  skills: readonly ToolbarSkill[]
}>()

const emit = defineEmits<{
  ask: []
  quote: []
  copy: []
  skill: [name: string]
}>()

const { t } = useI18n()

/** The gap between the toolbar and the line it sits over, and between it and the pane's edge. */
const GAP = 8
/** The gap between the toolbar and the skill list under it. */
const LIST_GAP = 4
/** The skill list's width, where the pane has room for it. */
const LIST_WIDTH = 300

const box = ref<HTMLElement | null>(null)
const list = ref<HTMLElement | null>(null)
const place = ref({ left: -9999, top: -9999 })
const compact = ref(false)
const skillsOpen = ref(false)
/** The rows the open list shows: read when Skills is pressed, so a skills read answering
 *  while it is open cannot move a row under the pointer; the next opening shows the new list. */
const shown = ref<readonly ToolbarSkill[]>([])
/** The list's box, in the toolbar's coordinates, and which side of the toolbar it is on. */
const listPlace = ref({ left: 0, top: 0, width: LIST_WIDTH, maxHeight: 0, above: false })

const buttons = computed((): { key: 'ask' | 'quote' | 'copy'; icon: IconName; label: string }[] => [
  { key: 'ask', icon: 'message-circle', label: t('viewer.toolbar.ask') },
  { key: 'quote', icon: 'notebook-pen', label: t('viewer.toolbar.quote') },
  { key: 'copy', icon: 'copy', label: t('viewer.toolbar.copy') },
])

async function position() {
  const element = box.value
  if (!element) {
    return
  }
  const { first, last, bounds } = props
  compact.value = false
  await nextTick()
  const room = bounds.right - bounds.left - 2 * GAP
  if (element.offsetWidth > room) {
    compact.value = true
    await nextTick()
  }
  const width = element.offsetWidth
  const height = element.offsetHeight
  const center = (first.left + first.right) / 2
  const left = Math.min(Math.max(center - width / 2, bounds.left + GAP), bounds.right - GAP - width)
  const over = first.top - GAP - height
  // Under the selection where the pane has room for it there, else under its first line.
  const under = last.bottom + GAP + height <= bounds.bottom - GAP ? last.bottom + GAP : first.bottom + GAP
  const top = props.coarse || over < bounds.top + GAP ? under : over
  place.value = { left: left - bounds.left, top: top - bounds.top }
  if (skillsOpen.value) {
    await nextTick()
    placeList()
  }
}

/** The list under the toolbar's right edge, shifted to stay `GAP` inside the pane, flipped
 *  above the toolbar where the pane has no room below; its height the room on that side. */
function placeList() {
  const element = box.value
  const menu = list.value
  if (!element || !menu) {
    return
  }
  const { bounds } = props
  const toolbar = element.getBoundingClientRect()
  const width = Math.min(LIST_WIDTH, bounds.right - bounds.left - 2 * GAP)
  const left = Math.min(Math.max(toolbar.right - width, bounds.left + GAP), bounds.right - GAP - width)
  const below = bounds.bottom - GAP - (toolbar.bottom + LIST_GAP)
  const over = toolbar.top - LIST_GAP - (bounds.top + GAP)
  const above = menu.offsetHeight > below && over > below
  listPlace.value = {
    left: left - toolbar.left,
    top: above ? -LIST_GAP : toolbar.height + LIST_GAP,
    width,
    maxHeight: Math.max(above ? over : below, 0),
    above,
  }
}

watch(
  () => [props.first, props.last, props.bounds, props.coarse, props.skills.length],
  () => void position(),
)

watch(
  () => props.skills.length,
  (count) => {
    if (!count) {
      skillsOpen.value = false
    }
  },
)

onMounted(() => void position())

async function toggleSkills() {
  skillsOpen.value = !skillsOpen.value
  if (skillsOpen.value) {
    shown.value = [...props.skills]
    await nextTick()
    placeList()
  }
}

function press(key: 'ask' | 'quote' | 'copy') {
  skillsOpen.value = false
  if (key === 'ask') {
    emit('ask')
  } else if (key === 'quote') {
    emit('quote')
  } else {
    emit('copy')
  }
}

function pick(name: string) {
  skillsOpen.value = false
  emit('skill', name)
}
</script>

<template>
  <div
    ref="box"
    class="toolbar"
    :class="{ compact }"
    role="toolbar"
    :aria-label="t('viewer.toolbar.label')"
    :style="{ left: `${place.left}px`, top: `${place.top}px` }"
    @mousedown.prevent
  >
    <button
      v-for="button in buttons"
      :key="button.key"
      type="button"
      class="tool"
      tabindex="-1"
      :aria-label="compact ? button.label : undefined"
      :title="compact ? button.label : undefined"
      @click="press(button.key)"
    >
      <UiIcon :name="button.icon" />
      <span v-if="!compact" class="tool-label">{{ button.label }}</span>
    </button>
    <template v-if="skills.length">
      <span class="rule" aria-hidden="true" />
      <button
        type="button"
        class="tool"
        :class="{ on: skillsOpen }"
        tabindex="-1"
        :aria-label="compact ? t('viewer.toolbar.skillsLabel') : undefined"
        :title="compact ? t('viewer.toolbar.skillsLabel') : undefined"
        aria-haspopup="menu"
        :aria-expanded="skillsOpen ? 'true' : 'false'"
        @click="toggleSkills"
      >
        <UiIcon name="square-slash" />
        <span v-if="!compact" class="tool-label">{{ t('viewer.toolbar.skillsLabel') }}</span>
      </button>
    </template>
    <div
      v-if="skillsOpen"
      ref="list"
      class="skills"
      :class="{ above: listPlace.above }"
      role="menu"
      :aria-label="t('viewer.toolbar.skills')"
      :style="{
        left: `${listPlace.left}px`,
        top: listPlace.above ? 'auto' : `${listPlace.top}px`,
        bottom: listPlace.above ? `calc(100% + ${LIST_GAP}px)` : 'auto',
        width: `${listPlace.width}px`,
        maxHeight: listPlace.maxHeight ? `${listPlace.maxHeight}px` : undefined,
      }"
    >
      <p class="skills-head">{{ t('viewer.toolbar.skills') }}</p>
      <div class="skills-list">
        <button
          v-for="skill in shown"
          :key="skill.name"
          type="button"
          class="skill"
          role="menuitem"
          tabindex="-1"
          :title="`/${skill.name}: ${skill.description}`"
          @click="pick(skill.name)"
        >
          <UiIcon :name="skill.icon" />
          <span class="skill-name mono">/{{ skill.name }}</span>
          <span class="skill-description">{{ skill.description }}</span>
        </button>
      </div>
    </div>
  </div>
</template>

<style scoped>
/* A glass capsule over the document, as the screens' toolbars are (docs/web.md § Design
   system, *Glass is for what floats*). */
.toolbar {
  position: absolute;
  z-index: 4;
  display: flex;
  align-items: center;
  gap: 2px;
  padding: var(--space-1);
  border-radius: var(--radius-full);
  background: var(--glass);
  box-shadow: var(--shadow-glass), var(--shadow-menu);
  -webkit-backdrop-filter: var(--glass-blur);
  backdrop-filter: var(--glass-blur);
  white-space: nowrap;
  animation: toolbar-in var(--duration-fast) var(--ease-enter);
}

@supports not (backdrop-filter: blur(1px)) {
  .toolbar {
    background: var(--paper);
  }
}

@keyframes toolbar-in {
  from {
    opacity: 0;
    transform: scale(0.96);
  }
}

@media (prefers-reduced-motion: reduce) {
  .toolbar,
  .skills {
    animation: none;
  }
}

/* The capsule's buttons: capsules themselves, the gray fill under the pointer. */
.tool {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 32px;
  padding: 0 12px 0 10px;
  border: 0;
  border-radius: var(--radius-full);
  background: transparent;
  color: var(--ink);
  font-size: var(--text-sm);
  cursor: pointer;
  transition:
    background var(--duration-fast) var(--ease),
    color var(--duration-fast) var(--ease);
}

.toolbar.compact .tool {
  justify-content: center;
  width: 36px;
  padding: 0;
}

.tool:hover,
.tool.on {
  background: var(--fill);
}

.tool > :deep(svg) {
  flex: none;
  width: 15px;
  height: 15px;
}

.rule {
  flex: none;
  width: 1px;
  height: 18px;
  margin-inline: 2px;
  background: var(--line-strong);
}

/* The skill list: the same surface, hung under the toolbar's right edge. */
.skills {
  position: absolute;
  z-index: 4;
  display: flex;
  flex-direction: column;
  padding: var(--space-1);
  border: 1px solid var(--menu-edge);
  border-radius: var(--radius-menu);
  background: var(--menu-surface);
  box-shadow: var(--shadow-menu);
  white-space: normal;
  transform-origin: top right;
  animation: toolbar-in var(--duration-fast) var(--ease-enter);
}

.skills.above {
  transform-origin: bottom right;
}

.skills-head {
  flex: none;
  margin: 0;
  padding: var(--space-2) var(--space-3) var(--space-1);
  color: var(--faint);
  font-size: var(--text-xs);
}

.skills-list {
  flex: 1 1 auto;
  min-height: 0;
  overflow-y: auto;
  overscroll-behavior: contain;
  scrollbar-width: none;
}

.skills-list::-webkit-scrollbar {
  display: none;
}

.skill {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  width: 100%;
  height: var(--control-height-sm);
  padding: 0 var(--space-3);
  border: 0;
  border-radius: var(--radius-menu-item);
  background: none;
  color: var(--ink);
  font-size: var(--text-sm);
  text-align: left;
  white-space: nowrap;
  cursor: pointer;
}

.skill:hover {
  background: var(--accent);
  color: var(--accent-fg);
}

.skill > :deep(svg) {
  flex-shrink: 0;
  width: 15px;
  height: 15px;
  color: var(--faint);
}

.skill:hover > :deep(svg) {
  color: currentcolor;
}

/* A skill's name keeps its width, up to most of the row, and the description takes the rest. */
.skill-name {
  flex: 0 0 auto;
  max-width: 70%;
  overflow: hidden;
  text-overflow: ellipsis;
}

.skill-description {
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  color: var(--muted);
  text-overflow: ellipsis;
}

.skill:hover .skill-description {
  color: currentcolor;
}
</style>
