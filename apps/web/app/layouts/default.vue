<script setup lang="ts">
import type { ComponentPublicInstance } from 'vue'
import { navEntries } from '~/lib/admin'
import type { MenuItem, Point } from '~/lib/menu'
import { SETTINGS_QUERY, settingsSectionOf } from '~/lib/settings'

/**
 * The signed-in frame.
 *
 * A fixed grid, not a scrolling document: the sidebar is a glass pane floating at the
 * window's left, and the content region is handed a bounded box, which is what lets each
 * screen keep its toolbar and title while its content scrolls (docs/web.md § Design system,
 * *The frame*). Nothing in the signed-in app puts a whole screen on one scrollbar.
 *
 * The account row is the session's own menu: the avatar-and-name button opens `UiContextMenu`
 * — the same floating surface the library's right-click opens — headed by the address that is
 * signed in, holding Upgrade (or Usage), Settings, Language, Admin console for an operator,
 * and sign-out below a rule.
 * Everything the account is, in one place, the way a desktop app's account menu holds it.
 * Language is a row rather than a submenu: it opens `LanguageDialog` (docs/web.md § The
 * account menu).
 *
 * Under `/admin` the frame is the operator console's: the nav lists the console's pages the
 * account can read, under a line naming the console with the way back to Files, and
 * everything else stays — one layout, so the account menu, the drawer and the dialogs are
 * the same code (docs/web.md § Design system, *The frame*; docs/admin.md § Pages).
 *
 * Below 900px the sidebar becomes a drawer behind a menu button. No icon rail and no bottom
 * tab bar: the labels are doing the work, and a tab bar would spend the scarcest axis on a
 * phone.
 */
const { t } = useI18n()
const route = useRoute()
const localePath = useLocalePath()
const auth = useAuth()

/**
 * The screen that is the work. Everything configured once — the agent, keys, the connector,
 * usage, theme, the account — is a section of the settings dialog behind the account menu,
 * so the nav never fills with things opened twice a month (docs/web.md § Pages).
 */
const NAV = [{ icon: 'files', to: '/files', label: 'nav.files' }] as const

/** Whether a console page is open: then the nav is the console's. */
const inConsole = computed(() => {
  const base = localePath('/admin')
  return route.path === base || route.path.startsWith(`${base}/`)
})
const adminConsole = useAdminConsole()
/** The console's pages the account can read, in nav order (`app/lib/admin.ts`). */
const consoleNav = computed(() => navEntries(adminConsole.permissions.value))

/** The signed-in identity: the name when Google sent one, the address otherwise. */
const account = computed(() => auth.me.value?.user.name?.trim() || auth.me.value?.user.email || '')
/** The Google avatar; the identity's first letter stands in when there is none (dev
 *  sign-in) or when the image fails to load. */
const picture = computed(() => auth.me.value?.user.picture ?? null)
const pictureFailed = ref(false)
watch(picture, () => {
  pictureFailed.value = false
})
const initial = computed(() => (account.value[0] ?? '?').toUpperCase())

// --- the account menu --------------------------------------------------------

const accountButton = ref<HTMLElement | null>(null)
/** Where the menu is anchored, and whether it is open at all. */
const accountMenu = ref<Point | null>(null)

/**
 * When the open menu last closed itself. Pressing the row while the menu is up closes it on
 * `pointerdown` — the menu dismisses on a press anywhere outside itself — and the `click`
 * that follows would then reopen it, so a second press on the row would never close it. The
 * press that dismissed a menu is not a press that opens one.
 */
let accountMenuClosedAt = 0

/** Anchored to the row's top-left. The row sits at the foot of a full-height sidebar, so
 *  `placeMenu` never finds room below it and flips it up over the nav — which is where an
 *  account menu belongs. */
function toggleAccountMenu() {
  if (accountMenu.value || performance.now() - accountMenuClosedAt < 200) {
    accountMenu.value = null
    return
  }
  const box = accountButton.value?.getBoundingClientRect()
  accountMenu.value = box ? { x: box.left, y: box.top } : { x: 0, y: 0 }
}

function closeAccountMenu() {
  accountMenu.value = null
  accountMenuClosedAt = performance.now()
}

