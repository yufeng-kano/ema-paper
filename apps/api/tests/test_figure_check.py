"""The figure check: rules on every box, the page's own model on a page they flag, and a
status per figure (docs/parsing.md § Figure check). Pages are drawn here, so every box's
neighbourhood is known; every upstream call is stubbed (docs/testing.md § Cost safety)."""

from __future__ import annotations

import base64
import io
import json
import re
import uuid
from pathlib import Path

import httpx
import pytest
import respx
from httpx import AsyncClient
from PIL import Image, ImageDraw
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from sqlalchemy import select, update

from sightread.agents.bbox import DEFAULT, GPT
from sightread.agents.parse import FIGURE_CHECK_SINCE_PIPELINE
from sightread.db.models import Job, JobPage, UsageLog
from sightread.jobs.runner import partial_result_payload
from sightread.parsing.figure_check import (
    MARK,
    PageInk,
    check_prompt,
    lands_on_another,
    mark_boxes,
    move_boxes,
    read_verdicts,
)
from sightread.parsing.figures import CROP_MARGIN, crop_rect
from tests.conftest import KANO_CHAT_URL
from tests.test_parse_end_to_end import MODEL, _authorize, _completion, _drain_queue

# A chart's plotted area on a 1000 x 1000 page, and the box a model that boxes only that
# area draws around it: its crop runs from 292 to 708 on both axes.
PLOT = (300, 300, 700, 700)
BOX = (300, 300, 700, 700)


def _page(path: Path, *ink: tuple[int, int, int, int], background: int = 255) -> Path:
    """A 1000 x 1000 page: `background` everywhere, each rectangle of `ink` (left, top,
    right, bottom, inclusive) black."""
    image = Image.new("L", (1000, 1000), background)
    draw = ImageDraw.Draw(image)
    for rect in ink:
        draw.rectangle(rect, fill=0)
    image.save(path)
    return path


# --- the rules ------------------------------------------------------------------------------


def test_a_side_that_cuts_through_ink_moves_out_to_the_white_space_past_it(tmp_path) -> None:
    # The y-axis title runs from x 270 to 296, across the crop's left edge at 292.
    page = _page(tmp_path / "page.png", PLOT, (270, 450, 296, 550))

    boxes, cut = PageInk(page).push_out([BOX], [0])

    assert cut == {0}
    # Out to a third of a blank run past the title's last column (270 - 2), as a box: only
    # the side that cut moved.
    assert boxes == [(300, 276, 700, 700)]
    assert crop_rect(boxes[0], 1000, 1000) == (268, 292, 708, 708)


def test_a_box_whose_crop_cuts_nothing_is_left_alone(tmp_path) -> None:
    page = _page(tmp_path / "page.png", PLOT)

    assert PageInk(page).push_out([BOX], [0]) == ([BOX], set())


def test_a_side_on_the_page_edge_is_never_a_cut(tmp_path) -> None:
    # A figure printed to the top of the page: its crop starts at the page's first line.
    page = _page(tmp_path / "page.png", (300, 0, 700, 400))

    assert PageInk(page).push_out([(0, 300, 400, 700)], [0]) == ([(0, 300, 400, 700)], set())


def test_another_figure_inside_the_margin_is_not_a_cut(tmp_path) -> None:
    # Two figures 4 units apart: each crop's margin reaches into the other's box.
    page = _page(tmp_path / "page.png", (300, 200, 699, 399), (300, 404, 699, 599))
    boxes = [(200, 300, 400, 700), (404, 300, 600, 700)]

    assert PageInk(page).push_out(boxes, [0, 1]) == (boxes, set())


def test_ink_that_runs_into_another_box_stops_the_side_where_it_was(tmp_path) -> None:
    # A label under the first figure crosses its crop's bottom edge (408) and runs on to
    # the second figure's box: whose label it is, the rules cannot tell.
    page = _page(
        tmp_path / "page.png", (300, 200, 699, 399), (400, 405, 500, 415), (300, 416, 699, 599)
    )
    boxes = [(200, 300, 400, 700), (416, 300, 600, 700)]

    assert PageInk(page).push_out(boxes, [0]) == (boxes, {0})


