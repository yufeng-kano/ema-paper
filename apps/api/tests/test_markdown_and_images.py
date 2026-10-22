"""Placeholder emission, bbox hygiene and image normalization."""

from __future__ import annotations

import pytest
from PIL import Image

from sightread.parsing.images import MAX_LONG_EDGE_PX, ImageError, normalize_image, probe_image
from sightread.parsing.markdown import PageMarkdown, assemble, clean_bbox


def test_clean_bbox_clamps_and_rejects() -> None:
    assert clean_bbox((10, 20, 30, 40)) == (10, 20, 30, 40)
    assert clean_bbox((-5, 20, 3000, 40)) == (0, 20, 1000, 40)
    assert clean_bbox((30, 20, 30, 40)) is None
    assert clean_bbox((10, 40, 30, 20)) is None


def test_vision_placeholders_are_renumbered_and_captions_captured() -> None:
    document = assemble(
        [
            PageMarkdown(
                page=1,
                markdown=(
                    "# Title\n\n"
                    "![fig7](sightread://p9/100,60,480,940)\n"
                    "Figure 1: the first chart\n\n"
                    "Some prose."
                ),
            ),
            PageMarkdown(
                page=2,
                markdown="![fig1](sightread://p2/10,10,20,20)\nFigure 2: the second chart",
            ),
        ]
    )

    assert "![fig1](sightread://p1/100,60,480,940)" in document.markdown
    assert "![fig2](sightread://p2/10,10,20,20)" in document.markdown
    assert document.figures[0] == {
        "id": "fig1",
        "page": 1,
        "bbox": [100, 60, 480, 940],
        "caption": "Figure 1: the first chart",
    }
    assert document.figures[1]["caption"] == "Figure 2: the second chart"
    assert document.dropped_figures == 0
    # The caption stays verbatim on the line after the placeholder.
    lines = document.markdown.splitlines()
    index = lines.index("![fig1](sightread://p1/100,60,480,940)")
    assert lines[index + 1] == "Figure 1: the first chart"


def test_a_figure_without_a_caption_claims_nothing_after_it() -> None:
    # The prompt asks for an empty line after a figure with no caption; the paragraph, the
    # table or the figure that follows is the next block, as the result viewer reads it.
    document = assemble(
        [
            PageMarkdown(
                page=1,
                markdown=(
                    "![fig](sightread://p1/10,10,200,500)\n\n"
                    "amount of training. We have performed learning rate sweeps.\n\n"
                    "![fig](sightread://p1/300,10,400,500)\n"
                    "| | Random | DINO |\n"
                    "|---|---|---|\n"
                    "| ViT-S/16 | 22.0 | 45.9 |\n\n"
                    "![fig](sightread://p1/500,10,600,500)\n"
                    "![fig](sightread://p1/650, 10, 680, 500)\n"
                    "![fig](sightread://p1/700,10,800,500)\n"
                    "Figure 5: the last one has a caption"
                ),
            )
        ]
    )

    assert [figure["caption"] for figure in document.figures] == [
        "",
        "",
        "",
        "",
        "Figure 5: the last one has a caption",
    ]


def test_malformed_placeholders_are_dropped_and_counted() -> None:
    document = assemble(
        [
            PageMarkdown(
                page=1,
                markdown=(
                    "Intro\n\n"
                    "![fig1](sightread://p1/500,10,100,900)\n"
                    "A backwards box\n\n"
                    "![fig2](sightread://p1/0,0,10,10)\n"
                    "A usable box"
                ),
            )
        ]
    )

    assert document.dropped_figures == 1
    assert [figure["id"] for figure in document.figures] == ["fig1"]
    assert "500,10,100,900" not in document.markdown
    assert "![fig1](sightread://p1/0,0,10,10)" in document.markdown


def test_out_of_range_boxes_are_clamped_not_dropped() -> None:
    document = assemble(
        [PageMarkdown(page=3, markdown="![fig](sightread://p3/-40,0,1200,1500)\nCaption")]
    )
    assert document.figures[0]["bbox"] == [0, 0, 1000, 1000]


def test_every_page_gets_a_marker_and_empty_pages_get_none() -> None:
    document = assemble(
        [
            PageMarkdown(page=1, markdown="First page."),
            PageMarkdown(page=2, markdown=""),
            PageMarkdown(page=3, markdown="Third page."),
        ]
    )

    assert document.markdown == "<!-- page: 1 -->\nFirst page.\n\n<!-- page: 3 -->\nThird page."


def test_image_normalization_downscales_and_keeps_original_dimensions(documents, tmp_path) -> None:
    normalized = normalize_image(documents["wide_png"], tmp_path)

    assert (normalized.width_px, normalized.height_px) == (3000, 1000)
    assert normalized.media_type == "image/png"
    with Image.open(normalized.path) as image:
        assert max(image.size) == MAX_LONG_EDGE_PX


@pytest.mark.parametrize("name", ["jpg", "png", "webp", "heic"])
def test_every_accepted_image_type_normalizes(documents, tmp_path, name: str) -> None:
    if name not in documents:
        pytest.skip("pillow-heif on this platform cannot write the fixture")
    out_dir = tmp_path / name
    out_dir.mkdir()
    normalized = normalize_image(documents[name], out_dir)

    assert normalized.path.exists()
    assert normalized.media_type in ("image/jpeg", "image/png")


def test_exif_orientation_is_applied(documents, tmp_path) -> None:
    # The stored bitmap is 400x200 but tagged "rotate 90", so it is a 200x400 page.
    assert probe_image(documents["rotated_jpg"]) == (200, 400)

    normalized = normalize_image(documents["rotated_jpg"], tmp_path)
    with Image.open(normalized.path) as image:
        assert image.size == (200, 400)


def test_unreadable_image_is_reported(documents, tmp_path) -> None:
    with pytest.raises(ImageError):
        probe_image(documents["corrupt_pdf"])
    with pytest.raises(ImageError):
        normalize_image(documents["corrupt_pdf"], tmp_path)
