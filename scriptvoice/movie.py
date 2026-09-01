"""Assembling the finished movie: one segment per spoken line, then a concat.

Assembly uses ffmpeg when it can be found. Without it, the audio, the shots and
an edit list (movie.edl.json) are still written, so nothing is lost - the project
can be assembled later, or opened in any editor.
"""

import json
import os
import shutil
import subprocess
import sys

from . import audio

VIDEO_EXT = (".mp4", ".webm", ".mkv", ".mov", ".gif", ".webp")
FRAME_SIZE = (1280, 720)


def find_ffmpeg(extra_hints=()):
    """Look on PATH, then in the usual ComfyUI / imageio places."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    hints = list(extra_hints) + [
        r"C:\ComfyUI", r"C:\ComfyUI_windows_portable",
        os.path.expanduser(r"~\ComfyUI"),
        os.path.expanduser(r"~\Documents\ComfyUI"),
        os.path.expanduser(r"~\AppData\Local\Programs\@comfyorgcomfyui-electron"),
    ]
    exe = "ffmpeg.exe" if sys.platform.startswith("win") else "ffmpeg"
    for root in hints:
        if not root or not os.path.isdir(root):
            continue
        for sub in ("", "ffmpeg", "bin", os.path.join("python_embeded", "Scripts"),
                    os.path.join("venv", "Scripts")):
            cand = os.path.join(root, sub, exe)
            if os.path.exists(cand):
                return cand
    return ""


def have_pyav():
    """PyAV carries its own ffmpeg libraries, so no binary need be installed."""
    try:
        import av                                  # noqa: F401
        return True
    except Exception:
        return False


def assemble_pyav(entries, dest, gap_seconds=0.35, size=FRAME_SIZE, fps=25, log=None,
                  cancel=None):
    """Cut the movie with PyAV: each line's picture held for the length of its clip.

    Same result as the ffmpeg path without shelling out - one video stream of
    still frames, one audio stream, muxed straight to mp4.
    """
    import av

    log = log or (lambda m: None)
    usable = [e for e in entries if e.get("audio") and os.path.exists(e["audio"])]
    if not usable:
        raise RuntimeError("No dialogue to cut - render the audio first.")

    rate = _first_audio_rate(usable)
    container = av.open(dest, "w")
    try:
        vs = container.add_stream("libx264", rate=fps)
        vs.width, vs.height = size
        vs.pix_fmt = "yuv420p"
        vs.options = {"crf": "20", "preset": "veryfast"}
        aud = container.add_stream("aac", rate=rate, layout="mono")

        frame_index = 0
        resampler = av.audio.resampler.AudioResampler(format="fltp", layout="mono", rate=rate)
        # AAC only accepts frames of exactly frame_size samples, so decoded
        # audio goes through a fifo rather than straight into the encoder.
        fifo = av.audio.fifo.AudioFifo()
        chunk = aud.frame_size or 1024

        def drain(partial=False):
            while True:
                block = fifo.read(chunk, partial=partial) if not partial else fifo.read_many(chunk, partial=True)
                if block is None:
                    return
                for one in (block if isinstance(block, list) else [block]):
                    for packet in aud.encode(one):
                        container.mux(packet)
                if partial:
                    return

        for n, e in enumerate(usable):
            if cancel is not None and cancel():
                raise RuntimeError("Cancelled")
            seconds = audio.duration(e["audio"]) + max(0.0, float(gap_seconds))
            picture = _still(e.get("image", ""), size)
            for _ in range(max(1, int(round(seconds * fps)))):
                picture.pts = frame_index
                frame_index += 1
                for packet in vs.encode(picture):
                    container.mux(packet)
            for frame in _audio_frames(e["audio"], resampler, seconds, rate):
                fifo.write(frame)
            drain()
            if n % 25 == 0:
                log("cut %d/%d" % (n + 1, len(usable)))

        drain(partial=True)
        for packet in vs.encode():
            container.mux(packet)
        for packet in aud.encode():
            container.mux(packet)
    finally:
        container.close()
    return dest


def _first_audio_rate(entries):
    import wave
    for e in entries:
        try:
            with wave.open(e["audio"]) as w:
                return w.getframerate()
        except Exception:
            continue
    return 22050


def _still(path, size):
    """One video frame: the shot, letterboxed, or black when there is no shot."""
    import av
    if path and os.path.exists(path) and os.path.splitext(path)[1].lower() not in VIDEO_EXT:
        with av.open(path) as src:
            for frame in src.decode(video=0):
                # Rebuilt from its pixels rather than reformatted in place: a
                # decoded still carries the time base of the file it came from,
                # which makes every pts we set invalid, and time_base cannot be
                # cleared on a frame. A fresh frame simply has none.
                pixels = frame.to_ndarray(format="rgb24")
                return av.VideoFrame.from_ndarray(pixels, format="rgb24").reformat(
                    width=size[0], height=size[1], format="yuv420p")
    return black_frame(size)


def black_frame(size):
    """A genuinely black frame.

    av.VideoFrame(w, h, fmt) allocates without initialising, so using one
    directly puts uninitialised memory - static - on screen.
    """
    import av
    import numpy as np
    return av.VideoFrame.from_ndarray(
        np.zeros((size[1], size[0], 3), dtype="uint8"), format="rgb24").reformat(
            format="yuv420p")


def _audio_frames(path, resampler, seconds=0.0, rate=22050):
    """The clip's audio, then silence to fill the gap the picture holds for."""
    import av
    import numpy as np

    spoken = 0
    with av.open(path) as src:
        for frame in src.decode(audio=0):
            for out in resampler.resample(frame):
                spoken += out.samples
                out.pts = None
                yield out
    pad = int(round(seconds * rate)) - spoken
    while pad > 0:
        block = min(pad, 1024)
        silence = av.AudioFrame.from_ndarray(
            np.zeros((1, block), dtype="float32"), format="fltp", layout="mono")
        silence.rate = rate
        silence.pts = None
        yield silence
        pad -= block


