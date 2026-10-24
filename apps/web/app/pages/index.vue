<script setup lang="ts">
/**
 * The one surface a signed-out visitor sees, and still the app's SEO page (docs/web.md
 * § Pages, `/`).
 *
 * One centered column, the way Apple introduces a product: the app icon and name, the hero
 * line and the body line, the sign-in card, then two facts — under the public bar and over
 * the public footer every public page shares (`PublicShell`).
 *
 * The Google button is the one prescribed surface: white background, `#dadce0` border, the
 * official four-color mark inlined as SVG (never a remote asset), and Google's own dark
 * variant. Those values are Google's to set, not ours to tokenize.
 *
 * The locale links live in the public footer rather than in the signed-in shell: this is the
 * page a visitor lands on, and it is the last point where the choice costs nothing.
 *
 * Full-bleed, so the page owns its frame rather than sitting in a layout.
 */
import { devLogin, isDevLoginAvailable } from '~/lib/api'
import { takeAfterSignIn } from '~/lib/settings'

definePageMeta({ layout: false })

const { t, locale } = useI18n()
const localePath = useLocalePath()
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

onMounted(async () => {
  await auth.ensureLoaded()
  // The OAuth callback redirects to the web root, so this is the hop that lands a signed-in
  // visitor in the app — on Files, or where the pricing page's Upgrade was going. No
  // signed-in variant of this page exists to fall back to.
  if (auth.signedIn.value) {
    await navigateTo(takeAfterSignIn() ?? localePath('/files'))
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
    await navigateTo(takeAfterSignIn() ?? localePath('/files'))
  } catch (error) {
    devLoginError.value = await resolve(error)
  } finally {
    signingIn.value = false
  }
}
</script>

<template>
  <PublicShell>
    <main class="intro">
      <header class="hero">
        <AppBrand size="lg" />
        <h1 class="hero-title">{{ t('login.pitchTitle') }}</h1>
        <p class="hero-body">{{ t('login.pitchBody') }}</p>
      </header>

      <section class="card" :aria-label="t('login.signIn')">
        <h2 class="card-title">{{ t('login.signIn') }}</h2>
        <p class="card-lede">{{ t('login.lede') }}</p>

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
          <UiButton :loading="signingIn" @click="signInAsDeveloper">
            {{ t('login.devSignIn') }}
          </UiButton>
          <!-- Set to be read rather than shrunk and greyed: it is the one thing that explains
               why a second sign-in control is on the page at all. -->
          <span class="login-dev-note">{{ t('login.devSignInNote') }}</span>
        </div>

        <UiBanner v-if="devLoginError" class="login-error" tone="error">
          {{ devLoginError }}
        </UiBanner>
      </section>

      <!-- Two facts, not a feature list: what you talk to it with, and whose money pays
           for the vision calls. -->
      <dl class="facts">
        <div class="fact">
          <dt>{{ t('login.factInterfaces') }}</dt>
          <dd>{{ t('login.factInterfacesValue') }}</dd>
        </div>
        <div class="fact">
          <dt>{{ t('login.factBilling') }}</dt>
          <dd>{{ t('login.factBillingValue') }}</dd>
        </div>
      </dl>
    </main>
  </PublicShell>
</template>

<style scoped>
.intro {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--space-10);
  width: 100%;
  max-width: 760px;
  margin: 0 auto;
  padding: var(--space-14) var(--space-6) var(--space-12);
  text-align: center;
}

.hero {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: var(--space-5);
}

.hero-title {
  max-width: 18em;
  margin-top: var(--space-2);
  font-size: var(--display-3xl);
  line-height: 1.08;
  text-wrap: balance;
}

.hero-body {
  max-width: 36em;
  color: var(--muted);
  font-size: var(--text-lg);
  line-height: 1.55;
  text-wrap: pretty;
}

/* The sign-in card: paper on the desk, the reader's cards' radius and lift. */
.card {
  display: flex;
  flex-direction: column;
  align-items: stretch;
  width: min(400px, 100%);
  padding: var(--space-8);
  border-radius: var(--radius-card);
  background: var(--paper);
  box-shadow: var(--shadow-pane);
  text-align: left;
}

.card-title {
  font-size: var(--display-md);
}

.card-lede {
  margin-top: var(--space-1);
  margin-bottom: var(--space-6);
  color: var(--muted);
  font-size: var(--text-base);
}

/* Google Sign-In branding: white surface, #dadce0 border, four-color mark. These are
   Google's prescribed values, not this app's palette — the one place where a literal color
   is correct. The shape is the app's capsule, which Google's guidelines allow ("pill"). */
.btn-google {
  /* Google's light and dark button variants — their values, not ours — following the same
     color-scheme the app's own tokens do (docs/web.md § Design system, *Theme*). */
  --g-bg: light-dark(#ffffff, #131314);
  --g-border: light-dark(#dadce0, #8e918f);
  --g-text: light-dark(#1f1f1f, #e3e3e3);
  --g-hover: light-dark(#f7f8f8, #1e1f20);

  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: var(--space-3);
  width: 100%;
  height: var(--control-height-lg);
  padding: 0 var(--space-5);
  border: 1px solid var(--g-border);
  border-radius: var(--radius-full);
  background: var(--g-bg);
  color: var(--g-text);
  font-size: var(--text-base);
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
  margin-top: var(--space-4);
}

.login-dev-note {
  color: var(--muted);
  font-size: var(--text-sm);
}

.login-error {
  margin-top: var(--space-4);
}

.facts {
  display: flex;
  flex-wrap: wrap;
  justify-content: center;
  gap: var(--space-6) var(--space-14);
  margin: 0;
}

.fact {
  display: grid;
  gap: var(--space-1);
}

.fact dt {
  color: var(--muted);
  font-size: var(--text-xs);
  font-weight: var(--weight-semibold);
}

.fact dd {
  margin: 0;
  font-size: var(--display-xs);
  font-weight: var(--weight-semibold);
}

@media (max-width: 640px) {
  .intro {
    gap: var(--space-8);
    padding: var(--space-10) var(--space-4) var(--space-10);
  }

  .hero-title {
    font-size: var(--display-lg);
  }

  .hero-body {
    font-size: var(--text-base);
  }

  .card {
    padding: var(--space-6);
  }
}
</style>
