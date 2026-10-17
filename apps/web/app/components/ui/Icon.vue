<script setup lang="ts">
/**
 * The app's glyphs, inlined.
 *
 * One Lucide set on Lucide's own 24 grid (1.75 stroke, round caps and joins) so a Copy in a
 * panel header, a Revoke row and a destination in the sidebar read as one family. Inline
 * rather than an icon package: a dozen glyphs do not justify a dependency, and a remote
 * sprite would be a render-blocking request for chrome that must paint immediately.
 *
 * Size is the call site's — every consumer sets width and height on the element — because
 * this design uses the same glyph at 13, 15, 16 and 22px.
 *
 * Always decorative. An icon-only control carries its name in `UiButton`'s `label` (which
 * becomes both the accessible name and the tooltip) and a nav item in the word beside it,
 * never in the glyph — so this is unconditionally `aria-hidden`.
 */
import type { IconName } from '~/lib/icons'

defineProps<{ name: IconName }>()
</script>

<template>
  <svg
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    stroke-width="1.75"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
    focusable="false"
  >
    <!-- scan-text: the brand mark's glyph — a page being read inside scan corners. Also
         the figure placeholder's mark in the result viewer. -->
    <template v-if="name === 'scan-text'">
      <path d="M3 7V5a2 2 0 0 1 2-2h2" />
      <path d="M17 3h2a2 2 0 0 1 2 2v2" />
      <path d="M21 17v2a2 2 0 0 1-2 2h-2" />
      <path d="M7 21H5a2 2 0 0 1-2-2v-2" />
      <path d="M7 8h8M7 12h10M7 16h6" />
    </template>

    <!-- chart-column (Usage): bars on a baseline — the page is cost and tokens over time. -->
    <template v-else-if="name === 'dashboard'">
      <path d="M3 3v16a2 2 0 0 0 2 2h16" />
      <path d="M18 17V9M13 17V5M8 17v-3" />
    </template>

    <!-- history: a clock with a rewind arrow — the record of what already ran. -->
    <template v-else-if="name === 'history'">
      <path d="M3 12a9 9 0 1 0 3.13-6.82" />
      <path d="M3 4.5V9h4.5" />
      <path d="M12 7.5V12l3.5 2" />
    </template>

    <!-- key-round (API keys): the bit end of a key. -->
    <template v-else-if="name === 'keys'">
      <path
        d="M2.586 17.414A2 2 0 0 0 2 18.828V21a1 1 0 0 0 1 1h3a1 1 0 0 0 1-1v-1a1 1 0 0 1 1-1h1a1 1 0 0 0 1-1v-1a1 1 0 0 1 1-1h.172a2 2 0 0 0 1.414-.586l.814-.814a6.5 6.5 0 1 0-4-4z"
      />
      <circle cx="16.5" cy="7.5" r=".5" fill="currentColor" />
    </template>

    <!-- plug (MCP Connector): prongs up, waiting to be plugged in. -->
    <template v-else-if="name === 'connect'">
      <path d="M9 2v6M15 2v6" />
      <path d="M6 8h12v3a6 6 0 0 1-12 0V8z" />
      <path d="M12 17v5" />
    </template>

    <!-- globe (Language): a sphere with a meridian and the equator — the one mark every
         product uses for a locale, so it needs no learning. -->
    <template v-else-if="name === 'globe'">
      <circle cx="12" cy="12" r="9" />
      <path d="M3 12h18" />
      <path d="M12 3a15 15 0 0 1 0 18a15 15 0 0 1 0-18z" />
    </template>

    <!-- bot (Agent): the one glyph every product uses for an agent that works for you. -->
    <template v-else-if="name === 'agent'">
      <path d="M12 8V4H8" />
      <rect width="16" height="12" x="4" y="8" rx="2" />
      <path d="M2 14h2M20 14h2" />
      <path d="M15 13v2M9 13v2" />
    </template>

    <!-- plus: create. -->
    <template v-else-if="name === 'plus'">
      <path d="M12 5v14M5 12h14" />
    </template>

    <!-- pencil: edit the thing beside it. -->
    <template v-else-if="name === 'edit'">
      <path d="M21.174 6.812a1 1 0 0 0-3.986-3.987L3.842 16.174a2 2 0 0 0-.5.83l-1.321 4.352a.5.5 0 0 0 .623.622l4.353-1.32a2 2 0 0 0 .83-.497z" />
      <path d="m15 5 4 4" />
    </template>

    <!-- trash: delete a stored value. Paired with a visible word wherever it destroys
         something the user would have to re-create. -->
    <template v-else-if="name === 'trash'">
      <path d="M3 6h18" />
      <path d="M8 6V4a1 1 0 0 1 1-1h6a1 1 0 0 1 1 1v2" />
      <path d="M19 6l-.867 12.142A2 2 0 0 1 16.138 20H7.862a2 2 0 0 1-1.995-1.858L5 6" />
      <path d="M10 11v6M14 11v6" />
    </template>

    <!-- copy: two offset sheets. -->
    <template v-else-if="name === 'copy'">
      <rect x="8" y="8" width="14" height="14" rx="2" />
      <path d="M4 16a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2" />
    </template>

    <!-- x: dismiss one thing. -->
    <template v-else-if="name === 'close'">
      <path d="M18 6 6 18M6 6l12 12" />
    </template>

    <!-- log-out: leave through the door. -->
    <template v-else-if="name === 'sign-out'">
      <path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4" />
      <path d="m16 17 5-5-5-5" />
      <path d="M21 12H9" />
    </template>

    <!-- menu: opens the sidebar drawer below the shell breakpoint. -->
    <template v-else-if="name === 'menu'">
      <path d="M4 6h16M4 12h16M4 18h16" />
    </template>

    <!-- refresh-cw: the gap and the arrowhead are what distinguish it from the spinner it
         is replaced by while a refresh is in flight. -->
    <template v-else-if="name === 'refresh'">
      <path d="M21 12a9 9 0 0 1-9 9 9 9 0 0 1-6.36-2.64L3 16" />
      <path d="M3 12a9 9 0 0 1 9-9 9 9 0 0 1 6.36 2.64L21 8" />
      <path d="M21 3v5h-5M3 21v-5h5" />
    </template>

    <!-- folders (Files): one folder behind another — the nav destination that is a tree. -->
    <template v-else-if="name === 'files'">
      <path d="M8 17a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h2.88a2 2 0 0 1 1.41.59l1.12 1.12a2 2 0 0 0 1.42.59H20a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2z" />
      <path d="M2 8v11a2 2 0 0 0 2 2h14" />
    </template>

    <!-- folder: a closed directory in the tree and in the list. -->
    <template v-else-if="name === 'folder'">
      <path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z" />
    </template>

    <!-- folder-open: the folder you are inside, and the one a drag is hovering over. -->
    <template v-else-if="name === 'folder-open'">
      <path d="M6 14l1.5-2.9A2 2 0 0 1 9.24 10H20a2 2 0 0 1 1.94 2.5l-1.54 6a2 2 0 0 1-1.95 1.5H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h3.9a2 2 0 0 1 1.69.9l.81 1.2a2 2 0 0 0 1.67.9H18a2 2 0 0 1 2 2v2" />
    </template>

    <!-- folder-plus: file something into the library that is not in it yet. -->
    <template v-else-if="name === 'folder-plus'">
      <path d="M12 10v6" />
      <path d="M9 13h6" />
      <path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z" />
    </template>

    <!-- folder-input: move this into a folder — the arrow points in. -->
    <template v-else-if="name === 'folder-input'">
      <path d="M2 9V5a2 2 0 0 1 2-2h3.9a2 2 0 0 1 1.69.9l.81 1.2a2 2 0 0 0 1.67.9H20a2 2 0 0 1 2 2v10a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2v-1" />
      <path d="M2 13h10" />
      <path d="m9 16 3-3-3-3" />
    </template>

    <!-- file-text: a parsed PDF, lines and all. -->
    <template v-else-if="name === 'file-text'">
      <path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z" />
      <path d="M14 2v4a2 2 0 0 0 2 2h4" />
      <path d="M16 13H8M16 17H8M10 9H8" />
    </template>

    <!-- image: a parsed image, distinguished from a PDF at a glance in the list. -->
    <template v-else-if="name === 'image'">
      <rect x="3" y="3" width="18" height="18" rx="2" />
      <circle cx="9" cy="9" r="1.5" />
      <path d="m21 15-3.09-3.09a2 2 0 0 0-2.82 0L6 21" />
    </template>

    <!-- upload: into the library, arrow up out of the tray. -->
    <template v-else-if="name === 'upload'">
      <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
      <path d="m17 8-5-5-5 5" />
      <path d="M12 3v12" />
    </template>

    <!-- chevron-right / chevron-down: a closed and an open disclosure in the folder tree,
         and the separator between breadcrumbs. -->
    <template v-else-if="name === 'chevron-right'">
      <path d="m9 18 6-6-6-6" />
    </template>

    <template v-else-if="name === 'chevron-down'">
      <path d="m6 9 6 6 6-6" />
    </template>

    <!-- chevron-up: the previous hit, beside chevron-down's next, in the viewer's find bar. -->
    <template v-else-if="name === 'chevron-up'">
      <path d="m18 15-6-6-6 6" />
    </template>

    <!-- search: find in the open document. -->
    <template v-else-if="name === 'search'">
      <circle cx="11" cy="11" r="7" />
      <path d="m21 21-4.35-4.35" />
    </template>

    <!-- panel-left: shows and hides the result viewer's page rail — the glyph is the
         layout it toggles, a column beside a body. -->
    <template v-else-if="name === 'panel-left'">
      <rect x="3" y="3" width="18" height="18" rx="2" />
      <path d="M9 3v18" />
    </template>

    <!-- notebook-text: the result viewer's notes column — a bound pad with lines on it. -->
    <template v-else-if="name === 'notebook'">
      <path d="M2 6h4M2 10h4M2 14h4M2 18h4" />
      <rect x="4" y="2" width="16" height="20" rx="2" />
      <path d="M9.5 8h5M9.5 12H16M9.5 16H14" />
    </template>

    <!-- message-circle: the result viewer's agent panel — a conversation. -->
    <template v-else-if="name === 'message-circle'">
      <path d="M7.9 20A9 9 0 1 0 4 16.1L2 22Z" />
    </template>

    <!-- arrow-up: send, in the composer's round accent button. -->
    <template v-else-if="name === 'arrow-up'">
      <path d="m5 12 7-7 7 7" />
      <path d="M12 19V5" />
    </template>

    <!-- paperclip: the composer's attach control. -->
    <template v-else-if="name === 'paperclip'">
      <path d="m21.44 11.05-9.19 9.19a6 6 0 0 1-8.49-8.49l8.57-8.57A4 4 0 1 1 18 8.84l-8.59 8.57a2 2 0 0 1-2.83-2.83l8.49-8.48" />
    </template>

    <!-- square: stop the running turn. -->
    <template v-else-if="name === 'square'">
      <rect x="4" y="4" width="16" height="16" rx="1.5" />
    </template>

    <!-- x, small: a tool call that failed, beside check for one that succeeded. -->
    <template v-else-if="name === 'x'">
      <path d="M18 6 6 18M6 6l12 12" />
    </template>

    <!-- reply: answer one passage of an answer — the arrow turns back to it. -->
    <template v-else-if="name === 'reply'">
      <path d="m9 17-5-5 5-5" />
      <path d="M20 18v-2a4 4 0 0 0-4-4H4" />
    </template>

    <!-- square-slash: the composer's command control — a key with the slash on it. -->
    <template v-else-if="name === 'square-slash'">
      <rect x="3" y="3" width="18" height="18" rx="2" />
      <path d="M9 15 15 9" />
    </template>

    <!-- terminal: a step that ran a command in the shell. -->
    <template v-else-if="name === 'terminal'">
      <path d="m4 17 6-6-6-6" />
      <path d="M12 19h8" />
    </template>

    <!-- hash: a step that counted lines or words. -->
    <template v-else-if="name === 'hash'">
      <path d="M4 9h16M4 15h16M10 3 8 21M16 3l-2 18" />
    </template>

    <!-- file-plus: a step that created a file. -->
    <template v-else-if="name === 'file-plus'">
      <path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z" />
      <path d="M14 2v4a2 2 0 0 0 2 2h4" />
      <path d="M9 15h6M12 18v-6" />
    </template>

    <!-- crop: a step that looked at one figure — the crop of a page. -->
    <template v-else-if="name === 'crop'">
      <path d="M6 2v14a2 2 0 0 0 2 2h14" />
      <path d="M18 22V8a2 2 0 0 0-2-2H2" />
    </template>

    <!-- book-open: a skill — instructions the agent reads before it starts. -->
    <template v-else-if="name === 'book-open'">
      <path d="M12 7v14" />
      <path d="M3 18a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1h5a4 4 0 0 1 4 4 4 4 0 0 1 4-4h5a1 1 0 0 1 1 1v13a1 1 0 0 1-1 1h-6a3 3 0 0 0-3 3 3 3 0 0 0-3-3z" />
    </template>

    <!-- rotate-ccw: put the shipped default back. -->
    <template v-else-if="name === 'rotate-ccw'">
      <path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8" />
      <path d="M3 3v5h5" />
    </template>

    <!-- check: the confirmation a copy swaps to. -->
    <template v-else>
      <path d="M20 6 9 17l-5-5" />
    </template>
  </svg>
</template>