def test_a_side_without_white_space_within_reach_stays_for_the_model(tmp_path) -> None:
    # Ink from x 200 to 296: more than the 40 lines a side may move on a 1000 px page.
    page = _page(tmp_path / "page.png", PLOT, (200, 450, 296, 550))

    assert PageInk(page).push_out([BOX], [0]) == ([BOX], {0})


def test_a_coloured_page_reads_its_own_colour_as_background(tmp_path) -> None:
    whole = _page(tmp_path / "whole.png", PLOT, background=120)
    cut = _page(tmp_path / "cut.png", PLOT, (270, 450, 296, 550), background=120)

    assert PageInk(whole).push_out([BOX], [0]) == ([BOX], set())
    assert PageInk(cut).push_out([BOX], [0]) == ([(300, 276, 700, 700)], {0})


# --- what the model is shown ----------------------------------------------------------------


def test_the_rectangles_are_drawn_outside_their_crops(tmp_path) -> None:
    page = _page(tmp_path / "page.png")
    marked = tmp_path / "marked.png"

    mark_boxes(page, [BOX], marked)

    with Image.open(marked) as image:
        # The outline runs just outside the box's crop, 292 to 708: nothing the crop takes
        # in is covered.
        assert image.getpixel((291, 500)) == MARK
        assert image.getpixel((708, 500)) == MARK
        assert image.getpixel((292, 500)) == (255, 255, 255)
        assert image.getpixel((707, 500)) == (255, 255, 255)
        # The number's tag sits above the top-left corner.
        assert image.getpixel((291, 285)) == MARK


def test_the_prompt_lists_every_crop_in_the_models_convention() -> None:
    # The second runs to the page's bottom edge, where its crop stops.
    boxes = [(100, 100, 500, 500), (600, 50, 1000, 950)]

    pixel = check_prompt(boxes, GPT, 2000, 1000)
    assert "1: [184, 92, 1016, 508]\n2: [84, 592, 1916, 1000]" in pixel
    assert "2000 pixels wide and 1000 pixels tall" in pixel

    normalized = check_prompt(boxes, DEFAULT, 2000, 1000)
    assert "1: [92, 92, 508, 508]\n2: [592, 42, 1000, 958]" in normalized
    assert "{boxes}" not in normalized and "{box_format}" not in normalized


# --- what its answer may change -------------------------------------------------------------


def test_verdicts_are_read_from_the_json_whatever_surrounds_it() -> None:
    answer = (
        '{"figures": [{"n": 1, "ok": true}, {"n": 2, "ok": false, "box": [100, 200, 300, 400]}]}'
    )

    for wrapped in (answer, f"```json\n{answer}\n```", f"Here is my check:\n{answer}\nDone."):
        # A corrected rectangle is a crop: the box is it less the crop's margin.
        assert read_verdicts(wrapped, 2, DEFAULT, 1000, 1000) == {0: None, 1: (108, 208, 292, 392)}


def test_a_pixel_rectangle_comes_back_as_a_box_in_the_stored_contract() -> None:
    answer = '{"figures": [{"n": 1, "ok": false, "box": [184, 92, 1016.4, 508]}]}'

    assert read_verdicts(answer, 1, GPT, 2000, 1000) == {0: (100, 100, 500, 500)}
    # A rectangle sent back as it was listed is the box it already has.
    for box in [(100, 100, 500, 500), (3, 0, 640, 997)]:
        [listed] = re.findall(r"^1: (\[.*\])$", check_prompt([box], GPT, 2000, 1000), re.M)
        answer = json.dumps({"figures": [{"n": 1, "ok": False, "box": json.loads(listed)}]})
        crop = crop_rect(read_verdicts(answer, 1, GPT, 2000, 1000)[0], 2000, 1000)
        assert crop == crop_rect(box, 2000, 1000)


