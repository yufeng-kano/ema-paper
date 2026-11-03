"""The editor's `view` of an image answers the image (docs/chat.md § Tools): decoded by the
inserted-image decoders only, shrunk to the attachment long edge, refused past the pixel
cap with the commands that read it in pieces, counted against the image budgets."""

from __future__ import annotations

import asyncio
import base64
import io
import os
import re
import shlex
import shutil
import subprocess
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
import respx
from PIL import Image, ImageFile

from sightread.jobs import chat_runner, chat_tools
from sightread.workspace.layout import user_root
from sightread.workspace.locks import workspace_lock
from tests.conftest import CSRF_HEADERS
from tests.test_chat import _run_queued_turn
from tests.test_chat_extensions import (
    chatting_client,
    new_conversation,
    one_call,
    send,
    stub_messages,
)

# The crop the reader's screenshot showed failing as "not a text file".
CROP = "files/DL/DIO/DINO Paper Presentation.pdf/figures/p11_330_115_785_880.png"
# What every command does to the image read from stdin before its own step.
FLAT = "+repage -background white -alpha remove -alpha off -strip"


def _encoded(image: Image.Image, fmt: str, **save) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format=fmt, **save)
    return buffer.getvalue()


def _images(*, max_bytes: int = 32 << 20, shell: bool = True) -> chat_tools.ImageView:
    return chat_tools.ImageView(
        max_bytes=max_bytes,
        max_pixels=40_000_000,
        long_edge=1568,
        decode_slot=asyncio.Semaphore(1),
        shell=shell,
    )


async def _view(
    root: Path,
    path: str,
    images: chat_tools.ImageView | None,
    *,
    max_bytes: int = 1_000_000,
    view_range: list[int] | None = None,
) -> chat_tools.ToolOutcome:
    tool_input: dict = {"command": "view", "path": f"/workspace/{path}"}
    if view_range is not None:
        tool_input["view_range"] = view_range
    return await chat_tools.run_editor(
        root, tool_input, "agent:1", max_chars=10_000, max_bytes=max_bytes, images=images
    )


def _place(root: Path, path: str, data: bytes) -> None:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def _shown(outcome: chat_tools.ToolOutcome) -> tuple[str, Image.Image, str]:
    """The image block's media type, the image it carries, and the line beside it."""
    assert not outcome.is_error, outcome.content
    assert isinstance(outcome.content, list)
    block, line = outcome.content
    assert block["type"] == "image" and block["source"]["type"] == "base64"
    image = Image.open(io.BytesIO(base64.b64decode(block["source"]["data"])))
    image.load()
    return block["source"]["media_type"], image, line["text"]


async def test_a_view_of_an_image_answers_the_image_whatever_its_name(tmp_path) -> None:
    root = tmp_path / "ws" / "1"
    crop = Image.new("RGBA", (200, 100), (255, 0, 0, 0))
    _place(root, CROP, _encoded(crop, "PNG"))
    # Formats by their bytes, never by the name: a PNG named .jpg stays a PNG, a photo
    # with no extension is still a photo.
    _place(root, "files/misnamed.jpg", _encoded(Image.new("RGB", (30, 20), "blue"), "PNG"))
    _place(root, ".cache/photo", _encoded(Image.new("RGB", (40, 30), "green"), "JPEG"))
    _place(root, "files/a.webp", _encoded(Image.new("RGB", (16, 16), "white"), "WEBP"))
    frames = [Image.new("P", (12, 8), 1), Image.new("P", (12, 8), 2)]
    gif = io.BytesIO()
    frames[0].save(gif, format="GIF", save_all=True, append_images=frames[1:])
    _place(root, "files/moving.gif", gif.getvalue())
    _place(root, "files/phone.heic", _encoded(Image.new("RGB", (64, 32), "gray"), "HEIF"))

    media, image, line = _shown(await _view(root, CROP, _images()))
    assert media == "image/png" and image.size == (200, 100)
    # Transparency is flattened onto white, as a page is.
    assert image.convert("RGB").getpixel((0, 0)) == (255, 255, 255)
    assert line == f"The image /workspace/{CROP}, 200x100 pixels."

    expected = {
        "files/misnamed.jpg": ("image/png", (30, 20)),
        ".cache/photo": ("image/jpeg", (40, 30)),
        "files/a.webp": ("image/jpeg", (16, 16)),
        "files/moving.gif": ("image/jpeg", (12, 8)),
        "files/phone.heic": ("image/jpeg", (64, 32)),
    }
    for path, (media_type, size) in expected.items():
        media, image, line = _shown(await _view(root, path, _images()))
        assert (media, image.size) == (media_type, size), path
        assert line.startswith(f"The image /workspace/{path}, "), line


