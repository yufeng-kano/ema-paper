# The PDF viewer

The web app draws a PDF's pages itself: with pdf.js's Canvas 2D renderer from stage 1, and with its own WebGPU renderer from stage 4, the default at stage 7. This doc is the home of that viewer: what reads the file, what draws it, the stages it is built in, what the stage 1 viewer does, what it takes from other engines, and the device check that tells what a browser's WebGPU can do.

Where the Original view sits in the result viewer is [web.md](./web.md) § Result viewer, *Original and Markdown*. The server's PDF work is [parsing.md](./parsing.md).

## Rules

- **The pages are drawn by pdf.js's Canvas 2D renderer from stage 1, and by the app's own WebGPU renderer from stage 4**, the default at stage 7. A page the WebGPU renderer draws is made of objects the app keeps and draws itself, so that a later editor can change one object and redraw (§ Room for editing).
- **The viewer displays; it does not edit.** No stage below builds an editing feature.
- **A solved problem is ported, not invented.** Where a mature engine already has the answer, the viewer takes that answer (§ What is taken from where).
- **Permissive licences only** (§ Licences).
- **Every reader sees the page.** A browser or a page the WebGPU renderer cannot serve is drawn another way (§ The fallback).

## Who does what

- **pdf.js reads the file.** `pdfjs-dist` 6.5.438, at that exact version with no range, parses the PDF in its worker and gives each page as an operator list: paths, glyph outlines, images, colors and transforms.
  - The app uses its display API only (`getDocument`, `PDFPageProxy.render`, `TextLayer`), never the `PDFViewer`, `PDFPageView` or `PDFLinkService` of its `web/pdf_viewer.mjs`.
  - It also gives the text layer the viewer selects in from stage 1 and finds in from stage 2.
  - It runs without WebAssembly, so the site's policy needs no `'wasm-unsafe-eval'` ([deployment.md](./deployment.md) § Security headers).
    - `useWasm: false` is passed, since pdf.js defaults it to true.
    - `wasmUrl` points at same-origin copies of `jbig2_nowasm_fallback.js` and `openjpeg_nowasm_fallback.js`, which the worker imports in place of the WebAssembly decoders. Without them JBIG2 and JPEG 2000 images fail.
    - No `iccUrl` is passed: pdf.js's ICC colour management (qcms) is WebAssembly.
  - Its operator list changes shape between major versions. One adapter module reads it, and nothing else in the app knows those shapes.
- **The adapter turns a page's operator list into a page scene**: a list of the page's objects, each with a stable id, its bounding box and its clip (§ Room for editing).
- **The renderer draws a page scene with WebGPU**, one canvas per visible page.
  - Antialiasing is coverage the renderer computes. It never asks for multisampling (§ Safari).
- **pdf.js's own Canvas 2D renderer draws the pages in stage 1**, as the renderer rather than a fallback (§ Stage 1: the Canvas 2D viewer). From stage 4 it is the fallback (§ The fallback).
- No Rust and no WebAssembly: the renderer is TypeScript and WGSL.

## Stages

The viewer is built in stages, each delivered on its own. The code is at stage 1.

| Stage | What it delivers |
|-------|------------------|
| 0 | These rules and the device check (§ The device check) |
| 1 | The in-app viewer on pdf.js's Canvas 2D renderer, with its own layout, zoom, page indicator, text layer and links, in place of the browser's own viewer in a frame |
| 2 | Find in the original, and a jump from a figure to its box on the page |
| 3 | The adapter and the page scene, built beside the Canvas 2D renderer and compared with it, drawing nothing |
| 4 | The WebGPU renderer's core: fills, strokes, clips and images |
| 5 | Text from a glyph atlas |
| 6 | Shadings, patterns, transparency groups and blend modes |
| 7 | The WebGPU renderer's memory budget, WebGPU as the default, the device check removed |

- The Original view of a PDF is the stage 1 viewer (§ Stage 1: the Canvas 2D viewer). The browser's own viewer is what *Open in new tab* shows.
- Stage 1 adds `pdfjs-dist`, and with it `THIRD_PARTY_NOTICES` and a licence check (§ Licences).
- Stage 2 gives the viewer and the figure boxes one page frame: the box `pdfinfo` sizes a page by and the box `pdftoppm` renders must be the same one ([parsing.md](./parsing.md)).
- Stage 4 opens with a speed measurement on an iPhone. The WebGPU renderer goes on only if a page draws fast enough there; the Canvas 2D viewer of stage 1 stays the product otherwise.

## Stage 1: the Canvas 2D viewer

The Original view of a PDF draws the file with pdf.js's display API and its Canvas 2D renderer. Its copy is the catalog's `viewer.pdf` group, in English and Traditional Chinese.

### Layout and scrolling

- **The pages form one vertical column** in a scroll pane under the header.
  - On desktop the column is centred, with 16px between pages and 16px above page 1.
  - On a phone a page at fit width is the pane's full width, with 10px between pages.