def test_an_unusable_verdict_is_no_verdict() -> None:
    entries = [
        '{"n": 0, "ok": true}',  # no such rectangle
        '{"n": 9, "ok": true}',
        '{"n": true, "ok": true}',  # a boolean is not a number
        '{"n": 1, "ok": "yes"}',
        '{"n": 1, "ok": false}',  # wrong, but no box to put right
        '{"n": 1, "ok": false, "box": [1, 2, 3]}',
        '{"n": 1, "ok": false, "box": ["10", 20, 30, 40]}',
        '{"n": 1, "ok": false, "box": [1e400, 20, 30, 40]}',  # infinity
        '{"n": 1, "ok": false, "box": [' + "9" * 400 + ", 20, 30, 40]}",  # past a float
        '{"n": 1, "ok": false, "box": [500, 20, 100, 40]}',  # upside down once cleaned
        '{"n": 1, "ok": false, "box": [100, 100, 110, 500]}',  # thinner than two margins
    ]
    for entry in entries:
        assert read_verdicts(f'{{"figures": [{entry}]}}', 2, DEFAULT, 1000, 1000) == {}, entry
    # The first verdict on a rectangle stands.
    twice = '{"figures": [{"n": 1, "ok": true}, {"n": 1, "ok": false, "box": [1, 2, 3, 4]}]}'
    assert read_verdicts(twice, 1, DEFAULT, 1000, 1000) == {0: None}
    deep = '{"figures": ' + "[" * 100_000 + "]" * 100_000 + "}"
    for answer in ("", "It looks fine.", "[1, 2]", '{"figures": {"n": 1}}', deep):
        assert read_verdicts(answer, 1, DEFAULT, 1000, 1000) == {}


def test_a_corrected_box_never_takes_in_another_figure() -> None:
    other = (100, 100, 300, 300)

    assert lands_on_another((100, 100, 300, 300), [other])
    assert lands_on_another((50, 50, 250, 250), [other])  # more than half of it
    assert not lands_on_another((250, 100, 500, 300), [other])  # a quarter of it
    assert not lands_on_another((400, 400, 600, 600), [other])


def test_moving_a_box_changes_its_placeholders_and_nothing_else() -> None:
    markdown = (
        "Text before.\n\n"
        "![a chart](sightread://p3/300, 300, 700, 700)\n"
        "Figure 1: the chart\n\n"
        "![fig](sightread://p3/800,100,900,200)\n"
        "Text after."
    )

    moved = move_boxes(markdown, {(300, 300, 700, 700): (300, 276, 700, 700)})

    assert moved == markdown.replace(
        "sightread://p3/300, 300, 700, 700", "sightread://p3/300,276,700,700"
    )


# --- in a parse -----------------------------------------------------------------------------

# The y-axis title of the chart on PLOT, across the crop's left edge.
TITLE = (270, 450, 296, 550)
CHART = "## Results\n\n![fig](sightread://p1/300,300,700,700)\nFigure 1: a chart\n\nBody text."
OK = '{"figures": [{"n": 1, "ok": true}]}'


def _corrected(*box: int) -> str:
    return json.dumps({"figures": [{"n": 1, "ok": False, "box": list(box)}]})


@pytest.fixture
async def api_client(make_client, sessionmaker) -> AsyncClient:
    return await _authorize(make_client(), sessionmaker)


class Upstream:
    """Transcribes every page as `markdown`, and answers the figure check with `answers`
    in order: a JSON string, or a whole response."""

    def __init__(self, markdown: str, answers: tuple[str | httpx.Response, ...] = ()) -> None:
        self.markdown = markdown
        self.answers = list(answers)
        self.checks: list[dict] = []
        self.transcriptions = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["messages"][0]["content"][0]["text"].startswith("This page image has"):
            self.checks.append(body)
            answer = self.answers.pop(0)
            return answer if isinstance(answer, httpx.Response) else _completion(answer)
        self.transcriptions += 1
        return _completion(self.markdown)


def _sent(body: dict) -> tuple[str, Image.Image]:
    """The prompt and the image of one call."""
    text, image = body["messages"][0]["content"]
    encoded = image["image_url"]["url"].split(",", 1)[1]
    return text["text"], Image.open(io.BytesIO(base64.b64decode(encoded))).convert("RGB")