async def test_an_image_is_shown_upright(tmp_path) -> None:
    root = tmp_path / "ws" / "1"
    photo = Image.new("RGB", (40, 20), "red")
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90° clockwise to display
    _place(root, "files/turned.jpg", _encoded(photo, "JPEG", exif=exif))
    _media, image, line = _shown(await _view(root, "files/turned.jpg", _images()))
    assert image.size == (20, 40)
    assert "20x40 pixels" in line
    # Turned once: the bytes sent carry no orientation that would turn them again, nor
    # does the PNG a page preview gets from the same photo.
    assert image.getexif().get(0x0112) is None
    page = chat_tools._model_image(_encoded(photo, "JPEG", exif=exif), 40_000_000)
    assert Image.open(io.BytesIO(page.data)).getexif().get(0x0112) is None


async def test_a_malformed_exif_block_shows_the_image_rather_than_failing(tmp_path) -> None:
    root = tmp_path / "ws" / "1"
    exif = Image.Exif()
    exif[0x0112] = 6
    exif[0x010F] = "Camera"  # Make, ASCII
    data = _encoded(Image.new("RGB", (40, 20), "red"), "JPEG", exif=exif)
    # Relabel Make as BitsPerSample, a SHORT tag: Pillow reads the file, and its own
    # `exif_transpose` raised rewriting the block.
    for make, bits in (
        (b"\x01\x0f\x00\x02", b"\x01\x02\x00\x02"),
        (b"\x0f\x01\x02\x00", b"\x02\x01\x02\x00"),
    ):
        data = data.replace(make, bits)
    _place(root, "files/odd.jpg", data)
    _media, image, _line = _shown(await _view(root, "files/odd.jpg", _images()))
    assert image.size in ((20, 40), (40, 20))


async def test_sixteen_bit_grey_is_not_clipped_to_white(tmp_path) -> None:
    root = tmp_path / "ws" / "1"
    grey = Image.new("I;16", (20, 10), 1000)
    grey.paste(Image.new("I;16", (10, 10), 30000), (10, 0))
    _place(root, "files/depth.png", _encoded(grey, "PNG"))
    _media, image, _line = _shown(await _view(root, "files/depth.png", _images()))
    rgb = image.convert("RGB")
    assert rgb.getpixel((2, 2)) == (3, 3, 3)
    assert rgb.getpixel((15, 2)) == (117, 117, 117)


async def test_a_large_image_is_shrunk_and_says_how_to_read_it_in_pieces(tmp_path) -> None:
    root = tmp_path / "ws" / "1"
    path = "files/Scans/board (1).png"
    _place(root, path, _encoded(Image.new("RGB", (4000, 1600), "white"), "PNG"))

    media, image, line = _shown(await _view(root, path, _images()))
    assert media == "image/png" and image.size == (1568, 627)
    assert "4000x1600 pixels, shown shrunk to 1568x627" in line
    # Pieces of the long edge are shown at full size: three across, two down. The file
    # comes in on stdin, its name never an argument ImageMagick would parse.
    quoted = shlex.quote(f"/workspace/{path}")
    assert (
        f"`convert '-[0]' {FLAT} -crop 1568x1568 +repage /workspace/.cache/piece-%d.png "
        f"< {quoted}`" in line
    )
    assert "(6 pieces)" in line
    assert (
        f"`convert '-[0]' {FLAT} -crop WxH+X+Y +repage /workspace/.cache/region.png "
        f"< {quoted}`" in line
    )

    # An animation's pieces are of its first frame; a GIF's, like a photo's, are JPEGs.
    frames = [Image.new("P", (4000, 1600), 1), Image.new("P", (4000, 1600), 2)]
    gif = io.BytesIO()
    frames[0].save(gif, format="GIF", save_all=True, append_images=frames[1:])
    _place(root, "files/anim.gif", gif.getvalue())
    _media, _image, line = _shown(await _view(root, "files/anim.gif", _images()))
    assert (
        f"`convert '-[0]' {FLAT} -crop 1568x1568 +repage /workspace/.cache/piece-%d.jpg "
        "< /workspace/files/anim.gif`" in line
    )

    # Without a shell the commands are not offered: the turn could not run them.
    _media, _image, line = _shown(await _view(root, path, _images(shell=False)))
    assert "shown shrunk to 1568x627" in line and "convert" not in line


