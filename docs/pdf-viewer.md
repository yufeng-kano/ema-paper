# The PDF viewer

The web app draws a PDF's pages itself, with WebGPU. This doc is the home of that viewer: what reads the file, what draws it, the stages it is built in, what it takes from other engines, and the device check that tells what a browser's WebGPU can do.

Where the Original view sits in the result viewer is [web.md](./web.md) § Result viewer, *Original and Markdown*. The server's PDF work is [parsing.md](./parsing.md).

## Rules

- **The pages are drawn by the app's own WebGPU renderer.** The page a reader sees is made of objects the app keeps and draws itself, so that a later editor can change one object and redraw (§ Room for editing).
- **The viewer displays; it does not edit.** No stage below builds an editing feature.
- **A solved problem is ported, not invented.** Where a mature engine already has the answer, the viewer takes that answer (§ What is taken from where).
- **Permissive licences only** (§ Licences).
- **Every reader sees the page.** A browser or a page the WebGPU renderer cannot serve is drawn another way (§ The fallback).

## Who does what

- **pdf.js reads the file.** `pdfjs-dist`, at one exact version with no range, parses the PDF in its worker and gives each page as an operator list: paths, glyph outlines, images, colors and transforms.
  - It also gives the text layer the viewer selects and finds in.
  - It runs without WebAssembly, so the site's policy needs no `'wasm-unsafe-eval'` ([deployment.md](./deployment.md) § Security headers).
  - Its operator list changes shape between major versions. One adapter module reads it, and nothing else in the app knows those shapes.
- **The adapter turns a page's operator list into a page scene**: a list of the page's objects, each with a stable id, its bounding box and its clip (§ Room for editing).
- **The renderer draws a page scene with WebGPU**, one canvas per visible page.
  - Antialiasing is coverage the renderer computes. It never asks for multisampling, which loses the device on some Safari versions.
- **pdf.js's own Canvas 2D renderer is the fallback** (§ The fallback).
- No Rust and no WebAssembly: the renderer is TypeScript and WGSL.

## Stages

The viewer is built in stages, each delivered on its own. The code is at stage 0.

| Stage | What it delivers |
|-------|------------------|
| 0 | These rules and the device check (§ The device check) |
| 1 | The in-app viewer on pdf.js's Canvas 2D renderer, in place of the browser's own viewer in a frame |
| 2 | Find in the original, and a jump from a figure to its box on the page |
| 3 | The adapter and the page scene, built beside the Canvas 2D renderer and compared with it, drawing nothing |
| 4 | The WebGPU renderer's core: fills, strokes, clips and images |
| 5 | Text from a glyph atlas |
| 6 | Shadings, patterns, transparency groups and blend modes |
| 7 | Memory budgets, WebGPU as the default, the frame removed |

- Until stage 1 the Original view of a PDF is the browser's own viewer in a frame, as [web.md](./web.md) describes it.
- Stage 1 adds `pdfjs-dist`, and with it a `THIRD_PARTY_NOTICES` file and a check that every dependency's licence is one § Licences allows.
- Stage 2 gives the viewer and the figure boxes one page frame: the box `pdfinfo` sizes a page by and the box `pdftoppm` renders must be the same one ([parsing.md](./parsing.md)).
- Stage 4 opens with a speed measurement on an iPhone. The WebGPU renderer goes on only if a page draws fast enough there; the Canvas 2D viewer of stage 1 stays the product otherwise.

## What is taken from where

| Problem | Taken from | How |
|---------|-----------|-----|
| Reading the file, fonts to outlines, the text layer, find | pdf.js | used as a library |
| The object model and the interface a renderer draws through | PDFium, hayro | the design, rewritten |
| Filling a path with antialiased coverage (sparse strips) | Vello (`vello_common`) | ported |
| Strokes, joins, caps and dashes | kurbo | ported |
| The glyph atlas and when a glyph is drawn as a path instead | hayro | ported |
| Gradient and blend-mode formulas | Skia | ported |

- A ported file names its source, the source's licence and the revision it was ported from in its header.
- `THIRD_PARTY_NOTICES` lists each of them, from the stage that adds the first.

## Licences

- The repository is MIT, and the viewer takes code under Apache-2.0, MIT or a BSD licence only.
- Every source in § What is taken from where is one of those.
- **Poppler is GPL and MuPDF is AGPL. No code is copied or adapted from either**, and neither is a dependency of the web app. The server runs Poppler as a separate program ([parsing.md](./parsing.md)), which copies nothing.

## The fallback

pdf.js's Canvas 2D renderer draws the page when:

- the browser has no WebGPU, or gives no adapter or device. Safari has WebGPU from macOS 26 and iOS 26 on;
- the device is lost and a new one cannot be had;
- the page uses a font pdf.js has no outlines for, one that is not embedded in the file.

