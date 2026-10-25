<script setup lang="ts">
/**
 * Modal dialog.
 *
 * Does the four things a dialog must do and that a bare overlay `<div>` does not: traps Tab
 * inside itself, closes on Escape, returns focus to whatever opened it, and hides the rest
 * of the app from assistive tech (`aria-modal`).
 *
 * Two shapes share this one primitive. A form dialog sizes to its content and scrolls its
 * body if it has to. A `tall` dialog — the result viewer — is a fixed-height grid of header
 * over `minmax(0, 1fr)`, because its body is itself a two-column layout with its own scroll
 * regions and must be handed a bounded box rather than growing to fit.
 *
 * Below 640px it becomes a bottom sheet — a centered box on a phone leaves the buttons under
 * the thumb-unfriendly middle of the screen and often behind the keyboard.
 *
 * No scroll lock is needed: dialogs only open on signed-in pages, whose shell is a fixed
 * frame (the document itself never scrolls), and the overlay is teleported to `<body>` so it
 * is not inside the scrolling region at all. `overscroll-behavior: contain` on the panel
 * body stops a scroll that reaches its end from chaining anywhere.
 */
import { modalAboveKey, modalPanelKey } from '~/lib/menu'

const props = withDefaults(
  defineProps<{
    /** The dialog's accessible name. Also the visible title unless the `title` slot fills it. */
    title: string
    /** `wide` is a form around one long text — a prompt, a skill's body. */
    size?: 'sm' | 'md' | 'wide' | 'lg' | 'full'
    /** Fixed-height grid body — for a dialog whose content owns its own scroll regions. */
    tall?: boolean
    /** The body manages its own padding. */
    flush?: boolean
    /**
     * A request this dialog started is still running, so it cannot be dismissed. Closing
     * it would not stop the request: a delete would land after the reader believed they
     * had backed out of it, and a failure would surface somewhere they are no longer
     * looking. The controls are already disabled while this is set — Escape and the scrim
     * are the two routes that would otherwise go around them.
     */
    busy?: boolean
    /**
     * The close drawn as a round back button at the header's left rather than an X at its
     * right — the reader's, a screen you go back from rather than a box you dismiss
     * (docs/web.md § Result viewer).
     */
    back?: boolean
    /**
     * Stays over the screen's own dialogs, also those that mount after it — the settings
     * dialog, over the viewer a reload opens behind it (docs/web.md § Pages). Dialogs opened
     * from inside it still stand over it.
     */
    above?: boolean
  }>(),
  { size: 'sm' },
)

const emit = defineEmits<{ close: [] }>()

const { t } = useI18n()
const overlay = ref<HTMLElement | null>(null)
const panel = ref<HTMLElement | null>(null)
provide(modalPanelKey, panel)
const insideAbove = inject(modalAboveKey, false)
provide(modalAboveKey, insideAbove || props.above)

/** The dialog held above that this one, mounted after it and not inside it, must go under:
 *  the first such overlay before this one on the page. */
function heldAbove(): HTMLElement | null {
  const own = overlay.value
  if (!own || props.above || insideAbove) {
    return null
  }
  const above = document.querySelector<HTMLElement>('body > [data-modal-above]')
  return above && above.compareDocumentPosition(own) & Node.DOCUMENT_POSITION_FOLLOWING ? above : null
}
/** Restored on unmount, so closing a dialog does not dump focus on `<body>`. */
let previouslyFocused: HTMLElement | null = null

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])'

function focusables(): HTMLElement[] {
  if (!panel.value) {
    return []
  }
  return [...panel.value.querySelectorAll<HTMLElement>(FOCUSABLE)].filter(
    (el) => el.offsetParent !== null || el === document.activeElement,
  )
}

/**
 * Bound on the document rather than on the overlay, because focus does not reliably stay
 * inside a dialog: a control that goes `loading` disables itself, and a disabled element
 * hands focus back to `<body>`. A key pressed there never reaches a listener on the
 * overlay, so a dialog whose action failed could not be closed with Escape and Tab
 * restarted from the top of the page behind it.
 */
/**
 * Whether this dialog is the one a key is meant for. Dialogs open over dialogs — the
 * settings dialog's confirmations, a prompt dialog inside it — and each listens on the
 * document, so without this the Escape that closes the top one would close every one
 * under it, and two focus traps would fight over Tab. The last of the overlays in document
 * order is the top one, since each is teleported after the ones before it — or put under one
 * held `above`, which stays the later; for Escape the
 * overlays also count a menu and a drawer that owns the key (`data-escape-owner`), which
 * close themselves.
 */
function topmost(selector: string): boolean {
  const overlays = document.querySelectorAll(selector)
  return overlays[overlays.length - 1] === panel.value
}

function onKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') {
    if (!topmost('[aria-modal="true"], [role="menu"], [data-escape-owner]')) {
      return
    }
    event.stopPropagation()
    if (!props.busy) {
      emit('close')
    }
    return
  }
  if (event.key !== 'Tab' || !topmost('[aria-modal="true"]')) {
    return
  }

  const items = focusables()
  if (!items.length) {
    // Nothing focusable inside: keep focus on the panel rather than letting Tab escape to
    // the page behind the overlay.
    event.preventDefault()
    panel.value?.focus()
    return
  }
  const first = items[0]!
  const last = items[items.length - 1]!
  const active = document.activeElement
  const outside = !panel.value?.contains(active)

  if (event.shiftKey && (active === first || active === panel.value || outside)) {
    event.preventDefault()
    last.focus()
  } else if (!event.shiftKey && (active === last || outside)) {
    event.preventDefault()
    first.focus()
  }
}

onMounted(async () => {
  document.addEventListener('keydown', onKeydown)
  // Under a dialog held above, by the order on the page — the order `topmost` and the eye
  // both go by — and leaving it the focus it holds.
  const above = heldAbove()
  if (above && overlay.value) {
    document.body.insertBefore(overlay.value, above)
    return
  }
  previouslyFocused = document.activeElement as HTMLElement | null
  await nextTick()
  // The first field if there is one, else the panel — never the close button, which would
  // make Escape and Enter do the same thing on open.
  const target = focusables().find((el) => el.tagName !== 'BUTTON') ?? panel.value
  target?.focus()
})

onBeforeUnmount(() => {
  document.removeEventListener('keydown', onKeydown)
  previouslyFocused?.focus?.()
})
</script>

<template>
  <Teleport to="body">
    <div
      ref="overlay"
      class="overlay"
      :class="{ full: size === 'full' }"
      :data-modal-above="above ? '' : undefined"
      @click.self="busy || emit('close')"
    >
      <div
        ref="panel"
        class="panel"
        :class="[size, { tall }]"
        role="dialog"
        aria-modal="true"
        :aria-label="title"
        tabindex="-1"
      >
        <header class="panel-head" :class="{ back }">
          <UiButton
            v-if="back"
            class="panel-back"
            variant="ghost"
            size="lg"
            icon-only
            :disabled="busy"
            :label="t('common.close')"
            @click="emit('close')"
          >
            <template #icon><UiIcon name="chevron-left" /></template>
          </UiButton>
          <div class="panel-heading">
            <slot name="title">
              <h2 class="panel-title">{{ title }}</h2>
            </slot>
          </div>
          <div class="panel-actions">
            <slot name="actions" />
            <UiButton
              v-if="!back"
              class="panel-close"
              variant="secondary"
              size="sm"
              icon-only
              :disabled="busy"
              :label="t('common.close')"
              @click="emit('close')"
            >
              <template #icon><UiIcon name="close" /></template>
            </UiButton>
          </div>
        </header>

        <div class="panel-body" :class="{ flush }">
          <slot />
        </div>

        <footer v-if="$slots.footer" class="panel-foot">
          <slot name="footer" />
        </footer>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.overlay {
  position: fixed;
  inset: 0;
  z-index: 60;
  display: grid;
  place-items: center;
  padding: var(--space-5);
  background: var(--overlay);
  animation: fade var(--duration) var(--ease-enter);
}

.panel {
  display: flex;
  flex-direction: column;
  width: 100%;
  /* Never wider than the overlay: a grid child defaults to `min-width: auto`, so one wide
     descendant would stretch the panel past the screen and carry the close button off the
     right edge with it. Wide content scrolls inside the body instead. */
  min-width: 0;
  /* Never taller than the viewport: the body scrolls, the header and footer stay, so the
     confirm button is always reachable without hunting for it. */
  max-height: min(760px, calc(100dvh - var(--space-10)));
  /* Solid, as a sheet is: a dialog is content, and glass is for what floats over content
     (docs/web.md § Design system). Clipped to its corners so a body's own surface (the
     settings dialog's section list) follows them. */
  overflow: hidden;
  background: var(--paper);
  border-radius: var(--radius-xl);
  box-shadow: var(--shadow-dialog);
  animation: rise var(--duration-slow) var(--ease-enter);
}

/* The panel takes focus on open only to hold it inside the dialog: it is the dialog, not a
   control, and a ring around the whole sheet would say otherwise. */
.panel:focus-visible {
  outline: none;
}

.panel.sm {
  max-width: 460px;
}

.panel.md {
  max-width: 480px;
}

.panel.wide {
  max-width: 760px;
}

