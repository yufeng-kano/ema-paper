"""Poppler wrappers, against real fixture documents.

Poppler is a real dependency of the test environment (docs/testing.md): these run actual
subprocesses, never mocks.
"""

from __future__ import annotations

import pytest
from PIL import Image

from sightread.parsing import poppler


async def test_pdf_info_reports_pages_and_dimensions(documents, tmp_path) -> None:
    info = await poppler.pdf_info(documents["mixed_pdf"], cwd=tmp_path)

    assert info.page_count == 3
    assert len(info.page_sizes) == 3
    assert (round(info.page_size(1).width_pt), round(info.page_size(1).height_pt)) == (612, 792)
    # Out-of-range pages fall back rather than raising.
    assert info.page_size(99) == info.page_size(1)


async def test_pdf_info_rejects_a_corrupt_file(documents, tmp_path) -> None:
    with pytest.raises(poppler.PopplerError):
        await poppler.pdf_info(documents["corrupt_pdf"], cwd=tmp_path)


async def test_render_page_stays_within_the_long_edge_budget(documents, tmp_path) -> None:
    info = await poppler.pdf_info(documents["text_pdf"], cwd=tmp_path)
    rendered = await poppler.render_page(documents["text_pdf"], 1, info.page_size(1), cwd=tmp_path)

    assert rendered.parent == tmp_path
    with Image.open(rendered) as image:
        assert max(image.size) <= poppler.MAX_RENDER_LONG_EDGE_PX
        assert max(image.size) > poppler.MAX_RENDER_LONG_EDGE_PX * 0.9


async def test_a_child_that_overruns_its_timeout_is_killed(documents, tmp_path) -> None:
    info = await poppler.pdf_info(documents["text_pdf"], cwd=tmp_path)
    with pytest.raises(poppler.PopplerTimeout):
        await poppler.render_page(
            documents["text_pdf"], 1, info.page_size(1), cwd=tmp_path, timeout=0.0005
        )


async def test_a_cancelled_render_kills_and_reaps_its_child(tmp_path, monkeypatch) -> None:
    import asyncio

    children = []
    started = asyncio.Event()
    real = asyncio.create_subprocess_exec

    async def spying(*args, **kwargs):
        process = await real(*args, **kwargs)
        children.append(process)
        started.set()
        return process

    monkeypatch.setattr(poppler.asyncio, "create_subprocess_exec", spying)
    running = asyncio.ensure_future(poppler._run(["sleep", "30"], cwd=tmp_path, timeout=60))
    await started.wait()
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    # Killed and reaped before the cancel went on: not left rendering outside the slot.
    assert children[0].returncode is not None


def test_render_dpi_scales_with_page_size() -> None:
    letter_dpi = poppler.render_dpi(poppler.PageSize(612, 792))
    poster_dpi = poppler.render_dpi(poppler.PageSize(1728, 2592))

    assert poster_dpi < letter_dpi <= poppler.MAX_RENDER_DPI
    assert 792 / 72 * letter_dpi <= poppler.MAX_RENDER_LONG_EDGE_PX