/** Upgrade on Free — the pricing page, where the plans are presented and the checkout
 *  starts — and Usage on a paid plan, the Usage section; the menu starts no checkout
 *  itself (docs/web.md § The account menu). */
const accountItems = computed<MenuItem[]>(() => [
  ...(auth.me.value
    ? [
        auth.me.value.plan === 'free'
          ? { key: 'upgrade', label: t('nav.upgrade'), icon: 'credit-card' as const }
          : { key: 'usage', label: t('nav.usage'), icon: 'dashboard' as const },
      ]
    : []),
  { key: 'settings', label: t('nav.settings'), icon: 'settings' },
  { key: 'language', label: t('nav.language'), icon: 'globe' },
  // Only for an account that holds a console permission while the console is on; a link,
  // so following it is the navigation (docs/web.md § The account menu).
  ...(auth.me.value?.console
    ? [{ key: 'admin', label: t('nav.admin'), icon: 'shield' as const, to: localePath('/admin') }]
    : []),
  { key: 'signOut', label: t('nav.signOut'), icon: 'sign-out', separated: true },
])

/** The language dialog the menu's Language row opens (`LanguageDialog`). */
const languageOpen = ref(false)

/**
 * The settings dialog is open while the route carries `?settings=<section>`
 * (`SettingsDialog`): opening it pushes the query over the current screen, so Back closes
 * it, and the screen stays where it was behind the scrim.
 */
const router = useRouter()
const settingsSection = computed(() => settingsSectionOf(route.query[SETTINGS_QUERY]))

function onAccountAction(action: string) {
  if (action === 'upgrade') {
    void navigateTo(localePath('/pricing'))
  } else if (action === 'settings' || action === 'usage') {
    void router.push({ query: { ...route.query, [SETTINGS_QUERY]: action === 'usage' ? 'usage' : 'general' } })
  } else if (action === 'language') {
    languageOpen.value = true
  } else if (action === 'signOut') {
    void signOut()
  }
}

// Following a menu item navigates; the route change is what closes the menu. The full
// path, since Settings navigates by query alone.
watch(() => route.fullPath, () => {
  accountMenu.value = null
})

const sidebar = ref<HTMLElement | null>(null)
/** A ref on a component yields its instance; `UiButton` with neither `to` nor `href` roots
 *  a real `<button>`, which is the element focus has to return to. */
const menuButton = ref<ComponentPublicInstance | null>(null)
const drawerOpen = ref(false)

const FOCUSABLE = 'a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"])'

/** Matches the 900px breakpoint in this component's own stylesheet. */
function isDrawerLayout(): boolean {
  return typeof matchMedia !== 'undefined' && matchMedia('(max-width: 900px)').matches
}

// A destination the user just picked is behind the drawer they picked it from — the
// settings dialog too, which opens by query from the drawer's account menu and is teleported
// outside the drawer, where the drawer's focus trap would otherwise hold Tab.
watch(() => route.fullPath, () => {
  drawerOpen.value = false
})

/**
 * Open, the drawer covers the page behind a scrim — that makes it modal, and modal surfaces
 * owe the same three things a dialog does: focus moves in, Tab stays in, and focus returns
 * to the trigger on close. Without the trap, Tab walks invisibly through the page beneath.
 */
watch(drawerOpen, async (open) => {
  if (!isDrawerLayout()) return
  if (!open) {
    ;(menuButton.value?.$el as HTMLElement | undefined)?.focus()
    return
  }
  await nextTick()
  sidebar.value?.querySelector<HTMLElement>(FOCUSABLE)?.focus()
})

function onKeydown(event: KeyboardEvent) {
  // `drawerOpen` only means anything below the breakpoint — above it the sidebar is a static
  // column, and trapping focus in it would strand the user in the nav. The flag can be left
  // true by a resize, so check the layout rather than trusting it alone.
  if (!drawerOpen.value || !isDrawerLayout()) return

  if (event.key === 'Escape') {
    drawerOpen.value = false
    return
  }
  if (event.key !== 'Tab' || !sidebar.value) return

  const items = [...sidebar.value.querySelectorAll<HTMLElement>(FOCUSABLE)]
  if (!items.length) return
  const first = items[0]!
  const last = items[items.length - 1]!
  const active = document.activeElement

  // Also catches focus already outside the drawer — a click on the scrim, say — pulling it
  // back rather than letting Tab continue through the page behind.
  if (event.shiftKey && (active === first || !sidebar.value.contains(active))) {
    event.preventDefault()
    last.focus()
  } else if (!event.shiftKey && (active === last || !sidebar.value.contains(active))) {
    event.preventDefault()
    first.focus()
  }
}

