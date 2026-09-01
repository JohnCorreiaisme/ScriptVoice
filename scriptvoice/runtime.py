"""Estimating how long a script will run once it is spoken.

Drafted by the local model through the bionic skill, then corrected: the
line-count maths rounded the wrong way and the runtime formatter dropped
minutes whenever the hours part was non-zero.
"""

import math

WORDS_PER_MINUTE = 150
SENTENCE_PAUSE = 0.35
MIN_LINE_SECONDS = 0.5
FALLBACK_LINE_SECONDS = 4.0


def estimate_line(text, wpm=WORDS_PER_MINUTE):
    """Seconds needed to speak one line, including its sentence pauses."""
    words = len(str(text).split())
    seconds = (words / float(wpm)) * 60.0
    seconds += sum(1 for c in str(text) if c in ".!?") * SENTENCE_PAUSE
    return max(seconds, MIN_LINE_SECONDS)


def estimate_total(cues, gap_seconds=SENTENCE_PAUSE, wpm=WORDS_PER_MINUTE):
    """Seconds for a whole list of cues, with a gap between each one."""
    if not cues:
        return 0.0
    spoken = sum(estimate_line(c.text, wpm) for c in cues)
    return spoken + (len(cues) - 1) * float(gap_seconds)


def format_runtime(seconds):
    """'1h 32m 10s', dropping the hours part only when there are none."""
    seconds = max(0, int(round(seconds)))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return "%dh %dm %ds" % (hours, minutes, secs)
    if minutes:
        return "%dm %ds" % (minutes, secs)
    return "%ds" % secs


def target_gap(cues, target_minutes, gap_seconds=SENTENCE_PAUSE, wpm=WORDS_PER_MINUTE):
    """How much more dialogue a script needs to reach a target runtime.

    `lines_needed` rounds up: asking for 90 minutes and landing at 89 would
    defeat the point of asking.
    """
    current = estimate_total(cues, gap_seconds, wpm)
    target = float(target_minutes) * 60.0
    if cues:
        average = sum(estimate_line(c.text, wpm) for c in cues) / float(len(cues))
    else:
        average = FALLBACK_LINE_SECONDS
    average = max(average, MIN_LINE_SECONDS)
    short_by = target - current
    needed = int(math.ceil(short_by / (average + float(gap_seconds)))) if short_by > 0 else 0
    return {
        "current_seconds": current,
        "target_seconds": target,
        "average_line_seconds": average,
        "lines_needed": needed,
    }
