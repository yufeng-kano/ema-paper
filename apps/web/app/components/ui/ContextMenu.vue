<script setup lang="ts">
/**
 * A context menu: a list of actions opened at a point (docs/web.md § Files).
 *
 * The page owns *what* it holds and *where* it was asked to open; this owns everything that
 * makes a floating menu behave like one:
 *
 *  - **It fits.** Measured after mount and placed by `lib/menu.ts`, so a click near an edge
 *    flips the menu rather than putting half of it outside the window.
 *  - **It is a menu to a keyboard too.** `role="menu"` over `role="menuitem"` buttons, arrows
 *    and Home/End to move, Enter or Space to choose, Escape to leave — and focus goes back to
 *    whatever opened it, which is the row the reader was on. Focus lands on the menu itself
 *    rather than on its first item, because focus is what draws the highlight: a menu that
 *    opens with a row already lit is claiming the pointer is on that row.
 *  - **It closes on anything that means "not this".** A press outside, a scroll under it, a
 *    resize, a Tab out, another right-click. A menu still hanging over content that has moved
 *    is the failure people remember from home-made menus. A scroll *inside* it is not such a
 *    thing: a menu taller than the window scrolls within its own edge.
 *
 * Teleported to `<body>`: the panel it opens from is a scroll container with its own stacking
 * context, and a menu that clips at the panel edge is not a menu. Inside a dialog it goes to
 * the dialog's panel instead (`modalPanelKey`), which is still above every scroll container
 * in it, and the only place `aria-modal` leaves visible to assistive tech.
 *
 * An item with `children` flies a submenu out beside its row — a second surface teleported
 * beside this one rather than drawn inside it, where the scrolling box would clip it, and
 * counted as inside: a press, a scroll or a right-click in it closes nothing, while a scroll
 * of the menu, which moves its row out from beside it, closes it. It opens on a press, on
 * ArrowRight or Enter with its first item highlighted, or when the pointer rests on the row;
 * another row takes over only after a longer rest, so a diagonal path to the submenu does
 * not lose it on the way. ArrowLeft or Escape goes back to the row.
 */
import { heightAbove, modalPanelKey, placeMenu, placeSubmenu, type MenuFact, type MenuItem, type Point } from '~/lib/menu'

const props = defineProps<{
  /** Where the gesture happened, in viewport coordinates. */
  at: Point
  items: MenuItem[]
  /** The menu's accessible name — what it is about, e.g. the row's name. */
  label: string
  /** A line of context above the items, e.g. which account this menu belongs to. */
  header?: string
  /** Label/value rows under the header, for the facts one line cannot hold — the result
   *  viewer's parse details. */
  facts?: MenuFact[]
  /**
   * Open with the first item already highlighted. For a menu opened from the keyboard, where
   * the highlight *is* the cursor — a pointer-opened menu highlights nothing until the
   * pointer picks a row, exactly as the platform's own menus behave.
   */
  focusFirst?: boolean
  /** Open over `at` rather than under it, wherever there is room there: a menu asked for at
   *  the top of a control it must not cover — the composer's, at the foot of its column. */
  above?: boolean
}>()

const emit = defineEmits<{
  select: [string]
  /** With the event that closed it, when one did — a press, a key, a scroll — so the page can
   *  tell the press on its own opener from any other. */
  close: [cause?: Event]
}>()

const menu = ref<HTMLElement | null>(null)
const modalPanel = inject(modalPanelKey, null)
/** The facts are the menu's description: a static list inside a menu is otherwise skipped,
 *  since arrow keys move only between the items. */
const factsId = useId()
/** Off-screen until measured, so the first paint is never at the wrong place. */
const position = ref<Point>({ x: -9999, y: -9999 })
/** The corner the menu grows from — the pointer's, so a flipped menu opens back towards it. */
const origin = ref('left top')
/** Restored on unmount: closing a menu must not drop focus on `<body>`. */
let previouslyFocused: HTMLElement | null = null
// `resolveComponent` is only valid at setup: an item is a link where it is a destination.
const NuxtLinkComponent = resolveComponent('NuxtLink')