onMounted(() => window.addEventListener('keydown', onKeydown))
onBeforeUnmount(() => window.removeEventListener('keydown', onKeydown))

async function signOut() {
  await auth.signOut()
  await navigateTo(localePath('/'))
}
</script>

<template>
  <div class="shell">
    <a class="skip-link" href="#content">{{ t('app.skipToContent') }}</a>

    <!-- Under the drawer and above the content; clicking it closes. -->
    <div v-if="drawerOpen" class="scrim" @click="drawerOpen = false" />

    <aside ref="sidebar" class="sidebar" :class="{ open: drawerOpen }">
      <div class="sidebar-head">
        <AppBrand :to="localePath('/files')" />
        <UiButton
          class="drawer-close"
          variant="ghost"
          size="sm"
          icon-only
          :label="t('nav.closeMenu')"
          @click="drawerOpen = false"
        >
          <template #icon><UiIcon name="close" /></template>
        </UiButton>
      </div>

      <!-- The console's nav: its name with the way back, then its pages. -->
      <template v-if="inConsole">
        <div class="console-head">
          <span class="console-title">{{ t('admin.title') }}</span>
          <NuxtLink class="console-back" :to="localePath('/files')">{{ t('admin.backToFiles') }}</NuxtLink>
        </div>
        <nav class="nav" :aria-label="t('admin.navLabel')">
          <NuxtLink
            v-for="item in consoleNav"
            :key="item.path"
            class="nav-item"
            :to="localePath(item.path)"
            active-class="active"
          >
            <UiIcon :name="item.icon" />
            <span class="nav-label">{{ t(item.label) }}</span>
          </NuxtLink>
        </nav>
      </template>

      <nav v-else class="nav" :aria-label="t('nav.primary')">
        <NuxtLink
          v-for="item in NAV"
          :key="item.to"
          class="nav-item"
          :to="localePath(item.to)"
          active-class="active"
        >
          <UiIcon :name="item.icon" />
          <span class="nav-label">{{ t(item.label) }}</span>
        </NuxtLink>
      </nav>

      <!-- Who is signed in: one button, opening the one menu the session has. -->
      <div class="sidebar-foot">
        <!-- Rendered even while `GET /api/me` is still in flight: the label is the row's
             only flexible track, and dropping it would slide the button across the foot the
             moment the identity arrives. -->
        <button
          ref="accountButton"
          type="button"
          class="account"
          :title="account || undefined"
          aria-haspopup="menu"
          :aria-expanded="accountMenu ? 'true' : 'false'"
          @click="toggleAccountMenu"
        >
          <img
            v-if="picture && !pictureFailed"
            class="avatar"
            :src="picture"
            alt=""
            referrerpolicy="no-referrer"
            @error="pictureFailed = true"
          >
          <span v-else class="avatar avatar-initial" aria-hidden="true">{{ initial }}</span>
          <span class="account-label">{{ account }}</span>
          <UiIcon class="account-caret" name="chevrons-up-down" />
        </button>

        <UiContextMenu
          v-if="accountMenu"
          :at="accountMenu"
          :items="accountItems"
          :label="account"
          :header="auth.me.value?.user.email"
          @select="onAccountAction"
          @close="closeAccountMenu"
        />

        <LanguageDialog v-if="languageOpen" @close="languageOpen = false" />
        <SettingsDialog v-if="settingsSection" :section="settingsSection" />
      </div>
    </aside>

    <div class="frame">
      <header class="mobile-bar">
        <UiButton
          ref="menuButton"
          variant="ghost"
          size="sm"
          icon-only
          :label="t('nav.openMenu')"
          :aria-expanded="drawerOpen ? 'true' : 'false'"
          @click="drawerOpen = true"
        >
          <template #icon><UiIcon name="menu" /></template>
        </UiButton>
        <AppBrand :to="localePath('/files')" />
      </header>

      <!--
        The content region. Every screen is a `UiScreen` — a toolbar, a title and content
        that scrolls on its own — so this element never scrolls above the breakpoint, and
        below it becomes the single one.
      -->
      <main id="content" class="content" tabindex="-1">
        <slot />
      </main>
    </div>
  </div>
