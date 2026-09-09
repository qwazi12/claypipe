"""Stage: assembly (SPEC §6).

Reassembles restyled frames at the extraction fps, muxes the ORIGINAL audio with
`-c:a copy`, and lays out the 9:16 comparison in one filter_complex.

Two hard failures live here, and neither is a warning:
  * frame-count mismatch  (extracted == restyled == reassembled)
  * audio packet-MD5 mismatch between the extracted track and the final file

Header note: SPEC §6 says `drawtext`, but ffmpeg builds vary and the installed
one has no such filter, so the header bar is rendered to a PNG with PIL and
composited with `overlay` (memory.md D1). Same output, no build dependency.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .. import ffmpeg
from ..config import RenderConfig, StyleProfile, hex_to_rgb
from ..logging import RunLogger
from ..run import Run
from .extract import count_frames


class AssemblyError(RuntimeError):
    """Assembly failed a verification check. Never worked around silently."""


def render_header(
    dst: Path, title: str, render: RenderConfig, profile: StyleProfile
) -> Path:
    """The branded header bar, as an image."""
    size = (render.width, render.header_height)
    img = Image.new("RGB", size, hex_to_rgb(profile.background_color))
    draw = ImageDraw.Draw(img)
    font = ImageFont.truetype(str(render.font_path()), render.header_font_size)

    text = title.upper()
    # Shrink to fit rather than overflow the bar.
    while draw.textlength(text, font=font) > render.width * 0.92 and font.size > 12:
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


def _panel_filter(render: RenderConfig) -> str:
    """Crop the central action band at the panel's aspect, then scale to it.

    Never stretches: the crop preserves aspect, the scale is then exact.
    """
    w, h = render.width, render.panel_height
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
    fps: int,
    frames: int,
    subtitles: Path | None,
    logger: RunLogger,
) -> Path:
    """The 9:16 stack: header bar, restyled on top, original below."""
    top_y = render.header_height
    bottom_y = render.header_height + render.panel_height + render.divider_height
    bg = "0x" + profile.background_color.lstrip("#")
    panel = _panel_filter(render)

    # The canvas is built with `pad` around the top panel rather than from a
    # `color` source. A colour generator is an INFINITE input: overlaying finite
    # panels onto it renders until the disk fills, which is exactly what it did
    # before this was changed. Padding a finite input keeps the whole graph
    # bounded by the restyled video's own length.
    graph = (
        f"[0:v]{panel},pad={render.width}:{render.height}:0:{top_y}:color={bg}[base];"
        f"[1:v]{panel}[bot];"
        f"[base][bot]overlay=x=0:y={bottom_y}[s2];"
        f"[s2][2:v]overlay=x=0:y=0:eof_action=repeat[hdr]"
    )
    if subtitles is not None:
        # libass burn-in, centred on the divider between the two panels.
        escaped = str(subtitles).replace("\\", "/").replace(":", "\\:")
        graph += f";[hdr]subtitles='{escaped}'[cap]"
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
        "-i", str(audio),
        "-filter_complex", graph,
        "-map", "[v]", "-map", "3:a:0",
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
        size=f"{render.width}x{render.height}",
        captions=bool(subtitles),
    )
    return dst


def verify_output(
    dst: Path,
    *,
    source_video: Path,
    extracted_audio_md5: str,
    expected_frames: int,
    render: RenderConfig,
    logger: RunLogger,
) -> dict:
    """Post-render acceptance checks. Any failure raises — never a warning.

    The audio is checked against BOTH the extracted track and the original
    source video, so a corrupted extraction cannot pass by matching itself.
    """
    video = ffmpeg.stream(dst, "video")
    audio = ffmpeg.stream(dst, "audio")

    width, height = int(video["width"]), int(video["height"])
    if (width, height) != (render.width, render.height):
        raise AssemblyError(
            f"output is {width}x{height}, expected {render.width}x{render.height}"
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
    }
    logger.info("assemble.verified", **result)
    return result


def assemble(run: Run, render: RenderConfig, profile: StyleProfile, source_audio_md5: str) -> dict:
    """Full assembly stage for a run."""
    p = run.paths
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
    render_header(p.header_png, run.manifest.clip_title, render, profile)
    build_comparison(
        restyled_video=p.restyled_video,
        source_video=Path(run.manifest.source_path),
        audio=p.audio,
        header=p.header_png,
        dst=p.final,
        render=render,
        profile=profile,
        fps=run.manifest.fps,
        frames=frames,
        subtitles=p.subtitles if p.subtitles.is_file() else None,
        logger=run.logger,
    )
    return verify_output(
        p.final,
        source_video=Path(run.manifest.source_path),
        extracted_audio_md5=source_audio_md5,
        expected_frames=frames,
        render=render,
        logger=run.logger,
    )
