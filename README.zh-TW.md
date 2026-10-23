# Ema Paper

<p align="center">
  <strong>視覺文件解析服務：將複雜 PDF 與圖片轉換為結構化 Markdown，並精準標記圖表座標。</strong>
</p>

<p align="center">
  <a href="./README.md">English</a> · <a href="./README.zh-TW.md">繁體中文</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/FastAPI-009688?style=flat-square&logo=fastapi&logoColor=white" alt="FastAPI" />
  <img src="https://img.shields.io/badge/Nuxt-00DC82?style=flat-square&logo=nuxt&logoColor=white" alt="Nuxt" />
  <img src="https://img.shields.io/badge/PostgreSQL-4169E1?style=flat-square&logo=postgresql&logoColor=white" alt="PostgreSQL" />
  <img src="https://img.shields.io/badge/MCP-Claude_Connectors-17607d?style=flat-square" alt="MCP" />
  <img src="https://img.shields.io/badge/隱私保護-解析後立即清除磁碟檔案-059669?style=flat-square" alt="Privacy" />
  <img src="https://img.shields.io/badge/授權條款-MIT-blue?style=flat-square" alt="License" />
</p>

---

**Ema Paper** 是一套專為開發者與 AI Agent 設計的自託管、多租戶視覺文件解析服務。它能將非結構化的 PDF、掃描件與各類圖片轉換為保留完整版面結構的 Markdown，並自動提取圖表與插圖的精準邊界座標。

一鍵連結您的 Kano Proxy 帳戶，或串接任何相容 OpenAI 的視覺端點。所有模型呼叫都使用您自己的帳戶，零中介抽成。

---

## 核心功能

- **視覺優先的高精度解析** — 完美處理傳統 OCR 工具容易跑版的多欄排版、複雜表格、數學公式與各類圖表。
- **標準化圖表座標提取** — 自動輸出圖表邊界座標（`sightread://p{頁數}/{ymin},{xmin},{ymax},{xmax}`），便於下游 RAG 管道或應用程式精準裁切。
- **使用自己的帳戶，零加價** — 透過 Kano Proxy 的授權畫面連結帳戶，或用自己的金鑰串接相容 OpenAI 的視覺端點，無訂閱加價或 Token 抽成。
- **原生支援 AI Agent 與 MCP** — 內建支援 OAuth 2.1 的 Model Context Protocol (MCP) 伺服器，可無縫串接 Claude Desktop、Claude Code 與自動化 Agent。
- **現代化檔案工作區** — 極簡石墨風格（Graphite）UI，支援多層資料夾、拖曳上傳、即時 SSE 進度串流與雙欄即時對照預覽。
- **隱私安全優先** — 上傳的原始文件在隔離子程序中處理，任務完成後立即自磁碟永久清除。

---

## 運作架構

<p align="center">
  <img src="./docs/assets/how-it-works.svg" alt="Ema Paper 運作架構" width="100%" />
</p>

1. **上傳**：透過 Web 介面、REST API 或 Claude MCP 工具上傳 PDF、掃描件或圖片。
2. **解析**：使用隔離的 Poppler 子程序與您設定的視覺模型進行多頁並行解析。
3. **輸出**：產出包含頁碼標記（`<!-- page: N -->`）與圖表座標的乾淨 Markdown。
4. **整合**：直接將解析成果送入下游 RAG 索引、文件搜尋庫或 Agent 上下文。

---

## 快速啟動

### 1. 使用 Docker Compose 啟動

```bash
cp .env.example .env
docker compose up --build
```

| 服務 | 網址 | 說明 |
|---|---|---|
| **Web 工作區** | `http://localhost:3000` | 完整檔案庫、Agent 頁與解析預覽器 |
| **REST API** | `http://localhost:8000` | 資料平面 API (`/v1/*`) 與 MCP 端點 (`/mcp`) |

> 提示：在 `.env` 中設定 `AUTH_DEV_MODE=true` 即可略過 Google OAuth 設定，直接在本機以開發者模式登入。

---

## 快速串接範例

### REST API

```bash
curl -X POST http://localhost:8000/v1/parse \
  -H "Authorization: Bearer <YOUR_API_KEY>" \
  -F "file=@financial-report.pdf" \
  -F "model=<您目錄中的模型 ID>"
```

### Claude Desktop / MCP 連線設定

在 `claude_desktop_config.json` 中加入 Ema Paper：

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

或直接在 Claude 中透過 OAuth 2.1 自定義連接器（Custom Connector）端點連接：`https://<your-domain>/mcp`。

---

## 系統架構與技術棧

| 層級 | 技術 | 核心用途 |
|---|---|---|
| **API 與資料平面** | FastAPI (Python 3.12, `uv`) | 高效能 REST 端點與 SSE 即時串流 |
| **任務佇列** | PostgreSQL (`SKIP LOCKED`) | 高可靠事務型佇列，無需額外引入 Redis |
| **PDF 渲染** | Poppler CLI (`pdftoppm`) | 隔離子程序渲染，無 AGPL 授權風險 |
| **Web 工作區** | Nuxt (Vue 3) + Tailwind CSS | 極簡 Graphite 設計系統、檔案庫與多語系 |
| **邊緣反向代理** | Caddy | 自動 TLS 憑證管理與 SSE 串流代理 |

---

## 完整文件

- [產品定位與設計目標](./docs/product.md)
- [API 規格與資料平面](./docs/api.md)
- [MCP 伺服器與 Claude 連接器](./docs/mcp.md)
- [解析管道與圖表座標約定](./docs/parsing.md)
- [部署與自託管指南](./docs/deployment.md)
- [完整文件目錄](./docs/index.md)

---

## 授權條款

[MIT](LICENSE)