def write_edl(path, entries, full_audio, fps=25):
    """A plain edit list: what plays when, in order. Readable by humans and code."""
    t = 0.0
    rows = []
    for e in entries:
        dur = float(e.get("seconds") or 0.0)
        rows.append({
            "index": e.get("index"),
            "speaker": e.get("speaker"),
            "line": e.get("text"),
            "shot": e.get("shot", ""),
            "image": e.get("image", ""),
            "audio": e.get("audio", ""),
            "start": round(t, 3),
            "duration": round(dur, 3),
        })
        t += dur
    data = {"fps": fps, "total_seconds": round(t, 3), "audio": full_audio, "shots": rows}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return path


def assemble(ffmpeg, entries, dest, gap_seconds=0.35, size=FRAME_SIZE, fps=25, log=None,
             cancel=None):
    """Build one segment per line (visual + its own audio), then concatenate.

    Returns the finished file path. Raises RuntimeError with ffmpeg's own message
    if a step fails.
    """
    log = log or (lambda m: None)
    work = os.path.join(os.path.dirname(dest), "_segments")
    os.makedirs(work, exist_ok=True)
    segments = []

    for e in entries:
        if cancel is not None and cancel():
            raise RuntimeError("Cancelled")
        clip = e.get("audio", "")
        visual = e.get("image", "")
        if not clip or not os.path.exists(clip):
            continue
        dur = audio.duration(clip) + max(0.0, float(gap_seconds))
        seg = os.path.join(work, "seg_%04d.mp4" % (e.get("index", 0) + 1))
        vf = ("scale=%d:%d:force_original_aspect_ratio=decrease,"
              "pad=%d:%d:(ow-iw)/2:(oh-ih)/2,setsar=1,format=yuv420p"
              % (size[0], size[1], size[0], size[1]))

        if visual and os.path.exists(visual):
            if os.path.splitext(visual)[1].lower() in VIDEO_EXT:
                cmd = [ffmpeg, "-y", "-stream_loop", "-1", "-i", visual, "-i", clip,
                       "-t", "%.3f" % dur, "-vf", vf, "-r", str(fps)]
            else:
                cmd = [ffmpeg, "-y", "-loop", "1", "-i", visual, "-i", clip,
                       "-t", "%.3f" % dur, "-vf", vf, "-r", str(fps)]
        else:                                   # audio only: hold on black
            cmd = [ffmpeg, "-y", "-f", "lavfi", "-i",
                   "color=c=black:s=%dx%d:r=%d" % (size[0], size[1], fps),
                   "-i", clip, "-t", "%.3f" % dur]

        cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                "-c:a", "aac", "-b:a", "192k", "-ar", "44100", "-ac", "2",
                "-shortest", "-movflags", "+faststart", seg]
        _run(cmd, "segment %d" % (e.get("index", 0) + 1))
        segments.append(seg)
        log("segment %d/%d" % (len(segments), len(entries)))

    if not segments:
        raise RuntimeError("No segments to assemble - render the dialogue first.")

    list_path = os.path.join(work, "segments.txt")
    with open(list_path, "w", encoding="utf-8") as f:
        for s in segments:
            f.write("file '%s'\n" % s.replace("\\", "/").replace("'", "'\\''"))
    _run([ffmpeg, "-y", "-f", "concat", "-safe", "0", "-i", list_path,
          "-c", "copy", "-movflags", "+faststart", dest], "final concat")
    return dest


def _run(cmd, what):
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           creationflags=_no_window())
    except OSError as e:
        raise RuntimeError("Couldn't start ffmpeg for %s: %s" % (what, e))
    if p.returncode != 0:
        tail = (p.stdout or b"").decode("utf-8", "replace").strip().splitlines()[-8:]
        raise RuntimeError("ffmpeg failed on %s:\n%s" % (what, "\n".join(tail)))


def _no_window():
    return 0x08000000 if sys.platform.startswith("win") else 0     # CREATE_NO_WINDOW