The fallback is a whole page at a time: a page is never drawn half by each renderer.

## Room for editing

The page scene is what an editor would change, so it is kept and never thrown away after drawing.

- **Objects are retained.** The renderer draws from the page scene, never straight from pdf.js's operator list.
- **Each object has a stable id**, the same on every reading of the same file, so that a change can name its object.
- **Each object has its bounding box** in page coordinates, which hit testing and partial redraw read.
- **Each object has an active flag.** An object switched off is skipped by the renderer and stays in the scene.
- **Clips are shared chains**, named by id, so that changing one object does not disturb the clip of the next.
- **Writing a PDF back is no part of the viewer.** pdf.js does not write files; an editor brings its own writer.

## Libraries

- `@webgpu/types` (BSD-3-Clause), a development dependency at one exact version: the WebGPU types TypeScript's own library lacks. Types only, nothing of it ships.
- `pdfjs-dist` (Apache-2.0) from stage 1, at one exact version.

## Testing

[testing.md](./testing.md) § Frontend holds the commands and the rule for what is unit tested.

- The adapter, the page scene and every pure part of the renderer are framework-free modules with vitest tests.
- The renderer's pictures are compared with stored hashes of its own earlier output, per test file. They are not compared pixel for pixel with Poppler's renders: two correct renderers differ at every antialiased edge.
- What needs a GPU is checked by hand on real devices, Safari on a Mac and on an iPhone first.

## The device check

`/pdf-probe` runs a fixed set of WebGPU checks in the browser that opens it and shows the answers. It exists so that the renderer's design is checked on real phones and laptops before the renderer is built.

- **Who sees it.** Any signed-in account, under `/en` and `/zh-TW`. Nothing links to it and crawlers are kept off it ([web.md](./web.md) § Search engines).
- **It sends nothing.** The checks run on the device, and the answers stay on the page until the reader copies them.
- **It goes at stage 7**, when the WebGPU renderer is the default.

The checks, in the order they are listed:

| Check | What it answers |
|-------|-----------------|
| WebGPU available | Whether `navigator.gpu` exists |
| Graphics adapter found | Whether `requestAdapter()` gives one, and in how long |
| Graphics device created | Whether `requestDevice()` gives one, and in how long |
| Half-float arrays | Whether `Float16Array` exists, which packs the renderer's half-float data |
| Five canvases at once | Whether one device can draw to five canvases together — one canvas per visible page |
| Drawing in a background worker | Whether a worker can draw with WebGPU into an `OffscreenCanvas` and hand the frame back, and in how long by the worker's own clock |
| Recovery after the device is lost | Whether, after the device is destroyed, a new adapter and device draw every canvas again without a reload, and in how long |

- **The checks run one at a time**, so no check's time includes another's work.
- **A check passes by reading back what it drew.** Each canvas is cleared to its own color and its middle pixel is compared with that color; the five canvases stay on the page, so the reader sees the same thing the check read.
  - The pixel is read in the task that drew it, and again once the frame has been shown if the first read failed. The check notes when only the second read answered.
- **A check that could not run is shown as skipped**, with the earlier failure above it.
- **What a check adds to its status is a note**, said in the page's language: how many canvases drew, why the device was lost, that the browser gave no adapter. An error's message is the browser's own words and is printed as it is.
- **A page that is not on HTTPS has no WebGPU**, on any device. The check says so in place of *no WebGPU*, and the report carries whether the page was a secure context.
- **The page lists**, under the checks, the browser's user agent, whether the page is a secure context, the device pixel ratio, the processor count, the preferred canvas format, the adapter's name, the adapter limits the renderer is sized by and the adapter's features.
- ***Copy results*** copies all of it as plain text: one `name: value` line per fact and no name twice, the checks first. It is in English whatever the page's language, a note as its code (`drawn(count=5, total=5)`), so that a pasted report reads the same from every device.
- **A run that starts clears the last run's answers**, so nothing copied is older than the colors on the page. A run that cannot finish shows the error's message.
- **The code.** `app/pages/pdf-probe.vue` draws and lists. `app/lib/pdf/probe.ts` holds what a run records and the copied text, and is unit tested. `probe-gpu.ts` runs the checks, `probe.worker.ts` is the worker's half and `probe-draw.ts` what the two share: the one drawing and the worker's messages. The page loads `probe-gpu.ts` only when a check is started, so no other page asks for a GPU device.

### Recorded devices

What the check answered on the devices the renderer is designed for. A row is added when a device's copied report is in hand.

| Device | Browser | Checks passed | Largest texture | Notes |
|--------|---------|---------------|-----------------|-------|
| — | — | — | — | No device is recorded yet |