async def _parse_image(client: AsyncClient, sessionmaker, path: Path) -> str:
    accepted = await client.post(
        "/v1/parse",
        files={"file": (path.name, path.read_bytes(), "image/png")},
        data={"model": MODEL},
    )
    assert accepted.status_code == 202, accepted.text
    await _drain_queue(client, sessionmaker)
    return accepted.json()["job_id"]


async def _usage(sessionmaker, job_id: str) -> list[UsageLog]:
    async with sessionmaker() as db:
        return list(
            (await db.execute(select(UsageLog).where(UsageLog.job_id == uuid.UUID(job_id))))
            .scalars()
            .all()
        )


@respx.mock
async def test_a_box_the_rules_moved_is_confirmed_by_the_model(
    api_client, sessionmaker, tmp_path
) -> None:
    upstream = Upstream(CHART, (OK,))
    respx.post(KANO_CHAT_URL).mock(side_effect=upstream)

    job_id = await _parse_image(
        api_client, sessionmaker, _page(tmp_path / "chart.png", PLOT, TITLE)
    )

    result = (await api_client.get(f"/v1/jobs/{job_id}/result")).json()
    assert result["figures"] == [
        {
            "id": "fig1",
            "page": 1,
            "bbox": [300, 276, 700, 700],
            "caption": "Figure 1: a chart",
            "check": "adjusted",
        }
    ]
    # The box moved in the markdown too, and nothing else did.
    assert (
        "![fig1](sightread://p1/300,276,700,700)\nFigure 1: a chart\n\nBody text."
        in (result["markdown"])
    )
    assert result["meta"]["figure_check"] == {"verified": 0, "adjusted": 1, "unverified": 0}
    # The page's own model was shown the page with the moved box's crop drawn on it, clear
    # of the title, and the crop in its convention.
    [asked] = upstream.checks
    assert asked["model"] == MODEL
    prompt, image = _sent(asked)
    assert "1: [292, 268, 708, 708]" in prompt
    assert image.getpixel((267, 500)) == MARK
    assert image.getpixel((268, 500)) == (255, 255, 255)
    assert image.getpixel((270, 500)) == (0, 0, 0)
    # Both calls billed, and the crop is the checked box's.
    assert len(await _usage(sessionmaker, job_id)) == 2
    figures = Path(api_client.app.state.settings.figures_dir) / job_id
    assert [path.name for path in figures.iterdir()] == ["p1_300_276_700_700.png"]


@respx.mock
async def test_a_box_the_model_corrects_is_asked_about_again(
    api_client, sessionmaker, tmp_path
) -> None:
    upstream = Upstream(CHART, (_corrected(250, 250, 750, 750), OK))
    respx.post(KANO_CHAT_URL).mock(side_effect=upstream)

    job_id = await _parse_image(
        api_client, sessionmaker, _page(tmp_path / "chart.png", PLOT, TITLE)
    )

    [figure] = (await api_client.get(f"/v1/jobs/{job_id}/result")).json()["figures"]
    # The box whose crop is the rectangle the model drew.
    assert (figure["bbox"], figure["check"]) == ([258, 258, 742, 742], "adjusted")
    assert len(upstream.checks) == 2
    prompt, image = _sent(upstream.checks[1])
    assert "1: [250, 250, 750, 750]" in prompt
    assert image.getpixel((249, 500)) == MARK
    assert len(await _usage(sessionmaker, job_id)) == 3


@respx.mock
async def test_a_box_still_corrected_in_the_last_round_opens_unverified(
    api_client, sessionmaker, tmp_path
) -> None:
    upstream = Upstream(CHART, (_corrected(250, 250, 750, 750), _corrected(240, 240, 760, 760)))
    respx.post(KANO_CHAT_URL).mock(side_effect=upstream)

    job_id = await _parse_image(
        api_client, sessionmaker, _page(tmp_path / "chart.png", PLOT, TITLE)
    )

    assert (await api_client.get(f"/v1/jobs/{job_id}")).json()["status"] == "succeeded"
    result = (await api_client.get(f"/v1/jobs/{job_id}/result")).json()
    # It keeps the last box it was given; FIGURE_CHECK_ROUNDS calls and no more.
    assert (result["figures"][0]["bbox"], result["figures"][0]["check"]) == (
        [248, 248, 752, 752],
        "unverified",
    )
    assert result["meta"]["figure_check"] == {"verified": 0, "adjusted": 0, "unverified": 1}
    assert len(upstream.checks) == 2


