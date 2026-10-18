"""The edit-operation seam: one place each operation's FFmpeg filters live.

An operation is three registrations, each one line beside its own code (sprint
6 plan, 2.1):

* its parameters, a strict model in `app/schemas/edit.py`;
* its rule, `register_rule` in `app/services/media_rules.py`, which the API
  runs before queueing and the worker runs again against the real file;
* its filters, `register_filter` in a module of its own in this package.

The worker applies operations in `OPERATION_ORDER`, whatever order a request
lists them, so no operation has to know which others ran before it. `crop.py`
and `scale.py` are the reference implementations: copy one to start.
"""

from collections.abc import Callable
from dataclasses import dataclass

from app.services.media_rules import OPERATION_ORDER, OPERATION_RULES, Frame, ordered


@dataclass(frozen=True, slots=True)
class FilterStep:
    """What one operation adds to the FFmpeg command.

    `video` goes into `-vf`, `audio` into `-af`, each in order. An operation
    that only changes the sound (speed's `atempo`) leaves `video` empty, and
    an operation that only changes the picture leaves `audio` empty.
    """

    video: tuple[str, ...] = ()
    audio: tuple[str, ...] = ()


# `build(params, frame)`: the params are already valid (schema and rule both
# ran); `frame` is the picture's size as it reaches this operation, when known.
EditFilter = Callable[[dict, Frame], FilterStep]

FILTERS: dict[str, EditFilter] = {}


def register_filter(name: str, build: EditFilter) -> EditFilter:
    """Add an operation's filters. Its rule must be registered already, so an
    operation the worker can run is never one the API cannot check."""
    if name not in OPERATION_RULES:
        raise ValueError(f"{name!r} has no rule in app.services.media_rules")
    if name in FILTERS:
        raise ValueError(f"{name!r} is registered twice")
    FILTERS[name] = build
    return build


# Operations from here on in OPERATION_ORDER work on the tone-mapped, 8-bit
# picture: a colour change then means the same on an HDR upload as on any
# other, and matches the preview the browser shows of the SDR result.
TONE_MAPPED_FROM = "adjust"


@dataclass(frozen=True, slots=True)
class FilterChain:
    """A request's filters, split where the worker inserts the tone-map."""

    before_tone_map: tuple[str, ...] = ()
    after_tone_map: tuple[str, ...] = ()
    audio: tuple[str, ...] = ()

    @property
    def video(self) -> tuple[str, ...]:
        return self.before_tone_map + self.after_tone_map


def filter_chain(operations: list[tuple[str, dict]], frame: Frame) -> FilterChain:
    """A request's filters, in `OPERATION_ORDER`.

    Operations without filters of their own -- clip is input seeking, convert
    is the output format -- are skipped. `frame` is the size the edit starts
    from; each operation's rule says what it becomes for the next one.
    """
    named = dict(operations)
    before: list[str] = []
    after: list[str] = []
    audio: list[str] = []
    for name in ordered(named):
        params = named[name]
        build = FILTERS.get(name)
        if build is not None:
            step = build(params, frame)
            mapped = OPERATION_ORDER.index(name) >= OPERATION_ORDER.index(TONE_MAPPED_FROM)
            (after if mapped else before).extend(step.video)
            audio.extend(step.audio)
        frame = OPERATION_RULES[name].check(params, frame)
    return FilterChain(tuple(before), tuple(after), tuple(audio))
