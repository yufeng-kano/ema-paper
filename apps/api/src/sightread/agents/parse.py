"""The `parse` agent: a document in, markdown with figure placeholders out
(docs/parsing.md, docs/agents.md).

Its run loop lives in `jobs/runner.py`; this module holds what defines its output — the
default prompt and the pipeline version that keys its cached results. The coordinate
contract its results declare is `agents/bbox.py`'s.
"""

from __future__ import annotations

# Part of the dedup cache key; bumped whenever results for the same input would change
# (docs/jobs.md; docs/parsing.md says what moved each version).
PIPELINE_VERSION = 10

# A job queued under an earlier version runs without the figure check: its result answers
# a dedup key that promised none (docs/parsing.md § Figure check).
FIGURE_CHECK_SINCE_PIPELINE = 10

# What the template substitutes per page (upstream/client.py): the chips the Agent page's
# prompt dialog inserts (docs/web.md).
PROMPT_TOKENS: tuple[str, ...] = ("{page}", "{bbox_format}", "{bbox_instructions}")

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
The box must enclose the whole figure: every panel, and everything drawn as part of it —
axes, tick labels, axis titles, legends, colour bars, panel titles and labels, and
annotations. A figure made of several panels is one figure with one placeholder, unless
each panel has a caption of its own. The box must exclude the figure's caption and any
surrounding body text: the caption is the text on the next line, never part of the box.
Emit no other Markdown images.
"""

# The figure check's question about a transcribed page whose boxes the rules flagged
# (docs/parsing.md § Figure check). The page image carries every box's crop as a numbered
# rectangle; `{box_format}` says what a rectangle's four integers are in the model's own
# convention and `{boxes}` lists each rectangle in it. Part of the pipeline, not a setting.
FIGURE_CHECK_PROMPT = """This page image has a numbered rectangle drawn around each figure of
the page. Check every rectangle.

A rectangle is right when it encloses its whole figure: every panel, and everything drawn as
part of it — axes, tick labels, axis titles, legends, colour bars, panel titles and labels,
and annotations — and nothing else: not the figure's caption, not the surrounding body text,
not another figure. Each rectangle stays on its own figure, or on its own part of a figure
drawn as several rectangles: never merge two rectangles into one box.

Each rectangle's box, as {box_format}:
{boxes}

Answer with JSON only, one entry per rectangle, in this shape, where a, b, c and d stand for
the four integers of a corrected box:
{"figures": [{"n": 1, "ok": true}, {"n": 2, "ok": false, "box": [a, b, c, d]}]}
"ok" is true when the rectangle is right. When it is not, "box" is the corrected box for the
same figure, in the same form as the boxes above.
"""