@respx.mock
async def test_an_answer_that_is_no_verdict_is_not_asked_again(
    api_client, sessionmaker, tmp_path
) -> None:
    upstream = Upstream(CHART, ("The rectangle looks right to me.",))
    respx.post(KANO_CHAT_URL).mock(side_effect=upstream)

    job_id = await _parse_image(
        api_client, sessionmaker, _page(tmp_path / "chart.png", PLOT, TITLE)
    )

    [figure] = (await api_client.get(f"/v1/jobs/{job_id}/result")).json()["figures"]
    # The rules' box stays; nothing confirmed it.
    assert (figure["bbox"], figure["check"]) == ([300, 276, 700, 700], "unverified")
    assert len(upstream.checks) == 1


@respx.mock
async def test_a_page_whose_boxes_cut_nothing_asks_no_model(
    api_client, sessionmaker, tmp_path
) -> None:
    upstream = Upstream(CHART)
    respx.post(KANO_CHAT_URL).mock(side_effect=upstream)

    job_id = await _parse_image(api_client, sessionmaker, _page(tmp_path / "chart.png", PLOT))

    result = (await api_client.get(f"/v1/jobs/{job_id}/result")).json()
    assert (result["figures"][0]["bbox"], result["figures"][0]["check"]) == (
        [300, 300, 700, 700],
        "verified",
    )
    assert result["meta"]["figure_check"] == {"verified": 1, "adjusted": 0, "unverified": 0}
    assert upstream.checks == []
    assert len(await _usage(sessionmaker, job_id)) == 1


@respx.mock
async def test_a_check_call_that_fails_fails_the_page(api_client, sessionmaker, tmp_path) -> None:
    upstream = Upstream(CHART, (httpx.Response(400, json={"error": "bad request"}),))
    respx.post(KANO_CHAT_URL).mock(side_effect=upstream)

    job_id = await _parse_image(
        api_client, sessionmaker, _page(tmp_path / "chart.png", PLOT, TITLE)
    )

    status = (await api_client.get(f"/v1/jobs/{job_id}")).json()
    assert status["status"] == "failed"
    assert status["error"] == "1 of 1 pages failed (page 1: upstream call failed)"
    async with sessionmaker() as db:
        [row] = (
            await db.execute(select(JobPage).where(JobPage.job_id == uuid.UUID(job_id)))
        ).scalars()
    # Nothing of it is kept: a retry transcribes it again.
    assert (row.status, row.markdown, row.figures) == ("failed", None, None)
    # The transcription was billed before the check was sent.
    assert len(await _usage(sessionmaker, job_id)) == 1


@respx.mock
async def test_a_job_queued_before_the_check_runs_without_it(
    api_client, sessionmaker, tmp_path
) -> None:
    upstream = Upstream(CHART)
    respx.post(KANO_CHAT_URL).mock(side_effect=upstream)
    path = _page(tmp_path / "chart.png", PLOT, TITLE)
    accepted = await api_client.post(
        "/v1/parse",
        files={"file": (path.name, path.read_bytes(), "image/png")},
        data={"model": MODEL},
    )
    job_id = accepted.json()["job_id"]
    async with sessionmaker() as db:
        await db.execute(
            update(Job)
            .where(Job.id == uuid.UUID(job_id))
            .values(pipeline_version=FIGURE_CHECK_SINCE_PIPELINE - 1)
        )
        await db.commit()

    await _drain_queue(api_client, sessionmaker)

    result = (await api_client.get(f"/v1/jobs/{job_id}/result")).json()
    assert result["figures"][0]["bbox"] == [300, 300, 700, 700]
    assert "check" not in result["figures"][0]
    assert "figure_check" not in result["meta"]
    assert upstream.checks == []
    async with sessionmaker() as db:
        [row] = (
            await db.execute(select(JobPage).where(JobPage.job_id == uuid.UUID(job_id)))
        ).scalars()
    assert row.figures is None