/** The submenu: the row it belongs to, by key, and where it is drawn. */
const sub = ref<HTMLElement | null>(null)
const subKey = ref<string | null>(null)
const subPosition = ref<Point>({ x: -9999, y: -9999 })
const subOrigin = ref('left top')
const subItem = computed(() => props.items.find((item) => item.key === subKey.value && item.children?.length) ?? null)
/** How long the pointer rests on a row before its submenu opens, and before another row
 *  takes over from an open one (docs/web.md § Design system, *Primitives*). */
const SUBMENU_OPEN_MS = 150
const SUBMENU_TAKEOVER_MS = 300
let restTimer: ReturnType<typeof setTimeout> | undefined

/** Items are buttons, or links where the item is a destination — both are options here. */
function options(surface: HTMLElement | null): HTMLElement[] {
  return [...(surface?.querySelectorAll<HTMLElement>('button:not([disabled]), a') ?? [])]
}

function focusAt(surface: HTMLElement | null, index: number) {
  const all = options(surface)
  if (!all.length) {
    return
  }
  // Wrapping, as a native menu does: past the last item is the first one.
  all[((index % all.length) + all.length) % all.length]!.focus()
}

function step(surface: HTMLElement | null, delta: number) {
  const all = options(surface)
  const from = all.indexOf(document.activeElement as HTMLElement)
  focusAt(surface, from < 0 ? (delta > 0 ? 0 : -1) : from + delta)
}

/** The arrow keys move within whichever surface holds focus; the rest is the caller's. */
function moveKeys(event: KeyboardEvent, surface: HTMLElement | null): boolean {
  const moves: Record<string, () => void> = {
    ArrowDown: () => step(surface, 1),
    ArrowUp: () => step(surface, -1),
    Home: () => focusAt(surface, 0),
    End: () => focusAt(surface, -1),
  }
  const move = moves[event.key]
  if (!move) {
    return false
  }
  event.preventDefault()
  move()
  return true
}

function onKeydown(event: KeyboardEvent) {
  const row = (event.target as HTMLElement).closest<HTMLElement>('[data-key]')
  const item = props.items.find((entry) => entry.key === row?.dataset.key)
  if (event.key === 'Escape') {
    event.stopPropagation()
    // One layer at a time: an open submenu closes before the menu does.
    if (subKey.value !== null) {
      backToRow()
    } else {
      emit('close', event)
    }
  } else if (event.key === 'ArrowRight' && item?.children?.length && !item.disabled) {
    event.preventDefault()
    void openSub(item, true)
  } else if (event.key === 'Tab') {
    // Tabbing away is leaving, and the browser's own focus move is the right one.
    emit('close', event)
  } else if (['ArrowDown', 'ArrowUp', 'Home', 'End'].includes(event.key)) {
    closeSub()
    moveKeys(event, menu.value)
  }
}

function onSubKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape' || event.key === 'ArrowLeft') {
    // Back to the row, not out of the menu — and the dialog behind must not hear the key.
    event.preventDefault()
    event.stopPropagation()
    backToRow()
  } else if (event.key === 'Tab') {
    emit('close', event)
  } else {
    moveKeys(event, sub.value)
  }
}

/** Whether an event happened in the menu or its submenu, which count as one. */
function inside(event: Event): boolean {
  const target = event.target as Node
  return Boolean(menu.value?.contains(target) || sub.value?.contains(target))
}

function onPointerdownOutside(event: PointerEvent) {
  if (!inside(event)) {
    emit('close', event)
  }
}

/** A second right-click closes this menu; the page opens the next one from its own handler. */
function onContextmenuOutside(event: MouseEvent) {
  if (!inside(event)) {
    emit('close', event)
  }
}

function close(event: Event) {
  emit('close', event)
}

