<script setup lang="ts">
/**
 * The one surface a signed-out visitor sees, and still the app's SEO page.
 *
 * Two columns: an ink brand panel that says what this is, and the sign-in panel. The brand
 * panel is deliberately dark in *both* themes — the single exception to the token palette —
 * so it carries its own local `--panel-*` values rather than reading `--paper`.
 *
 * The Google button is the other prescribed surface: white background, `#dadce0` border, the
 * official four-color mark inlined as SVG (never a remote asset), and Google's own dark
 * variant. Those values are Google's to set, not ours to tokenize.
 *
 * The locale links live here rather than in the signed-in shell: this is the page a visitor
 * lands on, and it is the last point where the choice costs nothing.
 *
 * Full-bleed, so the page owns its frame rather than sitting in a layout.
 */
import { devLogin, isDevLoginAvailable } from '~/lib/api'

definePageMeta({ layout: false })

const { t, locale, locales } = useI18n()
const localePath = useLocalePath()
const switchLocalePath = useSwitchLocalePath()
const auth = useAuth()
const { resolve } = useApiError()

useSeoMeta({
  title: () => t('login.seoTitle'),
  description: () => t('login.seoDescription'),
  ogTitle: () => t('login.seoTitle'),
  ogDescription: () => t('login.seoDescription'),
  ogType: 'website',
  twitterCard: 'summary',
})

const devLoginAvailable = ref(false)
const devLoginError = ref<string | null>(null)
const signingIn = ref(false)

/** A string, not a number: the interpolator runs numbers through `Intl.NumberFormat`, which
 *  would print the year with a thousands separator. */
const year = String(new Date().getFullYear())

onMounted(async () => {
  await auth.ensureLoaded()
  // The OAuth callback redirects to the web root, so this is the hop that lands a signed-in
  // visitor in the app. No signed-in variant of this page exists to fall back to.
  if (auth.signedIn.value) {
    await navigateTo(localePath('/files'))
    return
  }
  devLoginAvailable.value = await isDevLoginAvailable()
})

async function signInAsDeveloper() {
  signingIn.value = true
  devLoginError.value = null
  try {
    await devLogin()
    await auth.refresh()
    await navigateTo(localePath('/files'))
  } catch (error) {
    devLoginError.value = await resolve(error)
  } finally {
    signingIn.value = false
  }
}
</script>

