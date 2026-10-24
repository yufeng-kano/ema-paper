<script setup lang="ts">
/**
 * The app's only button.
 *
 * Renders as `<button>`, or as `<a>` / `<NuxtLink>` when given `href` / `to` — a link that
 * looks like a button is still a link, and must keep middle-click and "open in new tab".
 * `href` stays same-tab: the one place we use it is the server-redirect sign-in flow.
 *
 * `loading` shows a spinner *in place of* the icon and disables the control, so a pending
 * action can never be fired twice; the label stays put so the button does not resize
 * mid-press.
 *
 * Every size is a control-height token, so a button beside an input is the same box — see
 * docs/web.md § Design system.
 */
const props = withDefaults(
  defineProps<{
    variant?: 'primary' | 'secondary' | 'ghost' | 'danger'
    /** 44px (a toolbar's), 36px, 30px and 26px — the control heights a button takes. */
    size?: 'xs' | 'sm' | 'md' | 'lg'
    /** Round button with an icon and no visible label — `label` becomes its accessible name. */
    iconOnly?: boolean
    /** Fills its container's width, for a sheet's single primary action. */
    block?: boolean
    loading?: boolean
    disabled?: boolean
    /**
     * Accessible name. Required when `iconOnly`, where it is also the tooltip. On a
     * labelled button it *overrides* the visible text for assistive tech — for a control
     * whose words repeat down a list ("Revoke" on every row) and need the subject spelled
     * out. It must still contain the visible label, per WCAG 2.5.3: a voice-control user
     * says what they can see.
     */
    label?: string
    href?: string
    to?: string
    type?: 'button' | 'submit'
  }>(),
  // The three optional strings are spelled out as `undefined` because that is how
  // `vue/require-default-prop` is satisfied for a type-only declaration: absent is the
  // default, and saying so keeps the lint output free of noise to read past.
  { variant: 'secondary', size: 'md', type: 'button', label: undefined, href: undefined, to: undefined },
)

// Resolved once at setup: `resolveComponent` is only valid here or in render, not inside a
// getter that may run later.
const NuxtLinkComponent = resolveComponent('NuxtLink')

const isDisabled = computed(() => props.disabled || props.loading)
const tag = computed(() => (props.to ? NuxtLinkComponent : props.href ? 'a' : 'button'))

/**
 * A disabled *link* has no native equivalent, so it is downgraded to an inert anchor with
 * `aria-disabled` rather than shipping a clickable control that looks dead.
 */
const bindings = computed(() => {
  if (props.to) {
    return isDisabled.value ? { role: 'link', 'aria-disabled': 'true' } : { to: props.to }
  }
  if (props.href) {
    return isDisabled.value ? { role: 'link', 'aria-disabled': 'true' } : { href: props.href }
  }
  return { type: props.type, disabled: isDisabled.value }
})
</script>

<template>
  <component
    :is="tag"
    class="btn"
    :class="[
      `btn-${variant}`,
      `btn-${size}`,
      { 'btn-icon': iconOnly, 'btn-block': block, 'is-loading': loading },
    ]"
    :aria-label="label"
    :title="iconOnly ? label : undefined"
    :aria-busy="loading ? 'true' : undefined"
    v-bind="bindings"
  >
    <UiSpinner v-if="loading" class="btn-spinner" />
    <slot v-else name="icon" />
    <span v-if="!iconOnly" class="btn-label"><slot /></span>
  </component>
</template>

<style scoped>
.btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: var(--space-2);
  border: 1px solid transparent;
  /* A capsule, as the system draws a push button (docs/web.md § Design system). */
  border-radius: var(--radius-full);
  font-weight: var(--weight-medium);
  white-space: nowrap;
  cursor: pointer;
  /* Color and background only — no hover elevation and no transforms (docs/web.md §
     Design system). */
  transition:
    background var(--duration-fast) var(--ease),
    border-color var(--duration-fast) var(--ease),
    color var(--duration-fast) var(--ease);
}

/* The toolbar's size: a primary action beside the glass capsules. */
.btn-lg {
  height: var(--control-height-lg);
  padding: 0 var(--space-5) 0 var(--space-4);
  font-size: var(--text-base);
  font-weight: var(--weight-semibold);
}

.btn-md {
  height: var(--control-height);
  padding: 0 var(--space-4);
  font-size: var(--text-sm);
}

.btn-sm {
  height: var(--control-height-sm);
  padding: 0 var(--space-3);
  font-size: var(--text-sm);
}

.btn-xs {
  height: var(--control-height-xs);
  padding: 0 10px;
  font-size: var(--text-xs);
}

.btn-icon {
  padding: 0;
  aspect-ratio: 1;
}

.btn-icon.btn-lg {
  width: var(--control-height-lg);
}

.btn-icon.btn-md {
  width: var(--control-height);
}

.btn-icon.btn-sm {
  width: var(--control-height-sm);
}

.btn-icon.btn-xs {
  width: var(--control-height-xs);
}

/* Fills the column it sits in — a sheet's single primary action. */
.btn-block {
  width: 100%;
}

.btn:disabled,
.btn[aria-disabled='true'] {
  opacity: 0.45;
  cursor: not-allowed;
  pointer-events: none;
}

/* Loading keeps full opacity — the spinner already says "busy", and dimming on top of it
   reads as broken rather than pending. */
.btn.is-loading {
  opacity: 1;
  cursor: progress;
}

/* The control that commits: the accent, with the faint top highlight a filled system
   button has. */
.btn-primary {
  background: var(--accent);
  color: var(--accent-fg);
  box-shadow: inset 0 1px 0 var(--accent-highlight);
}

.btn-primary:hover {
  background: var(--accent-hover);
}

/* The system's gray push button: a translucent fill, no border, so it reads the same on
   paper, on a grouped list and on glass. */
.btn-secondary {
  background: var(--fill);
  color: var(--ink);
}

.btn-secondary:hover {
  background: var(--fill-strong);
}

.btn-ghost {
  background: transparent;
  color: var(--muted);
}

.btn-ghost:hover {
  background: var(--fill);
  color: var(--ink);
}

/* Destructive: the word in the system red on the gray fill — a revoke button sits in a
   table row and must not shout down the row it belongs to. */
.btn-danger {
  background: var(--fill);
  color: var(--danger);
}

.btn-danger:hover {
  background: var(--danger-hover);
}

.btn-spinner {
  flex-shrink: 0;
}

.btn :deep(svg) {
  flex-shrink: 0;
  width: 16px;
  height: 16px;
}

.btn-lg :deep(svg) {
  width: 18px;
  height: 18px;
}

.btn-sm :deep(svg),
.btn-xs :deep(svg) {
  width: 14px;
  height: 14px;
}
</style>