- **A page is white paper** with a 1px `--line-strong` border and no shadow.
- A column wider than the pane scrolls sideways. A narrower one stays centred.
- The pane has `overscroll-behavior: contain`, as the Markdown view's pane has, so a pan that reaches an edge never chains into Safari's back or forward swipe.
- **100% is 96/72 CSS pixels per PDF point** (pdf.js's `PixelsPerInch.PDF_TO_CSS_UNITS`): a Letter page is 816 × 1056 at 100%.
- **A page box is whole CSS pixels**: the page's size in points, its user unit included, × the zoom × 96/72, rounded down.
  - The layout model computes every box and every top, and sets each page's width and height inline from it. It is the one truth a scroll offset, a jump or a correction is computed from.
  - pdf.js's `approximateFraction` rounding is not ported: a box rounded to a fraction of its own would drift from the model's.
- **A page whose size is unknown is a placeholder at page 1's size.**
  - It gets its real size from `getPage(n)` (`view`, `rotate`, `userUnit`) once it comes within one pane height of the viewport, or into the render window. pdf.js has no call that reads every page's size.
  - A page that changes size at or below the viewport changes in place.
  - A page above the viewport changes in the same frame as a `scrollTop` correction of the same height, so what the reader sees does not move.
  - On touch that correction waits until scrolling has stopped: no scroll event for 150 ms and no touch down. Until then the page keeps its guessed size, since a scroll offset set during a fling stops it.
  - No `overflow-anchor` is used, in any browser: the correction above is the one thing that moves the scroll offset, and a browser's anchoring would move it a second time.
- **The render window.** Only the pages that intersect the pane, and one page on each side, hold a canvas, a text layer and a link layer.
  - Every page's `<section>` stays mounted at its size.
  - A page holding focus keeps its layers while focus is in it. A page whose layers must go first moves focus to its `<section>`.
- **A switch to Markdown and back** returns to the same page and zoom. The viewer is unmounted while Markdown shows; the result viewer keeps the page and the zoom it last reported and opens the viewer on them again, for as long as the result viewer stays open.

### Zoom

- **The zoom is a mode and a scale**: *fit width*, *fit page* or a custom scale, 1 being 100%.
- **Opening.** On desktop the view opens at 100% when page 1 at 100% fits the pane's width less its gutters, and at fit width otherwise. On a phone it opens at fit width. A switch back from Markdown opens at the zoom it left (above).
- **Fit width** is the pane's width less its side gutters (16px each side on desktop, none on a phone) over the current page's width.
- **Fit page** fits the current page whole, width and height, inside 16px gutters.
- Both are measured from the current page (§ Stage 1: the Canvas 2D viewer, *Page indicator*); the opening zoom is measured from page 1.
- **While fit width is chosen**, a pane resize keeps the current page at the pane's width. Any other zoom action leaves fit mode.
- **The range** is 25% to 500% on desktop. On a phone it runs from fit width, which is the floor, to 5 × fit width.
- **The zoom menu** opens from the zoom level: *Fit width*, *Fit page*, a separator, then 50, 75, 100 (with its key, ⌘0), 125, 150, 200, 300 and 400%. They are `menuitemradio` items with the current one checked. A level between presets checks nothing.
- **The ladder.** −, + and the keys step to the next rung above or below the current level on 25, 50, 75, 100, 125, 150, 200, 300, 400 and 500%, keeping the centre of the pane fixed.
- **The keys**, while the Original view shows: ⌘+ zooms in (⌘=, ⌘⇧= and the keypad's + too, on any layout), ⌘− zooms out, and ⌘0 goes to 100%.
  - Ctrl takes ⌘'s place off Apple platforms. None of them is read with ⌥ or Alt held.
  - The viewer reads them itself, on `document` in the capture phase while its dialog is the topmost `[aria-modal="true"]`, and cancels their default, so the web page never zooms.
  - Their labels come from `app/lib/shortcuts.ts` (`⌘+`, `⌘−`, `⌘0` on Apple platforms), and `zoomKey` there reads them.
  - *Fit width* has no key. ⌘9 is never bound, since Safari's ⌘9 goes to the last tab.
- **A trackpad pinch** zooms about the pointer. The pane handles both `wheel` with `ctrlKey` and WebKit's `gesturestart`, `gesturechange` and `gestureend`, and cancels their default, so the page around the PDF never zooms. A two-finger touch pinch zooms about the fingers' midpoint.
- **During a pinch only a CSS transform changes**, on the page column, anchored at the pointer or the midpoint.
  - The pinch ends on `gestureend` or `touchend`, or when no event has come for 150 ms.
  - Then the zoom commits: the model takes the new scale, the scroll offsets are set so the anchor point stays where it was, the transform goes and the canvases are drawn again.
- Buttons, keys and menu choices commit at once, anchored at the pane's centre.
- **A redraw draws into a fresh canvas** and swaps it in when done, so the old, stretched image stays in view meanwhile.
- Zooming never rebuilds a text or link layer (§ Stage 1: the Canvas 2D viewer, *Text*).
- **Until page 1's size is known**, and while the password prompt or a document error shows, −, the zoom level and + are disabled and the level reads "–", named *Zoom* (*縮放比例*). *Download* and *More* work.
- **The zoom level's name** is *Zoom {percent}* (*縮放比例 {percent}*) and its text is the percentage, so the visible text is part of the name. Percentages are written by `Intl.NumberFormat` in the page's locale, as percents.
- A zoom change made with a key or a pinch is announced once in the viewer's live region: the new level, or *Fit width*.

### Render budget

- **One page renders at a time**: the visible pages first, and among them the nearest first.
- **One module constant holds the page canvas cap in device pixels**: 5,242,880 on iOS, iPadOS and Android (pdf.js's own value, found with its own `isIOS` and `isAndroid` tests), 16,777,216 elsewhere, and no side over 32,767.
  - Both caps are below WebKit's per-canvas area limit. They are a memory budget, not that limit.
- **A page's backing size** is its CSS size × `devicePixelRatio`, cut down to the cap with `OutputScale#limitCanvas(width, height, maxPixels, maxDim)`. A capped canvas is drawn at the capped size and stretched with CSS.
- **A capped page also gets one detail canvas**, over its visible part and a margin, bounded by the same cap, at full device resolution.
  - It is drawn with `page.render({ canvas, viewport, transform: [dpr, 0, 0, dpr, -minX·dpr, -minY·dpr] })`, the transform pdf.js's own detail view uses.
  - It is drawn again once a scroll or a zoom has settled for 150 ms and the visible part has left the part it drew.
- **All live canvases share one budget**: 3 × the cap on iOS, 4 × the cap elsewhere. The pages furthest from the viewport give theirs up first.
- **Releasing a page** cancels its `RenderTask`, sets its canvases' `width` and `height` to 0, as pdf.js does, and calls `page.cleanup()`. A page that leaves the render window is released.
- **Pages render on the main thread.** `disableWorkerRendering: true` is passed and `GlobalWorkerOptions.rendererSrc` is never set: a canvas handed to pdf.js 6's renderer worker could not be released as above. Stage 3 looks at it again.
- Pages are drawn with the `canvas` render parameter, not the legacy `canvasContext`, on the default white background, with `annotationMode` left at its default.

### Page indicator

- **The indicator is a pill** centred at the pane's foot: 20px up and 36px tall on desktop, 24px up and 44px tall on a phone.
  - Its surface is opaque `--menu-surface` with a `--menu-edge` hairline and no shadow.
  - Its text is `--ink` at 14px (15px on a phone), in tabular figures, reading "3 / 14".
- In the DOM it comes after the header and before the pages, so it is the next Tab stop after *More*.
- Its name is "{page} / {total}, page {page} of {total}" (*{page} / {total}，第 {page} 頁，共 {total} 頁*). `aria-describedby` points at a visually hidden *Type a page number to go to it* (*輸入頁碼以跳頁*). It has no `aria-live`.
- **The current page** is the one filling the most of the pane's height. A tie goes to the lower number.
- **It shows on scroll and fades out 2 s after scrolling stops.**
  - The fade changes opacity alone, and a faded pill takes `pointer-events: none`, so a tap meant for the page never lands on it.
  - It stays in the Tab and VoiceOver order, and taking focus shows it again.
  - It stays fully shown, its timer paused, while it is hovered, while it or its field holds focus (`:focus-within`) and while the reader types. The timer starts again once the pointer and focus have both left.
- **Pressing it turns it into the page field**: a visible *Page* (*頁*, `aria-hidden`), an input holding the current number with its text selected, and a visible "/ 14" (`aria-hidden`).
  - The input's label is a visually hidden "Page number, {total} pages" (*頁碼，共 {total} 頁*).
  - It is `inputmode="numeric"` and 22px tall, so the 2px accent ring at 2px offset clears the pill's edge.
  - Under a coarse pointer its text is at least 16px ([web.md](./web.md) § Result viewer, *On a phone and a tablet*).
- **Enter commits**: a number scrolls to that page, clamped to 1…N. A blank or non-number entry cancels.
- **Leaving the field commits the same way**: Tab, a click elsewhere, iPhone's *完成* and a tap away. A number jumps, clamped; a blank or non-number entry cancels and the current page comes back. It is the same rule on both platforms.
- Typing the current page changes nothing.
- **A jump** puts the page's top one gap (16px on desktop, 10px on a phone) below the pane's top.
- After an Enter commit, focus moves to the target page's `<section>` (`focus({ preventScroll: true })`) and nothing else is announced. A commit by leaving leaves focus where it went.
- **Escape belongs to the field**: its wrapper carries `data-escape-owner`, which `UiModal` yields Escape to ([web.md](./web.md) § The settings dialog, *Keys over the dialog*). Escape cancels without committing, brings the pill back and returns focus to it. The viewer stays open.
- The pill is drawn inside the dialog's panel, so the result viewer's keyboard frame keeps it above the iOS keyboard with no wiring of its own ([web.md](./web.md) § Result viewer, *On a phone and a tablet*).

### Text

- **Each page in the render window has a pdf.js text layer**: `new TextLayer({ textContentSource: page.streamTextContent(), container, viewport })`. It lies over the canvas, its spans transparent, and the reader selects and copies with the platform's own selection.
- **The layers' geometry is CSS.**
  - The pane carries `--scale-factor` (the zoom × 96/72).
  - Each page carries `--user-unit` and `--total-scale-factor` (`calc(var(--scale-factor) * var(--user-unit))`), and `--scale-round-x` and `--scale-round-y` stay 1px. pdf.js 6 lays its layers out with these, not with `--scale-factor` alone.
  - The text and link layers are set to the full width and height of the page's content layer in the viewer's own CSS, over the size `setLayerDimensions` writes, so they always match the page box.
  - So zooming changes custom properties only, and never rebuilds a text or link layer.
- **The `.textLayer` rules and the link layer's** come from pdf.js's `web/pdf_viewer.css`, scoped under `.pdf-page-content`, its rotation rules too. Nothing else of that file is loaded.
- **Selection** keeps pdf.js's own fix: the text layer's end-of-content element and its `selectionchange` handling, ported from its `TextLayerBuilder`, so a selection dragged past the last span of a line does not jump to the rest of the page.
- **A copy on the Original view copies the selection as plain text**, its whitespace normalised. The Markdown view's serializer ([web.md](./web.md) § Result viewer, *Find and copy*) does not run there.
- **The text layer carries `lang` only when the document names its language** (`TextContent.lang`, the file's `/Lang`). Otherwise its `lang` is empty, so a screen reader does not read an English paper in the page's zh-Hant voice.
- **Fonts.** `cMapUrl` (with `cMapPacked: true`) and `standardFontDataUrl` point at same-origin copies of pdfjs-dist's `cmaps/` and of the Foxit files of its `standard_fonts/` (§ Stage 1: the Canvas 2D viewer, *Files served beside the build*).
  - A font that is neither embedded nor a Foxit one, such as Helvetica or Arial, is drawn with a system font (`useSystemFonts`, on by default).
  - Where the device has none, it takes the CSS generic family of its kind, such as `sans-serif`.

### Links

- **Links are the app's own `<a>` elements**, built from `page.getAnnotations()` into a link layer over the text layer, not pdf.js's `AnnotationLayer`.
  - Each is placed in percentages of the page with the arithmetic of pdf.js's `AnnotationElement._createContainer`, so a zoom moves it with the page in CSS.
  - A link over several quads may have one `<a>` per quad. Its rectangle alone is enough.
- **An external link** is `data.url`, pdf.js's checked URL, never `unsafeUrl`. Only `http:` and `https:` are followed; `ftp:`, `mailto:` and `tel:` are dropped. It opens in a new tab with `rel="noopener noreferrer"`.
- **An internal link** is `data.dest`, named or explicit.
  - A named destination is resolved with `getDestination`. An explicit one starts with a page reference, resolved with `getPageIndex`, or with a 0-based page index.
  - It scrolls to that page and the destination's top. The destination's zoom or fit type is ignored, so the zoom and fit-width mode do not change.
  - Focus then moves to the target page's `<section>` (`preventScroll`).
- **The named actions** NextPage, PrevPage, FirstPage and LastPage scroll.
- **Everything else does nothing**: other named actions, attachments, OCG states, JavaScript and form resets.
- **A link's name** is its overlaid text (`overlaidText`) when pdf.js has one. Without it, an internal link is named *Page {page}* (*第 {page} 頁*) and an external one by its URL's host.

### Loading

- **pdf.js is imported on first use** (`import()`), so no page loads it with its first paint, and a server render never does.
- **A feature probe picks the build** (`supportsModernPdfjs`).
  - The modern build calls the newest built-ins without a guard — `Map.prototype.getOrInsertComputed` on every render, `Promise.try` on every request that waits for a reply, `Math.sumPrecise`, `Uint8Array.prototype.toHex`, the iterator helpers — and an engine that lacks one fails part way through a page rather than at the start. The legacy build polyfills them.
  - The probe tests those built-ins on the main thread, which stands for the worker: both run on the same engine.
  - Each build runs with its own worker (`build/pdf.worker.min.mjs` or `legacy/build/pdf.worker.min.mjs`), set as `GlobalWorkerOptions.workerSrc`. The two report the same version, so a mismatched pair passes pdf.js's check and fails later.
- **The document opens with** `getDocument({ url, cMapUrl, cMapPacked: true, standardFontDataUrl, wasmUrl, useWasm: false, useWorkerFetch: true, enableXfa: false, disableWorkerRendering: true, verbosity: VerbosityLevel.ERRORS })`.
  - The asset URLs are absolute, built against the page's origin, because the worker resolves a relative one against its own `/_nuxt/` address.
  - Range requests, streaming and the background fetch keep their defaults, so a large file shows its first pages before it is all downloaded, and the rest keeps arriving.
- **Closing the viewer destroys the loading task** (`loadingTask.destroy()`). Every step that awaits pdf.js checks, when it resumes, that it still belongs to the document on screen, since a destroyed task's promise can stay pending for ever.
- **While the document opens**, a 2px accent bar under the header runs back and forth.
  - It is `role="progressbar"`, named *Opening the file* (*正在開啟檔案*), with no value attributes (indeterminate), and goes when the document is open.
  - Under `prefers-reduced-motion` it is static.
  - `loadingTask.onProgress` drives nothing visible: its `percent` is `NaN` without a total, and with range requests the first pages show before most of the file has come.
- **Each page is a white placeholder** before it is drawn. A page not drawn after 0.4 s shows a spinner: `UiSpinner` at 24px, in the content layer's light colours, with no `role="status"`.
- The pane carries `aria-busy="true"` until page 1 is drawn.
- **Opening and its end are announced** in the viewer's live region: *Opening {name}* (*正在開啟 {name}*), then *Opened, {total} pages* (*已開啟，共 {total} 頁*).

### Password

- **A PDF that needs a password asks for it in the page**, centred: a lock glyph, the heading *This PDF needs a password* (*這份 PDF 需要密碼*), the body, the field and *Open* (*開啟*). The browser's dialog is never used.
- The body says what holds: *The password is used only in this browser, to open the file. It isn’t sent to the server or saved.* (*密碼只在這個瀏覽器裡用來開啟檔案，不會傳到伺服器，也不會儲存。*)
- **The password goes to pdf.js only**, through `loadingTask.onPassword(updateCallback, reason)` (`PasswordResponses.NEED_PASSWORD` 1, `INCORRECT_PASSWORD` 2). It is never sent to the server, stored, logged or put in a URL. Reopening the file asks again.
- The field is a password field, and it takes focus when the prompt appears.
- **A wrong password** keeps focus in the field and selects its text. Under the field *That password isn’t right. Try again.* (*密碼不對，再試一次。*) appears with `role="alert"`, the field is `aria-invalid` and its `aria-describedby` points at that line. The first prompt shows no error.
- While the prompt shows, zoom is disabled, and *Download* and *More* work.

### Errors

- **A document that cannot be opened shows a centred state**: a glyph, an `<h3>` title and the reason, with actions by cause. No English error code and no blank view is ever shown.
- **Corrupt**: `InvalidPDFException`, any other parse failure, and any failure not named below.
  - *This PDF can’t be shown* (*無法顯示這份 PDF*) over *The file’s contents can’t be read. It may have been damaged when it was uploaded. You can download the original and open it in another app.* (*檔案的內容讀不出來，可能在上傳時就已經損壞。你可以下載原檔，用其他程式打開看看。*)
  - Its actions are *Download ({size})* (*下載（{size}）*; plain *Download* when the size is not known), the primary one, and *Open in new tab* (*在新分頁開啟*), a link.
- **Network**: the fetch rejects, or the server answers 5xx. *Could not reach the server.* (*無法連線到伺服器。*, `errors.network`) with *Retry*, which opens the document again.
- **Signed out**: a `ResponseException` with `status` 401. *Your session has ended. Sign in again to continue.* (*登入階段已結束，請重新登入。*, `errors.signedOut`), handled as an ended session is everywhere else in the app ([web.md](./web.md) § Rules).
- **Gone**: a `ResponseException` with `missing` true (a 404). *This file is no longer here.* (*這個檔案已經不在了。*) with *Back to Files* (*回到檔案*), which closes the viewer.
- **A document-level error is announced once**, its title and reason, in the viewer's live region.
  - Focus moves to the heading (`tabindex="-1"`) only if it was inside the content that went away or had fallen to `<body>`.
  - Focus on a header control stays there.
- **A page that fails to draw keeps its place** and its known size: its own, or page 1's while it has none. The other pages draw as usual.
- A page failure caused by a 401, a 404 or the network takes the wording of the matching document state, never a claim that the page is broken.
- **Any other `getPage` or `render` rejection** shows, centred on the page, *Page {page} can’t be shown* (*無法顯示第 {page} 頁*, an `<h3>`), *This page wasn’t drawn. The other pages aren’t affected.* (*這一頁沒有畫出來，其他頁不受影響。*) and *Retry*.
  - The `<section>` is then named *Page {page}, can’t be shown* (*第 {page} 頁，無法顯示*), and the button *Retry page {page}* (*重試第 {page} 頁*).
- **A page failure never moves focus.** It is announced once per failure, and only while the page is in view.
- **While a retry runs**, its button stays where it is, `aria-disabled` and busy, so it keeps focus. A retry that works moves focus to the page's `<section>`. A retry that fails keeps focus on the button and announces again.
- A cancelled render (`RenderingCancelledException`) and the abort that closing the viewer causes are not errors and show nothing.
- `stopAtErrors` stays false, so a damaged content stream draws as far as it goes.

### Colours

- **The header** is unpainted, so it is the panel's `--paper` (#ffffff light, #1c1c1e dark), the same on Original and on Markdown.
- **The surround**, the scroll pane behind the pages, is `--desk` (#f5f5f7 light, #000000 dark).
- **A page is always white paper**, its colours never inverted. Its border follows the theme (`--line-strong`: #d2d2d7, #48484a).
- **Each page has one content layer** that holds the canvas, the text layer, the link layer, the spinner and the failed-page state.
  - It carries `color-scheme: light`, so every token inside it takes its light value in both themes, and its paper is `--paper` under it.
  - The page element itself does not carry it, so its border follows the theme.
- **The pill** is opaque `--menu-surface` with `--menu-edge` in both themes, never glass, because it sits on white paper and has to be read there. In dark, *Page* and "/ 14" are `--muted` (#98989d), the digits `--ink`, and the input's fill is `--fill`.
- The header's controls are the existing glass capsule and round back button (`--glass`, `--shadow-glass`).
- **The state glyphs** — `lock`, `file-x` for a document, `file-exclamation-point` for a page — are 48px on a document state and 40px on a page, in `--faint`, at stroke 1.75, and `aria-hidden`.

### Phone

- **The header** is the compact one of the result viewer's phone layout: *Close* at the left, the filename centred at 14px semibold with no tracking, and at the right a capsule of *Download* and *More* only ([web.md](./web.md) § Result viewer, *On a phone and a tablet*).
- **More** has the same facts and views as on desktop, then a zoom group, set off by a rule: *Fit width* (*符合寬度*), *Zoom to 2×*, *Zoom to 3×* and *Zoom to 5×* (*放大 2 倍*, *放大 3 倍*, *放大 5 倍*), multiples of fit width with the current one checked, then *Open in new tab*. So zoom works without a gesture, under VoiceOver or Switch Control. It has no *Find* on the Original view.
- **Pinch** zooms about the fingers, from fit width up to 5 × fit width.
- **A double tap** toggles between fit width and 2 × fit width, about the point tapped.
- The pane takes pinch and double tap with `touch-action: pan-x pan-y` and its own handlers. The viewport meta keeps page zoom: no `user-scalable=no` and no `maximum-scale`.
- A long press still makes the platform's text selection.
- The system's left-edge back swipe is an accepted limit and is not fought.

### Accessibility

- **The DOM order** is the header (back, the `<h2>` filename, the capsule), then the page indicator, then the pane. Painting order is set by z-index alone.
- **The capsule** is `UiCapsule`'s `role="group"` named *Reader tools* (*閱讀工具*), with no roving tabindex. Disabled zoom buttons are natively `disabled`.
- The back button is named *Close*.
- **The pane is `<main tabindex="0">`** named after the file.
  - When the viewer opens on Original it takes focus with `focus({ preventScroll: true })`.
  - Its ring is the 2px accent drawn inside its edge, shown only while the keyboard moves focus ([web.md](./web.md) § Rules).
- **Each page is a `<section>`** named *Page {page} of {total}* (*第 {page} 頁，共 {total} 頁*), with `tabindex="-1"` and no ring.
  - Its canvases are `aria-hidden="true"`.
  - Its accessible content is the text layer and the links, in reading order.
- State titles are `<h3>`, one level under the shell's `<h2>`.
- **The viewer has its own polite live region** (`useStatusNote`), since the result viewer's sits with the Markdown view. It says: opening and opened, a zoom by key or pinch, a document error once, and a page error once while in view.
- Figure alt text from `getStructTree()` and a note that a page is a scan are not built.

### Keyboard

- **Once the pane holds focus**, Space, Shift+Space, PageUp, PageDown, Home, End and the arrow keys scroll it with the browser's own scrolling, sideways too when the page is wider than the pane.
- A click on a page leaves focus in the pane, the nearest focusable ancestor, so the keys keep working after a click.
- The zoom keys are in § Stage 1: the Canvas 2D viewer, *Zoom*.

### What stage 1 does not do

- **No find in the original.** Ctrl/⌘F is left to the browser on the Original view, and finds only in the text layers mounted at the time. *Open in new tab* is the way to search the whole file.
- **No print.** Ctrl/⌘P is left to the browser. *Open in new tab* is the way to print.
- No thumbnail sidebar, no jump from a figure to its box (stage 2), and no new preview for other kinds of file, which are handled apart.
- No go-to-page key and no fit-width key.
- No form fields, no annotation editing, no PDF JavaScript, no ICC colour management (qcms is WebAssembly) and no XFA.

### Files served beside the build

- **The worker** is `pdf.worker.min.mjs` of the chosen build, imported with Vite's `?url`, so the build emits it as a hashed `/_nuxt/` asset on this origin. pdf.js wraps a cross-origin `workerSrc` in a `blob:` URL, which the site's `worker-src 'self'` would refuse ([deployment.md](./deployment.md) § Security headers).
- **The data files** are served under `/pdfjs/<version>/`, `<version>` being the installed `pdfjs-dist`'s. These files, and only these:

  | Path | Files | For |
  |------|-------|-----|
  | `cmaps/` | `*.bcmap`, `LICENSE` | CJK text in fonts that are not embedded and use a predefined CMap |
  | `standard_fonts/` | `Foxit*.pfb`, `LICENSE_FOXIT` | The standard fonts pdf.js draws from its own data, Symbol and ZapfDingbats among them |
  | `wasm/` | `jbig2_nowasm_fallback.js`, `openjpeg_nowasm_fallback.js`, `LICENSE_JBIG2`, `LICENSE_PDFJS_JBIG2`, `LICENSE_OPENJPEG`, `LICENSE_PDFJS_OPENJPEG` | JBIG2 and JPEG 2000 images, decoded in JavaScript |

- No `.wasm` file, no QuickJS, no qcms, no `iccs/` and no Liberation font is served (§ Licences).
- **A local Nuxt module copies them** (`modules/pdfjs-assets.ts`).
  - At build time it copies that list from the installed `pdfjs-dist` into the build directory, and registers each folder as a nitro public asset directory at `/pdfjs/<version>/<folder>`, cached for a year.
  - It sets `runtimeConfig.public.pdfjsBase` to `/pdfjs/<version>`, read from the installed package, so no version is written by hand.
  - The version is in the path because a year's `immutable` cache would otherwise keep an older pdf.js's data after an upgrade.
  - Nothing of it is committed, and a path outside the list is a 404.

### The code

- Pure, framework-free modules in `app/lib/pdf/`, each with its vitest test, none importing `pdfjs-dist`:
  - `zoom.ts`: the zoom's mode and scale, the limits, the ladder, fit width and fit page, the opening zoom, a phone's multiples, the anchored scroll of a zoom commit, the percentage, which menu preset is checked, and the viewer's state as the result viewer reads it;
  - `layout.ts`: the page boxes, their tops, the column width, the render window, the pages that need a size, the current page, a jump's offset, the correction for a size change above the viewport and a destination's offset;
  - `budget.ts`: the cap by platform, a page's backing size, the detail canvas's part and size, and which canvases go under the total budget;
  - `page-input.ts`: the page field's parsing, clamping and commit or cancel, for Enter and for leaving;
  - `errors.ts`: a failure read as corrupt, network, signed out, gone or cancelled, and a page's own failure;
  - `links.ts`: the external URL filter, a link's box on the page, an explicit destination's page and top, and a named action's step;
  - `support.ts`: the probe that picks the modern or the legacy build.
- Browser-side modules in `app/lib/pdf/`, the only code that touches pdf.js:
  - `pdfjs.ts`: loads the chosen build and its worker once, and opens a document;
  - `page-view.ts`: one page's layers — base canvas, detail canvas, text layer and link layer — drawn, drawn again at a new scale, cancelled and released;
  - `text-selection.ts`: the selection fix and the plain-text copy, ported from pdf.js's `TextLayerBuilder`;
  - `gestures.ts`: pinch from `wheel` with `ctrlKey`, WebKit's gesture events and two-finger touch, and the double tap.
- `app/components/pdf/Viewer.vue` (`PdfViewer`) mounts it in the pane and owns the zoom keys and the live region; `PageIndicator.vue` (`PdfPageIndicator`) is the pill and the page field. The result viewer owns the header around them ([web.md](./web.md) § Result viewer).
- `app/assets/css/pdf-layers.css` holds the text and link layers' rules taken from pdf.js.

### The device test list

The stage 1 viewer is checked by hand with this list on the owner's Mac and iPhone ([testing.md](./testing.md) § Frontend).

**Mac, Safari 27.2** (and Chrome where noted):

1. Open a converted PDF on Original. Page 1 shows at 100% (or fit width in a narrow window), the header is white and the surround light grey.
2. Press ⌘+ and ⌘−. Only the PDF changes size, the header and menus do not, and the level steps through the ladder. Repeat in Chrome.
3. Press ⌘0. The PDF goes to 100% and the menu's 100% row is checked.
4. Pinch on the trackpad. The PDF zooms about the cursor, the page around it never zooms, and pages are sharp after you let go.
5. Zoom to 500% on a dense page. Nothing is blank and the visible part is sharp.
6. Zoom past the window's width, then two-finger swipe to the left edge and keep swiping. Safari does not go back and the viewer stays open.
7. Open the viewer by mouse and press Space, PageDown, the arrows, Home and End. The pages scroll with no ring drawn. Press Tab to the pane: an inset ring appears.
8. Choose *Fit width* and resize the window. The page keeps the window's width. Zoom by hand and resize again: it no longer follows.
9. Scroll, stop and wait 2 s. The pill fades. Hover it, or Tab or ⌥Tab to it: it shows and stays.
10. Click the pill, type 9 and press Enter. The view jumps to page 9 and the pill reads 9 / 14.
11. Type 99 and press Enter: the last page. Type nothing and press Enter: no jump.
12. Type 5 and press Tab: it jumps to 5. Clear the field and click a page: it cancels.
13. Press Escape in the field. The field closes and the viewer stays open.
14. Select a paragraph, press ⌘C and paste into Notes. The text comes out as plain text.
15. Click an internal link, such as a citation. The view scrolls to that page and the zoom does not change. Click a web link: it opens in a new tab.
16. Turn on dark mode. The header and surround are dark, the page is still white with its colours not inverted, and the pill is opaque and readable.
17. Open a PDF that needs a password. The cursor is in the field. A wrong password shows the red line and keeps the text selected, and VoiceOver reads it out.
18. Open a damaged PDF. *This PDF can’t be shown* shows with *Download ({size})* and *Open in new tab*, which opens the browser's viewer in a new tab.
19. Open a PDF with portrait and landscape pages mixed, and scroll fast past pages not yet sized. What you are reading does not jump.
20. Press ⌘F on Original. Safari's own find opens and finds text only on nearby pages.
21. With VoiceOver, Tab runs Close, the zoom controls, Download, More, the pill, then the pages. Each page reads as *Page N of M* followed by its text.
22. Zoom to 200% on page 5, switch to Markdown from More and back to Original. The view is on page 5 at 200%.

**iPhone, Safari 27.2**:

1. Open a PDF. It fits the width, you can scroll to the last page, and the header shows only Download and More.
2. Pinch. Only the PDF zooms, up to 5×, never below the screen's width, and it is sharp after you let go.
3. Zoom to 5× on a dense page. Nothing is blank.
4. Double tap: 2×. Double tap again: fit width.
5. Open More and choose *Zoom to 3×*. The view goes to 3× and the row is checked.
6. Flick fast through the file, landscape pages included. Nothing jumps and the fling is not cut short.
7. Tap the pill during a fling. The scroll stops and the pill stays for 2 s. The next tap opens the field.
8. Type 7 and tap *完成*: page 7. Open the field again, clear it and tap a page: no jump.
9. While typing, the pill sits above the keyboard and the header stays on screen.
10. Long-press a word. The system's selection appears and the page does not zoom.
11. Tap a web link inside the PDF. It opens in a new tab.
12. Turn on dark mode. The pages stay white and the pill is readable.

## What is taken from where

| Problem | Taken from | How |
|---------|-----------|-----|
| Reading the file, fonts to outlines, the text layer, find | pdf.js | used as a library |
| Selecting across a text layer, and the text and link layers' CSS | pdf.js's `web/` viewer (`TextLayerBuilder`, `pdf_viewer.css`) | ported |
| A capped page canvas with a detail canvas, and a link's box on the page | pdf.js's `web/` viewer and display layer (`PDFPageView`, `AnnotationElement`) | the design, rewritten |
| The object model and the interface a renderer draws through | PDFium, hayro | the design, rewritten |
| Filling a path with antialiased coverage (sparse strips) | Vello (`vello_common`) | ported |
| Strokes, joins, caps and dashes | kurbo | ported |
| The glyph atlas and when a glyph is drawn as a path instead | hayro | ported |
| Gradient and blend-mode formulas | Skia | ported |

- A ported file names its source, the source's licence and the revision it was ported from in its header.
- `THIRD_PARTY_NOTICES` lists each of them, from the stage that adds the first.

## Licences

- The repository is MIT, and the viewer takes code under Apache-2.0, MIT or a BSD licence only.
- **The rule covers what a dependency ships beside its code**: a font or a data file the browser is served from a dependency is held to the same list as its code.
- Every source in § What is taken from where is one of those.
- **Left out of pdfjs-dist**, and so never served (§ Stage 1: the Canvas 2D viewer, *Files served beside the build*):
  - the Liberation fonts (`standard_fonts/LiberationSans-*.ttf`), GPL-2.0 with a font exception. pdf.js fetches one only as the last fallback for a Helvetica or Arial that is not embedded, and without it that text takes a system font;
  - the ICC profile (`iccs/`, CC0-1.0, not on the list), which only the WebAssembly colour management reads;
  - every `.wasm` file, qcms and QuickJS (MIT, but WebAssembly), since the viewer runs without WebAssembly and runs no PDF JavaScript.
- **Poppler is GPL and MuPDF is AGPL. No code is copied or adapted from either**, and neither is a dependency of the web app. The server runs Poppler as a separate program ([parsing.md](./parsing.md)), which copies nothing.
- **`THIRD_PARTY_NOTICES`**, at the repository root, carries the notice and licence of everything third-party the viewer ships:
  - pdfjs-dist (Apache-2.0), which also covers the files ported from pdf.js;
  - Adobe's CMaps (BSD-3-Clause style);
  - the Foxit fonts (PDFium, BSD-3-Clause);
  - OpenJPEG (BSD-2-Clause) and PDFium's JBIG2 decoder (BSD-3-Clause), with pdf.js's wrappers of the two (`LICENSE_PDFJS_OPENJPEG`, BSD-2-Clause; `LICENSE_PDFJS_JBIG2`, Apache-2.0).
- **The licence check** is a vitest test in `apps/web` (`app/lib/`, node environment). It reads the installed tree of the viewer's libraries (§ Libraries) and fails on a licence outside the list.
  - A package that never reaches a browser is a named exception in the test, with its reason. `@napi-rs/canvas` is one: pdfjs-dist's optional canvas for Node, which pdf.js loads only under Node.

## The fallback

From stage 4, when the WebGPU renderer draws, pdf.js's Canvas 2D renderer draws the page when:

- the browser has no WebGPU, or gives no adapter or device. Safari has WebGPU from macOS 26 and iOS 26 on;
- the device is lost and a new one cannot be had;
- the page uses a font pdf.js has no outlines for, one that is not embedded in the file.

The fallback is a whole page at a time: a page is never drawn half by each renderer.

## Safari

Safari 27.2 is WebKit 625.2.8. These rules come from that source and from WebKit's bug tracker. The device check asks the first three of a real device (§ The device check), and § Recorded devices holds its answers; no check exercises the rest.

- **A canvas the reader sees is never read through a 2D canvas or made into a bitmap.** In Safari 27.2 `drawImage`, `toDataURL`, `toBlob` and `createImageBitmap` of a WebGPU canvas leave it showing a buffer nothing was drawn to, which is black, until the canvas is drawn again. A Mac and an iPhone on Safari 27.2 both show it: the canvas the check reads this way is black afterwards, and a second read gives opaque black. Chromium reads an empty frame from a canvas whose frame has been shown. A thumbnail or a print is drawn into a texture the renderer owns.
- **Pixels are read from the GPU**, with `copyTextureToBuffer` and `mapAsync`.
  - A canvas read this way is configured with `COPY_SRC`.
  - Its current texture is copied in the task that drew it. A copy made later reads nothing and raises no error.
  - `bgra8unorm` bytes come blue first.
- **A worker shows a page on a canvas the page handed it** with `transferControlToOffscreen()`. Both recorded devices draw it right.
- **A worker's `transferToImageBitmap()` frame is not what a page is shown from.** WebKit 625 copies the surface without waiting for the drawing to be scheduled. Both recorded devices hand back the frame drawn, in each of the three ways the check tries, yet the same Mac has read a wrong color from one.
- **No multisampling.** Safari 27.2 loses the device with it, transient attachments or not (WebKit bug 329089).
- **A lost device may leave the page with no adapter** until it is reloaded (WebKit bug 331190). That is the second case of § The fallback.
- **A texture view lives for the pass that uses it.** On iOS 27.2 a view that is kept makes the GPU process's memory grow without bound (WebKit bug 329399).
- **A WebGPU canvas costs three surfaces of its full size**, about 12 bytes for each device pixel. Stage 7's memory budget counts them, and a page out of view gives its canvas up. The stage 1 viewer's Canvas 2D canvases have their own budget (§ Stage 1: the Canvas 2D viewer, *Render budget*).
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
- `pdfjs-dist` 6.5.438 (Apache-2.0) from stage 1, at that exact version.
  - Its optional dependency `@napi-rs/canvas` (MIT) is a canvas for pdf.js under Node. The browser build never loads it, and nothing of it ships.
  - Besides the bundled code, its worker, its CMaps, its Foxit fonts and its two no-WebAssembly image decoders are served beside the build (§ Stage 1: the Canvas 2D viewer, *Files served beside the build*).

## Testing

[testing.md](./testing.md) § Frontend holds the commands and the rule for what is unit tested.

- The stage 1 viewer's pure parts are framework-free modules with vitest tests (§ Stage 1: the Canvas 2D viewer, *The code*). No test imports `pdfjs-dist` or fetches a PDF.
- What the stage 1 viewer does in a browser is checked by hand on the owner's Mac and iPhone, with § Stage 1: the Canvas 2D viewer, *The device test list*.
- The licence check runs with the unit tests (§ Licences).
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
- **A run that starts clears the last run's answers**, so nothing copied is older than the colors on the page. A run that cannot finish says so, with the error's message where the browser gave one.
- **The code.** `app/pages/pdf-probe.vue` draws and lists. `app/lib/pdf/probe.ts` holds what a run records and the copied text, and is unit tested. `probe-gpu.ts` runs the checks, `probe.worker.ts` is the worker's half and `probe-draw.ts` what the two share: the drawing, the read-back and the worker's messages. The page loads `probe-gpu.ts` only when a check is started, so no other page asks for a GPU device.

### Recorded devices

What the check answered on the devices the renderer is designed for. A row is added when a device's copied report is in hand.

| Device | Browser | Checks passed | Largest texture | Notes |
|--------|---------|---------------|-----------------|-------|
| Mac, Apple GPU, 8 cores, a 2× screen | Safari 27.2 | 11 of 11 | 16384 | Adapter and device in under 1 ms, the worker's device and first read in 3 ms, recovery from a lost device in 2 ms |
| iPhone, Apple GPU, 4 cores reported, a 3× screen | Safari 27.2 | 11 of 11 | 16384 | Adapter in under 1 ms, device in 4 ms, the worker's device and first read in 1 ms, recovery from a lost device in 437 ms |

On both:

- **The screen matches the dots** on every canvas but the one read through a 2D canvas. That one reads its color in the task that drew it, reads opaque black once the frame has been shown, and is black on the screen (§ Safari).
- **A worker's frame comes back in the color drawn** from all three ways of making a bitmap, and the canvas a worker draws shows its color.
- **The canvas format is `bgra8unorm`.**
- **The limits**: 2048 texture array layers, 11 bind groups, 44 sampled textures per shader stage, 8 color attachments and 12 vertex buffers. The largest buffer, and the largest storage and uniform buffer bindings, are 2147483644 bytes on the Mac and 1073741824 on the iPhone.
- **Among the features**: `float16-renderable`, `shader-f16`, `bgra8unorm-storage`, `texture-formats-tier1`, `texture-formats-tier2` and `timestamp-query`.