/** Moving to an item scrolls a menu taller than the window, and that must not close it. Its
 *  own scroll takes the row an open submenu stands beside away from it, so that closes; focus
 *  that was in the submenu goes to the menu, where the scroll leaves the reader. */
function onScrollOutside(event: Event) {
  if (!inside(event)) {
    emit('close', event)
  } else if (event.target === menu.value && subKey.value !== null) {
    const focused = sub.value?.contains(document.activeElement) ?? false
    closeSub()
    if (focused) {
      menu.value?.focus({ preventScroll: true })
    }
  }
}

function rowOf(key: string): HTMLElement | null {
  return [...(menu.value?.querySelectorAll<HTMLElement>('[data-key]') ?? [])].find((row) => row.dataset.key === key) ?? null
}

/** Restarts a surface's opening animation: a reused element would not run it again. */
function replay(element: HTMLElement) {
  element.style.animation = 'none'
  void element.offsetHeight
  element.style.animation = ''
}

/**
 * Opens a row's submenu beside it (`placeSubmenu`), measured from the layout boxes rather than
 * the drawn ones, which the opening animation's scale would shrink. From the keyboard its
 * first item is highlighted; from the pointer nothing is, as the menu itself opens.
 */
async function openSub(item: MenuItem, focusFirst: boolean) {
  clearTimeout(restTimer)
  subKey.value = item.key
  await nextTick()
  const element = sub.value
  const parent = menu.value
  const row = rowOf(item.key)
  if (!element || !parent || !row) {
    return
  }
  const left = position.value.x
  const top = position.value.y
  const rowTop = top + parent.clientTop + row.offsetTop - parent.scrollTop
  const rowLeft = left + parent.clientLeft + row.offsetLeft
  const at = placeSubmenu(
    { left: rowLeft, top: rowTop, right: rowLeft + row.offsetWidth, bottom: rowTop + row.offsetHeight },
    { left, top, right: left + parent.offsetWidth, bottom: top + parent.offsetHeight },
    { width: element.offsetWidth, height: element.offsetHeight },
    { width: window.innerWidth, height: window.innerHeight },
  )
  subPosition.value = at
  subOrigin.value = at.x + element.offsetWidth <= left ? 'right top' : 'left top'
  replay(element)
  if (focusFirst) {
    focusAt(element, 0)
  }
}

function closeSub() {
  clearTimeout(restTimer)
  subKey.value = null
}

function backToRow() {
  const key = subKey.value
  closeSub()
  if (key !== null) {
    rowOf(key)?.focus()
  }
}

/** The pointer resting on a row: a submenu row opens after a short rest, and while a submenu
 *  is open any other row takes over only after a longer one. */
function onRowEnter(item: MenuItem) {
  clearTimeout(restTimer)
  if (item.key === subKey.value) {
    return
  }
  const opens = Boolean(item.children?.length) && !item.disabled
  if (subKey.value === null) {
    if (opens) {
      restTimer = setTimeout(() => void openSub(item, false), SUBMENU_OPEN_MS)
    }
    return
  }
  restTimer = setTimeout(() => {
    closeSub()
    if (opens) {
      void openSub(item, false)
    }
  }, SUBMENU_TAKEOVER_MS)
}

/** The pointer left the row before it rested there, or reached the submenu. */
function cancelRest() {
  clearTimeout(restTimer)
}

/** Measured after the items are in the DOM, because where it fits depends on how big it is. */
async function place() {
  await nextTick()
  const element = menu.value
  if (element) {
    // Opened above its point, it is held to the room there, so a long list scrolls rather
    // than being pushed down over the control below.
    const room = props.above ? heightAbove(props.at) : null
    element.style.maxHeight = room === null ? '' : `${room}px`
    // The layout box, not the drawn one: the opening animation's scale would shrink it, and
    // a menu placed by a size it does not have runs past the point it was meant to end at.
    const at = placeMenu(
      props.at,
      { width: element.offsetWidth, height: element.offsetHeight },
      { width: window.innerWidth, height: window.innerHeight },
      { above: props.above },
    )
    position.value = at
    origin.value = `${at.x < props.at.x ? 'right' : 'left'} ${at.y < props.at.y ? 'bottom' : 'top'}`
    // Reopened on another row, the same element is reused and its opening animation would
    // not run again — so it is restarted by hand. The read is what forces the reflow.
    replay(element)
  }
  if (props.focusFirst) {
    focusAt(menu.value, 0)
  } else {
    // The menu, not an item: nothing is highlighted until the pointer or an arrow key picks
    // a row, while Escape and the arrows still have somewhere to land.
    menu.value?.focus()
  }
}

