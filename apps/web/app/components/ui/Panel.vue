<script setup lang="ts">
/**
 * A region of a screen's content: an optional header over a body that scrolls on its own.
 *
 * A panel has no border, no radius and no shadow: it is the content's own surface, on paper
 * (docs/web.md § Design system). On a screen the large title is the screen's, so the list's
 * panel there has no header; a section of the settings dialog gives its panels one.
 *
 * `min-width: 0` and `min-height: 0` are load-bearing on both the panel and its body: a
 * flex child defaults to `auto`, and one wide table would then push the whole content
 * region past the viewport instead of scrolling inside here.
 */
defineProps<{
  title?: string
}>()

defineSlots<{
  default: () => unknown
  /**
   * Replaces the `<h2>`. For a heading that is more than a string.
   */
  title?: () => unknown
  /** Right of the heading: this dataset's own metadata ("34 days recorded") or one small button. */
  meta?: () => unknown
}>()

const body = ref<HTMLElement | null>(null)

/**
 * Puts the top of the body back in view. A panel owns its own scroll axis, so something
 * inserted at the top of it — the one-time key reveal — can land entirely above the
 * viewport with nothing to tell the reader it is there.
 *
 * Both axes, because there are two: the body's own above the breakpoint, and the content
 * region's below it, where the body no longer scrolls and the panel itself can be the part
 * that is off-screen.
 */
function scrollToTop() {
  const el = body.value
  if (!el) {
    return
  }
  el.scrollTo({ top: 0 })
  el.scrollIntoView({ block: 'nearest' })
}

defineExpose({ scrollToTop })
</script>

<template>
  <section class="panel">
    <header v-if="title || $slots.title || $slots.meta" class="panel-head">
      <slot name="title">
        <h2 v-if="title" class="panel-title">{{ title }}</h2>
      </slot>
      <div v-if="$slots.meta" class="panel-meta">
        <slot name="meta" />
      </div>
    </header>

    <div ref="body" class="panel-body">
      <slot />
    </div>
  </section>
</template>

<style scoped>
.panel {
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
  background: var(--paper);
}

/* Baseline alignment, not centre: the bold heading and the smaller meta beside it have
   different cap heights, and centring them leaves the meta floating. */
.panel-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--space-4);
  flex-shrink: 0;
  padding: var(--space-6) var(--panel-x) var(--space-3);
}

.panel-title {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: var(--display-xs);
  letter-spacing: var(--tracking-heading);
}

/* Quiet: a fact about this panel's dataset, never a subtitle explaining it. */
.panel-meta {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  flex-shrink: 0;
  font-size: var(--text-xs);
  color: var(--muted);
  white-space: nowrap;
}

/* The scroll axis this panel owns. */
.panel-body {
  flex: 1;
  min-width: 0;
  min-height: 0;
  overflow: auto;
  overscroll-behavior: contain;
  padding: 0 var(--panel-x) var(--space-6);
}

/*
 * Below the breakpoint the whole content region becomes one scroll axis, so a panel's body
 * is no longer a bounded box: it takes its natural height and `overflow: auto` then only
 * ever produces the horizontal scrollbar a wide table needs.
 */
@media (max-width: 900px) {
  .panel-head {
    padding-top: var(--space-5);
  }
}
</style>
