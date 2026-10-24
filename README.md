# Ema Paper

<p align="center">
  <strong>Vision document parsing service: turn complex PDFs and images into structured Markdown with precise figure coordinates.</strong>
</p>

<p align="center">
  <a href="./README.md">English</a> · <a href="./README.zh-TW.md">繁體中文</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/FastAPI-009688?style=flat-square&logo=fastapi&logoColor=white" alt="FastAPI" />
  <img src="https://img.shields.io/badge/Nuxt-00DC82?style=flat-square&logo=nuxt&logoColor=white" alt="Nuxt" />
  <img src="https://img.shields.io/badge/PostgreSQL-4169E1?style=flat-square&logo=postgresql&logoColor=white" alt="PostgreSQL" />
  <img src="https://img.shields.io/badge/MCP-Claude_Connectors-17607d?style=flat-square" alt="MCP" />
  <img src="https://img.shields.io/badge/Privacy-Zero_Disk_Retention-059669?style=flat-square" alt="Privacy" />
  <img src="https://img.shields.io/badge/License-MIT-blue?style=flat-square" alt="License" />
</p>

---

**Ema Paper** is a self-hosted, multi-user document parsing service designed for developers and AI agents. It transforms unstructured PDFs, scans, and images into clean, layout-preserving Markdown, extracting exact bounding box coordinates for figures and charts.

Connect your Kano Proxy account in one click, or bring your own OpenAI-compatible vision endpoint. Every vision call runs on your own account with zero markup.

---

## Key Features

- **Vision-First Precision** — Accurately extracts multi-column text, dense tables, mathematical formulas, and diagrams that break traditional OCR engines.
- **Normalized Figure Coordinates** — Returns visual figure boundaries as normalized `[ymin, xmin, ymax, xmax]` coordinates (`sightread://p{page}/{coords}`) for precise downstream cropping in RAG pipelines.
- **Zero Markup / Your Own Account** — Connect Kano Proxy through its consent screen, or add any OpenAI-compatible vision endpoint with your own key. No middleman subscription fees or token markups.
- **Built for AI Agents & MCP** — Native Model Context Protocol (MCP) server with OAuth 2.1 support for Claude Desktop, Claude Code, and autonomous agents.
- **Modern Web Workspace** — an Apple-style UI with restrained Liquid Glass, featuring nested folders, drag-and-drop uploads, live SSE progress streaming, and instant side-by-side previews.
- **Privacy by Default** — Uploaded source documents are processed in isolated subprocesses and purged from disk immediately once parsing completes.

---

## How It Works

<p align="center">
  <img src="./docs/assets/how-it-works.svg" alt="How Ema Paper Works" width="100%" />
</p>

1. **Upload** a PDF, scan, or image via the Web UI, REST API, or Claude MCP tool.
2. **Process** pages in parallel using isolated Poppler subprocesses and your configured vision model.
3. **Receive** clean Markdown with embedded page markers (`<!-- page: N -->`) and figure coordinates.
4. **Integrate** seamlessly into downstream RAG indexing, document search, or agent context.

---

## Quick Start

### 1. Launch with Docker Compose

```bash
cp .env.example .env
docker compose up --build
```

| Service | URL | Notes |
|---|---|---|
| **Web Workspace** | `http://localhost:3000` | Full file library, the Agent page, and result viewer |
| **REST API** | `http://localhost:8000` | Data plane (`/v1/*`) and MCP endpoint (`/mcp`) |

> Set `AUTH_DEV_MODE=true` in `.env` to sign in locally without Google OAuth credentials.

---

## Integrations

### REST API

```bash
curl -X POST http://localhost:8000/v1/parse \
  -H "Authorization: Bearer <YOUR_API_KEY>" \
  -F "file=@financial-report.pdf" \
  -F "model=<model-id-from-your-catalog>"
```

### Claude Desktop / MCP Configuration

Add Ema Paper to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "emapaper": {
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote",
        "http://localhost:8000/mcp",
        "--header",
        "Authorization: Bearer <YOUR_API_KEY>"
      ]
    }
  }
}
```

Or connect it directly in Claude via the OAuth Custom Connector endpoint: `https://<your-domain>/mcp`.

---

## Architecture & Stack

| Layer | Technology | Purpose |
|---|---|---|
| **API & Data Plane** | FastAPI (Python 3.12, `uv`) | High-performance REST endpoints and SSE streaming |
| **Task Queue** | PostgreSQL (`SKIP LOCKED`) | Reliable transactional job queue without Redis |
| **PDF Rendering** | Poppler CLI (`pdftoppm`) | Crash-isolated subprocess rendering (MIT-safe) |
| **Web Interface** | Nuxt (Vue 3), hand-rolled CSS | Light-glass design system, file workspace, i18n |
| **Edge & Proxy** | Caddy | Automatic TLS certificates and SSE reverse proxy |

---

## Documentation

- [Product Overview & Design Goals](./docs/product.md)
- [API Reference & Data Plane](./docs/api.md)
- [MCP Server & Claude Connectors](./docs/mcp.md)
- [Parsing Pipeline & Bounding Boxes](./docs/parsing.md)
- [Deployment & Self-Hosting Guide](./docs/deployment.md)
- [Full Documentation Map](./docs/index.md)

---

## License

[MIT](LICENSE)