/**
 * Opened again while it was already open — a right-click on another row. The page closes and
 * reopens in one tick, so this component is reused rather than remounted and `onMounted`
 * never runs again; without this the second menu would draw at the first one's point.
 */
watch(
  () => props.at,
  () => {
    closeSub()
    void place()
  },
)

onMounted(() => {
  previouslyFocused = document.activeElement as HTMLElement | null
  void place()
  window.addEventListener('pointerdown', onPointerdownOutside, true)
  window.addEventListener('contextmenu', onContextmenuOutside, true)
  window.addEventListener('resize', close)
  // Capture, because the scroll that matters happens inside a panel, not on the window.
  window.addEventListener('scroll', onScrollOutside, true)
})

onBeforeUnmount(() => {
  clearTimeout(restTimer)
  window.removeEventListener('pointerdown', onPointerdownOutside, true)
  window.removeEventListener('contextmenu', onContextmenuOutside, true)
  window.removeEventListener('resize', close)
  window.removeEventListener('scroll', onScrollOutside, true)
  previouslyFocused?.focus?.()
})

/** A row chosen: a submenu row opens its submenu — highlighting its first item when the
 *  keyboard chose it (a click with no `detail` is Enter or Space) — any other is the choice. */
function choose(item: MenuItem, event?: MouseEvent) {
  if (item.disabled) {
    return
  }
  if (item.children?.length) {
    void openSub(item, event?.detail === 0)
    return
  }
  emit('select', item.key)
  emit('close')
}
</script>

<template>
  <Teleport :to="modalPanel ?? 'body'">
    <div
      ref="menu"
      class="menu"
      role="menu"
      tabindex="-1"
      :aria-label="label"
      :aria-describedby="facts?.length ? factsId : undefined"
      :style="{
        left: `${position.x}px`,
        top: `${position.y}px`,
        transformOrigin: origin,
      }"
      @keydown="onKeydown"
      @contextmenu.prevent
    >
      <p v-if="header" class="header">{{ header }}</p>

      <dl v-if="facts?.length" :id="factsId" class="facts">
        <template v-for="fact in facts" :key="fact.label">
          <dt>{{ fact.label }}</dt>
          <dd :class="{ mono: fact.mono }">{{ fact.value }}</dd>
        </template>
      </dl>

      <!-- A destination is a real link: it can be opened in a new tab, and the locale items
           are URLs rather than actions. -->
      <component
        :is="item.to ? NuxtLinkComponent : 'button'"
        v-for="item in items"
        :key="item.key"
        v-bind="item.to ? { to: item.to } : { type: 'button', disabled: item.disabled }"
        :role="item.checked === undefined ? 'menuitem' : 'menuitemradio'"
        :aria-checked="item.checked"
        :aria-haspopup="item.children ? 'menu' : undefined"
        :aria-expanded="item.children ? subKey === item.key : undefined"
        :data-key="item.key"
        tabindex="-1"
        class="item"
        :class="{ danger: item.danger, separated: item.separated, open: subKey === item.key }"
        :title="item.description ? `${item.label}: ${item.description}` : item.label"
        @click="choose(item, $event)"
        @pointerenter="onRowEnter(item)"
        @pointerleave="cancelRest"
      >
        <UiIcon v-if="item.checked" name="check" />
        <UiIcon v-else-if="item.icon" :name="item.icon" />
        <span v-else class="icon-space" />
        <span class="label" :class="{ mono: item.labelMono, described: item.description }">{{ item.label }}</span>
        <span v-if="item.description" class="description">{{ item.description }}</span>
        <span v-if="item.shortcut" class="shortcut" :class="{ mono: item.shortcutMono }">{{ item.shortcut }}</span>
        <UiIcon v-if="item.children" name="chevron-right" class="chevron" />
      </component>
    </div>

    <div
      v-if="subItem"
      ref="sub"
      class="menu submenu"
      role="menu"
      tabindex="-1"
      :aria-label="subItem.label"
      :style="{ left: `${subPosition.x}px`, top: `${subPosition.y}px`, transformOrigin: subOrigin }"
      @keydown="onSubKeydown"
      @pointerenter="cancelRest"
      @contextmenu.prevent
    >
      <button
        v-for="child in subItem.children"
        :key="child.key"
        type="button"
        :role="child.checked === undefined ? 'menuitem' : 'menuitemradio'"
        :aria-checked="child.checked"
        tabindex="-1"
        class="item"
        :class="{ danger: child.danger, separated: child.separated }"
        :disabled="child.disabled"
        :title="child.description ? `${child.label}: ${child.description}` : child.label"
        @click="choose(child)"
      >
        <UiIcon v-if="child.checked" name="check" />
        <UiIcon v-else-if="child.icon" :name="child.icon" />
        <span v-else class="icon-space" />
        <span class="label" :class="{ mono: child.labelMono, described: child.description }">{{ child.label }}</span>
        <span v-if="child.description" class="description">{{ child.description }}</span>
        <span v-if="child.shortcut" class="shortcut" :class="{ mono: child.shortcutMono }">{{ child.shortcut }}</span>
      </button>
    </div>
  </Teleport>
