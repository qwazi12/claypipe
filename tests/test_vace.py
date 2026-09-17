"""V2 acceptance — the Wan VACE video-to-video backend.

NO NETWORK IN THIS FILE. The client is a one-method seam and every test here
uses a stub, so nothing can reach fal by accident.

The backend's real job is absorbing an impedance mismatch: the pipeline speaks
FRAMES everywhere (extraction, scoring, assembly, the frame-count invariant)
and VACE speaks VIDEOS. Each call encodes frames to a clip, generates, and
decodes back to exactly as many frames as it was given — "exactly" being the
load-bearing word, since a short chunk shortens the clip and desyncs the audio
in a file that plays perfectly.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from claypipe.config import BILLED_FRAMES_PER_SECOND, ConfigError, load_weights
from claypipe.pipeline.restyle import ClipRestyleBackend, RestyleBackend, get_clip_backend
from claypipe.pipeline.vace import (
    CONTROL_SIGNALS,
    DEFAULT_VACE_MODEL,
    MAX_CHUNK_FRAMES,
    MIN_CHUNK_FRAMES,
    NATIVE_FPS,
    PRICED_RESOLUTIONS,
    VaceBackend,
    VaceClientLike,
    VaceError,
    _decode,
    _encode,
)


@pytest.fixture
def source_frames(tmp_path: Path) -> list[Path]:
    """81 frames — VACE's minimum, so the default case is the purchasable one."""
    from PIL import Image

    src = tmp_path / "src"
    src.mkdir()
    paths = []
    for i in range(1, MIN_CHUNK_FRAMES + 1):
        img = Image.new("RGB", (64, 64), (30, 30, 40))
        img.paste(Image.new("RGB", (16, 16), (200, 180, 90)), (i % 40, 20))
        path = src / f"f_{i:05d}.png"
        img.save(path)
        paths.append(path)
    return paths


class StubClient:
    """Faithful to the seam: takes frames in, returns an MP4 of the same length."""

    def __init__(self, *, frames_returned: int | None = None):
        self.calls: list[dict] = []
        self.frames_returned = frames_returned

    def generate_video(
        self, *, video_path, prompt, negative_prompt, task, resolution,
        num_frames, frames_per_second, seed, model, guidance_scale=None,
    ) -> bytes:
        self.calls.append({
            "video_path": Path(video_path), "prompt": prompt,
            "negative_prompt": negative_prompt, "task": task,
            "resolution": resolution, "num_frames": num_frames,
            "frames_per_second": frames_per_second, "seed": seed, "model": model,
            "guidance_scale": guidance_scale,
        })
        count = self.frames_returned if self.frames_returned is not None else num_frames
        out = Path(video_path).parent / f"stub_{count}.mp4"
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostdin", "-y",
             "-f", "lavfi", "-i", f"testsrc=size=64x64:rate={frames_per_second}",
             "-frames:v", str(count), "-c:v", "libx264", "-crf", "20",
             "-pix_fmt", "yuv420p", str(out)],
            check=True, capture_output=True,
        )
        return out.read_bytes()


def _backend(**kwargs) -> VaceBackend:
    kwargs.setdefault("live", True)
    kwargs.setdefault("client", StubClient())
    kwargs.setdefault("_cfg", load_weights().firewalls.cost)
    return VaceBackend(**kwargs)


# ---------------------------------------------------------------------------
# Protocol and the verified schema
# ---------------------------------------------------------------------------

def test_it_is_a_clip_backend_and_not_a_frame_backend():
    backend = _backend()
    assert isinstance(backend, ClipRestyleBackend)
    assert not isinstance(backend, RestyleBackend)


def test_the_chunk_bounds_match_fals_schema():
    """Quoted: num_frames "must be between 81 to 241 (inclusive)"."""
    backend = _backend()
    assert (backend.min_chunk_frames, backend.max_chunk_frames) == (81, 241)
    assert (MIN_CHUNK_FRAMES, MAX_CHUNK_FRAMES) == (81, 241)


def test_the_native_rate_is_the_billing_rate():
    """"Video seconds are calculated at 16 frames per second." The control clip
    is encoded at this rate, not the pipeline's 12."""
    assert NATIVE_FPS == BILLED_FRAMES_PER_SECOND == 16
    assert _backend().native_fps == 16


