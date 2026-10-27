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

    <!-- copy-plus: a second sheet made beside the first — Duplicate. -->
    <template v-else-if="name === 'copy-plus'">
      <path d="M15 12v6M12 15h6" />
      <rect x="8" y="8" width="14" height="14" rx="2" />
      <path d="M4 16a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2" />
    </template>

    <!-- clipboard-paste: what was copied, put here. -->
    <template v-else-if="name === 'clipboard-paste'">
      <path d="M11 14h10" />
      <path d="M16 4h2a2 2 0 0 1 2 2v1.344" />
      <path d="m17 18 4-4-4-4" />
      <path d="M8 4H6a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h12a2 2 0 0 0 1.793-1.113" />
      <rect x="8" y="2" width="8" height="4" rx="1" />
    </template>

    <!-- undo-2: Put Back — out of the Trash to where it was. -->
    <template v-else-if="name === 'undo-2'">
      <path d="M9 14 4 9l5-5" />
      <path d="M4 9h10.5a5.5 5.5 0 0 1 5.5 5.5a5.5 5.5 0 0 1-5.5 5.5H11" />
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

    <!-- chevron-left: the result viewer's back button, which closes it. -->
    <template v-else-if="name === 'chevron-left'">
      <path d="m15 18-6-6 6-6" />
    </template>

    <!-- chevrons-up-down: the sidebar's account button, which opens a menu over itself. -->
    <template v-else-if="name === 'chevrons-up-down'">
      <path d="m7 15 5 5 5-5" />
      <path d="m7 9 5-5 5 5" />
    </template>

    <!-- panel-left-close / panel-right-close: closing the reader's notes card (at the left)
         and agent card (at the right). -->
    <template v-else-if="name === 'panel-left-close'">
      <rect width="18" height="18" x="3" y="3" rx="2" />
      <path d="M9 3v18" />
      <path d="m16 15-3-3 3-3" />
    </template>

    <template v-else-if="name === 'panel-right-close'">
      <rect width="18" height="18" x="3" y="3" rx="2" />
      <path d="M15 3v18" />
      <path d="m8 9 3 3-3 3" />
    </template>

    <!-- square-pen: a new conversation, as the system's compose button draws it. -->
    <template v-else-if="name === 'square-pen'">
      <path d="M12 3H5a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7" />
      <path
        d="M18.375 2.625a1 1 0 0 1 3 3l-9.013 9.014a2 2 0 0 1-.853.505l-2.873.84a.5.5 0 0 1-.62-.62l.84-2.873a2 2 0 0 1 .506-.852z"
      />
    </template>

    <!-- search: find in the open document. -->
    <template v-else-if="name === 'search'">
      <circle cx="11" cy="11" r="7" />
      <path d="m21 21-4.35-4.35" />
    </template>

    <!-- ellipsis: the result viewer's More menu — what the header does not show. -->
    <template v-else-if="name === 'ellipsis'">
      <circle cx="12" cy="12" r="1" />
      <circle cx="19" cy="12" r="1" />
      <circle cx="5" cy="12" r="1" />
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

    <!-- circle-alert: a file that is not ready because something failed — its parse or its
         re-convert — in the danger tone after its name (docs/web.md § Files). -->
    <template v-else-if="name === 'circle-alert'">
      <circle cx="12" cy="12" r="10" />
      <path d="M12 8v4M12 16h.01" />
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

    <!-- locate: go to the place in the document an answer names. -->
    <template v-else-if="name === 'locate'">
      <path d="M2 12h3M19 12h3M12 2v3M12 19v3" />
      <circle cx="12" cy="12" r="7" />
    </template>
    <!-- bookmark: a memory the agent keeps about the reader. -->
    <template v-else-if="name === 'bookmark'">
      <path d="m19 21-7-4-7 4V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2v16z" />
    </template>
    <!-- bookmark-x: a memory removed, or a save undone. -->
    <template v-else-if="name === 'bookmark-x'">
      <path d="m19 21-7-4-7 4V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2v16z" />
      <path d="m14.5 7.5-5 5M9.5 7.5l5 5" />
    </template>
    <!-- text-quote: a passage of the document that goes with a message. -->
    <template v-else-if="name === 'text-quote'">
      <path d="M17 6H3M21 12H8M21 18H8M3 12v6" />
    </template>
    <!-- pin: always loaded. -->
    <template v-else-if="name === 'pin'">
      <path d="M12 17v5" />
      <path d="M9 10.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24V16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V7a1 1 0 0 1 1-1 2 2 0 0 0 0-4H8a2 2 0 0 0 0 4 1 1 0 0 1 1 1z" />
    </template>
    <!-- pause: memory kept but not used. -->
    <template v-else-if="name === 'pause'">
      <rect x="6" y="4" width="4" height="16" rx="1" />
      <rect x="14" y="4" width="4" height="16" rx="1" />
    </template>
    <!-- heading: a whole section of the document, named by its heading. -->
    <template v-else-if="name === 'heading'">
      <path d="M6 12h12" />
      <path d="M6 20V4" />
      <path d="M18 20V4" />
    </template>
    <!-- grip-vertical: a block's handle — six dots to take hold of. -->
    <template v-else-if="name === 'grip-vertical'">
      <circle cx="9" cy="12" r="1" />
      <circle cx="9" cy="5" r="1" />
      <circle cx="9" cy="19" r="1" />
      <circle cx="15" cy="12" r="1" />
      <circle cx="15" cy="5" r="1" />
      <circle cx="15" cy="19" r="1" />
    </template>
    <!-- languages: translate, on every surface that offers it. -->
    <template v-else-if="name === 'languages'">
      <path d="m5 8 6 6" />
      <path d="m4 14 6-6 2-3" />
      <path d="M2 5h12" />
      <path d="M7 2h1" />
      <path d="m22 22-5-10-5 10" />
      <path d="M14 18h6" />
    </template>
    <!-- notebook-pen: quote a passage in the notes. -->
    <template v-else-if="name === 'notebook-pen'">
      <path d="M13.4 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-7.4" />
      <path d="M2 6h4M2 10h4M2 14h4M2 18h4" />
      <path
        d="M21.378 5.626a1 1 0 1 0-3.004-3.004l-5.01 5.012a2 2 0 0 0-.506.854l-.837 2.87a.5.5 0 0 0 .62.62l2.87-.837a2 2 0 0 0 .854-.506z"
      />
    </template>
    <!-- quote: a passage quoted in the notes — the backlink marker and its menu item. -->
    <template v-else-if="name === 'quote'">
      <path
        d="M16 3a2 2 0 0 0-2 2v6a2 2 0 0 0 2 2 1 1 0 0 1 1 1v1a2 2 0 0 1-2 2 1 1 0 0 0-1 1v2a1 1 0 0 0 1 1 6 6 0 0 0 6-6V5a2 2 0 0 0-2-2z"
      />
      <path
        d="M5 3a2 2 0 0 0-2 2v6a2 2 0 0 0 2 2 1 1 0 0 1 1 1v1a2 2 0 0 1-2 2 1 1 0 0 0-1 1v2a1 1 0 0 0 1 1 6 6 0 0 0 6-6V5a2 2 0 0 0-2-2z"
      />
    </template>
    <!-- link: copy a link to a block. -->
    <template v-else-if="name === 'link'">
      <path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71" />
      <path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71" />
    </template>
    <!-- repeat: turn a block into another kind. -->
    <template v-else-if="name === 'repeat'">
      <path d="m17 2 4 4-4 4" />
      <path d="M3 11v-1a4 4 0 0 1 4-4h14" />
      <path d="m7 22-4-4 4-4" />
      <path d="M21 13v1a4 4 0 0 1-4 4H3" />
    </template>
    <!-- arrow-down: move a block down, beside arrow-up's move up. -->
    <template v-else-if="name === 'arrow-down'">
      <path d="M12 5v14" />
      <path d="m19 12-7 7-7-7" />
    </template>
    <!-- message-circle-plus: add a section to the agent's context. -->
    <template v-else-if="name === 'message-circle-plus'">
      <path d="M7.9 20A9 9 0 1 0 4 16.1L2 22Z" />
      <path d="M8 12h8M12 8v8" />
    </template>
    <!-- corner-up-left: back to where the reader was before a jump. -->
    <template v-else-if="name === 'corner-up-left'">
      <path d="M20 20v-7a4 4 0 0 0-4-4H4" />
      <path d="M9 14 4 9l5-5" />
    </template>
    <!-- list-tree: the outline, where its ticks would not fit. -->
    <template v-else-if="name === 'list-tree'">
      <path d="M21 12h-8M21 6H8M21 18h-8" />
      <path d="M3 6v4c0 1.1.9 2 2 2h3" />
      <path d="M3 10v6c0 1.1.9 2 2 2h3" />
    </template>
    <!-- type: a plain paragraph, in the slash menu. -->
    <template v-else-if="name === 'type'">
      <path d="M12 4v16" />
      <path d="M4 7V5a1 1 0 0 1 1-1h14a1 1 0 0 1 1 1v2" />
      <path d="M9 20h6" />
    </template>
    <!-- heading-2 / heading-3: a heading and a subheading. -->
    <template v-else-if="name === 'heading-2'">
      <path d="M4 12h8M4 18V6M12 18V6" />
      <path d="M21 18h-4c0-4 4-3 4-6 0-1.5-2-2.5-4-1" />
    </template>
    <template v-else-if="name === 'heading-3'">
      <path d="M4 12h8M4 18V6M12 18V6" />
      <path d="M17.5 10.5c1.7-1 3.5 0 3.5 1.5a2 2 0 0 1-2 2" />
      <path d="M17 17.5c2 1.5 4 .3 4-1.5a2 2 0 0 0-2-2" />
    </template>
    <!-- list / list-ordered: a bulleted and a numbered list. -->
    <template v-else-if="name === 'list'">
      <path d="M3 6h.01M3 12h.01M3 18h.01" />
      <path d="M8 6h13M8 12h13M8 18h13" />
    </template>
    <template v-else-if="name === 'list-ordered'">
      <path d="M10 6h11M10 12h11M10 18h11" />
      <path d="M4 10h2M4 6h1v4" />
      <path d="M6 18H4c0-1 2-2 2-3s-1-1.5-2-1" />
    </template>
    <!-- sigma: a display formula. -->
    <template v-else-if="name === 'sigma'">
      <path d="M18 7V5a1 1 0 0 0-1-1H6.5a.5.5 0 0 0-.4.8l4.5 6a2 2 0 0 1 0 2.4l-4.5 6a.5.5 0 0 0 .4.8H17a1 1 0 0 0 1-1v-2" />
    </template>
    <!-- code: a code block. -->
    <template v-else-if="name === 'code'">
      <path d="m16 18 6-6-6-6" />
      <path d="m8 6-6 6 6 6" />
    </template>
    <!-- table: a table. -->
    <template v-else-if="name === 'table'">
      <rect x="3" y="3" width="18" height="18" rx="2" />
      <path d="M12 3v18M3 9h18M3 15h18" />
    </template>
    <!-- square-function: the fix-math skill. -->
    <template v-else-if="name === 'square-function'">
      <rect x="3" y="3" width="18" height="18" rx="2" />
      <path d="M9 17c2 0 2.8-1 2.8-2.8V10c0-2 1-3.3 3.2-3" />
      <path d="M9 11.2h5.7" />
    </template>
    <!-- settings: the account menu's Settings row and the sheet's General section. -->
    <template v-else-if="name === 'settings'">
      <path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z" />
      <circle cx="12" cy="12" r="3" />
    </template>
    <!-- user: the Account section. -->
    <template v-else-if="name === 'user'">
      <path d="M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2" />
      <circle cx="12" cy="7" r="4" />
    </template>
    <!-- credit-card: Upgrade in the account menu. -->
    <template v-else-if="name === 'credit-card'">
      <rect x="2" y="5" width="20" height="14" rx="2" />
      <path d="M2 10h20" />
    </template>
    <!-- sun / moon / monitor: the three theme choices. -->
    <template v-else-if="name === 'sun'">
      <circle cx="12" cy="12" r="4" />
      <path d="M12 2v2M12 20v2m-7.07-17.07 1.41 1.41m11.32 11.32 1.41 1.41M2 12h2m16 0h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41" />
    </template>
    <template v-else-if="name === 'moon'">
      <path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z" />
    </template>
    <template v-else-if="name === 'monitor'">
      <rect x="2" y="3" width="20" height="14" rx="2" />
      <path d="M8 21h8M12 17v4" />
    </template>
    <!-- file: a file kept as it is, of no kind the list draws on its own. -->
    <template v-else-if="name === 'file'">
      <path d="M15 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V7Z" />
      <path d="M14 2v4a2 2 0 0 0 2 2h4" />
    </template>

    <!-- film: a video file. -->
    <template v-else-if="name === 'film'">
      <rect x="3" y="3" width="18" height="18" rx="2" />
      <path d="M7 3v18M17 3v18M3 7.5h4M3 12h18M3 16.5h4M17 7.5h4M17 16.5h4" />
    </template>

    <!-- music: an audio file. -->
    <template v-else-if="name === 'music'">
      <path d="M9 18V5l12-2v13" />
      <circle cx="6" cy="18" r="3" />
      <circle cx="18" cy="16" r="3" />
    </template>

    <!-- sheet: a spreadsheet. -->
    <template v-else-if="name === 'sheet'">
      <rect x="3" y="3" width="18" height="18" rx="2" />
      <path d="M3 9h18M3 15h18M9 9v12M15 9v12" />
    </template>

    <!-- presentation: a slide deck. -->
    <template v-else-if="name === 'presentation'">
      <path d="M2 3h20" />
      <path d="M21 3v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V3" />
      <path d="m7 21 5-5 5 5" />
    </template>

    <!-- archive: a compressed file. -->
    <template v-else-if="name === 'archive'">
      <rect x="2" y="3" width="20" height="5" rx="1" />
      <path d="M4 8v11a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8" />
      <path d="M10 12h4" />
    </template>

    <!-- download: the file's bytes, saved. -->
    <template v-else-if="name === 'download'">
      <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
      <path d="m7 10 5 5 5-5M12 15V3" />
    </template>
    <!-- sliders-horizontal: the Preferences section. -->
    <template v-else-if="name === 'sliders-horizontal'">
      <path d="M21 4h-7M10 4H3M21 12h-9M8 12H3M21 20h-5M12 20H3" />
      <path d="M14 2v4M8 10v4M16 18v4" />
    </template>

    <!-- shield: the account menu's Admin console — the operator's own pages. -->
    <template v-else-if="name === 'shield'">
      <path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z" />
    </template>

    <!-- users-round: the console's Groups — people bound to permissions. -->
    <template v-else-if="name === 'users-round'">
      <path d="M18 21a8 8 0 0 0-16 0" />
      <circle cx="10" cy="8" r="5" />
      <path d="M22 20c0-3.37-2-6.5-4-8a5 5 0 0 0-.45-8.3" />
    </template>

    <!-- scroll-text: the console's Audit — the record of every change. -->
    <template v-else-if="name === 'scroll-text'">
      <path d="M15 12h-5M15 8h-5" />
      <path d="M19 17V5a2 2 0 0 0-2-2H4" />
      <path d="M8 21h12a2 2 0 0 0 2-2v-1a1 1 0 0 0-1-1H11a1 1 0 0 0-1 1v1a2 2 0 1 1-4 0V5a2 2 0 1 0-4 0v2a1 1 0 0 0 1 1h3" />
    </template>

    <!-- check: the confirmation a copy swaps to. -->
    <template v-else>
      <path d="M20 6 9 17l-5-5" />
    </template>
  </svg>
</template>
