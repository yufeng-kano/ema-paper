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
  - Antialiasing is coverage the renderer computes. It never asks for multisampling (§ Safari).
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

## Safari

Safari 27.2 is WebKit 625.2.8. These rules come from that source and from WebKit's bug tracker. The device check asks the first three of a real device (§ The device check); no check exercises the rest.

- **A canvas the reader sees is never read through a 2D canvas or made into a bitmap.** In Safari 27.2 `drawImage`, `toDataURL`, `toBlob` and `createImageBitmap` of a WebGPU canvas leave it showing a buffer nothing was drawn to, which is black, until the canvas is drawn again. Chromium reads an empty frame from a canvas whose frame has been shown. A thumbnail or a print is drawn into a texture the renderer owns.
- **Pixels are read from the GPU**, with `copyTextureToBuffer` and `mapAsync`.
  - A canvas read this way is configured with `COPY_SRC`.
  - Its current texture is copied in the task that drew it. A copy made later reads nothing and raises no error.
  - `bgra8unorm` bytes come blue first.
- **A worker shows a page on a canvas the page handed it** with `transferControlToOffscreen()`. `transferToImageBitmap()` on a WebGPU `OffscreenCanvas` is not used: in Safari 27.2 it can hand back the frame from before the drawing.
- **No multisampling.** Safari 27.2 loses the device with it, transient attachments or not (WebKit bug 329089).
- **A lost device may leave the page with no adapter** until it is reloaded (WebKit bug 331190). That is the second case of § The fallback.
- **A texture view lives for the pass that uses it.** On iOS 27.2 a view that is kept makes the GPU process's memory grow without bound (WebKit bug 329399).
- **A canvas costs three surfaces of its full size**, about 12 bytes for each device pixel. Stage 7's memory budget counts them, and a page out of view gives its canvas up.
- **A frame is few command buffers, all submitted.** A thousand command buffers made and never submitted lose the device.

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
| A drawing read back from the GPU | Whether a texture that is no canvas's holds the color it was cleared to |
| Five canvases from one device | Whether one device draws five canvases in one task — one canvas per visible page |
| A canvas read through a 2D canvas | What `drawImage` reads from a WebGPU canvas, as a thumbnail or a print would: in the task that drew it, and once the frame has been shown |
| WebGPU in a background worker | Whether a worker gets a device and draws a texture that reads back right, and in how long by the worker's own clock |
| A worker's frame handed over as a bitmap | What the page reads from a worker's `OffscreenCanvas` frame: from `transferToImageBitmap()` and from `createImageBitmap()` in the task that drew it, and from `transferToImageBitmap()` once `onSubmittedWorkDone()` has resolved, each on a canvas of its own |
| A worker drawing a canvas of the page | Whether a worker draws a canvas the page handed it with `transferControlToOffscreen()` |
| Recovery after the device is lost | Whether, after a device is destroyed, a new adapter and device draw the same canvas again without a reload, and in how long |

**What a check reads, and what only the screen says.**

- **A drawing is read back from the GPU itself**: the texture is cleared to a color, copied to a buffer in the same submit, and the middle pixel is compared with that color. A canvas's texture is read this way in the task that drew it, before the browser presents it.
- **Reading a canvas through a 2D canvas is a check of its own**, on a canvas of its own, since the read may change what the canvas then shows.
- **No check can read what a canvas shows on the screen.** So each way of drawing has canvases of its own, and no way touches another's:

  | Canvases | How they are drawn |
  |----------|--------------------|
  | Five | Once, from one device, never read through a 2D canvas |
  | One | Once, then read through a 2D canvas |
  | One | Again on every frame |
  | One | By a worker, once, after the page handed it over |
  | One | By a device that is then destroyed, and again by the one that replaces it |

- **Under each canvas is a dot in the color it should show**, drawn by CSS. A screenshot of the page is the answer for the screen, and the page asks for one with the copied results.
- **A wrong pixel is reported with what was read**, as four bytes beside the four that were drawn: black, transparent and a swap of red and blue read differently.
- **Every run draws on new canvases.** A canvas a device was configured on, or one handed to a worker, is not used again.
- **The device that is destroyed is the loss check's own.** The device the other canvases are drawn from stays.

**What the page shows.**

- **The checks run one at a time**, so no check's time includes another's work. The canvas drawn on every frame starts after the last check.
- **A step the browser throws on fails its own check only**, with the browser's message. The other checks still run.
- **The worker answers stage by stage.** A stage that never answers is noted as out of time, and the stages that did answer still count.
- **A check that could not run is shown as skipped**, with the earlier failure above it.
- **What a check adds to its status is a note**, said in the page's language: how many canvases drew, the pixel read, why the device was lost, that the browser gave no adapter. An error's message is the browser's own words and is printed as it is.
- **A page that is not on HTTPS has no WebGPU**, on any device. The check says so in place of *no WebGPU*, and the report carries whether the page was a secure context.
- **The page lists**, under the checks, the browser's user agent, whether the page is a secure context, the device pixel ratio, the processor count, the preferred canvas format, the adapter's name, the adapter limits the renderer is sized by and the adapter's features.
- ***Copy results*** copies all of it as plain text: one `name: value` line per fact and no name twice, the checks first. It is in English whatever the page's language, a note as its code and what it carries (`drawn(count=5, total=5)`), so that a pasted report reads the same from every device.
- **A run that starts clears the last run's answers**, so nothing copied is older than the colors on the page. A run that cannot finish shows the error's message.
- **The code.** `app/pages/pdf-probe.vue` draws and lists. `app/lib/pdf/probe.ts` holds what a run records and the copied text, and is unit tested. `probe-gpu.ts` runs the checks, `probe.worker.ts` is the worker's half and `probe-draw.ts` what the two share: the drawing, the read-back and the worker's messages. The page loads `probe-gpu.ts` only when a check is started, so no other page asks for a GPU device.

### Recorded devices

What the check answered on the devices the renderer is designed for. A row is added when a device's copied report is in hand.

| Device | Browser | Checks passed | Largest texture | Notes |
|--------|---------|---------------|-----------------|-------|
| Mac, Apple GPU, 8 cores, a 2× screen | Safari 27.2 | Not yet run in the form above | 16384 | Canvas format `bgra8unorm`. Adapter and device in under 1 ms, recovery from a lost device in 260 ms. `float16-renderable`, `shader-f16`, `bgra8unorm-storage` and `timestamp-query` are among the features. The canvases turned black once read through a 2D canvas, and a worker's `transferToImageBitmap()` frame read a wrong color (§ Safari) |

The other limits of that Mac: 2048 texture array layers, 11 bind groups, 44 sampled textures per shader stage, 8 color attachments, a 2147483644-byte largest buffer and 12 vertex buffers.