def test_only_the_two_verified_control_signals_are_accepted():
    """fal's task enum is depth | pose | inpainting | outpainting | reframe.
    Only depth and pose are restyle control signals."""
    assert CONTROL_SIGNALS == ("depth", "pose")
    for signal in CONTROL_SIGNALS:
        assert _backend(control_signal=signal).control_signal == signal


@pytest.mark.parametrize("missing", ["canny", "lineart", "scribble", "openpose"])
def test_canny_and_friends_are_refused_with_the_reason(missing):
    """The silhouette-locking signal the plan assumed cannot be bought here at
    any price. Refusing loudly beats silently substituting depth."""
    with pytest.raises(VaceError) as exc:
        _backend(control_signal=missing)
    message = str(exc.value)
    assert "NO canny or lineart" in message
    assert "depth" in message and "pose" in message


@pytest.mark.parametrize("resolution", sorted(PRICED_RESOLUTIONS))
def test_priced_resolutions_are_accepted(resolution):
    assert _backend(resolution=resolution).resolution == resolution


@pytest.mark.parametrize("unpriced", ["auto", "240p", "360p", "1080p"])
def test_unpriced_resolutions_are_refused(unpriced):
    """They exist in fal's enum but have no published rate, and authorising
    against an unknown rate is refused by design."""
    with pytest.raises(VaceError, match="not priced"):
        _backend(resolution=unpriced)


# ---------------------------------------------------------------------------
# Money
# ---------------------------------------------------------------------------

def test_each_resolution_bills_against_its_own_ledger_key():
    """480p and 720p are the same endpoint at different rates. Charging both to
    one key would make the run total unreconcilable against the invoice."""
    keys = {r: _backend(resolution=r).ledger_backend for r in PRICED_RESOLUTIONS}
    assert len(set(keys.values())) == len(keys)
    assert keys["480p"] == "wan_vace_480p"


def test_cost_is_frames_over_sixteen():
    backend = _backend(resolution="480p")
    assert backend.cost_per_video_second_usd() == pytest.approx(0.04)
    assert backend.cost_for_frames(81) == pytest.approx(0.2025)
    assert backend.cost_for_frames(720) == pytest.approx(1.80)


def test_a_wrongly_united_price_entry_is_refused():
    """If someone re-registers VACE as wall-clock, that is a 2x error and must
    surface as a refusal rather than a quiet halving of the bill."""
    from claypipe.config import CostConfig

    cfg = CostConfig.model_validate({
        "estimated_usd_per_call": {"dummy": 0.0},
        "pricing": {"wan_vace_480p": {"unit": "video_second", "rate": 0.04}},
    })
    backend = _backend(_cfg=cfg)
    with pytest.raises(ConfigError, match="2x error"):
        backend.cost_per_video_second_usd()


def test_an_unpriced_resolution_key_is_refused():
    from claypipe.config import CostConfig

    cfg = CostConfig.model_validate({"estimated_usd_per_call": {"dummy": 0.0}})
    with pytest.raises(ConfigError, match="not priced"):
        _backend(_cfg=cfg).cost_per_video_second_usd()


def test_a_quote_above_the_ceiling_is_refused():
    backend = _backend()
    backend.assert_affordable(0.20, 81)          # under the ceiling, fine
    with pytest.raises(VaceError, match="above the configured ceiling"):
        backend.assert_affordable(0.50, 81)


def test_plan_reports_the_call_without_making_it():
    backend = VaceBackend(_cfg=load_weights().firewalls.cost)   # not live
    entry = backend.plan([Path(f"f_{i:05d}.png") for i in range(81)],
                         prompt="claymation", seed=1000)
    assert entry["num_frames"] == 81
    assert entry["task"] == "depth"
    assert entry["billed_seconds"] == pytest.approx(81 / 16)
    assert entry["estimated_usd"] == pytest.approx(0.2025)
    assert entry["ledger_backend"] == "wan_vace_480p"
    assert backend.planned_calls == [entry]


# ---------------------------------------------------------------------------
# The double lock
# ---------------------------------------------------------------------------

def test_it_refuses_without_live(source_frames, tmp_path: Path):
    backend = VaceBackend(live=False, client=StubClient(),
                          _cfg=load_weights().firewalls.cost)
    with pytest.raises(NotImplementedError, match="--live"):
        backend.restyle_clip(source_frames, tmp_path / "out",
                             prompt="clay", strength=0.65, seed=1000)


