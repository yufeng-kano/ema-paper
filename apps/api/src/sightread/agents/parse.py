"""The `parse` agent: a document in, markdown with figure placeholders out
(docs/parsing.md, docs/agents.md).

Its run loop lives in `jobs/runner.py`; this module holds what defines its output — the
default prompt, the coordinate contract it declares, and the pipeline version that keys
its cached results.
"""

from __future__ import annotations

# Coordinate contract: [ymin, xmin, ymax, xmax], normalized 0-1000, origin top-left. The
# service never converts coordinates (docs/parsing.md).
BBOX_FORMAT_YXYX = "yxyx_norm1000"

# Part of the dedup cache key; bump when the pipeline changes results (docs/jobs.md).
PIPELINE_VERSION = 5  # 5: page/slide numbers and running headers and footers dropped

# Transcription prompt for a rendered page. `{page}` is the real page number: the model is
# told what to write so the placeholder it emits already matches our own numbering.
DEFAULT_PROMPT_TEMPLATE = """Transcribe this page image into GitHub-flavoured Markdown.
Reproduce the text verbatim and in reading order, keeping headings, lists, tables and
formulas. Do not summarise, do not comment, and do not wrap your answer in a code fence.

Leave out the page's furniture: the page or slide number, and any running header or footer
that repeats across pages. A number that belongs to the content is not furniture and must be
kept — a number inside a sentence, a figure or table label, a citation marker, a numbered
heading, or any number inside a table.

Where a figure, chart, photograph, diagram or map appears, emit a line of the form
![fig](sightread://p{page}/YMIN,XMIN,YMAX,XMAX)
at that position, with the figure's caption verbatim on the very next line (an empty line
when it has none). YMIN, XMIN, YMAX and XMAX are integers in the {bbox_format} coordinate
space: [ymin, xmin, ymax, xmax] normalized to 0-1000, origin at the top-left corner of
this page image. The box must tightly bound the graphic itself — the plotted area, drawing
or photograph — and must exclude the figure's caption or title and any surrounding body
text: the caption is the text on the next line, never part of the box. Emit no other
Markdown images.
"""
