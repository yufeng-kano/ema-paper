"""Coordinate conventions per model family (docs/parsing.md § Coordinate conventions).

The stored contract is always `yxyx_norm1000`; what a model is asked for follows its
family, and the answer is converted before anything downstream sees it. Vision calls run
against a stubbed upstream only.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx
from PIL import Image

from sightread.agents import bbox
from sightread.agents.bbox import (
    canonicalize,
    claude_input_size,
    convention_for_job,
    convention_for_model,
    prepare_image,
)
from sightread.parsing.images import JPEG_QUALITY, image_size
from sightread.upstream.client import Connection, transcribe_page
from tests.conftest import CSRF_HEADERS, KANO_BASE, KANO_CHAT_URL

SECRET = "test-secret-key-not-a-real-one"


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("google/gemini-2.5-pro", bbox.GEMINI),
        ("gemini-2.5-flash", bbox.GEMINI),
        ("anthropic/claude-sonnet-4-5", bbox.CLAUDE),
        ("Claude-Opus", bbox.CLAUDE),
        ("claude-code/claude-opus-5", bbox.CLAUDE),
        ("some-proxy/gpt-6", bbox.GPT),
        ("openai/gpt-6", bbox.GPT),
        ("gpt-6-astra", bbox.GPT),
        ("qwen/qwen3-vl", bbox.DEFAULT),
        (None, bbox.DEFAULT),
    ],
)
def test_the_convention_follows_the_model_family(model, expected) -> None:
    assert convention_for_model(model) is expected


def test_a_job_queued_under_the_old_pipeline_keeps_the_stored_contract() -> None:
    # Its snapshotted prompt still asks for yxyx 0-1000; reading that as pixels would
    # corrupt the boxes after a paid call.
    assert convention_for_job("anthropic/claude-sonnet-4-5", 5) is bbox.DEFAULT
    assert convention_for_job("anthropic/claude-sonnet-4-5", 6) is bbox.CLAUDE
    assert convention_for_job("google/gemini-2.5-pro", 6) is bbox.GEMINI


def test_only_the_pixel_families_are_asked_in_pixels() -> None:
    assert not bbox.GEMINI.pixel and not bbox.DEFAULT.pixel
    assert bbox.CLAUDE.pixel and bbox.GPT.pixel
    assert "YMIN,XMIN,YMAX,XMAX" in bbox.GEMINI.instructions(800, 600)
    pixel = bbox.CLAUDE.instructions(800, 600)
    assert "X1,Y1,X2,Y2" in pixel and "800 pixels wide and 600 pixels tall" in pixel
    # `{page}` is left for the caller's substitution, like the rest of the template.
    assert "p{page}" in pixel


def test_claude_input_size_matches_the_published_examples() -> None:
    # The A4 scan and the 1080p screenshot from Claude's own vision-coordinates guide.
    assert claude_input_size(1075, 1520) == (924, 1307)
    assert claude_input_size(1920, 1080) == (1456, 819)
    # Portrait is the transpose of landscape, and a fitting image is left alone.
    assert claude_input_size(1080, 1920) == (819, 1456)
    assert claude_input_size(800, 600) == (800, 600)


def test_pixel_boxes_are_rewritten_into_the_stored_contract() -> None:
    markdown = "Text\n\n![fig](sightread://p3/100,50,300,250)\nA caption\n"
    # x1=100, y1=50, x2=300, y2=250 on a 1000x500 image → [y 100, x 100, y 500, x 300].
    assert canonicalize(markdown, bbox.GPT, 1000, 500) == (
        "Text\n\n![fig](sightread://p3/100,100,500,300)\nA caption\n"
    )
    # A normalized family's answer is already the contract.
    assert canonicalize(markdown, bbox.GEMINI, 1000, 500) == markdown
    # Clamping is not this step's job — an out-of-image pixel is handed on as is.
    assert canonicalize("![f](sightread://p1/0,0,1200,600)", bbox.CLAUDE, 1000, 500) == (
        "![f](sightread://p1/0,0,1200,1200)"
    )


def test_only_claude_gets_a_pre_resized_image(documents) -> None:
    wide = documents["wide_png"]  # 3000x1000, over every Claude limit
    as_is = prepare_image(wide, bbox.GPT)
    assert (as_is.path, as_is.derived) == (wide, False)
    assert (as_is.width_px, as_is.height_px) == (3000, 1000)

    sent = prepare_image(wide, bbox.CLAUDE)
    assert sent.derived and sent.path != wide
    assert (sent.width_px, sent.height_px) == claude_input_size(3000, 1000)
    assert image_size(sent.path) == (sent.width_px, sent.height_px)
    sent.cleanup()
    assert not sent.path.exists() and wide.exists()

    small = prepare_image(documents["png"], bbox.CLAUDE)
    assert not small.derived

    # A JPEG is re-encoded at the normalizer's quality, not Pillow's default.
    wide_jpg = wide.with_name("wide.jpg")
    Image.open(wide).convert("RGB").save(wide_jpg, format="JPEG", quality=JPEG_QUALITY)
    sent_jpg = prepare_image(wide_jpg, bbox.CLAUDE)
    with Image.open(sent_jpg.path) as reencoded:
        assert reencoded.format == "JPEG"
        # Pillow's quality-75 tables start at 8; the quality-92 ones start at 3.
        assert reencoded.quantization[0][0] <= 3
    sent_jpg.cleanup()
    wide_jpg.unlink()


def _key() -> Connection:
    return Connection(
        api_key="sk-kano-proxy-test",
        base_url=f"{KANO_BASE}/openai/v1",
        snapshot=KANO_BASE,
    )


@respx.mock
async def test_a_pixel_model_is_asked_in_pixels_and_answers_in_the_contract(documents) -> None:
    width, height = image_size(documents["png"])
    route = respx.post(KANO_CHAT_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": f"![fig](sightread://p2/0,0,{width},{height})\nCaption"
                        }
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )
    )

    result = await transcribe_page(
        _key(),
        "openai/gpt-6",
        "Page {page}. {bbox_instructions} Id: {bbox_format}.",
        documents["png"],
        2,
        convention=convention_for_model("openai/gpt-6"),
    )

    sent = json.loads(route.calls[0].request.content)["messages"][0]["content"][0]["text"]
    assert sent.startswith("Page 2. ")
    assert "sightread://p2/X1,Y1,X2,Y2" in sent
    assert f"{width} pixels wide and {height} pixels tall" in sent
    assert sent.endswith("Id: xyxy_px.")
    # The whole image, in pixels, is the whole page in the stored contract.
    assert result.markdown == "![fig](sightread://p2/0,0,1000,1000)\nCaption"


@respx.mock
async def test_a_normalized_model_keeps_the_original_contract_wording(documents) -> None:
    route = respx.post(KANO_CHAT_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "![fig](sightread://p1/10,20,30,40)"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )
    )
    result = await transcribe_page(
        _key(),
        "google/gemini-2.5-flash",
        "{bbox_instructions} Id: {bbox_format}.",
        documents["png"],
        1,
        convention=convention_for_model("google/gemini-2.5-flash"),
    )
    sent = json.loads(route.calls[0].request.content)["messages"][0]["content"][0]["text"]
    assert "sightread://p1/YMIN,XMIN,YMAX,XMAX" in sent and sent.endswith("Id: yxyx_norm1000.")
    assert result.markdown == "![fig](sightread://p1/10,20,30,40)"


async def test_the_agents_payload_names_the_parse_models_convention(signed_in, make_client) -> None:
    before = (await signed_in.get("/api/agents")).json()["agents"][0]
    assert before["bbox"] == {"family": "default", "format": "yxyx_norm1000"}

    client = make_client(
        parse_model="anthropic/claude-sonnet-4-5",
        chat_models="",
        model_prices_json=json.dumps(
            {
                "anthropic/claude-sonnet-4-5": {
                    "input": 1,
                    "cache_read": 1,
                    "cache_write": 1,
                    "output": 1,
                }
            }
        ),
    )
    assert (await client.post("/api/auth/dev-login", headers=CSRF_HEADERS)).status_code == 200
    after = (await client.get("/api/agents")).json()["agents"][0]
    assert after["settings"]["model"] == "anthropic/claude-sonnet-4-5"
    assert after["bbox"] == {"family": "claude", "format": "xyxy_px"}


async def test_me_carries_the_prompt_cap(signed_in) -> None:
    limits = (await signed_in.get("/api/me")).json()["limits"]
    assert limits["prompt_max_chars"] == signed_in.app.state.settings.system_prompt_max_chars