<template>
  <div class="login-page">
    <aside class="login-brand">
      <div class="login-brand-inner">
        <AppBrand size="lg" />

        <div class="login-pitch">
          <h2>{{ t('login.pitchTitle') }}</h2>
          <p>{{ t('login.pitchBody') }}</p>
        </div>

        <!-- Two facts, not a feature list: what you talk to it with, and whose money pays
             for the vision calls. -->
        <dl class="login-facts">
          <div class="fact">
            <dt>{{ t('login.factInterfaces') }}</dt>
            <dd>{{ t('login.factInterfacesValue') }}</dd>
          </div>
          <div class="fact">
            <dt>{{ t('login.factBilling') }}</dt>
            <dd>{{ t('login.factBillingValue') }}</dd>
          </div>
        </dl>
      </div>
    </aside>

    <main class="login-panel">
      <div class="login-form">
        <p class="eyebrow">{{ t('login.account') }}</p>
        <h1>{{ t('login.signIn') }}</h1>
        <p class="login-lede">{{ t('login.lede') }}</p>

        <!-- A plain link, not a client OAuth call: the backend owns the redirect flow. The
             locale rides along because it lives in the URL everywhere else, and Google is
             the one leg that would drop it: the callback returns the visitor to the web app
             in the language they were reading this page in. -->
        <a class="btn-google" :href="`/api/auth/login?locale=${encodeURIComponent(locale)}`">
          <svg class="btn-google-mark" viewBox="0 0 18 18" aria-hidden="true" focusable="false">
            <path
              fill="#4285F4"
              d="M17.64 9.2045c0-.6381-.0573-1.2518-.1636-1.8409H9v3.4814h4.8436c-.2086 1.125-.8427 2.0782-1.7959 2.7164v2.2581h2.9087c1.7018-1.5668 2.6836-3.874 2.6836-6.615z"
            />
            <path
              fill="#34A853"
              d="M9 18c2.43 0 4.4673-.806 5.9564-2.1805l-2.9087-2.2581c-.8059.54-1.8368.859-3.0477.859-2.344 0-4.3282-1.5831-5.036-3.7104H.9574v2.3318C2.4382 15.9832 5.4818 18 9 18z"
            />
            <path
              fill="#FBBC05"
              d="M3.964 10.71c-.18-.54-.2822-1.1168-.2822-1.71s.1023-1.17.2823-1.71V4.9582H.9573A8.9965 8.9965 0 0 0 0 9c0 1.4523.3477 2.8268.9573 4.0418L3.964 10.71z"
            />
            <path
              fill="#EA4335"
              d="M9 3.5795c1.3214 0 2.5077.4541 3.4405 1.346l2.5813-2.5814C13.4632.8918 11.426 0 9 0 5.4818 0 2.4382 2.0168.9573 4.9582L3.964 7.29C4.6718 5.1627 6.6559 3.5795 9 3.5795z"
            />
          </svg>
          <span>{{ t('login.google') }}</span>
        </a>

        <div v-if="devLoginAvailable" class="login-dev">
          <UiButton class="dev-button" :loading="signingIn" @click="signInAsDeveloper">
            {{ t('login.devSignIn') }}
          </UiButton>
          <!-- Set to be read rather than shrunk and greyed: it is the one thing that explains
               why a second sign-in control is on the page at all. -->
          <span class="login-dev-note">{{ t('login.devSignInNote') }}</span>
        </div>

        <UiBanner v-if="devLoginError" class="login-error" tone="error">
          {{ devLoginError }}
        </UiBanner>
      </div>

      <footer class="login-footer">
        <span>{{ t('login.copyright', { year, name: t('app.name') }) }}</span>
        <!-- Each option is written in its own language, so it is legible whichever catalog
             is loaded. Real links: the locale lives in the URL, so the choice is
             bookmarkable and works without JavaScript on this prerendered page. -->
        <NuxtLink class="locale" :to="localePath('/pricing')">{{ t('login.pricing') }}</NuxtLink>
        <nav class="locales" :aria-label="t('nav.language')">
          <NuxtLink
            v-for="option in locales"
            :key="option.code"
            class="locale"
            :class="{ active: option.code === locale }"
            :to="switchLocalePath(option.code)"
            :aria-current="option.code === locale ? 'true' : undefined"
          >
            {{ option.name }}
          </NuxtLink>
        </nav>
      </footer>
    </main>
  </div>
</template>

<style scoped>
.login-page {
  display: grid;
  grid-template-columns: minmax(0, 1.05fr) minmax(0, 1fr);
  min-height: 100dvh;
  background: var(--paper);
}

/* --- Brand panel (intentionally dark in both themes) ----------------------- */

.login-brand {
  --panel-bg: #15181c;
  --panel-text: #eef1f4;
  --panel-muted: #aeb6bd;
  --panel-label: #9aa3ab;
  --panel-rule: rgb(255 255 255 / 12%);
  --panel-grid: rgb(255 255 255 / 4%);

  position: relative;
  overflow: hidden;
  display: flex;
  align-items: center;
  padding: var(--space-14);
  color: var(--panel-text);
  background: var(--panel-bg);
}

/* Vertical grid lines only — a ruled page rather than a texture. Clipped by the panel's own
   `overflow: hidden` and inert to the pointer. */
.login-brand::before {
  content: '';
  position: absolute;
  inset: 0;
  background-image: linear-gradient(90deg, var(--panel-grid) 1px, transparent 1px);
  background-size: 118px 100%;
  pointer-events: none;
}

/* A column measure, not spacing: the copy sits against the panel's inner edge so it reads as
   one spread with the sign-in block rather than as two centred islands. */
.login-brand-inner {
  position: relative;
  display: grid;
  gap: var(--space-11);
  width: min(470px, 100%);
  margin-left: auto;
}

/* The mark keeps the accent square here too — this is the brand, not the palette. */
.login-brand :deep(.brand) {
  color: var(--panel-text);
}

.login-pitch h2 {
  margin-bottom: var(--space-5);
  font-size: var(--display-3xl);
  line-height: 1.14;
  text-wrap: balance;
}

.login-pitch p {
  max-width: 44ch;
  line-height: 1.7;
  color: var(--panel-muted);
}

.login-facts {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-8);
  margin: 0;
  padding-top: var(--space-7);
  border-top: 1px solid var(--panel-rule);
}

.fact dt {
  margin-bottom: var(--space-2);
  font-size: var(--text-3xs);
  font-weight: var(--weight-semibold);
  letter-spacing: var(--tracking-facts);
  text-transform: uppercase;
  color: var(--panel-label);
}

.fact dd {
  margin: 0;
  font-family: var(--font-display);
  font-size: var(--display-sm);
}

