"""The chat agent's skills: named instructions for one kind of task (docs/chat.md § Skills).

The built-ins are defined here and every user has them; a user's own skills and their edits
of a built-in are rows in `skills`. This module merges the two and renders a skill for the
model. It reads no database: the caller hands it the rows.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

# A skill's name, and a subagent's: what `/name` and the tools take (docs/chat.md § Skills).
NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
DESCRIPTION_MAX_CHARS = 1024
ARGUMENT_HINT_MAX_CHARS = 128
ARGUMENTS_PLACEHOLDER = "$ARGUMENTS"
SKILL_FIELDS = ("description", "argument_hint", "body", "user_invocable", "model_invocable")


@dataclass(frozen=True)
class SkillDefinition:
    name: str
    description: str
    argument_hint: str
    body: str
    user_invocable: bool = True
    model_invocable: bool = True


CHECK_FIGURES = SkillDefinition(
    name="check-figures",
    description="Check each figure caption against its page image and correct the captions that differ.",
    argument_hint="[pages]",
    body="""Check every figure caption in the range against the page it is on.

Range: $ARGUMENTS (the whole document when empty).

For each figure placeholder in doc.md within the range:
1. Open the crop with view_figure and the whole page with view_page.
2. Compare the caption line under the placeholder with the page: the figure number, the sub-figure labels, numbers, names and units.
3. Where the caption differs from the page, correct it with the smallest str_replace edit that makes it match.

Change nothing but captions. When the page cannot settle a caption, leave it and say so. End with a list of what you changed, by page.""",
    user_invocable=True,
    model_invocable=False,
)

FIX_MATH = SkillDefinition(
    name="fix-math",
    description="Check formulas against the page images and correct transcription errors in them.",
    argument_hint="[pages]",
    body="""Check the formulas in the range against the page images and correct them.

Range: $ARGUMENTS (the whole document when empty).

For each formula in doc.md within the range — `$...$` inline, `$$...$$` on its own lines:
1. Open its page with view_page.
2. Compare symbol by symbol: sub- and superscripts, Greek letters, operators, brackets and equation numbers.
3. Where the transcription differs, correct it with the smallest str_replace edit, in KaTeX syntax.

Change nothing else. When the page cannot settle a formula, leave it and say so. End with a list of what you changed, by page.""",
    user_invocable=True,
    model_invocable=True,
)

# The reader's command only: a translation asked for in their own words is answered in the
# reply, and running the command is the request to write one (docs/chat.md § Translation).
TRANSLATE = SkillDefinition(
    name="translate",
    description="Translate the selected passage, or the whole document, inserting each block's translation right under it.",
    argument_hint="[language] [instructions]",
    body="""Translate into the document: each block's translation goes right under the block, as ordinary markdown. The user ran this command to have the translation written into the document.

Arguments: $ARGUMENTS
The first word is the target language when it names one (zh-TW, 日本語, English); otherwise translate into the reader locale named in the message's context. The rest is the user's own instructions for this translation: pass them word for word to every subagent you start, and follow them over the rules here.

Range: the blocks the selected passage covers when the message carries a selection, the whole document otherwise. If the range is already in the target language, say so and stop.

1. Find the range in doc.md. Read its headings and its opening (the abstract, in a paper) and list the key terms with the translation each must take, so that every piece uses the same words.
2. Split the range at its page markers into pieces of two to four pages; a short range is one piece. Start one `translator` per piece, all in the same round, giving each its pages, the target language, the user's instructions and your list of terms.
3. Insert what the translators report with the editor's insert: for each `@@ N` entry, insert after line N an empty line followed by the translation. Go from the last piece to the first and, within a piece, from its last entry to its first, so that every line number still points where the translator read it. Make one piece's inserts per round of calls.
4. When a report says its translator stopped before the end of its pages, then once every report is inserted, start a `translator` for the pages it did not finish and insert its report the same way.
5. Start one `translation-checker` per piece, all in the same round, with the same pages, language, instructions and list of terms. Apply each correction it reports with str_replace, checking first that the text to replace is still there.

End with a short summary in the user's terms: what was translated, what the check corrected, and anything left untranslated.""",
    user_invocable=True,
    model_invocable=False,
)

BUILTIN_SKILLS: tuple[SkillDefinition, ...] = (CHECK_FIGURES, FIX_MATH, TRANSLATE)
BUILTIN_SKILL_NAMES = frozenset(skill.name for skill in BUILTIN_SKILLS)


class SkillRow(Protocol):
    """What a stored skill offers: a `skills` row or anything shaped like one."""

    name: str
    description: str
    argument_hint: str
    body: str
    user_invocable: bool
    model_invocable: bool


@dataclass(frozen=True)
class ResolvedSkill:
    """A skill as the user has it: a built-in, with or without their edit, or their own."""

    name: str
    description: str
    argument_hint: str
    body: str
    user_invocable: bool
    model_invocable: bool
    builtin: bool
    # A built-in the user's edit stands over.
    modified: bool
    # A built-in's shipped values; None for a skill of the user's own.
    default: SkillDefinition | None


def _resolved(
    source: SkillRow | SkillDefinition, *, builtin: bool, modified: bool, default
) -> ResolvedSkill:
    return ResolvedSkill(
        name=source.name,
        description=source.description,
        argument_hint=source.argument_hint,
        body=source.body,
        user_invocable=bool(source.user_invocable),
        model_invocable=bool(source.model_invocable),
        builtin=builtin,
        modified=modified,
        default=default,
    )


def resolve_skills(rows: Iterable[SkillRow]) -> list[ResolvedSkill]:
    """The built-ins in their code order, each as the user's edit when there is one, then
    the user's own by name (docs/api.md)."""
    by_name = {row.name: row for row in rows}
    resolved = []
    for builtin in BUILTIN_SKILLS:
        row = by_name.pop(builtin.name, None)
        resolved.append(
            _resolved(
                row if row is not None else builtin,
                builtin=True,
                modified=row is not None,
                default=builtin,
            )
        )
    for name in sorted(by_name):
        resolved.append(_resolved(by_name[name], builtin=False, modified=False, default=None))
    return resolved


def same_as_default(values: dict, default: SkillDefinition) -> bool:
    """Whether an edit says nothing the built-in does not: such an edit is not stored."""
    return all(values[name] == getattr(default, name) for name in SKILL_FIELDS)


def render_skill(body: str, arguments: str) -> str:
    """The body with `$ARGUMENTS` replaced — or, when it names none and the call passed
    some, followed by an `ARGUMENTS:` line, as Claude Code does (docs/chat.md § Skills)."""
    given = arguments.strip()
    if ARGUMENTS_PLACEHOLDER in body:
        return body.replace(ARGUMENTS_PLACEHOLDER, given)
    return f"{body}\n\nARGUMENTS: {given}" if given else body