async def test_an_image_past_the_pixel_cap_says_how_to_look_at_it_in_steps(tmp_path) -> None:
    root = tmp_path / "ws" / "1"
    # A 1-bit image is a few kilobytes on disk and 49 million pixels decoded.
    _place(root, "files/huge.png", _encoded(Image.new("1", (7000, 7000)), "PNG"))

    refused = await _view(root, "files/huge.png", _images())
    assert refused.is_error and isinstance(refused.content, str)
    assert "is 7000x7000 pixels, more than this tool opens (40,000,000)" in refused.content
    assert "-resize 1568x1568 /workspace/.cache/small.jpg" in refused.content
    assert "(25 pieces)" in refused.content

    plain = await _view(root, "files/huge.png", _images(shell=False))
    assert plain.is_error and "convert" not in plain.content

    # A portrait photo stored on its side is stated upright, and cut upright.
    exif = Image.Exif()
    exif[0x0112] = 6
    _place(root, "files/portrait.jpg", _encoded(Image.new("RGB", (8000, 6000)), "JPEG", exif=exif))
    turned = await _view(root, "files/portrait.jpg", _images())
    assert "is 6000x8000 pixels" in turned.content
    assert (
        f"`convert '-[0]' -rotate 90 {FLAT} -resize 1568x1568 /workspace/.cache/small.jpg "
        "< /workspace/files/portrait.jpg`" in turned.content
    )
    assert "(24 pieces)" in turned.content and "piece-%d.jpg" in turned.content


async def test_an_image_past_the_pixel_cap_is_refused_before_it_is_decoded(
    tmp_path, monkeypatch
) -> None:
    """A PNG's own EXIF read decodes the raster to look for a chunk after it: past the cap
    nothing may decode, so its size comes from the header alone."""
    root = tmp_path / "ws" / "1"
    _place(root, "files/huge.png", _encoded(Image.new("1", (7000, 7000)), "PNG"))
    decoded: list[tuple[int, int]] = []
    load = ImageFile.ImageFile.load

    def recording_load(image):
        decoded.append(image.size)
        return load(image)

    monkeypatch.setattr(ImageFile.ImageFile, "load", recording_load)
    refused = await _view(root, "files/huge.png", _images())
    assert refused.is_error and "is 7000x7000 pixels" in refused.content
    assert decoded == []
    # Within the cap the same read finds the image and decodes it once.
    _place(root, "files/fits.png", _encoded(Image.new("1", (700, 700)), "PNG"))
    _shown(await _view(root, "files/fits.png", _images()))
    assert (700, 700) in decoded


async def test_pieces_without_a_header_are_named_by_what_is_known(tmp_path, monkeypatch) -> None:
    """Where no header was read, the pieces are PNGs only for a PNG: by its bytes past
    Pillow's own bomb check, by its name past the byte cap."""
    root = tmp_path / "ws" / "1"
    plot_data = _encoded(Image.new("RGB", (100, 100)), "PNG")
    photo_data = _encoded(Image.new("RGB", (100, 100)), "JPEG")
    _place(root, "files/plot.png", plot_data)
    _place(root, "files/photo", photo_data)
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1000)
    plot = await _view(root, "files/plot.png", _images())
    assert "is too many pixels" in plot.content and "-auto-orient" in plot.content
    assert "piece-%d.png" in plot.content
    photo = await _view(root, "files/photo", _images())
    assert "is too many pixels" in photo.content and "piece-%d.jpg" in photo.content
    monkeypatch.undo()

    cap = min(len(plot_data), len(photo_data)) // 2
    tight = _images(max_bytes=cap)
    by_name = await _view(root, "files/plot.png", tight, max_bytes=cap)
    assert "is larger than this tool reads" in by_name.content
    assert "piece-%d.png" in by_name.content
    unnamed = await _view(root, "files/photo", tight, max_bytes=cap)
    assert "piece-%d.jpg" in unnamed.content


