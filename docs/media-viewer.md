# The media viewer

The web app shows a kept file that is not a PDF's pages itself: an image in the result viewer's Original view, and a video, an audio file, plain text or a file with no preview in the file viewer. This doc is the home of those views, of the shell both viewers share with the PDF viewer, and of the More menu every original's viewer opens.

Where a file opens from is [web.md](./web.md) § Files, *Opening a file*. How a PDF's pages are drawn is [pdf-viewer.md](./pdf-viewer.md). The bytes come from `…/file` and `…/original` ([api.md](./api.md)).

## Which viewer shows what

| The file | Viewer | Pane |
|----------|--------|------|
| A PDF | result viewer | the PDF viewer ([pdf-viewer.md](./pdf-viewer.md)) |
| Any `image/*`, SVG, GIF, HEIC and TIFF included | result viewer | the image view (§ The image view) |
| Any `video/*` | file viewer | the player's video layout; its audio layout when the file has no video track; the failure sheet when its picture cannot be decoded (§ The player) |
| Any `audio/*` | file viewer | the player's audio layout |
| `text/plain` | file viewer | the text pane (§ Text and files with no preview) |
| Anything else | file viewer | the no-preview sheet |

- `filePreview` and `originalKind` in `app/lib/library.ts` decide it, from the row's `media_type`.
- **An SVG opens in the image view.** The file route serves it as an attachment under `Content-Security-Policy: sandbox` ([api.md](./api.md) § `GET /api/library/documents/{id}/file`), and an `<img>` still draws it: an SVG in an `<img>` runs no script and loads nothing.
- A PDF or an image converted or not, and a failed parse, open in the result viewer (§ Result viewer in [web.md](./web.md)).

## The shell

The result viewer and the file viewer are two components on one shell, `UiModal size="full" tall flush back`. They look and behave the same.

- **The header**, unpainted on `--paper`:
  - at the left, the round glass back button, named *Close*;
  - the filename, centred, alone: no kind, no size;
  - at the right, one glass capsule. The result viewer's is named *Reader tools* (*閱讀工具*), the file viewer's *File tools* (*檔案工具*).
- **The capsule**:
  - on the Original view of a PDF or an image, on desktop: *Zoom out*, the zoom level, *Zoom in*, a rule, *Download* and *More*;
  - on a video, an audio file, a text file and a file with no preview: *Download* and *More*;
  - on a phone, everywhere: *Download* and *More* alone.
- **Download is a button in both viewers.** It saves the file from its `?download=1` address. It is never a link, because the dialog focuses its first field that is not a button on open, and Enter on a link there would start a download.
- **The pane** is a `<main>` under the header, on `--desk`, named after the file.
- **The loading bar.** While the file opens, a 2px accent bar runs back and forth under the header, `role="progressbar"`, named *Opening the file* (*正在開啟檔案*), with no value. It is static under `prefers-reduced-motion`.
  - On an image, a video and an audio file, a 24px `UiSpinner` shows centred on the desk once the file has taken 0.4 s.
  - On a PDF the spinner is on each page instead ([pdf-viewer.md](./pdf-viewer.md) § Stage 1: the Canvas 2D viewer, *Loading*).
  - A text file shows its skeleton lines instead, and no bar. A file with no preview has nothing to load.
- **The state sheet** is one layout, centred on the desk: a 48px glyph in `--faint` at stroke 1.75, an `<h3>` title at `--text-base` semibold, the body at `--text-sm` in `--muted` at most 44ch wide, and the actions under it, 12px apart. A PDF that cannot be opened, an image that cannot be shown, a video or an audio file that cannot be played and a file with no preview all use it. A PDF's password prompt keeps its own form ([pdf-viewer.md](./pdf-viewer.md) § Stage 1: the Canvas 2D viewer, *Password*).
- **One More menu** (§ The More menu).
- **On a phone** (`viewerLayout` in `app/lib/columns.ts`, [web.md](./web.md) § Result viewer, *On a phone and a tablet*) both viewers draw the compact header: *Close* at the left, the filename centred at `--text-sm` semibold with no tracking, and the capsule of *Download* and *More*. Both read the layout when they open and again when the window changes size or the pointer changes between fine and coarse, and both pass it to the dialog as its compact header.
- **Dark mode** is the same structure: the header on `--paper`, the pane on `--desk`. A file's own pixels are never inverted.

## The More menu

One menu for every PDF, image, video, audio, text and other file. A markdown document's More is [web.md](./web.md)'s (§ Views and editability).

