"""WAV stitching with the standard library only."""

import os
import wave


class AudioError(RuntimeError):
    pass


def trailing_silence(frames, params, threshold=0.02):
    """How many frames of near-silence sit at the end of a clip.

    Speech engines pad every utterance. Left alone that pad stacks on top of
    the gap between lines, so a film ends up mostly dead air.
    """
    if params.sampwidth != 2 or not frames:
        return 0
    step = params.nchannels * 2
    limit = int(32768 * threshold)
    end = len(frames) - (len(frames) % step)
    quiet = 0
    for i in range(end - step, -1, -step):
        sample = int.from_bytes(frames[i:i + 2], "little", signed=True)
        if abs(sample) > limit:
            break
        quiet += 1
    return quiet


def trim_tail(frames, params, keep_seconds=0.12):
    """Drop a clip's trailing pad, keeping a short natural tail."""
    quiet = trailing_silence(frames, params)
    keep = int(params.framerate * keep_seconds)
    drop = max(0, quiet - keep)
    if not drop:
        return frames
    step = params.nchannels * params.sampwidth
    return frames[:len(frames) - drop * step]


def concat_wavs(paths, dest, gap_seconds=0.35, trim=False):
    """Join WAV files into `dest`, inserting silence between them.

    With `trim`, each clip's trailing silence is cut back first so the pause
    between lines is the one asked for rather than the engine's pad plus it.
    """
    paths = [p for p in paths if p and os.path.exists(p)]
    if not paths:
        raise AudioError("Nothing to stitch together.")

    params = None
    with wave.open(paths[0], "rb") as w:
        params = w.getparams()

    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    gap = b""
    with wave.open(dest, "wb") as out:
        out.setparams(params)
        if gap_seconds > 0:
            frames = int(params.framerate * gap_seconds)
            gap = b"\x00" * (frames * params.sampwidth * params.nchannels)
        for i, p in enumerate(paths):
            with wave.open(p, "rb") as w:
                pw = w.getparams()
                if (pw.nchannels, pw.sampwidth, pw.framerate) != (
                        params.nchannels, params.sampwidth, params.framerate):
                    raise AudioError(
                        "%s is %dch/%dbit/%dHz but the first clip is %dch/%dbit/%dHz. "
                        "Make the workflow's save node use one consistent format."
                        % (os.path.basename(p), pw.nchannels, pw.sampwidth * 8, pw.framerate,
                           params.nchannels, params.sampwidth * 8, params.framerate))
                frames = w.readframes(w.getnframes())
                out.writeframes(trim_tail(frames, params) if trim else frames)
            if gap and i < len(paths) - 1:
                out.writeframes(gap)
    return dest


def duration(path):
    try:
        with wave.open(path, "rb") as w:
            return w.getnframes() / float(w.getframerate())
    except Exception:
        return 0.0
