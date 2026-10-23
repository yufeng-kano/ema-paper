<script setup lang="ts">
/**
 * One of the three public policy pages (docs/web.md § Pages, `/terms`, `/privacy`,
 * `/refund`): the title, when the text last changed, a lede, then numbered sections of a
 * heading and a paragraph or two — every word from the catalog, the sections read off it
 * until the next number is missing (`app/lib/legal.ts`), so adding one is a catalog edit in
 * both locales, which a test holds to. The operator's name and
 * the support address are the deployment's (`GET /api/site`), read when the page opens and
 * printed only when the API gives them: nothing is typed into the page.
 */
import { getSite, type SiteInfo } from '~/lib/api'
import { formatDate } from '~/lib/format'
import { LEGAL_UPDATED, type LegalDoc } from '~/lib/legal'

const props = defineProps<{ doc: LegalDoc }>()

const { t, te, locale } = useI18n()

useSeoMeta({
  title: () => t(`legal.${props.doc}.seoTitle`),
  description: () => t(`legal.${props.doc}.seoDescription`),
  ogTitle: () => t(`legal.${props.doc}.seoTitle`),
  ogDescription: () => t(`legal.${props.doc}.seoDescription`),
  ogType: 'article',
  twitterCard: 'summary',
})

const site = ref<SiteInfo | null>(null)

onMounted(async () => {
  try {
    site.value = await getSite()
  } catch {
    // The text stands on its own; only the contact and operator lines need the API.
  }
})

interface Section {
  title: string
  paragraphs: string[]
}

/** The catalog's numbered sections, `s1Title`/`s1Body` on; a body's blank line is a break. */
const sections = computed<Section[]>(() => {
  const named = { name: t('app.name') }
  const found: Section[] = []
  for (let n = 1; te(`legal.${props.doc}.s${n}Title`, locale.value); n += 1) {
    found.push({
      title: t(`legal.${props.doc}.s${n}Title`),
      paragraphs: t(`legal.${props.doc}.s${n}Body`, named).split('\n\n'),
    })
  }
  return found
})
</script>

<template>
  <PublicShell :title="t(`legal.${doc}.title`)">
    <main class="content">
      <article class="document">
        <header class="head">
          <h1 class="title">{{ t(`legal.${doc}.title`) }}</h1>
          <p class="meta">
            <span>{{ t('legal.updated', { date: formatDate(LEGAL_UPDATED[doc], locale) }) }}</span>
            <span v-if="site?.operator_name">{{ t('legal.operator', { operator: site.operator_name }) }}</span>
          </p>
          <p class="lede">{{ t(`legal.${doc}.lede`, { name: t('app.name') }) }}</p>
        </header>

        <section v-for="(section, index) in sections" :key="index" class="section">
          <h2 class="section-title">{{ index + 1 }}. {{ section.title }}</h2>
          <p v-for="(paragraph, n) in section.paragraphs" :key="n">{{ paragraph }}</p>
        </section>

        <section v-if="site?.support_email" class="section">
          <h2 class="section-title">{{ t('legal.contactTitle') }}</h2>
          <p>
            {{ t('legal.contactBody') }}
            <a class="mail" :href="`mailto:${site.support_email}`">{{ site.support_email }}</a>
          </p>
        </section>
      </article>
    </main>
  </PublicShell>
</template>

<style scoped>
.content {
  width: 100%;
  max-width: 720px;
  margin: 0 auto;
  padding: var(--space-10) var(--space-6) var(--space-12);
}

.document {
  display: flex;
  flex-direction: column;
  gap: var(--space-8);
}

.head {
  display: grid;
  gap: var(--space-3);
}

.title {
  font-size: var(--display-lg);
  font-weight: var(--weight-semibold);
  letter-spacing: -0.02em;
  line-height: 1.1;
}

.meta {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2) var(--space-4);
  color: var(--muted);
  font-size: var(--text-sm);
}

.lede {
  color: var(--muted);
  font-size: var(--text-lg);
  line-height: 1.6;
}

.section {
  display: grid;
  gap: var(--space-3);
}

.section-title {
  font-size: var(--text-lg);
  font-weight: var(--weight-semibold);
}

.section p {
  line-height: 1.7;
}

.mail {
  color: var(--accent);
  text-decoration: underline;
  text-underline-offset: 3px;
}

@media (max-width: 640px) {
  .content {
    padding: var(--space-8) var(--space-4) var(--space-10);
  }

  .title {
    font-size: var(--display-md);
  }
}
</style>
