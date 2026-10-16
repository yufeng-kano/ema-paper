"""Figure crop persistence (docs/parsing.md § Figure crops). Pillow only, no services."""

from __future__ import annotations

import uuid

from PIL import Image

from sightread.parsing.figures import (
    CROP_MARGIN,
    crop_path,
    discard_job_figures,
    parse_bbox_path,
    save_page_figures,
)


def _page_image(path, size=(1000, 500), color=(200, 10, 10)):
    Image.new("RGB", size, color).save(path, format="PNG")
    return path


def test_placeholders_are_cropped_under_our_page_number(tmp_path) -> None:
    image = _page_image(tmp_path / "page.png")
    markdown = (
        "## Heading\n\n"
        "![fig](sightread://p9/100,100,500,600)\nFigure 1: chart\n\n"
        "![fig](sightread://p9/700,0,900,1000)\n"
    )

    saved = save_page_figures(markdown, image, 3, tmp_path / "job")

    assert saved == 2
    # Named by OUR page number and the cleaned bbox — what the placeholder will carry.
    first = tmp_path / "job" / "p3_100_100_500_600.png"
    assert first.is_file()
    assert (tmp_path / "job" / "p3_700_0_900_1000.png").is_file()

    with Image.open(first) as crop:
        # bbox space is 0-1000 of each page edge; the crop carries a margin per side,
        # clamped to the page. Width: (600-100+2*margin)/1000 of 1000px.
        assert crop.size[0] == (500 + 2 * CROP_MARGIN)
        # Height: (500-100+2*margin)/1000 of 500px.
        assert crop.size[1] == round((400 + 2 * CROP_MARGIN) / 1000 * 500)


def test_degenerate_and_absent_boxes_save_nothing(tmp_path) -> None:
    image = _page_image(tmp_path / "page.png")
    assert save_page_figures("no figures here", image, 1, tmp_path / "job") == 0
    degenerate = "![fig](sightread://p1/500,500,100,100)"
    assert save_page_figures(degenerate, image, 1, tmp_path / "job") == 0
    assert not (tmp_path / "job").exists()


def test_a_missing_image_is_swallowed(tmp_path) -> None:
    saved = save_page_figures(
        "![fig](sightread://p1/0,0,500,500)", tmp_path / "gone.png", 1, tmp_path / "job"
    )
    assert saved == 0


def test_bbox_paths_parse_and_clean() -> None:
    assert parse_bbox_path("100,200,300,400") == (100, 200, 300, 400)
    # Clamped into 0-1000 like every stored bbox.
    assert parse_bbox_path("-5,0,2000,1000") == (0, 0, 1000, 1000)
    assert parse_bbox_path("500,500,100,100") is None  # degenerate after cleaning
    assert parse_bbox_path("1,2,3") is None
    assert parse_bbox_path("a,b,c,d") is None
    assert parse_bbox_path("1,2,3,4,5") is None


def test_crop_path_is_deterministic(tmp_path) -> None:
    job_id = uuid.uuid4()
    path = crop_path(tmp_path, job_id, 2, (10, 20, 30, 40))
    assert path == tmp_path / str(job_id) / "p2_10_20_30_40.png"


def test_discard_removes_only_that_jobs_crops(tmp_path) -> None:
    doomed, kept = uuid.uuid4(), uuid.uuid4()
    for job_id in (doomed, kept):
        (tmp_path / str(job_id)).mkdir()
        (tmp_path / str(job_id) / "p1_0_0_10_10.png").write_bytes(b"x")

    discard_job_figures(tmp_path, doomed)

    assert not (tmp_path / str(doomed)).exists()
    assert (tmp_path / str(kept) / "p1_0_0_10_10.png").is_file()
    # A job with no crops is a no-op, not an error.
    discard_job_figures(tmp_path, uuid.uuid4())


def test_the_per_page_cap_keeps_the_first_boxes_in_reading_order(tmp_path) -> None:
    image = _page_image(tmp_path / "page.png")
    markdown = "\n".join(f"![fig](sightread://p1/{y},0,{y + 50},100)" for y in range(0, 500, 50))

    saved = save_page_figures(markdown, image, 1, tmp_path / "job", limit=3)

    assert saved == 3
    assert (tmp_path / "job" / "p1_0_0_50_100.png").is_file()
    assert (tmp_path / "job" / "p1_100_0_150_100.png").is_file()
    assert not (tmp_path / "job" / "p1_150_0_200_100.png").exists()


def test_a_running_jobs_crop_is_found_in_its_attempt_directory(tmp_path) -> None:
    from sightread.parsing.figures import find_crop

    job_id = uuid.uuid4()
    bbox = (10, 20, 30, 40)
    assert find_crop(tmp_path, job_id, 1, bbox) is None
    attempt = tmp_path / f"{job_id}.abc123"
    attempt.mkdir()
    (attempt / "p1_10_20_30_40.png").write_bytes(b"attempt")
    assert find_crop(tmp_path, job_id, 1, bbox) == attempt / "p1_10_20_30_40.png"
    # Published, the job's own directory answers first.
    (tmp_path / str(job_id)).mkdir()
    (tmp_path / str(job_id) / "p1_10_20_30_40.png").write_bytes(b"published")
    assert find_crop(tmp_path, job_id, 1, bbox) == crop_path(tmp_path, job_id, 1, bbox)


def test_a_crop_published_under_the_open_is_opened_where_it_landed(tmp_path, monkeypatch) -> None:
    """The publication can rename the attempt directory between the lookup and the open:
    the open is tried again where the crop landed, and the route streams from that
    descriptor rather than from a path the rename may have moved (docs/parsing.md
    § Figure crops)."""
    from sightread.parsing import figures

    job_id = uuid.uuid4()
    bbox = (10, 20, 30, 40)
    attempt = tmp_path / f"{job_id}.abc123"
    attempt.mkdir()
    (attempt / "p1_10_20_30_40.png").write_bytes(b"crop")
    real = figures.find_crop
    looks = 0

    def racing(figures_dir, job, page, box):
        nonlocal looks
        looks += 1
        path = real(figures_dir, job, page, box)
        if looks == 1:
            # The result commits between the look and the read.
            figures.publish_job_figures(figures_dir, job, attempt)
        return path

    monkeypatch.setattr(figures, "find_crop", racing)
    handle = figures.open_crop(tmp_path, job_id, 1, bbox)
    assert handle is not None and looks == 2
    assert b"".join(figures.crop_chunks(handle)) == b"crop"
    assert handle.closed
    assert figures.open_crop(tmp_path, uuid.uuid4(), 1, bbox) is None