@respx.mock
async def test_a_rendered_pdf_page_is_checked_too(api_client, sessionmaker, tmp_path) -> None:
    # The chart drawn on a letter page: the plot from 30% to 70% of each edge, and its
    # y-axis title across the box's crop on the left.
    path = tmp_path / "chart.pdf"
    pdf = canvas.Canvas(str(path), pagesize=letter)
    width, height = letter
    pdf.rect(0.3 * width, 0.3 * height, 0.4 * width, 0.4 * height, stroke=0, fill=1)
    pdf.rect(0.27 * width, 0.45 * height, 0.026 * width, 0.1 * height, stroke=0, fill=1)
    pdf.showPage()
    pdf.save()
    upstream = Upstream(CHART, (OK,))
    respx.post(KANO_CHAT_URL).mock(side_effect=upstream)

    accepted = await api_client.post(
        "/v1/parse",
        files={"file": (path.name, path.read_bytes(), "application/pdf")},
        data={"model": MODEL},
    )
    job_id = accepted.json()["job_id"]
    await _drain_queue(api_client, sessionmaker)

    [figure] = (await api_client.get(f"/v1/jobs/{job_id}/result")).json()["figures"]
    assert figure["check"] == "adjusted"
    # Only the side that cut the title moved, and its crop now starts left of the title.
    y_min, x_min, y_max, x_max = figure["bbox"]
    assert (y_min, y_max, x_max) == (300, 700, 700)
    assert x_min - CROP_MARGIN < 270 < x_min
    assert len(upstream.checks) == 1
    figures = Path(api_client.app.state.settings.figures_dir) / job_id
    assert (figures / f"p1_{y_min}_{x_min}_{y_max}_{x_max}.png").is_file()


@respx.mock
async def test_a_kept_page_keeps_its_statuses_and_is_not_checked_again(
    api_client, sessionmaker, documents
) -> None:
    """A requeued or retried attempt restores the pages it kept as they were checked
    (docs/jobs.md § Retrying failed pages); a page kept from before the check carries none."""
    # In the bottom margin, blank on every fixture page: nothing for the rules to find.
    margin = "![fig](sightread://p{page}/900,100,980,900)\nFigure: a margin"
    upstream = Upstream(margin.format(page=2))
    respx.post(KANO_CHAT_URL).mock(side_effect=upstream)
    accepted = await api_client.post(
        "/v1/parse",
        files={"file": ("mixed.pdf", documents["mixed_pdf"].read_bytes(), "application/pdf")},
        data={"model": MODEL},
    )
    job_id = uuid.UUID(accepted.json()["job_id"])
    async with sessionmaker() as db:
        db.add_all(
            [
                JobPage(
                    job_id=job_id,
                    page_no=page_no,
                    method="vision",
                    status="succeeded",
                    markdown=margin.format(page=page_no),
                    model=MODEL,
                    figures=figures,
                )
                for page_no, figures in ((1, {"900,100,980,900": "unverified"}), (3, None))
            ]
        )
        await db.execute(update(Job).where(Job.id == job_id).values(pages_done=2))
        await db.commit()

    await _drain_queue(api_client, sessionmaker)

    result = (await api_client.get(f"/v1/jobs/{job_id}/result")).json()
    assert [figure.get("check") for figure in result["figures"]] == [
        "unverified",
        "verified",
        None,
    ]
    assert result["meta"]["figure_check"] == {"verified": 1, "adjusted": 0, "unverified": 1}
    assert (upstream.transcriptions, upstream.checks) == (1, [])


def test_a_running_jobs_snapshot_carries_its_finished_pages_statuses() -> None:
    job = Job(id=uuid.uuid4(), agent="parse", model=MODEL, bbox_format="yxyx_norm1000")
    rows = [
        JobPage(
            page_no=1,
            method="vision",
            status="succeeded",
            markdown="![fig](sightread://p1/10,10,20,20)\n",
            model=MODEL,
            figures={"10,10,20,20": "adjusted"},
        )
    ]

    assert partial_result_payload(job, rows)["figures"][0]["check"] == "adjusted"