- **It is built when it opens and does not grow while it is open.** A fact the browser learns later, such as an image's dimensions, is in it the next time it opens.
- **Its groups**, in this order, with a rule between each:
  1. **The header and the facts.**
     - A file not converted, and a failed parse: the header *File details* (*檔案資訊*), then *Kind*; *Dimensions* (*尺寸*) on an image and a video, and *Duration* (*長度*) on a video and an audio file, each once the browser knows it; then *Size*, *Date added* and *Location*.
     - A converted document: the header *Parse details* and the parse's facts ([web.md](./web.md) § Result viewer, *The header*).
  2. **Original** and **Markdown**, on a PDF and an image ([web.md](./web.md) § Result viewer, *Original and Markdown*).
  3. **On a phone, on the Original view of a PDF or an image, the zoom group**: *Fit width* on a PDF, *Fit to window* (*符合視窗*) on an image, then *Zoom to 2×*, *Zoom to 3×* and *Zoom to 5×* (*放大 2 倍*, *放大 3 倍*, *放大 5 倍*), multiples of that fit, as `menuitemradio` choices with the current one checked.
  4. **Rename** (`edit`), **Duplicate** (`copy-plus`) and **Show in Files** (`folder-open`).
  5. **Move to Trash** (`trash`), in the danger colour.
  6. **Open in new tab** (`external-link`), on the Original view of a PDF or an image: the file in the browser's own viewer.
     - An SVG has none, since the file route serves it as an attachment and a new tab would download it.
     - An image the view could not show has none while its failure sheet is up: the sheet's *Download* is the way to the file, and a tab could only download it or fail the same way. A PDF keeps it, as its error sheet does ([pdf-viewer.md](./pdf-viewer.md) § Stage 1: the Canvas 2D viewer, *Errors*).
- A video, an audio file, a text file and a file with no preview have groups 1, 4 and 5 alone.
- **A converted document** shows groups 1 (its parse facts), 2, 4, 5 and, on its Original view, 3 and 6. The file operations are there on both of its views, the Markdown view included.
- **The facts' values.** Dimensions read *4032 × 3024*, whole pixels with no grouping. A duration reads *12:48* under an hour and *1:02:03* from one, cut to the second (`formatDuration` in `app/lib/format.ts`). A duration the browser reports as infinite or unknown is left out.
- **The file operations are the Files page's own** ([web.md](./web.md) § Files, *Menus and file operations*), on the open row:
  - *Rename* closes the viewer and opens the row's name for editing in the list, the row selected.
  - *Duplicate* makes the copy in the same folder, with ` (2)` where the name is taken. The viewer stays open.
  - *Show in Files* closes the viewer on the row, selected and in view.
  - *Move to Trash* closes the viewer, and the status note says so with *Undo*.
  - **On a converted document, each of the four first writes back what is open and waits for it**: the open block, the images waiting on it, then the document's and the notes' saves, as an ask to the agent waits ([web.md](./web.md) § The agent panel, *An ask that sends waits for the save*). A save that had failed is sent once more first. Only then is the file renamed, copied, shown or trashed, so a copy holds the last edit.
  - *Move to Trash* closes the viewer once the saves have settled and before the trash is sent, so nothing can be typed into a document on its way to the Trash, and a trashed document is never written to after it went.
  - A save that fails, or meets a file changed on disk, stops the operation: the header's *Not saved* or the reload note says why, and the reader can choose it again.
  - The operation is the Files page's once it is chosen: a viewer closed while the saves are still settling does not cancel it, and it runs on the file that was open when it was chosen.
- **It opens under More's right edge.** Opened from the keyboard, its first item is highlighted. A press on More while the menu is open closes it and does not open it again.

## The image view

The Original view of an image. Its copy is the catalog's `viewer.image` group, beside the `viewer.pdf` keys it shares with the PDF viewer.

### Layout