async def test_a_transparent_key_colour_is_shown_on_white(tmp_path) -> None:
    """A grey or RGB PNG keeps its transparency as one key colour (tRNS), not a channel."""
    root = tmp_path / "ws" / "1"
    for mode, key, ink in (("L", 0, 128), ("RGB", (0, 0, 0), (128, 0, 0))):
        keyed = Image.new(mode, (10, 10), key)
        keyed.putpixel((0, 0), ink)
        _place(root, f"files/{mode}.png", _encoded(keyed, "PNG", transparency=key))
        _media, image, _line = _shown(await _view(root, f"files/{mode}.png", _images()))
        rgb = image.convert("RGB")
        assert rgb.getpixel((5, 5)) == (255, 255, 255), mode
        assert rgb.getpixel((0, 0)) != (255, 255, 255), mode


async def _run_in_shell(command: str, root: Path) -> None:
    """A command the note gave, run as the VM's bash would, /workspace being `root`."""
    argv = ["/bin/bash", "-c", command.replace("/workspace/", f"{root}/")]
    await asyncio.to_thread(subprocess.run, argv, check=True, cwd=root)  # noqa: S603


@pytest.mark.skipif(shutil.which("convert") is None, reason="ImageMagick is not installed here")
async def test_the_pieces_are_cut_from_the_image_the_tool_showed(tmp_path) -> None:
    """The commands run as given (the VM's ImageMagick 6) cut the stated pieces, upright as
    the tool showed the image for every EXIF orientation — a PNG's and a WebP's too, which
    ImageMagick 6 does not read, and a transposed one, whose canvas it leaves stale — from
    the named file alone, whatever its name holds, flattened onto white as the tool shows
    it, and a piece viewed is not turned a second time."""
    root = tmp_path / "ws" / "1"
    cache = root / ".cache"
    cache.mkdir(parents=True)
    small = chat_tools.ImageView(  # noqa: S604 - shell means the agent has bash
        max_bytes=1 << 25,
        max_pixels=40_000_000,
        long_edge=100,
        decode_slot=asyncio.Semaphore(1),
        shell=True,
    )
    upright = Image.new("RGB", (150, 300), (0, 0, 255))
    upright.paste((255, 0, 0), (0, 0, 75, 150))  # the top-left quarter is red
    stored_as = {1: None, **{o: turn for o, (turn, _option) in chat_tools.ORIENTATIONS.items()}}

    async def cut(name: str) -> tuple[str, list[Image.Image]]:
        for old in cache.iterdir():
            old.unlink()
        _media, _image, line = _shown(await _view(root, f"files/{name}", small))
        await _run_in_shell(re.search(r"Every piece[^`]*`([^`]*)`", line).group(1), root)
        named = sorted(cache.iterdir(), key=lambda p: int(re.search(r"(\d+)\.", p.name)[1]))
        return line, [Image.open(piece).convert("RGB") for piece in named]

    for orientation, turn in stored_as.items():
        # The raster as a camera would store it: turned the other way, to be turned back.
        stored = upright
        if turn is not None:
            stored = upright.transpose(
                {
                    Image.Transpose.ROTATE_90: Image.Transpose.ROTATE_270,
                    Image.Transpose.ROTATE_270: Image.Transpose.ROTATE_90,
                }.get(turn, turn)
            )
        exif = Image.Exif()
        exif[0x0112] = orientation
        for fmt, suffix in (("JPEG", "jpg"), ("PNG", "png"), ("WEBP", "webp")):
            name = f"scan *[1] %d {orientation}.{suffix}"
            data = _encoded(stored, fmt, exif=exif)
            _place(root, f"files/{name}", data)
            # What a glob or a frame range in the name would also have picked up.
            _place(root, f"files/scan X1 0 {orientation}.{suffix}", data)
            line, pieces = await cut(name)
            label = f"{fmt} orientation {orientation}"
            assert "150x300 pixels" in line and "(6 pieces)" in line, label
            assert [piece.size for piece in pieces] == [(100, 100), (50, 100)] * 3, label
            assert pieces[0].getpixel((50, 50))[0] > 200, label
            assert pieces[0].getpixel((50, 50))[2] < 60, label
    # The last cut was a WebP's, into JPEGs: one viewed is shown as cut, not turned again.
    _media, piece, _line = _shown(await _view(root, ".cache/piece-0.jpg", small))
    assert piece.size == (100, 100) and piece.convert("RGB").getpixel((50, 50))[0] > 200

    # Transparent: white where the tool shows white, the ink kept.
    clear = Image.new("RGBA", (200, 100), (0, 0, 0, 0))
    clear.paste((0, 0, 0, 255), (0, 0, 20, 20))
    _place(root, "files/clear.webp", _encoded(clear, "WEBP", lossless=True))
    _line, pieces = await cut("clear.webp")
    assert pieces[0].getpixel((60, 60)) == (255, 255, 255)
    assert max(pieces[0].getpixel((5, 5))) < 40

    # A PNG ImageMagick cropped earlier keeps an offset on a larger canvas.
    whole = root / "whole.png"
    upright.resize((300, 300)).save(whole)
    await asyncio.to_thread(
        subprocess.run,  # noqa: S603 - fixed argv
        ["convert", str(whole), "-crop", "150x100+50+50", str(root / "files/offset.png")],
        check=True,
    )
    line, pieces = await cut("offset.png")
    assert "(2 pieces)" in line
    assert [piece.size for piece in pieces] == [(100, 100), (50, 100)]