</template>

<style scoped>
/*
 * A floating surface, built the way the platform builds one: rounded, hairlined, translucent
 * over what it covers, and lifted by a two-part shadow — a tight contact shadow so the edge
 * reads, and a wide soft one for the height. This is the design's one exception to flat
 * square paper, and it is deliberate: a menu that shares the page's geometry looks pasted
 * into it (docs/web.md § Design system).
 */
.menu {
  position: fixed;
  /* Focused on open so the keys have a home, but it is a container, not a control: the
     highlight belongs to whichever row the pointer or an arrow key picks. */
  outline: none;
  z-index: 90;
  min-width: 200px;
  max-width: 300px;
  /* Never taller than the window, less the margin `placeMenu` keeps on both sides: a menu
     that is scrolls, rather than putting its last items out of reach. */
  max-height: calc(100dvh - 16px);
  /* Never sideways: a label longer than the menu is cut with an ellipsis instead. */
  overflow-x: hidden;
  overflow-y: auto;
  padding: var(--space-1);
  border-radius: var(--radius-menu);
  border: 1px solid var(--menu-edge);
  background: var(--menu-surface);
  box-shadow: var(--shadow-menu);
  backdrop-filter: blur(24px) saturate(180%);
  -webkit-backdrop-filter: blur(24px) saturate(180%);
  /* The pop: a menu appears, it does not fade in place. The origin is the corner the menu
     opened from, which `place()` sets — a flipped menu grows back towards the pointer. */
  animation: menu-in var(--duration-fast) var(--ease-enter);
}

/* Without blur support the translucency would leave text over text, so the surface goes
   opaque instead. */
@supports not (backdrop-filter: blur(1px)) {
  .menu {
    background: var(--paper);
  }
}

@keyframes menu-in {
  from {
    opacity: 0;
    transform: scale(0.96);
  }
}

@media (prefers-reduced-motion: reduce) {
  .menu {
    animation: none;
  }
}

/* Who or what the menu is about — a fact, not an item: it takes no focus and does nothing.
   Ellipsized because an email address is as long as it is. */