def test_it_refuses_without_a_client(source_frames, tmp_path: Path):
    backend = VaceBackend(live=True, client=None,
                          _cfg=load_weights().firewalls.cost)
    with pytest.raises(NotImplementedError, match="client"):
        backend.restyle_clip(source_frames, tmp_path / "out",
                             prompt="clay", strength=0.65, seed=1000)


def test_the_resolver_marks_it_paid():
    from claypipe.cli import PAID_BACKENDS

    assert "wan_vace" in PAID_BACKENDS


def test_the_resolver_returns_it_without_a_client_when_not_live():
    backend = get_clip_backend("wan_vace", live=False)
    assert isinstance(backend, VaceBackend)
    assert backend.client is None, "a non-live resolve must not build a client"


def test_the_resolver_passes_the_control_signal_through():
    backend = get_clip_backend("wan_vace", live=False, control_signal="pose",
                               resolution="580p")
    assert backend.control_signal == "pose"
    assert backend.resolution == "580p"
    assert backend.ledger_backend == "wan_vace_580p"


def test_an_unknown_clip_backend_is_refused():
    with pytest.raises(ValueError, match="unknown clip backend"):
        get_clip_backend("nope")


# ---------------------------------------------------------------------------
# The round trip — frames in, the same number of frames out
# ---------------------------------------------------------------------------

def test_the_round_trip_preserves_the_frame_count(source_frames, tmp_path: Path):
    """V2 acceptance: dummy-mode round trip green, no network."""
    client = StubClient()
    backend = _backend(client=client)
    written = backend.restyle_clip(
        source_frames, tmp_path / "out", prompt="claymation", strength=0.65,
        seed=1000,
    )
    assert len(written) == len(source_frames) == MIN_CHUNK_FRAMES
    # Named after the SOURCE frames, so every downstream stage still finds them.
    assert [p.name for p in written] == [p.name for p in source_frames]
    for path in written:
        assert path.is_file() and path.stat().st_size > 0


def test_the_request_carries_the_verified_parameters(source_frames, tmp_path: Path):
    client = StubClient()
    backend = _backend(client=client, control_signal="pose", resolution="580p")
    backend.restyle_clip(source_frames, tmp_path / "out", prompt="claymation",
                         strength=0.65, seed=4242)
    call = client.calls[0]
    assert call["task"] == "pose"
    assert call["resolution"] == "580p"
    assert call["num_frames"] == MIN_CHUNK_FRAMES
    assert call["frames_per_second"] == NATIVE_FPS
    assert call["seed"] == 4242
    assert call["prompt"] == "claymation"
    assert call["model"] == DEFAULT_VACE_MODEL
    assert call["video_path"].is_file()


def test_strength_is_accepted_but_not_sent(source_frames, tmp_path: Path):
    """VACE has no img2img strength. Mapping it onto something else would make
    the operator's dial lie, so it is documented as inert rather than faked."""
    client = StubClient()
    backend = _backend(client=client)
    backend.restyle_clip(source_frames, tmp_path / "out", prompt="clay",
                         strength=0.99, seed=1000)
    assert "strength" not in client.calls[0]


def test_a_short_chunk_from_the_endpoint_is_refused(source_frames, tmp_path: Path):
    """The failure with no per-frame analogue: it shortens the clip and desyncs
    the audio, in a file that plays perfectly."""
    backend = _backend(client=StubClient(frames_returned=MIN_CHUNK_FRAMES - 3))
    with pytest.raises(VaceError, match="returned 78 frames"):
        backend.restyle_clip(source_frames, tmp_path / "out", prompt="clay",
                             strength=0.65, seed=1000)


def test_an_empty_response_is_refused(source_frames, tmp_path: Path):
    class Empty:
        def generate_video(self, **kwargs):
            return b""

    backend = _backend(client=Empty())
    with pytest.raises(VaceError, match="empty response"):
        backend.restyle_clip(source_frames, tmp_path / "out", prompt="clay",
                             strength=0.65, seed=1000)