async def test_the_image_cap_is_not_the_text_cap(tmp_path) -> None:
    root = tmp_path / "ws" / "1"
    noise = Image.frombytes("RGB", (120, 120), os.urandom(120 * 120 * 3))
    data = _encoded(noise, "PNG")
    _place(root, "files/noise.png", data)
    _place(root, "files/big.txt", b"a" * (len(data) + 10))
    text_cap = len(data) // 2

    # Past the text cap, within the image one: the image is shown.
    media, _image, _line = _shown(
        await _view(root, "files/noise.png", _images(), max_bytes=text_cap)
    )
    assert media == "image/png"
    # A text file that size is still too large to view.
    big = await _view(root, "files/big.txt", _images(), max_bytes=text_cap)
    assert big.is_error and big.content == "/workspace/files/big.txt is too large to view; use bash"
    # Past the image cap too: refused before anything is decoded, with the way round it.
    over = await _view(root, "files/noise.png", _images(max_bytes=text_cap), max_bytes=text_cap)
    assert over.is_error and "is larger than this tool reads" in over.content
    assert "-resize 1568x1568" in over.content


async def test_other_binaries_are_still_refused(tmp_path) -> None:
    root = tmp_path / "ws" / "1"
    _place(root, "files/scan.bmp", _encoded(Image.new("RGB", (8, 8)), "BMP"))
    _place(root, "files/scan.tiff", _encoded(Image.new("RGB", (8, 8)), "TIFF"))
    _place(root, "files/blob.bin", bytes(range(256)) * 4)
    _place(root, "files/logo.svg", b'<svg xmlns="http://www.w3.org/2000/svg"></svg>\n')
    whole = _encoded(Image.new("RGB", (64, 64), "red"), "PNG")
    _place(root, "files/cut.png", whole[: len(whole) // 2])

    for path in ("files/scan.bmp", "files/scan.tiff", "files/blob.bin"):
        refused = await _view(root, path, _images())
        assert refused.is_error, path
        assert refused.content == (
            f"/workspace/{path} is not a text file or an image this tool shows "
            "(PNG, JPEG, WebP, GIF, HEIC); use bash"
        )
    # An SVG is text.
    svg = await _view(root, "files/logo.svg", _images())
    assert not svg.is_error and svg.content.startswith("     1\t<svg")
    # A PNG by its header whose data stops short.
    cut = await _view(root, "files/cut.png", _images())
    assert (
        cut.is_error and cut.content == "/workspace/files/cut.png could not be decoded as an image"
    )
    # A caller that offers no image view keeps the old answer.
    old = await _view(root, "files/cut.png", None)
    assert old.is_error and old.content == "/workspace/files/cut.png is not a text file"


async def test_view_range_does_not_apply_to_an_image(tmp_path) -> None:
    root = tmp_path / "ws" / "1"
    _place(root, "files/a.png", _encoded(Image.new("RGB", (10, 10)), "PNG"))
    _media, _image, line = _shown(await _view(root, "files/a.png", _images(), view_range=[1, 5]))
    assert line.endswith("view_range does not apply to an image; this is all of it.")


async def test_an_image_is_decoded_after_the_workspace_lock_is_released(
    tmp_path, monkeypatch
) -> None:
    root = tmp_path / "ws" / "1"
    _place(root, "files/a.png", _encoded(Image.new("RGB", (10, 10)), "PNG"))
    started, release = threading.Event(), threading.Event()
    decode = chat_tools._model_image

    def slow_decode(*args, **kwargs):
        started.set()
        release.wait(5)
        return decode(*args, **kwargs)

    monkeypatch.setattr(chat_tools, "_model_image", slow_decode)
    images = _images()
    viewing = asyncio.ensure_future(_view(root, "files/a.png", images))
    await asyncio.to_thread(started.wait, 5)
    # Mid-decode: another write may take the lock; the turn's one decode slot is held.
    async with asyncio.timeout(2), workspace_lock(root):
        pass
    assert images.decode_slot.locked()
    release.set()
    assert not (await viewing).is_error
    assert not images.decode_slot.locked()


def _editor_round_ctx(root: Path, *, round_budget: int, turn_budget: int) -> SimpleNamespace:
    return SimpleNamespace(
        settings=SimpleNamespace(
            chat_image_round_max_bytes=round_budget,
            chat_image_turn_max_bytes=turn_budget,
            chat_page_image_max_pixels=40_000_000,
            chat_attachment_image_long_edge=1568,
            chat_tool_result_max_chars=1000,
            upload_max_bytes=1,
            content_max_bytes=1000,
            chat_bash_timeout_seconds=1,
        ),
        conversation=SimpleNamespace(id=1, session="s"),
        paths=SimpleNamespace(root=root),
        document=SimpleNamespace(id=1),
        decode_slot=asyncio.Semaphore(1),
        job=SimpleNamespace(page_count=12, kind="pdf"),
        render_slots=None,
        sandbox=None,
        turn=SimpleNamespace(id=uuid.uuid4(), user_id=1),
        image_spent=0,
        credits_exhausted=False,
        round_admission=lambda calls: lambda index: True,
        writers={},
        write_gate=asyncio.Lock(),
    )


async def test_an_editor_image_counts_against_the_image_budgets(tmp_path, monkeypatch) -> None:
    viewed: list[dict] = []

    async def fake_run_editor(root, tool_input, author, *, images=None, **kwargs):
        viewed.append({"path": tool_input.get("path"), "images": images})
        if tool_input.get("command") != "view":
            return chat_tools.ToolOutcome(f"{tool_input['command']} applied")
        image = {"type": "image", "source": {"type": "base64", "data": "x" * 100}}
        return chat_tools.ToolOutcome([image, {"type": "text", "text": "The image."}])

    async def fake_view_page(paths, tool_input, **kwargs):
        image = {"type": "image", "source": {"type": "base64", "data": "x" * 100}}
        return chat_tools.ToolOutcome([image, {"type": "text", "text": "Page."}])

    async def no_events(ctx, event, data):
        return None

    async def never_cancelled(ctx):
        return False

    monkeypatch.setattr(chat_tools, "run_editor", fake_run_editor)
    monkeypatch.setattr(chat_tools, "view_page", fake_view_page)
    monkeypatch.setattr(chat_runner, "_emit", no_events)
    monkeypatch.setattr(chat_runner, "_cancelled", never_cancelled)

    def view(index: int) -> dict:
        return {
            "type": "tool_use",
            "id": f"v{index}",
            "name": "str_replace_based_edit_tool",
            "input": {"command": "view", "path": f"/workspace/{index}.png"},
        }

    # A page and three editor images in one round of 250: the page and the first image
    # fit, then the round is spent — the editor's images and the reads' share it.
    ctx = _editor_round_ctx(tmp_path, round_budget=250, turn_budget=1 << 20)
    round_ = [
        {"type": "tool_use", "id": "p", "name": "view_page", "input": {"page": 1}},
        view(1),
        view(2),
        view(3),
    ]
    results = await chat_runner._run_tools(ctx, round_, set())
    kept = [result["tool_use_id"] for result in results if not result.get("is_error")]
    assert len(kept) == 2 and "p" in kept
    assert all(
        result["content"] == chat_runner.IMAGE_BUDGET_SPENT
        for result in results
        if result.get("is_error")
    )
    assert ctx.image_spent == 200
    # The editor offers the image view, without crop commands on a turn with no shell.
    assert viewed[0]["images"].max_bytes == 250 and viewed[0]["images"].shell is False

    # Across rounds, the turn's budget; an edit is never refused for a spent budget.
    ctx = _editor_round_ctx(tmp_path, round_budget=1 << 20, turn_budget=200)
    first = await chat_runner._run_tools(ctx, [view(1), view(2), view(3)], set())
    assert [bool(result.get("is_error")) for result in first] == [False, False, True]
    assert first[2]["content"] == chat_runner.IMAGE_TURN_BUDGET_SPENT
    # The turn's budget is spent to the byte: the next round's edit must still run.
    assert ctx.image_spent == 200
    edit = {
        "type": "tool_use",
        "id": "e",
        "name": "str_replace_based_edit_tool",
        "input": {"command": "str_replace", "path": "/workspace/doc.md"},
    }
    second = await chat_runner._run_tools(ctx, [view(3), edit], set())
    assert second[0]["content"] == chat_runner.IMAGE_TURN_BUDGET_SPENT
    assert second[1] == {
        "type": "tool_result",
        "tool_use_id": "e",
        "content": "str_replace applied",
    }


@respx.mock
async def test_a_turn_and_a_subagent_send_the_image_their_editor_viewed(
    make_client, tmp_path
) -> None:
    client = await chatting_client(make_client(workspaces_dir=str(tmp_path / "workspaces")))
    root = user_root(client.app.state.settings.workspaces_dir, client.user_id)
    _place(root, ".cache/chart.png", _encoded(Image.new("RGB", (300, 200), "navy"), "PNG"))
    view = {"command": "view", "path": "/workspace/.cache/chart.png"}
    created = await client.post(
        "/api/subagents",
        json={"name": "looker", "description": "Looks", "prompt": "Look at it."},
        headers=CSRF_HEADERS,
    )
    assert created.status_code == 201, created.text
    main, sub = stub_messages(
        [
            one_call("v1", "str_replace_based_edit_tool", view),
            one_call("s1", "subagent", {"name": "looker", "task": "Look at the chart."}),
            ([{"type": "text", "text": "A navy rectangle."}], "end_turn"),
        ],
        [
            one_call("v2", "str_replace_based_edit_tool", view),
            ([{"type": "text", "text": "Navy."}], "end_turn"),
        ],
    )
    conversation_id = await new_conversation(client)
    sent = await send(client, conversation_id, text="What is in the chart?")
    assert sent.status_code == 202, sent.text
    await _run_queued_turn(client)

    for requests in (main, sub):
        result = requests[1]["messages"][2]["content"][0]
        assert not result.get("is_error"), result
        block, line = result["content"]
        assert block["type"] == "image" and block["source"]["media_type"] == "image/png"
        assert line["text"] == "The image /workspace/.cache/chart.png, 300x200 pixels."
    # The panel's copy of the history carries no image bytes.
    detail = (await client.get(f"/api/conversations/{conversation_id}")).json()
    stored = [
        part
        for message in detail["messages"]
        if isinstance(message["content"], list)
        for item in message["content"]
        if item.get("type") == "tool_result" and isinstance(item.get("content"), list)
        for part in item["content"]
    ]
    assert stored and all("data" not in part.get("source", {}) for part in stored)


def test_the_prompt_and_the_subagent_editor_say_the_view_shows_images() -> None:
    from sightread.agents import chat as chat_agent

    assert "shows you the image itself" in chat_agent.DEFAULT_SYSTEM_PROMPT
    assert "as the image itself" in chat_agent.SUBAGENT_EDITOR_TOOL["description"]
