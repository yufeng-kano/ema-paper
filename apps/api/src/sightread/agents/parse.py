"""The `parse` agent: a document in, markdown with figure placeholders out
(docs/parsing.md, docs/agents.md).

Its run loop lives in `jobs/runner.py`; this module holds what defines its output — the
default prompt, the coordinate contract it declares, and the pipeline version that keys
its cached results.
"""

from __future__ import annotations

# Coordinate contract every result declares: [ymin, xmin, ymax, xmax], normalized 0-1000,
# origin top-left. What a model is asked for depends on its family and is converted into
# this space before storage (agents/bbox.py, docs/parsing.md § Coordinate conventions).
BBOX_FORMAT_YXYX = "yxyx_norm1000"

# Part of the dedup cache key; bump when the pipeline changes results (docs/jobs.md).
PIPELINE_VERSION = 6  # 6: the coordinate paragraph follows the model's family

# Transcription prompt for a rendered page. `{page}` is the real page number: the model is
# told what to write so the placeholder it emits already matches our own numbering.
# `{bbox_instructions}` is the coordinate paragraph for the model's family — the placeholder
# to emit and what its four integers mean (agents/bbox.py).
DEFAULT_PROMPT_TEMPLATE = """Transcribe this page image into GitHub-flavoured Markdown.
Reproduce the text verbatim and in reading order, keeping headings, lists, tables and
formulas. Do not summarise, do not comment, and do not wrap your answer in a code fence.

Leave out the page's furniture: the page or slide number, and any running header or footer
that repeats across pages. A number that belongs to the content is not furniture and must be
kept — a number inside a sentence, a figure or table label, a citation marker, a numbered
heading, or any number inside a table.

{bbox_instructions}
The box must tightly bound the graphic itself — the plotted area, drawing
or photograph — and must exclude the figure's caption or title and any surrounding body
text: the caption is the text on the next line, never part of the box. Emit no other
Markdown images.
"""