@pytest.mark.parametrize("count", [MIN_CHUNK_FRAMES - 1, MAX_CHUNK_FRAMES + 1])
def test_a_request_outside_the_schema_bounds_is_refused(count, tmp_path: Path):
    from PIL import Image

    src = tmp_path / "s"
    src.mkdir()
    frames = []
    for i in range(1, count + 1):
        path = src / f"f_{i:05d}.png"
        Image.new("RGB", (32, 32), (10, 10, 10)).save(path)
        frames.append(path)
    backend = _backend()
    with pytest.raises(VaceError, match="frames per call"):
        backend.restyle_clip(frames, tmp_path / "out", prompt="clay",
                             strength=0.65, seed=1000)


def test_the_control_clip_is_encoded_at_the_native_rate(source_frames, tmp_path: Path):
    """Sending 12fps frames while declaring 16 would make VACE read the motion
    as slower than it is, and the output would drift out of step with the audio
    it has to be re-muxed against."""
    from claypipe import ffmpeg

    dst = tmp_path / "control.mp4"
    _encode(source_frames, dst, fps=NATIVE_FPS)
    stream = ffmpeg.stream(dst, "video")
    numerator, denominator = stream["r_frame_rate"].split("/")
    assert int(numerator) / int(denominator) == pytest.approx(NATIVE_FPS)


def test_decode_emits_exactly_the_frames_present(tmp_path: Path):
    """Decoded with -vsync 0 so a short chunk cannot be hidden by ffmpeg
    duplicating frames to fill a target rate."""
    video = tmp_path / "v.mp4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-f", "lavfi",
         "-i", "testsrc=size=32x32:rate=16", "-frames:v", "9",
         "-c:v", "libx264", "-crf", "20", "-pix_fmt", "yuv420p", str(video)],
        check=True, capture_output=True,
    )
    out = tmp_path / "out"
    out.mkdir()
    written = _decode(video, out, names=[f"f_{i:05d}.png" for i in range(1, 10)])
    assert len(written) == 9


# ---------------------------------------------------------------------------
# Prompt controls (2026-09-17). VACE has NO control-strength or
# conditioning-scale parameter — verified on the schema — so `guidance_scale`
# is the only lever on the prompt-versus-input balance, and it works the
# OPPOSITE way from a control strength: raising it pushes toward the prompt.
# ---------------------------------------------------------------------------

def test_the_negative_prompt_reaches_the_endpoint(source_frames, tmp_path: Path):
    client = StubClient()
    backend = _backend(client=client, negative_prompt="photorealistic, live action")
    backend.restyle_clip(source_frames, tmp_path / "out", prompt="clay",
                         strength=0.65, seed=1000)
    assert client.calls[0]["negative_prompt"] == "photorealistic, live action"


def test_guidance_scale_reaches_the_endpoint(source_frames, tmp_path: Path):
    client = StubClient()
    backend = _backend(client=client, guidance_scale=7.5)
    backend.restyle_clip(source_frames, tmp_path / "out", prompt="clay",
                         strength=0.65, seed=1000)
    assert client.calls[0]["guidance_scale"] == pytest.approx(7.5)


def test_an_unset_guidance_scale_leaves_the_endpoint_default_alone(
    source_frames, tmp_path: Path
):
    """null means "do not pin a number we have not measured", not "send 0"."""
    client = StubClient()
    backend = _backend(client=client)
    assert backend.guidance_scale is None
    backend.restyle_clip(source_frames, tmp_path / "out", prompt="clay",
                         strength=0.65, seed=1000)
    assert client.calls[0]["guidance_scale"] is None


def test_the_shipped_clay_style_carries_the_rewritten_prompt():
    """V7's failure was traced to the prompt telling the model to change
    nothing. The preservation clause must stay deleted, not softened."""
    from claypipe.config import load_styles

    clay = load_styles().profile("clay")
    assert "keep exact same" not in clay.prompt
    assert "framing and colors" not in clay.prompt
    # It must describe a MATERIAL AND PROCESS, not just a look...
    for token in ("thumbprint", "tool marks", "seams", "plasticine"):
        assert token in clay.prompt.lower(), token
    # ...and restyle the WORLD, not only the people (R3).
    for token in ("miniature set", "clay walls", "clay furniture"):
        assert token in clay.prompt.lower(), token
    assert "photorealistic" in clay.negative_prompt.lower()
    assert clay.guidance_scale is not None and clay.guidance_scale > 5.0