</template>

<style scoped>
.shell {
  display: grid;
  /* The sidebar's column holds the pane and the inset on both its sides. */
  grid-template-columns: calc(var(--sidebar-width) + 2 * var(--sidebar-inset)) minmax(0, 1fr);
  /* The frame owns the viewport: the document behind it never scrolls, which is what keeps
     the sidebar put and gives every screen's panels a bounded box to scroll inside. */
  height: 100dvh;
  overflow: hidden;
  background: var(--paper);
}

.skip-link {
  position: absolute;
  top: var(--space-2);
  left: var(--space-2);
  z-index: 80;
  padding: var(--space-2) var(--space-4);
  border-radius: var(--radius-full);
  background: var(--accent);
  color: var(--accent-fg);
  font-size: var(--text-sm);
  font-weight: var(--weight-medium);
  transform: translateY(-200%);
}

.skip-link:focus {
  transform: none;
}

/*
 * Only the drawer layout has a scrim, and `drawerOpen` can still be true when the viewport
 * crosses back above the breakpoint — a rotated tablet. Declared hidden here rather than
 * only styled inside the media query: an unstyled `div` is a perfectly good grid child, and
 * it was taking the sidebar's cell and pushing the whole frame one column over.
 */
.scrim {
  display: none;
}

/* --- Sidebar -------------------------------------------------------------- */

/* A glass pane floating at the window's left, inset by the same margin on three sides
   (docs/web.md § Design system, *Glass is for what floats*). */
.sidebar {
  display: flex;
  flex-direction: column;
  width: var(--sidebar-width);
  min-height: 0;
  margin: var(--sidebar-inset) 0 var(--sidebar-inset) var(--sidebar-inset);
  padding: 14px 10px 10px;
  border-radius: var(--radius-card);
  background: var(--glass-pane);
  -webkit-backdrop-filter: var(--glass-blur);
  backdrop-filter: var(--glass-blur);
  box-shadow: var(--shadow-pane);
}

@supports not (backdrop-filter: blur(1px)) {
  .sidebar {
    background: var(--rail);
  }
}

.sidebar-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-2);
  min-height: var(--space-10);
  padding: 0 var(--space-2);
  margin-bottom: 14px;
}

/* The close control only exists once the sidebar is a drawer. Qualified by its parent so it
   outranks `UiButton`'s own `display` without depending on stylesheet order. */
.sidebar-head .drawer-close {
  display: none;
}

/* Under /admin: the console's name, quiet, with the way back at its right. */
.console-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: var(--space-2);
  padding: 0 var(--space-3) var(--space-2);
}

.console-title {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--muted);
  font-size: var(--text-xs);
  font-weight: var(--weight-semibold);
}

.console-back {
  flex-shrink: 0;
  color: var(--accent);
  font-size: var(--text-xs);
}

.console-back:hover {
  color: var(--accent-hover);
}

.nav {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-height: 0;
  overflow-y: auto;
  /* As a drawer this sits over the content region; scrolling it to its end must not chain
     through to the page behind. */
  overscroll-behavior: contain;
}

.nav-item {
  display: flex;
  align-items: center;
  gap: 10px;
  height: var(--control-height);
  padding: 0 var(--space-3);
  border-radius: var(--radius-md);
  color: var(--ink);
  font-size: var(--text-sm);
  overflow: hidden;
  transition: background var(--duration-fast) var(--ease);
}

.nav-item:hover {
  background: var(--fill-weak);
}

/* The current place: a fill and a weight step, as the system's sidebar marks it. */
.nav-item.active {
  background: var(--fill);
  font-weight: var(--weight-semibold);
}

/* The sidebar's glyphs carry the accent, as the system tints a sidebar's symbols. */
.nav-item :deep(svg) {
  width: 18px;
  height: 18px;
  flex-shrink: 0;
  color: var(--accent);
  pointer-events: none;
}