- **The image sits on the desk**, with no radius, no border and no shadow.
- **100% is one image pixel per CSS pixel**: the image's `naturalWidth` × `naturalHeight`, which the browser gives in the photo's EXIF orientation.
- **The image box** is that size × the zoom, in whole CSS pixels, rounded down (with the PDF viewer's allowance for a rounding error), so a fit never overflows the pane by a pixel.
- **A box that fits inside the pane less its gutters is centred.** A larger one scrolls in the pane, with 16px of desk beyond each edge on desktop and on a tablet, and none on a phone. Each axis is taken on its own, as the PDF viewer takes it, so the margin never jumps from what is left over to the gutter as a box grows past the pane.
- **A transparent image has a light checkerboard behind it**, inside the image box only and once the image has loaded: `--checker-size` (16px) squares of `--checker-base` and `--checker-square` (#ffffff and #e5e5ea), the same in both themes.
- While it loads, the image takes no room in the pane, so a large photo never gives the pane scrollbars before it shows.
- **The image is an `<img>`**, so a GIF and every animated format animate, and a photo is turned upright by its EXIF orientation (`image-orientation: from-image`, the default).
- **An SVG with no width or height** of its own takes the size the browser gives it: Chrome gives one 150px high, its width from the `viewBox`. An image whose natural size the browser reports as 0 takes 300 × 150, the CSS default size of a replaced element (`naturalSize`), and More's *Dimensions* reads that; the drawing keeps its own ratio inside that box, so the checkerboard can show beside it.

### Zoom

- **The zoom is a mode and a scale**: *fit to window* or a custom scale, 1 being 100%.
- **Opening.** The view opens at fit to window: the whole image inside the pane less a 16px gutter on each side on desktop and on a tablet (none on a phone), never above 100%. So an image smaller than that box opens at its own size, centred.
- **A switch back from Markdown** (a converted image) returns to where the reader was: the zoom it left, a fit refitted to the pane, and the image point that was at the pane's centre at the centre again, as a PDF returns to its page. The file is served `no-store`, so the image may load again on the way back, with the loading bar for that moment.
- **The range** on desktop runs from the smaller of fit and 25% to 500%. On a phone it runs from fit, which is the floor, to the larger of 5 × fit and 100%.
- A custom scale that comes to the floor where the floor is the fit is fit to window itself, so it keeps following the window.
- **The zoom menu** opens from the zoom level: *Fit to window*, a separator, then 50, 75, 100 (with its key, ⌘0), 125, 150, 200, 300 and 400%. They are `menuitemradio` items with the current one checked. A fit checks only *Fit to window*, never the percentage it happens to come to, and a level between presets checks nothing.
- **The ladder.** −, + and the keys step to the next rung above or below the current level, kept inside the range, keeping the centre of the pane fixed.
  - On desktop the rungs are the PDF viewer's: 25, 50, 75, 100, 125, 150, 200, 300, 400 and 500%. A fit between rungs steps to the rung above or below it: from 26%, + goes to 50% and − to 25%.
  - On a phone the rungs are 1×, 2×, 3× and 5× fit, then the ceiling.
  - Rungs are compared as whole percentages, as the PDF viewer compares them.
- **The keys**, while the image shows: ⌘+ zooms in (⌘=, ⌘⇧= and the keypad's + too), ⌘− zooms out, and ⌘0 goes to 100%. They are read as the PDF viewer reads them ([pdf-viewer.md](./pdf-viewer.md) § Stage 1: the Canvas 2D viewer, *Zoom*): Ctrl off Apple platforms, never with ⌥, on `document` in the capture phase while the viewer's dialog is the topmost. *Fit to window* has no key, and ⌘9 is never bound.
- **While the Original view of an image is open, in any state** — loading, failed or shown — ⌘+, ⌘− and ⌘0 never zoom the web page: their default is cancelled whether or not the image can act on them.
- **A trackpad pinch and Ctrl with the wheel** zoom about the pointer, a two-finger touch pinch about the fingers' midpoint, through the PDF viewer's gesture handling (`attachPinch`). The page around the image never zooms.
- **During a pinch only a CSS transform changes.** When the pinch ends, as the PDF viewer's does, the zoom commits: the image box takes its real width and height, the pane scrolls so the point under the anchor stays where it was, and the transform goes, so Safari draws the image sharp again.
- Buttons, keys and menu choices commit at once, anchored at the pane's centre.
- **Panning.** While the image box is wider or taller than the pane:
  - a drag with the mouse or a pen pans it, the cursor `grab` and `grabbing` while held;
  - the wheel and a two-finger trackpad swipe scroll the pane;
  - Space, Shift+Space, PageUp, PageDown, Home, End and the arrow keys scroll the pane once it holds focus;
  - on touch, one finger scrolls the pane.
  - The pane has `overscroll-behavior: contain`, so a pan that reaches an edge never turns into Safari's back or forward swipe.
- **A double click** zooms about the pointer: from fit to window to 100%, and from any other zoom back to fit. Where the image fits the window at its own size, so fit is 100%, it goes from fit to 200% instead.
- **A double tap on a phone** goes from fit to 2 × fit about the point tapped, and from any other zoom back to fit. A double tap on a larger touch screen does what a double click does.
- **While fit to window is chosen**, a resize of the pane — the window, a rotation, the layout changing — fits the image again. A custom zoom keeps its scale, kept inside the new range, with the point at the pane's centre kept there.
- **The zoom level's name** is *Zoom {percent}* (*縮放比例 {percent}*), and at fit *Zoom {percent}, fit to window* (*縮放比例 {percent}，符合視窗*). Its text is the percentage, written by `Intl.NumberFormat` in the page's locale.
- A zoom made with a key or a pinch is announced once in the viewer's live region: the new level, or *Fit to window*.
- **Until the image has loaded, and when it has failed**, −, the zoom level and + are disabled, the level reads "–", named *Zoom* (*縮放比例*). On a phone, More's zoom group is disabled. *Download* and *More* work, More without *Dimensions*.

### Loading

- **The image is read from** `…/file` while the row is a file kept as it is, and from `…/original` once it is a parsed document ([api.md](./api.md)).
- The image is not shown until it has loaded. Meanwhile the pane shows the desk, the loading bar and, after 0.4 s, the spinner, and carries `aria-busy="true"`.
- Once it has loaded it shows whole, at the opening zoom.
- Opening and its end are announced in the viewer's live region: *Opening {name}* (*正在開啟 {name}*), then *Opened* (*已開啟*).

### Failure

- **An `<img>` that fails says nothing of why.** So the viewer then reads one byte of the same address (`Range: bytes=0-0`, `probeFileStatus` in `app/lib/api.ts`) and classes the failure by the answer (`mediaFailure` in `app/lib/media/failure.ts`):
  - **Network**: the read never reached the server, the browser is offline, or the server answered 5xx. The PDF viewer's *Could not reach the server.* (*無法連線到伺服器。*) with *Retry*, which loads the image again.
  - **Signed out**: a 401. *Your session has ended. Sign in again to continue.* (*登入階段已結束，請重新登入。*), handled as an ended session is everywhere else in the app.
  - **Gone**: a 404. *This file is no longer here.* (*這個檔案已經不在了。*) with *Back to Files* (*回到檔案*), which closes the viewer.
  - **Not shown**: any other answer, a 206 included. The file is there and the browser cannot draw it. *This browser doesn’t support this image format, or the file is damaged. You can download the original and open it in another app.* (*這個瀏覽器不支援這種圖片格式，或是檔案已經損壞。你可以下載原檔，用其他程式打開看看。*) with *Download ({size})* (*下載（{size}）*; plain *Download* when the size is not known), the primary action, a button.
- Every cause is drawn as the state sheet, its glyph `file-x`, its title *This image can’t be shown* (*無法顯示這張圖片*), its body the cause.
- HEIC and TIFF are drawn by Safari alone. Chrome and Firefox show the *Not shown* state for them.
- An `<img>` gives no cause, so an image whose transfer broke while the one-byte read then gets through is classed *Not shown*, not *Network*. Its *Download* still works.
- The failure is announced once, its title and its cause, in the viewer's live region. Focus moves to the heading (`tabindex="-1"`) only if it was inside the pane or had fallen to `<body>`.

### Phone

- The header is the compact one, with *Download* and *More* alone (§ The shell). More holds the zoom group (§ The More menu).
- **Pinch** zooms only the image, about the fingers, from fit up to the larger of 5 × fit and 100%.
- The pane takes pinch and double tap with `touch-action: pan-x pan-y` and its own handlers. The viewport meta keeps page zoom: no `user-scalable=no` and no `maximum-scale`.
- The system's left-edge back swipe is an accepted limit and is not fought.

## The player: video and audio

The file viewer's pane for a video or an audio file. Its copy is the catalog's `viewer.video`, `viewer.audio` and `viewer.media` groups.

### Video

- **The browser's own controls**: `<video controls playsinline preload="metadata">`, never autoplayed.
  - `playsinline`, so an iPhone plays it in the page; the controls' own full-screen button still goes full screen.
  - Its address carries the media fragment `#t=0.001`, so Safari on an iPhone seeks to the first frame and shows it before play. The fragment is never sent to the server, and the controls still start at 0:00.
- **Until the video's size is known**, the pane shows the desk, the loading bar and, after 0.4 s, the spinner. The video is not shown and takes no room, so a portrait video never shows a landscape box first. The one exception is a browser that reads nothing until a play (§ A device that does not preload).
- **One element reads the file at a time.** A Retry, a new file and the audio layout's `<audio>` replace the element, and the one replaced is released (its `src` removed and `load()` called), so it stops reading and nothing it still reports acts on the new one.
- **Once `loadedmetadata` has come with a picture**, the video is a black box (`--media-matte`, #000000 in both themes), centred: the largest rectangle of the video's aspect ratio inside the pane less a 16px gutter on each side on desktop and on a tablet, none on a phone, in whole pixels (rounded down, with the image box's allowance, so the limiting side meets the gutter exactly). A video smaller than that grows to it. It has no radius.
- The box follows the pane when it is resized.
- **No zoom.** The capsule is *Download* and *More*.
- **The keys**, with no modifier held:
  - Space plays or pauses;
  - ← and → go back and forward 5 seconds, within the video;
  - F puts the video full screen, and out of it again (`requestFullscreen`, or `webkitEnterFullscreen` where only that exists, as on an iPhone);
  - M mutes and unmutes.
  - F and M are read by the key they sit on too (`KeyF`, `KeyM`), so they work under any input source. A held Space, F or M does not repeat, and its repeats are taken too, so they never reach the browser's controls; a held arrow repeats.
- **Space, F and M are the pane's wherever focus is inside it**: on the pane, on the video after a click on it, or on one of its controls after Tab. The pane takes them before the video and its controls do (a listener in the capture phase that cancels the key and stops it there), so each acts exactly once in every browser. WebKit's own controls ignore Space while the video holds focus, and Chrome's would act on it a second time, so leaving them to the browser would break either way. The release of a Space the pane took is stopped too, so a focused control button cannot also press itself.
- **← and → are the pane's while the pane holds focus.** Once the video or one of its controls holds focus they are the browser's own: the page cannot tell its scrubber from its volume slider, and a seek must never take a volume change's place.
- Tab, Escape and any key with ⌘, Ctrl, ⌥ or Shift are never taken.
- **Video has no *Open in new tab*.**

### A video with no picture

- **A video whose `loadedmetadata` comes with `videoWidth` 0 is one of two things**, which the browser reports the same way: a file with sound alone, or one whose picture this browser cannot decode while it plays the sound over black. Chrome without an HEVC decoder does the second with an HEVC MP4, and so does Firefox (Mozilla bug 1818542): no `error`, `videoWidth` 0, no decoded frames, and no `videoTracks` to ask.
- **So the viewer asks the file.** While the loading bar stays up:
  - where the browser has `videoTracks` (Safari), it counts them;
  - otherwise it reads the track list of an MP4 or a QuickTime file itself (`scanIsoTracks` in `app/lib/media/tracks.ts`): the movie header's tracks and their handler, over `Range` reads of the same address, one when the header is at the start of the file and two or three when it is at the end, never more than four reads or a header over 16 MiB.
- **A file with a video track** gets the failure sheet: *This video can’t be played* with *Download ({size})* (§ *Failure*, below).
- **A file with no video track** gets the audio layout, exactly as an audio file has it: the glyph and the player, nothing else.
- **What the read cannot tell** — a WebM or a Matroska file, a QuickTime header it does not read, a read that fails — gets the audio layout too. In practice a WebM's picture is VP8, VP9 or AV1, which Chrome and Firefox decode themselves, so there `videoWidth` 0 means sound alone; an HEVC inside Matroska is the case left out.

### Audio

- **The glyph and the player, centred in the pane**: the `music` glyph at 48px in `--faint`, and under it, 16px apart (the state sheet's glyph spacing), the browser's own `<audio controls preload="metadata">`, `min(100%, var(--audio-width))` wide (360px).
- The pane repeats neither the name nor the facts: the name is the header's, the facts are More's.
- **While it opens**, the pane shows the desk, the loading bar and, after 0.4 s, the spinner, as a video's does; the glyph and the player appear together once `loadedmetadata` has come. No board draws this state.
- The keys are the video's, without F: Space, ← and → and M, read the same way. No board names them; they are there so a reader who knows the video's keys finds them here too.

### A device that does not preload

No board draws this state. It is a safety net, so a browser that reads nothing until a play never leaves the reader at a spinner with no play button.

- **A `suspend` while nothing has loaded (`readyState` 0) is not a sign by itself.** Chrome fires one on every video and audio file before its metadata arrives. So it only starts a 3 s wait (`NO_PRELOAD_MS`); any `progress`, the metadata, an error or a close ends the wait, and the pane keeps the desk, the bar and the spinner throughout. On a browser that reads the file, the wait never runs out and nothing on screen differs.
- **If 3 s pass with nothing read**, the browser is waiting for a play before it reads the file. Then the player shows so the reader can press play: a video in a 16:9 box, an audio file in its layout. A video takes its real box when `loadedmetadata` comes, so a portrait video there changes shape once.
- Whether any device does this is not known; iOS in Low Power Mode is the one suspected. The iPhone list checks it.

### Failure

- **The `error` event** is classed by the same one-byte read as an image's (§ The image view, *Failure*): network, signed out and gone take that wording and those actions. Then the `MediaError` code and the moment narrow it:
  - `MEDIA_ERR_ABORTED` is ignored.
  - `MEDIA_ERR_NETWORK` is **Network** unless the read answered 401 or 404: the browser says the transfer broke, and the read that came after may have found the connection back. A file that is gone or a session that ended reads as that, whatever the element said.
  - An error after the video's or the audio file's metadata had come, other than a decode error, is **Network** too, again unless the read answered 401 or 404: a file that opened and then failed is not a format the browser cannot play. Its *Retry* opens the file again at the moment it failed, paused.
  - So a video that played for minutes and lost the connection says *Could not reach the server.* with *Retry*, never that the format is not supported.
- **A not-supported or decode error, before the metadata, with a read that got through** means the browser cannot play the file:
  - a video: *This video can’t be played* (*無法播放這部影片*) over *This browser doesn’t support this video’s format. You can download the original and open it in another player.* (*這個瀏覽器不支援這部影片的格式。你可以下載原檔，用其他播放程式打開。*);
  - an audio file: *This audio file can’t be played* (*無法播放這個聲音檔*) over *This browser doesn’t support this audio file’s format. You can download the original and open it in another player.* (*這個瀏覽器不支援這個聲音檔的格式。你可以下載原檔，用其他播放程式打開。*);
  - each with *Download ({size})*, the primary action, a button.
- For a network, signed-out or gone failure the title is the video's or the audio file's, the body the cause.
- A decode error after the metadata, with a read that got through, takes the same sheet: the file is damaged past its start.
- Every cause is the state sheet with the `file-x` glyph, announced once, with focus moved as on an image.
- **Safari plays neither MKV nor AVI**, and fails them this way. Chrome plays some MKV files (H.264 or VP9 with AAC or Opus), and may play others with a picture and no sound (AC3 or DTS sound). An HEVC video in Chrome or Firefox is told apart as § *A video with no picture* says.
- **What the viewer cannot detect**: a picture with no sound; and a file the browser accepts but then never draws, which can show a black box with no sheet (the Linux WebKit does this with an AVI). *Download* is in the capsule for both.

## Text and files with no preview

- **A text file** shows its first 256 KiB (262,144 bytes) in a monospace pane on the desk, `--text-sm` at 1.5, wrapping anywhere, at most 84ch wide and centred, scrolling in the pane. A file cut there ends with *The first {size} of the file.* (*只顯示檔案開頭的 {size}。*), the size written as every size in the app is, 1000-based and rounded down: *262 kB*. A read that fails is a banner in the pane, its message the API's.
  - The pane takes focus on open, so the keys scroll it.
- **Any other file** is the state sheet: the file's own glyph, the title *There is no preview for this kind of file* (*這種檔案沒有預覽*), the body *The Agent can still read it in any conversation.* (*在任何對話中，Agent 仍然可以讀取它。*) and *Download ({size})*, the primary action, a button.

## Colours

- **The header** is `--paper`, the pane `--desk` (#f5f5f7 light, #000000 dark), as the PDF viewer's ([pdf-viewer.md](./pdf-viewer.md) § Stage 1: the Canvas 2D viewer, *Colours*).
- **Three tokens keep one value in both themes** (`app/assets/css/main.css`):
  - `--checker-base` #ffffff and `--checker-square` #e5e5ea, the image view's checkerboard;
  - `--media-matte` #000000, the video's box.
- Two size tokens sit beside the spacing: `--checker-size` (16px), the checkerboard's square, and `--audio-width` (360px), the audio player's width.
- The state glyphs are 48px in `--faint`, at stroke 1.75, and `aria-hidden`.

## Accessibility

- **The DOM order** is the header (back, the `<h2>` filename, the capsule), then the pane.
- **The pane is `<main tabindex="0">`** named after the file, except while it shows a state sheet. When the viewer opens it takes focus with `focus({ preventScroll: true })`. Its ring is the 2px accent outline drawn inside its edge (`outline-offset: -2px`), shown only while the keyboard moves focus ([web.md](./web.md) § Rules): one rule in `main.css` for every pane, the PDF's included. Each pane carries `.keyboard-pane` and sits in a `.keyboard-frame`, the positioned box around it that does not scroll, and the frame draws the ring over the pane's content: an outline on the scrolling pane itself is painted under what the pane positions, so a zoomed image, a PDF's pages or a full-width video would hide it.
- A pane showing a state sheet is not a Tab stop, unlike the boards, which draw it with `tabindex="0"`: it holds nothing to scroll or zoom, and the sheet's heading takes focus instead.
- **The image** is an `<img>` whose `alt` is the filename.
- **The video and the audio player** keep their native controls in the Tab order: the dialog's Tab trap counts `video[controls]` and `audio[controls]` as controls (`app/lib/focus.ts`). Until the player shows, the element is `inert`, so the trap never counts a control that cannot take focus.
- The video pane carries `aria-keyshortcuts="Space ArrowLeft ArrowRight F M"`, the audio pane the same without F.
- **Each pane has its own polite live region** (`useStatusNote`). It says: opening and opened, a zoom by key or pinch, and a failure once.
- State titles are `<h3>`, one level under the shell's `<h2>`.
- The views and the zoom group in More are `menuitemradio`, and the facts are the menu's description ([web.md](./web.md) § Design system, *Primitives*).
- **Reduced motion**: the loading bar is static and the spinner pulses. A zoom is never animated.

## The code

- **Pure, framework-free modules in `app/lib/media/`**, each with its vitest test:
  - `image-zoom.ts`: the image view's zoom — fit to window, the range, a menu choice, the ladder, a pinch, a double click and a double tap, a resize, which choice is checked — and its geometry: the image box, where it sits on the scrolling stage, and the scroll that keeps a point under the anchor;
  - `player.ts`: the video box in the pane, a seek kept inside the video, and the no-preload wait;
  - `tracks.ts`: whether an MP4 or a QuickTime file holds a video track, from its box tree, and which bytes to read next;
  - `failure.ts`: a failure read from the one-byte read's answer, the `MediaError` code and whether the metadata had come.
- `app/lib/api.ts` holds `probeFileStatus`, the one-byte read, `readFileBytes`, a range of a file for the track read, and `downloadFile`, which saves an address as a download. `app/lib/shortcuts.ts` holds `mediaKey`, the player's keys; `app/lib/format.ts` holds `formatDuration`.
- **`app/components/media/ImageViewer.vue` (`MediaImageViewer`)** is the image view's pane. It owns its zoom keys, its gestures and its live region; the result viewer owns the header around it and drives the zoom through the exposed `zoomIn`, `zoomOut` and `zoomTo`, as it drives the PDF viewer's ([web.md](./web.md) § Result viewer).
- **`app/components/media/Player.vue` (`MediaPlayer`)** is the video and audio pane, with its keys and its live region; the file viewer owns the header.
- **`app/components/viewer/`** holds what every original's viewer shares: `StateSheet.vue` (`ViewerStateSheet`), the state sheet, and `Loading.vue` (`ViewerLoading`), the loading bar and the delayed spinner. `app/composables/useKeyboardFocus.ts` tells the panes when the keyboard moves focus, and the `.keyboard-pane` and `.keyboard-frame` rules in `app/assets/css/main.css` draw their ring. `app/composables/useWindowLayout.ts` gives the file viewer the window's `viewerLayout`, read again on a resize and on a change of pointer, as the result viewer reads its own.
- The image view takes from `app/lib/pdf/` what is not about PDFs, as it is: `attachPinch` from `gestures.ts`, the ladder, the presets, the phone's multiples, the desktop range and `percentOf` from `zoom.ts`, the spacing from `layout.ts`, and `LoadErrorKind` from `errors.ts`. Nothing in `app/lib/pdf/` imports `app/lib/media/`.

## What this does not do

- No previous or next file, no Space to preview from the list, no figure opened large from the document, and no thumbnails in the list.
- No video poster from the server, no transcoding, and no conversion of HEIC or TIFF for a browser that cannot draw them.
- No remembered playback position: a video opens at its start.
- No pixel-exact drawing at high zoom: an image is smoothed past 100%, as Preview smooths it.
- No stall detection: a file the browser accepts and never draws is not told from one that is slow.

## Edge cases

- **An empty file** (0 bytes) answers the one-byte read with 416. An empty image therefore reads *This image can’t be shown* with the format wording and *Download (0 byte)*, and an empty video or audio file the *can’t be played* sheet the same way. An empty text file shows an empty pane. This is a known limit, not a separate state.
- **A very large box.** The ceiling stays 500% whatever the image's size, so a 20000 px tall image at 500% is a box 100000 px tall. Chrome draws it. Whether Safari does is on the Mac list; if it draws it blank, a cap is the owner's decision.
- **A very long filename** is cut with an ellipsis in the header; the capsule never shrinks.
- **More opened before the browser knows a fact** (an image's dimensions, a video's size or length) has no row for it and does not grow; it has the row when it next opens.

## The device test list

The media viewer is checked by hand with this list on the owner's Mac and iPhone ([testing.md](./testing.md) § Frontend). None of it chooses *Markdown* or converts a file.

**Mac, Safari 27.2** (and Chrome where noted):

1. Open a large JPEG kept as a file. The whole photo shows inside the window with a margin, the header is white with the filename alone, the surround is light grey, and the level reads its fit, such as 26%.
2. Press ⌘+ and ⌘−. Only the image changes size, never the header or the menus, and the level steps through the ladder from the fit. Repeat in Chrome.
3. Press ⌘0. The image goes to 100% and the zoom menu's 100% row is checked.
4. Pinch on the trackpad. The image zooms about the cursor, the page around it never zooms, and the image is sharp after you let go.
5. At 200%, drag the image: the cursor is a hand and the image follows. Two-finger swipe to the left edge and keep swiping: Safari does not go back and the viewer stays open. Choose *Fit to window* and two-finger swipe left and right: the viewer stays open.
6. Double-click a point of the photo at fit: 100% about that point. Double-click again: fit. Open a small PNG and double-click: 200%, then back to its own size.
7. Choose *Fit to window* and resize the window: the image keeps fitting. Zoom by hand and resize again: it keeps its zoom.
8. Open a tall image, such as a long screenshot. It fits whole, with no scrolling, at fit.
9. Open a transparent PNG. The light checkerboard is behind the image and nowhere else. Turn on dark mode: the surround is black, the checkerboard is still light and the image's colours are not inverted.
10. Open an SVG. It shows, and is sharp at 500%. Its More has no *Open in new tab*.
11. Open an animated GIF: it plays. Open a photo taken sideways: it is upright, and More's *Dimensions* reads it upright.
12. Open a HEIC kept as a file: Safari shows it. Open it in Chrome: *This image can’t be shown* with *Download (size)*, which downloads the file.
13. Turn Wi-Fi off and open an image: *Could not reach the server.* with *Retry*. Turn it on and press *Retry*: the image shows.
14. Open More on an image kept as a file: *File details* with *Dimensions*, *Original* checked and *Markdown*, *Rename*, *Duplicate*, *Show in Files*, *Move to Trash* in red, and *Open in new tab*, which opens the image in a new tab. Open More before the image has loaded: no *Dimensions* row, and the menu does not grow when the image arrives.
15. Choose *Duplicate*: the viewer stays and the copy is in the folder behind it. Choose *Rename*: the viewer closes and the row's name is ready to edit. Open it again and choose *Move to Trash*: the viewer closes and the note offers *Undo*.
16. Open More on a PDF kept as a file. It holds the same four file operations, between the views and *Open in new tab*.
17. Open a landscape video. Nothing plays by itself, the first frame shows in a black box as wide as the window allows, and the controls are Safari's own.
18. Press Space: it plays. Press Space again: it pauses. Press → and ←: 5 seconds on and back. Press M: muted. Press F: full screen; press F again: back. Click the video (the click itself plays or pauses it) and press Space: it plays or pauses once, never twice and never not at all. Press Space again: it goes the other way. Note what ← and → do while the video holds focus.
19. Open a portrait video: it fills the window's height, with the surround at its sides. Turn on dark mode: the surround is black.
20. Open a video and press Enter at once: nothing downloads.
21. Safari only: open an MKV and an AVI. Each shows *This video can’t be played* with *Download (size)*. (Chrome plays some MKV files; this check is not run there.)
22. Open an MP4 with sound alone: the music glyph and the player, and nothing else. In Chrome, open an HEVC MP4 that Chrome cannot decode (if this Mac has a decoder, Chrome plays it with its picture, which passes too): *This video can’t be played* with *Download (size)*, never sound over black.
23. Open an M4A: the glyph and a 360px player, centred, nothing else in the pane. Press Space: it plays.
24. Open a ZIP: *There is no preview for this kind of file* with *Download (size)*. Open a text file: its text, on the desk.
25. Click More, then click it again: the menu opens, then closes, and does not open again.
26. Turn on Safari › Settings › Advanced › *Press Tab to highlight each item on a webpage* first. With VoiceOver, Tab runs Close, the zoom controls, Download, More, then the image's pane; on a video, Close, Download, More, the pane, then the video's controls. The opening and a failure are read out.
27. Open a long image (20000 px tall) and zoom to 500%: it still draws, not blank. Repeat in Chrome.
28. Open a video taken on the iPhone (an HEVC `.mov`), in Safari and in Chrome: each shows its picture.
29. Open a video and check Safari's controls: picture-in-picture and playback speed are there.
30. Turn on Reduce Motion and open a large image: the loading bar does not move, and the spinner pulses rather than spins.
31. Open an audio file Safari cannot play, such as a `.wma`: *This audio file can’t be played* with *Download (size)*.
32. Play a video, turn Wi-Fi off and seek far ahead: *Could not reach the server.* with *Retry*. Turn Wi-Fi on and press *Retry*: the video is back where it stopped, paused.
33. Open More on a large video before it has loaded: no *Dimensions* or *Duration*, and the menu does not grow. Close and reopen it: both are there.
34. Open a file whose name is 120 characters long: the header cuts it with an ellipsis, and the capsule stays whole.

**iPhone, Safari 27.2**:

1. Open a large photo. It fits the screen, whole, and the header shows only Download and More.
2. Pinch. Only the photo zooms, never below the screen's fit and no further than 5 × fit or 100%, whichever is larger. A 48 MP photo at 100% is sharp after you let go.
3. Double tap: 2 × fit. Double tap again: fit.
4. Open More and choose *Zoom to 3×*. The view goes to 3 × fit and the row is checked.
5. Zoomed in, drag with one finger: the photo pans.
6. Turn the phone at fit: the photo fits again.
7. Open a HEIC from the iPhone's camera: it shows.
8. Open a video. Its first frame shows before play, it plays in the page rather than full screen, the controls' full-screen button works, and scrubbing is smooth.
9. Turn on Low Power Mode and open a portrait video. Note whether a landscape box shows before you press play, and report it: § A device that does not preload waits on this. You can press play, and it plays.
10. Open an M4A and play it.
11. Turn on dark mode. The surround is black and the checkerboard stays light.
12. Open a ZIP and a text file: each has the compact header, the filename centred, and Download and More at the right.
13. Open More on a photo: the facts, *Original* and *Markdown*, the zoom group, *Rename*, *Duplicate*, *Show in Files*, *Move to Trash* and *Open in new tab*. Choose *Rename*: the viewer closes and the row's name is ready to edit.

## Testing

[testing.md](./testing.md) § Frontend holds the commands and the rule for what is unit tested.

- The pure modules in `app/lib/media/` and the helpers named in § The code have vitest tests. No test loads an image or a video, or reaches the network.
- What the views do in a browser is checked by hand with § The device test list.