/* --- Sign-in panel -------------------------------------------------------- */

.login-panel {
  /* The sign-in block centres in the space above the footer, which stays pinned to the
     bottom edge like a normal site footer. */
  display: grid;
  grid-template-rows: 1fr auto;
  padding: var(--space-14) var(--space-14) var(--space-8);
  background: var(--paper);
}

.login-form {
  width: min(370px, 100%);
  align-self: center;
}

.login-form h1 {
  margin-top: var(--space-3);
  font-size: var(--display-xl);
}

.login-lede {
  margin-top: var(--space-2);
  margin-bottom: var(--space-7);
  color: var(--muted);
}

/* Google Sign-In branding: white surface, #dadce0 border, four-color mark. These are
   Google's prescribed values, not this app's palette — the one place besides the brand panel
   where a literal color is correct. */
.btn-google {
  /* Google's light and dark button variants — their values, not ours — following the same
     color-scheme the app's own tokens do (docs/web.md § Design system, *Theme*). */
  --g-bg: light-dark(#ffffff, #131314);
  --g-border: light-dark(#dadce0, #8e918f);
  --g-text: light-dark(#1f1f1f, #e3e3e3);
  --g-hover: light-dark(#f7f8f8, #1e1f20);

  width: 100%;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: var(--space-3);
  padding: var(--space-3) var(--space-4);
  border: 1px solid var(--g-border);
  border-radius: var(--radius);
  background: var(--g-bg);
  color: var(--g-text);
  font-weight: var(--weight-medium);
  transition: background var(--duration-fast) var(--ease);
}

.btn-google:hover {
  background: var(--g-hover);
}

/* 18px is the mark's own size in Google's spec, like any other icon dimension. */
.btn-google-mark {
  width: 18px;
  height: 18px;
  flex-shrink: 0;
}

.login-dev {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: var(--space-3);
  margin-top: var(--space-5);
}

/* One step under the Google button it sits below: this is the secondary way in, and on a
   dev build only. */
.login-dev .dev-button {
  height: var(--control-height-md);
}

.login-dev-note {
  color: var(--muted);
  font-size: var(--text-sm);
}

.login-error {
  margin-top: var(--space-4);
}

.login-footer {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: var(--space-3) var(--space-5);
  padding-top: var(--space-5);
  border-top: 1px solid var(--line);
  color: var(--muted);
  font-size: var(--text-xs);
}

.locales {
  display: inline-flex;
  gap: var(--space-4);
  flex-shrink: 0;
}

.locale {
  color: var(--muted);
  white-space: nowrap;
  transition: color var(--duration-fast) var(--ease);
}

.locale:hover {
  color: var(--accent);
}

/* The current locale is ink and a weight step, underlined in the accent — the accent's one
   navigational use, marking which of two states of the same page you are on. */
.locale.active {
  color: var(--ink);
  font-weight: var(--weight-semibold);
  padding-bottom: 2px;
  border-bottom: 1px solid var(--accent);
}

/* --- Responsive ----------------------------------------------------------- */

/* Two columns still fit, but the full gutters start crowding the copy. */
@media (max-width: 1080px) {
  .login-brand {
    padding: var(--space-10);
  }

  .login-panel {
    padding: var(--space-10) var(--space-10) var(--space-8);
  }

  .login-pitch h2 {
    font-size: var(--display-xl);
  }
}

@media (max-width: 860px) {
  /* Brand strip sizes to content; the sign-in panel absorbs the rest so its surface reaches
     the bottom of the viewport. */
  .login-page {
    grid-template-columns: minmax(0, 1fr);
    grid-template-rows: auto 1fr;
  }

  .login-brand {
    padding: var(--space-8) var(--space-6);
  }

  .login-brand-inner {
    width: 100%;
    margin-left: 0;
    gap: var(--space-6);
  }

  /* Single column: the form sits at the top of the panel, and the footer keeps the same
     width so its rule lines up with the sign-in block above it. */
  .login-panel {
    justify-items: center;
    padding: var(--space-10) var(--space-6) var(--space-8);
  }

  .login-form {
    align-self: start;
  }

  .login-footer {
    width: min(370px, 100%);
  }
}

@media (max-width: 560px) {
  .login-pitch h2 {
    font-size: var(--display-md);
  }

  .login-brand {
    padding: var(--space-6) var(--space-4);
  }

  .login-panel {
    padding: var(--space-8) var(--space-4) calc(var(--space-6) + env(safe-area-inset-bottom, 0px));
  }
}
</style>