.nav-label {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* Pinned to the bottom: the session's own row. The popover anchors to it, so it owns a
   positioning context. */
.sidebar-foot {
  display: flex;
  align-items: center;
  margin-top: auto;
  padding-top: var(--space-2);
}

/* The disclosure: the avatar, the name and the chevrons are one button, a row like the
   nav's with the same hover. */
.account {
  display: flex;
  align-items: center;
  gap: 10px;
  flex: 1;
  min-width: 0;
  height: var(--space-12);
  padding: 0 var(--space-2);
  border: none;
  border-radius: var(--radius-lg);
  background: transparent;
  cursor: pointer;
  text-align: left;
  transition: background var(--duration-fast) var(--ease);
}

.account:hover,
.account[aria-expanded='true'] {
  background: var(--fill-weak);
}

.avatar {
  display: grid;
  place-items: center;
  width: 30px;
  height: 30px;
  flex-shrink: 0;
  border-radius: var(--radius-full);
  background: var(--paper);
  object-fit: cover;
}

/* No picture: the identity's first letter on a gray disc, as the system draws a contact
   without one. */
.avatar-initial {
  background: linear-gradient(180deg, var(--faint), var(--muted));
  color: var(--accent-fg);
  font-size: var(--text-xs);
  font-weight: var(--weight-semibold);
}

.account-label {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: var(--text-sm);
  font-weight: var(--weight-medium);
  color: var(--ink);
}

/* The one mark that says this row opens something, kept quiet. */
.account-caret {
  width: 14px;
  height: 14px;
  flex-shrink: 0;
  color: var(--muted);
}

/* --- Content -------------------------------------------------------------- */

.frame {
  display: flex;
  flex-direction: column;
  min-width: 0;
  min-height: 0;
}

.mobile-bar {
  display: none;
}

/*
 * A single bounded row for the screen inside it. `minmax(0, 1fr)` is what hands `UiScreen`
 * a height it can be shorter than, which is what its panels scroll within.
 */
.content {
  display: grid;
  grid-template-rows: minmax(0, 1fr);
  flex: 1;
  min-width: 0;
  min-height: 0;
  background: var(--paper);
}

/* The skip link's target: focused to move where the keyboard starts, a region and not a
   control, so it draws no ring, as a dialog's panel draws none (docs/web.md § Rules). A click
   on a title or a gap also focuses it, and in Safari a click on any button, and a key pressed
   then would put the ring around the whole screen. */
.content:focus {
  outline: none;
}

/* --- Responsive ----------------------------------------------------------- */

@media (max-width: 900px) {
  .shell {
    grid-template-columns: minmax(0, 1fr);
  }

  .scrim {
    display: block;
    position: fixed;
    inset: 0;
    z-index: 40;
    background: var(--overlay);
    overscroll-behavior: contain;
    animation: fade var(--duration) var(--ease-enter);
  }

  .sidebar {
    position: fixed;
    inset: var(--sidebar-inset) auto var(--sidebar-inset) var(--sidebar-inset);
    z-index: 50;
    margin: 0;
    background: var(--paper);
    box-shadow: var(--shadow-dialog);
    transform: translateX(calc(-100% - 2 * var(--sidebar-inset)));
    /* Off-screen *and* inert: a translated-away drawer is still focusable, so Tab would walk
       into a menu nobody can see. `visibility` fixes that, but it is not interpolable —
       applied plainly it would snap to hidden on the first frame and eat the slide-out.
       Hence the `0s` step delayed by the transform's own duration on close, undelayed on
       open. */
    visibility: hidden;
    transition:
      transform var(--duration) var(--ease-exit),
      visibility 0s linear var(--duration);
  }

  .sidebar.open {
    transform: none;
    visibility: visible;
    transition:
      transform var(--duration-slow) var(--ease-enter),
      visibility 0s;
  }

  .sidebar-head .drawer-close {
    display: inline-flex;
  }

  .mobile-bar {
    display: flex;
    align-items: center;
    gap: var(--space-3);
    min-height: var(--space-12);
    flex-shrink: 0;
    padding: var(--space-2) var(--space-4);
    background: var(--paper);
    border-bottom: 1px solid var(--line);
  }

  /* One scroll axis: the screen's toolbar, title and content scroll together. */
  .content {
    display: block;
    overflow-y: auto;
    overscroll-behavior: contain;
  }

  @keyframes fade {
    from {
      opacity: 0;
    }
  }
}
</style>