.header {
  margin: 0;
  padding: var(--space-2) var(--space-3) var(--space-3);
  overflow: hidden;
  color: var(--faint);
  font-size: var(--text-xs);
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* Facts, like the header: no focus, no action. They wrap rather than ellipsize, because a
   model id cut short is a different model id. */
.facts {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  gap: var(--space-1) var(--space-4);
  margin: 0;
  padding: var(--space-2) var(--space-3) var(--space-3);
  font-size: var(--text-xs);
  line-height: 1.5;
}

.facts dt {
  color: var(--faint);
}

.facts dd {
  margin: 0;
  color: var(--ink);
  overflow-wrap: anywhere;
}

.item {
  position: relative;
  display: flex;
  align-items: center;
  gap: var(--space-3);
  width: 100%;
  height: var(--control-height-sm);
  padding: 0 var(--space-3);
  border: none;
  border-radius: var(--radius-menu-item);
  background: none;
  color: var(--ink);
  font-size: var(--text-sm);
  text-align: left;
  white-space: nowrap;
  text-decoration: none;
  cursor: default;
  transition:
    background var(--duration-fast) var(--ease),
    color var(--duration-fast) var(--ease);
}

/*
 * The group rule: a line in the gap above the item, inset from the rounded edge. Drawn as a
 * pseudo-element rather than a real one, which would end up focusable or read out as an item
 * of its own — and rather than a border, which the highlight would swallow.
 */
.item.separated {
  margin-top: calc(2 * var(--space-1) + 1px);
}

.item.separated::before {
  content: '';
  position: absolute;
  right: var(--space-2);
  left: var(--space-2);
  top: calc(-1 * var(--space-1) - 1px);
  height: 1px;
  background: var(--menu-edge);
}

/*
 * The highlight is the accent, filled, and it is the one place the accent covers a surface.
 * It earns it: the highlighted row is the action about to be committed, which is exactly
 * what the accent is reserved for. A row whose submenu is open keeps it, as the platform's
 * menus do, so the eye can follow where the submenu came from.
 */
.item:hover,
.item:focus,
.item.open {
  background: var(--accent);
  color: var(--accent-fg);
  outline: none;
}

.item:hover :deep(svg),
.item:focus :deep(svg) {
  color: currentcolor;
}

.item:disabled {
  color: var(--faint);
  background: none;
}

.item.danger {
  color: var(--danger);
}

.item.danger:hover,
.item.danger:focus {
  background: var(--danger);
  color: var(--accent-fg);
}

.item :deep(svg) {
  width: 15px;
  height: 15px;
  flex-shrink: 0;
  color: var(--faint);
}

.icon-space {
  width: 15px;
  flex-shrink: 0;
}

/* A label wider than the menu ends in an ellipsis; the row's title says it whole. */
.label {
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
}

/* A skill's name and what it does on one line: the name keeps its width, up to most of the
   row, and the description takes the rest, cut with an ellipsis — shrunk in proportion to
   their widths, a long description would cut the name to a letter. */
.label.described {
  flex: 0 0 auto;
  max-width: 70%;
}

.description {
  flex: 1 1 auto;
  min-width: 0;
  overflow: hidden;
  color: var(--muted);
  text-overflow: ellipsis;
}

/* The key, or the markdown the row stands for, right-aligned and quiet. */
.shortcut {
  flex-shrink: 0;
  margin-left: auto;
  padding-left: var(--space-3);
  color: var(--faint);
  font-size: var(--text-xs);
}

/* A key names nothing on a coarse pointer, which has no keyboard to press it on; markdown or
   a command (mono) is typed, and stays. */
@media (pointer: coarse) {
  .shortcut:not(.mono) {
    display: none;
  }
}

.item:hover .description,
.item:focus .description,
.item.open .description,
.item:hover .shortcut,
.item:focus .shortcut,
.item.open .shortcut {
  color: currentcolor;
}

.item:disabled .description,
.item:disabled .shortcut {
  color: var(--faint);
}

.item .chevron {
  margin-left: auto;
}

/* Its own second surface, the menu's width or less: a short list of parallel choices. */
.submenu {
  min-width: 180px;
}
</style>
