import { defineConfig } from 'vitest/config'

// Only framework-free modules are unit tested: the API client, the display transforms
// and what the server routes build (docs/testing.md § Frontend). No browser, no Nuxt runtime.
export default defineConfig({
  test: {
    environment: 'node',
    include: ['app/**/*.test.ts', 'shared/**/*.test.ts'],
  },
})