.panel.lg {
  max-width: 1040px;
}

/* The viewport, edge to edge. Not an interruption to the screen behind it but the thing the
   reader came for — see docs/web.md § Result viewer, its only caller. */
.overlay.full {
  padding: 0;
}

.panel.full {
  max-width: none;
  border-radius: 0;
  box-shadow: none;
  animation: none;
}

.panel.tall.full {
  height: 100dvh;
  max-height: none;
}

/*
 * A fixed box, not a max: the viewer's body is a grid whose columns scroll independently,
 * and a body sized to its content has no height for them to scroll within.
 */
.panel.tall {
  display: grid;
  /* `minmax(0, 1fr)`, never the implicit `auto` track: an auto column is sized by its
     content, so one wide line inside the body would push the whole dialog past the viewport
     and carry its header off the right edge. The body scrolls; the panel does not grow. */
  grid-template-columns: minmax(0, 1fr);
  grid-template-rows: auto minmax(0, 1fr);
  height: min(820px, calc(100dvh - var(--space-10)));
  max-height: none;
}

.panel-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-4);
  padding: var(--space-4) var(--space-4) var(--space-3) var(--space-6);
}

/*
 * The reader's header: its controls float on the page, the back button at the left and the
 * capsule at the right, with the title centred between them — centred on the window while
 * the two sides leave it room, pushed off centre rather than under a control when they do
 * not. Each side track is at least its own width.
 */
.panel-head.back {
  display: grid;
  grid-template-columns: minmax(max-content, 1fr) minmax(0, auto) minmax(max-content, 1fr);
  padding: 14px var(--space-4) var(--space-4);
}

.panel-head.back .panel-heading {
  text-align: center;
}

.panel-head.back .panel-actions {
  justify-content: flex-end;
}

/* A round glass button, the only control on its side. */
.panel-back {
  justify-self: start;
  background: var(--glass);
  -webkit-backdrop-filter: var(--glass-blur);
  backdrop-filter: var(--glass-blur);
  box-shadow: var(--shadow-glass);
  color: var(--ink);
}

.panel-back:hover {
  background: var(--glass);
  color: var(--accent);
}

.panel-back :deep(svg) {
  width: 20px;
  height: 20px;
}

@supports not (backdrop-filter: blur(1px)) {
  .panel-back {
    background: var(--paper);
  }
}

.panel-heading {
  min-width: 0;
}

/* Ellipsizes rather than shoving the close button out of the header — the one control that
   must survive every title. */
.panel-title {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: var(--display-xs);
  font-weight: var(--weight-semibold);
  letter-spacing: var(--tracking-heading);
}

.panel-actions {
  display: flex;
  align-items: center;
  gap: var(--space-3);
  flex-shrink: 0;
}

/* The sheet's close, as the system draws it: a gray disc with a small X. */
.panel-close {
  color: var(--muted);
}

.panel-close :deep(svg) {
  width: 15px;
  height: 15px;
}

.panel-body {
  flex: 1;
  min-height: 0;
  min-width: 0;
  overflow: auto;
  overscroll-behavior: contain;
  padding: var(--space-2) var(--space-6) var(--space-6);
}

.panel.tall .panel-body {
  overflow: hidden;
}

.panel-body.flush {
  padding: 0;
}

.panel-foot {
  display: flex;
  justify-content: flex-end;
  gap: var(--space-2);
  padding: var(--space-3) var(--space-6) var(--space-5);
}

@keyframes fade {
  from {
    opacity: 0;
  }
}

/* A sheet arrives: a few pixels of rise and scale, never a slide across the screen. */
@keyframes rise {
  from {
    opacity: 0;
    transform: translateY(8px) scale(0.98);
  }
}

/* Bottom sheet on phones. */
@media (max-width: 640px) {
  .overlay {
    place-items: end stretch;
    padding: 0;
  }

  .panel,
  .panel.sm,
  .panel.md,
  .panel.lg {
    max-width: none;
    /* `dvh`, not `vh`: the dynamic viewport shrinks when the on-screen keyboard opens, so a
       focused field stays above it instead of being covered by it. */
    max-height: 92dvh;
    border-radius: var(--radius-xl) var(--radius-xl) 0 0;
    animation: sheet var(--duration-slow) var(--ease-enter);
  }

  .panel.full {
    border-radius: 0;
    animation: none;
  }

  .panel.tall {
    height: 92dvh;
  }

  .panel-foot {
    /* Clears the home indicator on gesture-nav phones. */
    padding-bottom: max(var(--space-4), env(safe-area-inset-bottom));
  }

  @keyframes sheet {
    from {
      transform: translateY(100%);
    }
  }
}
</style>
