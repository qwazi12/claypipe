"""Stage: assembly (SPEC §6).

Reassembles restyled frames at the extraction fps, muxes the ORIGINAL audio with
`-c:a copy`, and lays out the 9:16 comparison in one filter_complex.

Two hard failures live here, and neither is a warning:
  * frame-count mismatch  (extracted == restyled == reassembled)
  * audio packet-MD5 mismatch between the extracted track and the final file

T12: captions are composited into the gap band as a finite PNG sequence, NOT
burned over the picture with libass. The `subtitles=` filter is gone — see
captions.py for why.

Header note: SPEC §6 says `drawtext`, but ffmpeg builds vary and the installed
one has no such filter, so the header bar is rendered to a PNG with PIL and
composited with `overlay` (memory.md D1). Same output, no build dependency.

T9: the vertical stack is no longer a set of configured heights. `layout.py`
derives panel height from the SOURCE's aspect ratio, centres the panel pair as
a block, and hands the top margin to the header. Nothing in this module may
hardcode a band height — ask the Layout.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .. import ffmpeg
from ..config import RenderConfig, StyleProfile, hex_to_rgb
from ..logging import RunLogger
from ..run import Run
from . import captions
from .extract import count_frames
from .layout import Layout, source_aspect_of


class AssemblyError(RuntimeError):
    """Assembly failed a verification check. Never worked around silently."""


def render_header(
    dst: Path, title: str, render: RenderConfig, profile: StyleProfile,
    layout: Layout,
) -> Path:
    """The branded header bar, as an image, sized to the derived top margin."""
    size = (layout.canvas_width, layout.header_height)
    img = Image.new("RGB", size, hex_to_rgb(profile.background_color))
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype(str(render.font_path()), render.header_font_size)

    text = title.upper()
    # Shrink to fit rather than overflow the bar.
    while draw.textlength(text, font=font) > layout.canvas_width * 0.92 and font.size > 12:
        font = ImageFont.truetype(str(render.font_path()), font.size - 2)

    left, top, right, bottom = draw.textbbox((0, 0), text, font=font)
    draw.text(
        ((size[0] - (right - left)) / 2 - left, (size[1] - (bottom - top)) / 2 - top),
        text,
        font=font,
        fill=hex_to_rgb(profile.header_text_color),
    )
    dst.parent.mkdir(parents=True, exist_ok=True)
    img.save(dst)
    return dst


def encode_frames_to_video(
    frames_dir: Path, dst: Path, fps: int, render: RenderConfig, logger: RunLogger
) -> int:
    """Restyled frames -> silent video at the extraction fps. Returns frame count."""
    expected = count_frames(frames_dir)
    if expected == 0:
        raise AssemblyError(f"no restyled frames in {frames_dir}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    ffmpeg.run(
        ["-framerate", str(fps), "-start_number", "1",
         "-i", str(frames_dir / "f_%05d.png"),
         "-an", "-c:v", "libx264", "-crf", str(render.crf),
         "-pix_fmt", render.pix_fmt, "-r", str(fps), str(dst)],
        what="restyled frame reassembly",
    )
    actual = _count_video_frames(dst)
    if actual != expected:
        raise AssemblyError(
            f"reassembly frame-count mismatch: {expected} restyled frames in "
            f"{frames_dir}, but {dst.name} holds {actual}"
        )
    logger.info("assemble.reassembled", frames=actual, fps=fps, path=str(dst))
    return actual


def _count_video_frames(path: Path) -> int:
    tools = ffmpeg.require_ffmpeg()
    import subprocess

    proc = subprocess.run(
        [tools.ffprobe, "-v", "error", "-select_streams", "v:0",
         "-count_frames", "-show_entries", "stream=nb_read_frames",
         "-of", "default=nokey=1:noprint_wrappers=1", str(path)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise ffmpeg.FFmpegError(f"frame count of {path} failed:\n{proc.stderr.strip()}")
    return int(proc.stdout.strip())


def _panel_filter(layout: Layout) -> str:
    """Crop to the panel's aspect, then scale to it.

    Since T9 the panel aspect IS the source aspect, so for a normal run this
    crop is a no-op and the whole frame survives — which is the point: the old
    fixed 882px panel cropped real picture away from every source that was not
    1080x882. The crop stays as the safety net for the case where the two
    aspects disagree (a forced canvas, or a source whose aspect was overridden),
    because a mismatched scale would stretch faces and nothing downstream would
    catch it.
    """
    w, h = layout.canvas_width, layout.panel_height
    return (
        f"crop=w=trunc(min(iw\\,ih*{w}/{h})/2)*2:"
        f"h=trunc(min(ih\\,iw*{h}/{w})/2)*2:"
        f"x=(iw-ow)/2:y=(ih-oh)/2,"
        f"scale={w}:{h}:flags=lanczos,setsar=1"
    )


def build_comparison(
    *,
    restyled_video: Path,
    source_video: Path,
    audio: Path,
    header: Path,
    dst: Path,
    render: RenderConfig,
    profile: StyleProfile,
    layout: Layout,
    fps: int,
    frames: int,
    caption_track: Path | None,
    logger: RunLogger,
) -> Path:
    """The 9:16 stack: header bar, restyled on top, caption gap, original below."""
    top_y = layout.restyled_y
    bottom_y = layout.original_y
    bg = "0x" + profile.background_color.lstrip("#")
    panel = _panel_filter(layout)

    # The canvas is built with `pad` around the top panel rather than from a
    # `color` source. A colour generator is an INFINITE input: overlaying finite
    # panels onto it renders until the disk fills, which is exactly what it did
    # before this was changed. Padding a finite input keeps the whole graph
    # bounded by the restyled video's own length.
    graph = (
        f"[0:v]{panel},pad={layout.canvas_width}:{layout.canvas_height}:0:{top_y}:color={bg}[base];"
        f"[1:v]{panel}[bot];"
        f"[base][bot]overlay=x=0:y={bottom_y}[s2];"
        f"[s2][2:v]overlay=x=0:y=0:eof_action=repeat[hdr]"
    )
    if caption_track is not None:
        # T12: captions are a LAYOUT ELEMENT composited into the gap band, not
        # a subtitle filter drawn over the picture. libass has no idea the gap
        # exists, so a long cue at a large size spills onto a panel and crops a
        # face — in a file that plays perfectly. The track is a finite PNG
        # sequence at the run's own fps, exactly `frames` long, overlaid at the
        # gap's y offset, so a caption physically cannot reach a panel.
        graph += f";[hdr][3:v]overlay=x=0:y={layout.gap_y}:eof_action=pass[cap]"
    else:
        graph += ";[hdr]null[cap]"

    # Pin the video to exactly the restyled frame count. The source panel runs at
    # its own (higher) rate and is usually a fraction of a second longer, which
    # otherwise trails 1-2 repeated frames past the end of the restyled sequence
    # and breaks the extracted==restyled==reassembled invariant.
    #
    # This MUST happen in the filtergraph, not as `-frames:v`. That option
    # finishes the mux as soon as the video stream ends, which truncates the
    # audio and voids the bit-identity guarantee (observed, then fixed here).
    graph += f";[cap]trim=end_frame={frames},setpts=PTS-STARTPTS[v]"

    args = [
        "-i", str(restyled_video),
        "-i", str(source_video),
        # No -loop: a single still is repeated by the overlay, and looping it
        # would reintroduce an unbounded input.
        "-i", str(header),
    ]
    # The caption track goes in as input 3 so the audio index shifts; both are
    # referenced by number in the graph and the map, so this order is load-
    # bearing. A PNG sequence at a fixed framerate is a FINITE input — the
    # unbounded-input trap D5 records applies to `color` and `-loop`, not this.
    audio_index = 3
    if caption_track is not None:
        args += ["-framerate", str(fps), "-i", str(caption_track)]
        audio_index = 4
    args += [
        "-i", str(audio),
        "-filter_complex", graph,
        "-map", "[v]", "-map", f"{audio_index}:a:0",
        "-c:v", "libx264", "-crf", str(render.crf), "-pix_fmt", render.pix_fmt,
        "-r", str(fps),
        # The sync guarantee: the original audio is copied, never re-encoded.
        "-c:a", "copy",
        "-movflags", "+faststart",
        str(dst),
    ]
    ffmpeg.run(args, what="9:16 comparison render")
    logger.info(
        "assemble.rendered",
        path=str(dst),
        size=f"{layout.canvas_width}x{layout.canvas_height}",
        captions=bool(caption_track),
        caption_band_y=layout.gap_y if caption_track else None,
        **layout.as_dict(),
    )
    return dst


def verify_output(
    dst: Path,
    *,
    source_video: Path,
    extracted_audio_md5: str,
    expected_frames: int,
    render: RenderConfig,
    layout: Layout,
    logger: RunLogger,
) -> dict:
    """Post-render acceptance checks. Any failure raises — never a warning.

    The audio is checked against BOTH the extracted track and the original
    source video, so a corrupted extraction cannot pass by matching itself.
    """
    video = ffmpeg.stream(dst, "video")
    audio = ffmpeg.stream(dst, "audio")

    width, height = int(video["width"]), int(video["height"])
    if (width, height) != (layout.canvas_width, layout.canvas_height):
        raise AssemblyError(
            f"output is {width}x{height}, expected "
            f"{layout.canvas_width}x{layout.canvas_height}"
        )
    if not layout.closes():
        raise AssemblyError(
            f"layout does not close: {layout.top_margin} + 2*{layout.panel_height} "
            f"+ {layout.gap_height} + {layout.bottom_margin} != {layout.canvas_height}"
        )

    actual_frames = _count_video_frames(dst)
    if actual_frames != expected_frames:
        raise AssemblyError(
            f"output frame-count mismatch: {expected_frames} restyled frames but "
            f"{dst.name} holds {actual_frames}"
        )

    out_md5 = ffmpeg.stream_md5(dst, "audio")
    src_md5 = ffmpeg.stream_md5(source_video, "audio")
    for label, expected in (("extracted audio.aac", extracted_audio_md5),
                            ("source video", src_md5)):
        if out_md5 != expected:
            raise AssemblyError(
                f"AUDIO WAS NOT COPIED BIT-FOR-BIT: {label} md5 {expected} != "
                f"output audio md5 {out_md5}. The sync guarantee is void; "
                "refusing to pass this run."
            )

    result = {
        "width": width,
        "height": height,
        "video_codec": video.get("codec_name"),
        "audio_codec": audio.get("codec_name"),
        "audio_md5": out_md5,
        "audio_bit_identical": True,
        "frames_expected": expected_frames,
        "frames_actual": actual_frames,
        "duration_s": ffmpeg.duration_seconds(dst),
        "layout": layout.as_dict(),
    }
    logger.info("assemble.verified", **result)
    return result


def layout_for_run(run: Run, render: RenderConfig) -> Layout:
    """The run's vertical geometry, derived from its SOURCE's aspect ratio.

    Dimensions are recorded at intake. A run created before T9 has none, so the
    source is re-probed and the gap is logged as a warning — never silently
    defaulted to some canvas-shaped guess, because a wrong aspect here crops
    picture away and still produces a plausible-looking video.
    """
    m = run.manifest
    width, height = m.source_width, m.source_height
    if width <= 0 or height <= 0:
        stream = ffmpeg.stream(Path(m.source_path), "video")
        width, height = int(stream["width"]), int(stream["height"])
        run.logger.warn(
            "assemble.layout.reprobed",
            reason="run.json predates T9 and records no source dimensions",
            source_width=width,
            source_height=height,
        )
    layout = render.layout_for(source_aspect_of(width, height))
    run.logger.info(
        "assemble.layout",
        source=f"{width}x{height}",
        **layout.as_dict(),
    )
    return layout


def assemble(run: Run, render: RenderConfig, profile: StyleProfile, source_audio_md5: str) -> dict:
    """Full assembly stage for a run."""
    p = run.paths
    layout = layout_for_run(run, render)
    extracted = count_frames(p.source_frames)
    restyled = count_frames(p.restyled_frames)
    if extracted != restyled:
        raise AssemblyError(
            f"frame-count mismatch before assembly: {extracted} extracted vs "
            f"{restyled} restyled. Re-run `claypipe batch` — assembly will not "
            "guess which frames are missing."
        )

    frames = encode_frames_to_video(
        p.restyled_frames, p.restyled_video, run.manifest.fps, render, run.logger
    )
    render_header(p.header_png, run.manifest.clip_title, render, profile, layout)

    # T12: a cue file, if the operator made one, becomes the caption track.
    # Absent, the run renders with no captions rather than with a guess.
    caption_track: Path | None = None
    if p.cues.is_file():
        cues = captions.load_cues(p.cues)
        captions.render_caption_track(
            cues, out_dir=p.caption_frames, layout=layout, render=render,
            profile=profile, fps=run.manifest.fps, total_frames=frames,
            logger=run.logger,
        )
        caption_track = p.caption_frames / captions.CAPTION_FRAME_PATTERN
        captions.write_srt(cues, p.subtitles)

    build_comparison(
        restyled_video=p.restyled_video,
        source_video=Path(run.manifest.source_path),
        audio=p.audio,
        header=p.header_png,
        dst=p.final,
        render=render,
        profile=profile,
        layout=layout,
        fps=run.manifest.fps,
        frames=frames,
        caption_track=caption_track,
        logger=run.logger,
    )
    return verify_output(
        p.final,
        source_video=Path(run.manifest.source_path),
        extracted_audio_md5=source_audio_md5,
        expected_frames=frames,
        render=render,
        layout=layout,
        logger=run.logger,
    )
