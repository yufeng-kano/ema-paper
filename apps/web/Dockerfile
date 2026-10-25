# Nuxt control plane (docs/web.md). Nitro node-server output, port 3000.
FROM node:24-bookworm-slim AS build

ENV PNPM_HOME=/pnpm \
    PATH=/pnpm:$PATH
RUN corepack enable

WORKDIR /app

# The whole project is copied before installing because the `postinstall` script runs
# `nuxt prepare`, which needs nuxt.config.ts; the pnpm store cache keeps installs cheap.
COPY . .
RUN --mount=type=cache,id=pnpm,target=/pnpm/store pnpm install --frozen-lockfile
# The public origin, fixed into the prerendered pages (docs/web.md § Search engines). Unset
# keeps their links relative and the sitemap empty, as a local build has them.
ARG WEB_URL
RUN pnpm build

FROM node:24-bookworm-slim AS runtime

ENV NODE_ENV=production \
    NITRO_PORT=3000 \
    NITRO_HOST=0.0.0.0

WORKDIR /app
# The build output is self-contained: no node_modules, no package manager at runtime.
COPY --from=build /app/.output ./.output

USER node
EXPOSE 3000
CMD ["node", ".output/server/index.mjs"]
