"""Burned-in captions and a cinematic title for a finished cut.

`director captions` (and the `captions` stage of `director cut`, which runs
after `reframe`) takes the job's words on the shared timeline, keeps only
what the viewer hears (inside kept spans, no Muse-only words, no marked
fillers), groups them into short karaoke lines, writes `captions.srt` (for
Final Cut and Resolve) and `captions.ass` (the house style, through libass),
renders an optional serif title PNG for the first 3.5 s, and burns both
into `vertical.mp4` by default (into the horizontal master only with
`--captions-horizontal`). The un-captioned files are kept as `*.clean.mp4`,
so a re-run burns from clean, never from captioned.

House preset `house` (the default) is the owner's Reel style: Poppins
SemiBold sentence case, 2-4 words, white with a wide soft dark halo, the
spoken word highlighted in champagne (`#F3D9A4`) by colour only. Preset
`bold` is Montserrat-style ExtraBold caps, 3-4 words, for energetic promos.
The title hook is `--title "..."`, or `--title auto` for one Muse Spark
call over the first ~60 s of the cut's transcript; a mechanical check
rejects any auto title with words the speaker didn't say.

Built by Muse Code on muse-spark-1.3 from docs/muse-code/CAPTIONS-BRIEF.md.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path

from ghostcut.config import read_json, write_json
from ghostcut.ffmpeg_tools import (
    ToolingError,
    extract_media_stats,
    probe_media,
    run_command,
)

from . import angles as angles_mod
from . import cutlist

PRESET_HOUSE = "house"
PRESET_BOLD = "bold"
PRESETS = (PRESET_HOUSE, PRESET_BOLD)

HOUSE_FONT = "Poppins SemiBold"
BOLD_FONT = "Montserrat ExtraBold"
HOUSE_SIZE = 82
HOUSE_OUTLINE = 5
HOUSE_MARGIN_LR = 60
HOUSE_MARGIN_V = 780
HOUSE_BLUR = 7
ACTIVE_BGR = "&H00A4D9F3&"  # champagne #F3D9A4
WHITE_BGR = "&H00FFFFFF&"

HOUSE_MIN_WORDS = 2
HOUSE_MAX_WORDS = 4
BOLD_MIN_WORDS = 3
BOLD_MAX_WORDS = 4
MIN_LINE_S = 0.4
PAUSE_SPLIT_S = 0.6

TITLE_END_S = 3.5
TITLE_MAX_W = 960
TITLE_MAX_SIZE = 108
TITLE_MIN_SIZE = 40
TITLE_TOP_Y = 262
TITLE_GRADIENT_H = 820
TITLE_GRADIENT_ALPHA = 150
TITLE_WINDOW_S = 60.0
TITLE_MIN_WORDS = 5
TITLE_MAX_WORDS = 8
TITLE_MAX_OUTPUT_TOKENS = 4000  # Spark reasons before it answers: 300 ran out at 298 reasoning tokens
SPARK_TOKEN_RATES_USD_PER_1M = {
    "muse-spark-1.3": (1.25, 4.25),
    "muse-wonder-1.0": (4.00, 20.00),
}
DEFAULT_MODEL = "muse-spark-1.3"

SHIPPED_FONTS_DIR = Path(__file__).resolve().parents[2] / "assets" / "fonts"
SHIPPED_POPPINS = SHIPPED_FONTS_DIR / "Poppins-SemiBold.otf"

TITLE_DEVELOPER = (
    "You write the opening hook for a captioned vertical video. You are "
    "given the first minute of the cut's transcript with times. Propose "
    "one title of 5-8 words in sentence case, a question or a bold claim, "
    "taken from what the speaker actually says: every word of the title "
    "must appear in the transcript, preferably as one contiguous phrase "
    "the speaker said. Never invent words, names or claims. Give a "
    "one-sentence reason an editor would accept, naming where in the "
    "transcript the phrase comes from."
)


class CaptionsError(RuntimeError):
    """A captions step failed. The message always starts with 'captions: '."""


class TitleUnavailable(RuntimeError):
    """The title cannot be proposed or rendered (guestlist/PIL missing)."""


@dataclass(slots=True)
class CaptionWord:
    """One heard word on the output (video, 0-based) timeline."""

    text: str
    start_s: float
    end_s: float
    span: int = 0  # kept-span index on the source timeline


@dataclass(slots=True)
class CaptionLine:
    """2-4 heard words shown together, one highlight event per word."""

    id: int
    start_s: float
    end_s: float
    text: str
    words: list[CaptionWord] = field(default_factory=list)


# ------------------------------------------------------------ words (§2)


def filter_caption_words(words: list[angles_mod.TimedWord]
                         ) -> list[angles_mod.TimedWord]:
    """Drop Muse-only words and marked fillers. Pure.

    Captions show what the viewer hears, cleanly: words only Muse heard
    (`heard_by: "muse"`) are transcript repair, not audio, and words
    flagged as disfluencies are the ums and repeats the clean read drops.
    """
    return [word for word in words
            if word.heard_by != "muse" and not word.disfluency]


def _span_index_of(tighten: dict, t_s: float) -> int | None:
    """Kept-span index containing the source moment, else None. Pure."""
    for pos, span in enumerate(tighten.get("kept_spans", []) or []):
        if not isinstance(span, dict):
            continue
        try:
            start, end = float(span["start_s"]), float(span["end_s"])
        except (TypeError, ValueError, KeyError):
            continue
        if start <= t_s <= end:
            return pos
    return None


def map_words_to_output(words: list[angles_mod.TimedWord],
                        tighten: dict | None, origin_s: float,
                        duration_s: float | None) -> list[CaptionWord]:
    """Heard words on the output timeline. Pure.

    With a tighten payload, words map through `cutlist.source_to_output`
    (words in removed spans, or straddling a kept edge, drop out) and
    carry their kept-span index so lines never cross a join. Without one
    the mapping is identity. Either way times shift by `-origin_s` (the
    first shot's start, like `finish` and `export`) and words outside
    `[0, duration_s]` drop: captions show what the viewer hears.
    """
    out: list[CaptionWord] = []
    for word in words:
        start, end = float(word.start_s), float(word.end_s)
        span = 0
        if tighten is not None:
            mapped_start = cutlist.source_to_output(tighten, start)
            mapped_end = cutlist.source_to_output(tighten, end)
            if mapped_start is None or mapped_end is None:
                continue
            found = _span_index_of(tighten, start)
            if found is None:
                continue
            start, end, span = mapped_start, mapped_end, found
        start -= origin_s
        end -= origin_s
        if end <= 0:
            continue
        if duration_s is not None and start >= duration_s:
            continue
        if duration_s is not None:
            end = min(end, duration_s)
        if end <= start:
            continue
        out.append(CaptionWord(word.text, round(max(0.0, start), 3),
                               round(end, 3), span))
    out.sort(key=lambda item: (item.start_s, item.end_s))
    return out


def cut_times_from_shots(shots: list[dict], origin_s: float) -> list[float]:
    """Interior shot boundaries on the output timeline. Pure."""
    cuts: list[float] = []
    for shot in shots[1:]:
        if not isinstance(shot, dict):
            continue
        try:
            cuts.append(float(shot["start_s"]) - origin_s)
        except (TypeError, ValueError, KeyError):
            continue
    return sorted(cuts)


def origin_from_shots(shots: list[dict]) -> float:
    """The first shot's start, else 0.0. Pure."""
    if not shots or not isinstance(shots[0], dict):
        return 0.0
    try:
        return float(shots[0].get("start_s", 0.0))
    except (TypeError, ValueError):
        return 0.0


def duration_from_shots(shots: list[dict], origin_s: float) -> float | None:
    """The cut length from the shots, else None. Pure."""
    if not shots or not isinstance(shots[-1], dict):
        return None
    try:
        return max(0.0, float(shots[-1].get("end_s", 0.0)) - origin_s)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------ lines (§3)


def line_display_text(words: list[CaptionWord], *, caps: bool) -> str:
    """Sentence case straight from the words, trailing "." and "," off. Pure."""
    text = " ".join(word.text for word in words).strip().rstrip(".,").strip()
    return text.upper() if caps else text


def _chunk_words(words: list[CaptionWord], min_words: int, max_words: int
                 ) -> list[list[CaptionWord]]:
    """A hard-bounded run into lines of min-max words. Pure.

    Greedy max-word lines, except a remainder below the minimum joins
    back: a 5-word house run splits 2+3, never 4+1. A run that cannot
    split cleanly (5 words at 3-4) stays one over-long line rather than
    strand a 1-2 word tail; a lone word stays a lone line.
    """
    chunks: list[list[CaptionWord]] = []
    pos, total = 0, len(words)
    while total - pos > max_words:
        rest = total - pos - max_words
        if rest >= min_words:
            chunks.append(words[pos:pos + max_words])
            pos += max_words
            continue
        remaining = total - pos
        first = max(min_words, min(max_words, remaining // 2))
        second = remaining - first
        if min_words <= second <= max_words:
            chunks.append(words[pos:pos + first])
            chunks.append(words[pos + first:])
        else:
            chunks.append(words[pos:])
        pos = total
    if pos < total:
        chunks.append(words[pos:])
    return chunks


def group_lines(words: list[CaptionWord], cut_times: list[float], *,
                min_words: int = HOUSE_MIN_WORDS,
                max_words: int = HOUSE_MAX_WORDS,
                caps: bool = False) -> list[CaptionLine]:
    """Caption words to lines. Pure.

    A line never crosses a kept-span join, a cut point falling in an
    inter-word gap, or a pause over 0.6 s. Each line runs from its first
    word's start to its last word's end, extended to at least 0.4 s but
    never into the next line.
    """
    if not words:
        return []
    ordered = sorted(words, key=lambda item: (item.start_s, item.end_s))
    segments: list[list[CaptionWord]] = [[ordered[0]]]
    for prev, word in zip(ordered, ordered[1:]):
        gap = word.start_s - prev.end_s
        cut_between = any(prev.end_s + 1e-9 < cut < word.start_s - 1e-9
                          for cut in cut_times)
        if (word.span != prev.span or gap > PAUSE_SPLIT_S + 1e-9
                or cut_between):
            segments.append([word])
        else:
            segments[-1].append(word)
    lines: list[CaptionLine] = []
    for segment in segments:
        for chunk in _chunk_words(segment, min_words, max_words):
            lines.append(CaptionLine(
                len(lines), chunk[0].start_s, chunk[-1].end_s,
                line_display_text(chunk, caps=caps), list(chunk)))
    for pos, line in enumerate(lines):
        end = max(line.end_s, line.start_s + MIN_LINE_S)
        if pos + 1 < len(lines):
            end = min(end, lines[pos + 1].start_s)
        line.end_s = round(max(end, line.start_s + 0.01), 3)
    return lines


# --------------------------------------------------------- ASS / SRT (§4)


def style_line(preset: str, width: int, height: int) -> str:
    """The one ASS style, scaled to the frame. Pure.

    Vertical (`house`, 1080x1920) is the brief's style line verbatim:
    bottom-centre with MarginV 780, so the baseline sits about y = 1140.
    Horizontal is a lower third sized for landscape (1920x1080: 58 px,
    outline 4, margins 115/115/119).
    """
    if preset not in PRESETS:
        raise CaptionsError(f"captions: unknown preset {preset!r} "
                            f"(known: {', '.join(PRESETS)})")
    scale = height / 1920.0
    size = max(8, round(HOUSE_SIZE * scale))
    outline = max(1, round(HOUSE_OUTLINE * scale))
    margin_lr = round(HOUSE_MARGIN_LR * scale)
    margin_v = round(HOUSE_MARGIN_V * scale)
    if width > height:
        # Landscape is its own layout: scaling the 9:16 numbers gave 46 px captions at
        # mid-chest on the final take. A true lower third reads at ~5.4% of frame height.
        size = max(8, round(height * 0.054))
        outline = max(1, round(height * 0.0037))
        margin_lr = round(width * 0.06)
        margin_v = round(height * 0.11)
    font = HOUSE_FONT if preset == PRESET_HOUSE else BOLD_FONT
    return (f"Style: Cap,{font},{size},&H00FFFFFF,&H00FFFFFF,&H4C000000,"
            f"&H78000000,0,0,0,0,100,100,0,0,1,{outline},0,2,"
            f"{margin_lr},{margin_lr},{margin_v},1")


def blur_tag(height: int) -> str:
    """`{\\blur7}` vertical, scaled for horizontal. Pure."""
    return f"{{\\blur{max(1, round(HOUSE_BLUR * height / 1920.0))}}}"


def format_ass_time(t_s: float) -> str:
    """Seconds to H:MM:SS.cc. Pure."""
    total_cs = max(0, int(round(float(t_s) * 100)))
    hours, rem = divmod(total_cs, 360000)
    minutes, rem = divmod(rem, 6000)
    seconds, cs = divmod(rem, 100)
    return f"{hours}:{minutes:02d}:{seconds:02d}.{cs:02d}"


def format_srt_time(t_s: float) -> str:
    """Seconds to HH:MM:SS,mmm. Pure."""
    total_ms = max(0, int(round(float(t_s) * 1000)))
    hours, rem = divmod(total_ms, 3600000)
    minutes, rem = divmod(rem, 60000)
    seconds, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{ms:03d}"


def ass_escape(text: str) -> str:
    """Escape ASS override braces in caption text. Pure."""
    return text.replace("{", "\\{").replace("}", "\\}")


def _word_event_times(line: CaptionLine) -> list[tuple[float, float]]:
    """Each word's event span, clamped to the line. Pure.

    The last word's event holds to the line end (the 0.4 s floor), so no
    event overlaps its neighbour and the highlight never blanks mid-line.
    """
    spans: list[tuple[float, float]] = []
    for pos, word in enumerate(line.words):
        start = min(max(word.start_s, line.start_s), line.end_s)
        if pos == len(line.words) - 1:
            end = line.end_s
        else:
            end = min(max(word.end_s, start), line.end_s)
            following = line.words[pos + 1].start_s
            end = min(end, max(following, start))
        if end <= start:
            end = min(line.end_s, start + 0.08)
        if end <= start:
            start = max(line.start_s, end - 0.01)
        spans.append((round(start, 3), round(end, 3)))
    return spans


def build_ass(lines: list[CaptionLine], *, preset: str,
              width: int, height: int) -> str:
    """The full .ass: one style, one event per spoken word. Pure.

    Each event carries the line's full text with only the spoken word
    wrapped in champagne; colour changes, never size or weight, so the
    line cannot reflow and jitter.
    """
    blur = blur_tag(height)
    out = ["[Script Info]",
           "ScriptType: v4.00+",
           f"PlayResX: {width}",
           f"PlayResY: {height}",
           "WrapStyle: 0",
           "ScaledBorderAndShadow: yes",
           "YCbCr Matrix: TV.709",
           "",
           "[V4+ Styles]",
           "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
           "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
           "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
           "Alignment, MarginL, MarginR, MarginV, Encoding",
           style_line(preset, width, height),
           "",
           "[Events]",
           "Format: Layer, Start, End, Style, Name, MarginL, MarginR, "
           "MarginV, Effect, Text"]
    for line in lines:
        tokens = [ass_escape(token) for token in line.text.split(" ")]
        for pos, (start, end) in enumerate(_word_event_times(line)):
            shown = [f"{{\\c{ACTIVE_BGR}}}{token}{{\\c{WHITE_BGR}}}"
                     if i == pos else token
                     for i, token in enumerate(tokens)]
            out.append(f"Dialogue: 0,{format_ass_time(start)},"
                       f"{format_ass_time(end)},Cap,,0,0,0,,"
                       f"{blur}{' '.join(shown)}")
    return "\n".join(out) + "\n"


def build_srt(lines: list[CaptionLine]) -> str:
    """The full .srt: one plain-text entry per line. Pure."""
    out: list[str] = []
    for pos, line in enumerate(lines, 1):
        out.append(str(pos))
        out.append(f"{format_srt_time(line.start_s)} --> "
                   f"{format_srt_time(line.end_s)}")
        out.append(line.text)
        out.append("")
    return "\n".join(out)


def parse_srt_times(text: str) -> list[tuple[float, float]]:
    """SRT cue (start, end) seconds; garbage gives []. Pure."""
    found = re.findall(
        r"(\d+):(\d\d):(\d\d),(\d\d\d)\s*-->\s*"
        r"(\d+):(\d\d):(\d\d),(\d\d\d)", text or "")

    def _to_s(h: str, m: str, s: str, ms: str) -> float:
        return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0

    out: list[tuple[float, float]] = []
    for item in found:
        h1, m1, s1, ms1, h2, m2, s2, ms2 = item
        out.append((_to_s(h1, m1, s1, ms1), _to_s(h2, m2, s2, ms2)))
    return out


# ------------------------------------------------- the title hook (§6)


def normalize_tokens(text: str) -> list[str]:
    """Lower-case alnum tokens (the tighten measure). Pure."""
    return re.findall(r"[a-z0-9']+", (text or "").lower())


def spoken_token_set(words: list[CaptionWord]) -> set[str]:
    """Every token the cut speaks, for the title check. Pure."""
    found: set[str] = set()
    for word in words:
        found.update(normalize_tokens(word.text))
    return found


def check_title(text: str, spoken: set[str]) -> tuple[bool, str]:
    """The mechanical gate over an auto title. Pure.

    Returns (ok, reason): the title must be 5-8 words, all spoken. An
    explicit `--title` never passes through here; the editor may write
    what they like.
    """
    tokens = normalize_tokens(text)
    if not TITLE_MIN_WORDS <= len(tokens) <= TITLE_MAX_WORDS:
        return False, f"title_length:{len(tokens)}"
    unknown = [token for token in tokens if token not in spoken]
    if unknown:
        return False, f"title_unspoken:{','.join(sorted(set(unknown)))}"
    return True, ""


def title_transcript(lines: list[CaptionLine]) -> str:
    """The first ~60 s of the cut as timed lines, for the prompt. Pure."""
    shown = [f"[{line.start_s:.1f}s] {line.text}" for line in lines
             if line.start_s < TITLE_WINDOW_S]
    return "\n".join(shown)


def title_schema() -> dict:
    """The strict title_hook schema. Pure."""
    return {
        "type": "object", "additionalProperties": False,
        "required": ["title", "reason"],
        "properties": {
            "title": {"type": "string"},
            "reason": {"type": "string"},
        },
    }


def title_prompt(transcript: str) -> str:
    """The hook request over the timed transcript. Pure."""
    return (
        "Propose the opening hook for this cut. Reply with the title "
        "(5-8 words, sentence case, a question or a bold claim, every "
        "word taken from the transcript below, preferably one contiguous "
        "phrase the speaker said) and a one-sentence reason naming "
        "where the phrase comes from.\n\n"
        f"Transcript (first minute):\n{transcript}"
    )


def estimate_title_usd(prompt: str, model: str
                       ) -> tuple[float, str | None]:
    """(Pre-call estimate, default-rates note or None). Pure."""
    rates = SPARK_TOKEN_RATES_USD_PER_1M.get(model)
    note = None
    if rates is None:
        rates = SPARK_TOKEN_RATES_USD_PER_1M[DEFAULT_MODEL]
        note = f"captions_estimate_default_rates:{model}"
    in_rate, out_rate = rates
    estimate = (len(TITLE_DEVELOPER + prompt) / 4 * in_rate / 1e6
                + TITLE_MAX_OUTPUT_TOKENS * out_rate / 1e6)
    return estimate, note


def propose_title_with_muse(*, transcript: str, spoken: set[str],
                            model: str, max_usd: float
                            ) -> tuple[str | None, str, float, bool,
                                       list[str]]:
    """One Spark call for the hook. Returns (title, reason, usd, cached, warnings).

    Muse proposes; `check_title` decides. Anything unusable — missing
    client, over budget, a failed call, a bad reply, a rejected title —
    degrades to no title with a warning, never an exception: captions
    still burn. Raises TitleUnavailable only when guestlist is missing
    (the caller degrades that too).
    """
    try:
        from guestlist.meta import cache, spark
    except ImportError as error:
        raise TitleUnavailable(str(error)) from error
    warnings: list[str] = []
    prompt = title_prompt(transcript)
    schema = title_schema()
    key = cache.key("captions", model, TITLE_DEVELOPER, prompt, schema,
                    "medium")
    hit = cache.get("spark", key)
    if hit:
        data, spend, cached = hit["data"], 0.0, True
    else:
        estimate, note = estimate_title_usd(prompt, model)
        if note is not None:
            warnings.append(note)
        if estimate > max_usd:
            warnings.append("title_unavailable:exceeds --max-usd")
            return None, "", 0.0, False, warnings
        try:
            cache.guard(estimate)
            reply = spark._structured(
                key, [{"type": "input_text", "text": prompt}],
                schema=schema, name="title_hook",
                developer=TITLE_DEVELOPER, model=model, effort="medium",
                max_output_tokens=TITLE_MAX_OUTPUT_TOKENS,
                record={}, detail={"job": "captions"})
        except Exception as error:
            cause = f"{type(error).__name__}: {error}"[:160]
            warnings.append(f"title_unavailable:{cause}")
            return None, "", 0.0, False, warnings
        data = reply.data
        try:
            spend = round(float(reply.usd or 0.0), 4)
        except (TypeError, ValueError):
            spend = 0.0
        cached = False
    if not isinstance(data, dict):
        warnings.append("title_unavailable:invalid reply")
        return None, "", spend, cached, warnings
    title = str(data.get("title", "")).strip()
    reason = str(data.get("reason", "")).strip()
    ok, why = check_title(title, spoken)
    if not ok:
        warnings.append(f"title_rejected:{why}")
        return None, reason, spend, cached, warnings
    return title, reason, spend, cached, warnings


# ------------------------------------------------- title rendering (§6)


def resolve_title_font(fonts_dir: Path | None) -> tuple[Path, str]:
    """The title face: Playfair, else Georgia Bold, else shipped Poppins. Pure-ish.

    Playfair Display SemiBold is never shipped (its licence reserves the
    name); it is searched in `--fonts-dir`, `~/Library/Fonts` and
    `/Library/Fonts` (plus the macOS system font folders, where Georgia
    lives). Reads directory listings only; returns (path, label).
    """
    search: list[Path] = []
    if fonts_dir is not None:
        search.append(Path(fonts_dir))
    search.append(Path.home() / "Library" / "Fonts")
    search.append(Path("/Library/Fonts"))
    search.append(Path("/System/Library/Fonts"))
    search.append(Path("/System/Library/Fonts/Supplemental"))
    candidates: list[Path] = []
    for directory in search:
        try:
            entries = sorted(directory.iterdir())
        except OSError:
            continue
        for entry in entries:
            if entry.suffix.lower() in (".ttf", ".otf", ".ttc"):
                candidates.append(entry)
    playfairs = [path for path in candidates
                 if "playfair" in path.name.lower()]
    if playfairs:
        semi = [path for path in playfairs
                if any(tag in path.name.lower()
                       for tag in ("semibold", "semi-bold", "600", "demi"))]
        return (semi[0] if semi else playfairs[0]), "playfair"
    georgias = [path for path in candidates
                if "georgia" in path.name.lower()
                and "bold" in path.name.lower()]
    if georgias:
        upright = [path for path in georgias
                   if "italic" not in path.name.lower()]
        return (upright[0] if upright else georgias[0]), "georgia"
    return SHIPPED_POPPINS, "poppins"


def _pil_available() -> bool:
    try:
        import PIL  # noqa: F401
    except ImportError:
        return False
    return True


def fit_title_lines(text: str, font_path: Path, *, max_width: int,
                    max_size: int, min_size: int = TITLE_MIN_SIZE
                    ) -> tuple[list[str], int]:
    """The title as one or two lines at the largest fitting size.

    Needs Pillow (the `director` extra); raises TitleUnavailable without
    it. Tries sentence-wide, then every two-line split, shrinking until
    both lines fit; a single over-wide word keeps the floor size.
    """
    try:
        from PIL import ImageFont
    except ImportError as error:
        raise TitleUnavailable(f"Pillow is not installed: {error}") from error
    words = text.split()
    if not words:
        raise CaptionsError("captions: refusing an empty title")
    for size in range(max_size, min_size - 1, -2):
        font = ImageFont.truetype(str(font_path), size)
        widths = [font.getlength(word) for word in words]
        if len(words) == 1:
            if widths[0] <= max_width:
                return words, size
            continue
        one_line = sum(widths) + font.getlength(" ") * (len(words) - 1)
        if one_line <= max_width:
            return [" ".join(words)], size
        space = font.getlength(" ")
        best: tuple[float, int] | None = None
        for split in range(1, len(words)):
            left = sum(widths[:split]) + space * (split - 1)
            right = sum(widths[split:]) + space * (len(words) - split - 1)
            if left <= max_width and right <= max_width:
                width = max(left, right)
                if best is None or width < best[0]:
                    best = (width, split)
        if best is not None:
            _, split = best
            return [" ".join(words[:split]), " ".join(words[split:])], size
    floor = ImageFont.truetype(str(font_path), min_size)
    if len(words) == 1 or (
            sum(floor.getlength(w) for w in words)
            + floor.getlength(" ") * (len(words) - 1) <= max_width):
        return [" ".join(words)], min_size
    split = len(words) // 2
    return [" ".join(words[:split]), " ".join(words[split:])], min_size


def render_title_png(text: str, png_path: Path, *, width: int, height: int,
                     font_path: Path) -> dict:
    """The serif title over a soft top gradient as an RGBA PNG.

    Up to 108 px, auto-fit to 960 px wide, at most two lines starting at
    y = 262 on 9:16 (each axis scaled for 16:9). Needs Pillow; returns
    {"lines", "size", "font_path"} for captions.json.
    """
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError as error:
        raise TitleUnavailable(f"Pillow is not installed: {error}") from error
    scale_h = height / 1920.0
    max_width = round(TITLE_MAX_W * width / 1080.0)
    max_size = max(12, round(TITLE_MAX_SIZE * scale_h))
    floor = max(10, round(TITLE_MIN_SIZE * scale_h))
    lines, size = fit_title_lines(text, font_path, max_width=max_width,
                                  max_size=max_size, min_size=floor)
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    gradient_h = min(height, max(1, round(TITLE_GRADIENT_H * scale_h)))
    mask = Image.new("L", (1, gradient_h))
    for y in range(gradient_h):
        fade = 1.0 - y / gradient_h
        mask.putpixel((0, y), round(TITLE_GRADIENT_ALPHA * fade * fade))
    img.paste(Image.new("RGBA", (width, gradient_h), (0, 0, 0, 255)),
              (0, 0), mask.resize((width, gradient_h)))
    font = ImageFont.truetype(str(font_path), size)
    ascent, descent = font.getmetrics()
    step = ascent + descent + max(2, round(size * 0.12))
    draw = ImageDraw.Draw(img)
    top = round(TITLE_TOP_Y * scale_h)
    for pos, line in enumerate(lines):
        draw.text((width / 2, top + pos * step), line, font=font,
                  fill=(255, 255, 255, 255), anchor="ma")
    png_path = Path(png_path)
    png_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(png_path)
    return {"lines": list(lines), "size": size,
            "font_path": str(font_path)}


# ------------------------------------------------- fonts and burn (§5, §7)


def stage_fonts_dir(out_dir: Path, fonts_dir: Path | None) -> Path:
    """The fontsdir libass reads: shipped fonts, plus the editor's own.

    Always a job-local `caption-fonts` directory staging the shipped
    Poppins beside the editor's `--fonts-dir` folder (symlinks, copied
    when linking fails), since `subtitles` takes one fontsdir — and
    since the shipped folder also holds the licence text, which libass
    would otherwise try to open as a font. A fresh clone looks the same.
    """
    staged = Path(out_dir) / "caption-fonts"
    staged.mkdir(parents=True, exist_ok=True)
    wanted: dict[str, tuple[str, Path]] = {
        "poppins-semibold.otf": ("poppins-semibold.otf", SHIPPED_POPPINS)}
    if fonts_dir is not None:
        try:
            entries = sorted(Path(fonts_dir).iterdir())
        except OSError as error:
            raise CaptionsError(
                f"captions: cannot read --fonts-dir {fonts_dir}: {error}"
            ) from error
        for entry in entries:
            if entry.is_file() and entry.suffix.lower() in (".ttf", ".otf",
                                                            ".ttc"):
                key = entry.name.lower()
                if key not in wanted:
                    wanted[key] = (entry.name, entry)
    for _key, (name, source) in wanted.items():
        link = staged / name
        if link.exists() or link.is_symlink():
            continue
        try:
            link.symlink_to(source)
        except OSError:
            import shutil

            try:
                shutil.copyfile(source, link)
            except OSError:
                continue
    return staged


def _escape_filter_path(path: Path) -> str:
    text = str(path)
    for char in ("\\", ":", ",", "'"):
        text = text.replace(char, "\\" + char)
    return text


def burn_command(*, clean_path: Path, ass_path: Path, fonts_dir: Path,
                 out_path: Path, title_png: Path | None) -> list[str]:
    """One ffmpeg burn: title overlay, then subtitles. Pure.

    The title PNG overlays first (0-3.5 s) and the subtitles filter runs
    after it, so captions sit above the title. Video re-encodes, audio
    copies.
    """
    subs = (f"subtitles=filename={_escape_filter_path(Path(ass_path))}"
            f":fontsdir={_escape_filter_path(Path(fonts_dir))}")
    encode = ["-c:v", "libx264", "-preset", "medium", "-crf", "16",
              "-c:a", "copy", "-movflags", "+faststart"]
    if title_png is None:
        return ["ffmpeg", "-v", "error", "-y", "-i", str(clean_path),
                "-map", "0:v:0", "-map", "0:a?", "-vf", subs,
                *encode, str(out_path)]
    graph = (f"[0:v][1:v]overlay=0:0:enable='between(t,0,{TITLE_END_S})'"
             f"[v0];[v0]{subs}[v]")
    return ["ffmpeg", "-v", "error", "-y", "-i", str(clean_path),
            "-i", str(title_png), "-filter_complex", graph,
            "-map", "[v]", "-map", "0:a?", *encode, str(out_path)]


# ------------------------------------------------------------ orchestrator


def _probe_size(path: Path) -> tuple[int, int] | None:
    """(width, height) of a video, or None when it will not probe."""
    try:
        stats = extract_media_stats(probe_media(Path(path)))
    except (ToolingError, OSError, ValueError):
        return None
    try:
        width, height = int(stats.get("width") or 0), int(stats.get("height") or 0)
    except (TypeError, ValueError):
        return None
    return (width, height) if width > 0 and height > 0 else None


def _clean_source(target: Path) -> Path | None:
    """The un-captioned source for a burn target, else None.

    A re-run burns from the kept `*.clean.mp4`, never from captioned;
    the first run renames the target aside (keeping an existing clean).
    """
    clean = target.with_name(f"{target.stem}.clean{target.suffix}")
    if clean.exists():
        return clean
    if target.exists():
        target.rename(clean)
        return clean
    return None


def run(*, words_path: Path, angles_path: Path,
        tighten_path: Path | None, out_dir: Path,
        preset: str = PRESET_HOUSE,
        title: str | None = None, no_title: bool = False,
        fonts_dir: Path | None = None, burn_horizontal: bool = False,
        model: str = DEFAULT_MODEL, max_usd: float = 5.0,
        no_muse: bool = False) -> dict:
    """Words to SRT/ASS/title burns in a job directory.

    Writes `captions.srt`, `captions.ass` (vertical house geometry),
    `captions.json` and, when burning horizontal, `captions-horizontal.ass`
    (the same style scaled to 1920x1080); burns `vertical.mp4` by default
    plus `cut.mp4` with `burn_horizontal`, keeping `*.clean.mp4`.
    Returns the captions.json payload (also written).
    """
    if preset not in PRESETS:
        raise CaptionsError(f"captions: unknown preset {preset!r} "
                            f"(known: {', '.join(PRESETS)})")
    if max_usd < 0:
        raise CaptionsError("captions: --max-usd must be >= 0")
    out_dir = Path(out_dir)
    words_path, angles_path = Path(words_path), Path(angles_path)
    if fonts_dir is not None:
        fonts_dir = Path(fonts_dir)
    warnings: list[str] = []

    words = angles_mod.load_timed_words(words_path)
    heard = filter_caption_words(words)
    angles_payload = read_json(angles_path)
    if not isinstance(angles_payload, dict) \
            or not isinstance(angles_payload.get("shots"), list):
        raise CaptionsError(f"captions: not an angles.json: {angles_path}")
    shots = angles_payload["shots"]
    origin = origin_from_shots(shots)
    duration = duration_from_shots(shots, origin)
    tighten: dict | None = None
    if tighten_path is not None:
        tighten = cutlist.load_tighten(Path(tighten_path))
    caption_words = map_words_to_output(heard, tighten, origin, duration)
    cuts = cut_times_from_shots(shots, origin)
    if preset == PRESET_BOLD:
        lines = group_lines(caption_words, cuts, min_words=BOLD_MIN_WORDS,
                            max_words=BOLD_MAX_WORDS, caps=True)
    else:
        lines = group_lines(caption_words, cuts)

    # The title: explicit text, one Spark proposal, or none. Every
    # unavailable path degrades to captions without a title.
    title_text: str | None = None
    title_reason = ""
    title_source: str | None = None
    title_spend = 0.0
    title_info: dict | None = None
    title_png: Path | None = None
    want_title = title is not None and not no_title
    if no_title and title is not None:
        warnings.append("captions_title_disabled:--no-title")
    if want_title and not _pil_available():
        warnings.append("title_unavailable:Pillow is not installed")
        want_title = False
    if want_title and title == "auto" and no_muse:
        warnings.append("title_unavailable:--no-muse")
        want_title = False
    if want_title and title == "auto":
        assert title is not None
        try:
            found, reason, spend, _cached, notes = propose_title_with_muse(
                transcript=title_transcript(lines),
                spoken=spoken_token_set(caption_words),
                model=model, max_usd=max_usd)
        except TitleUnavailable as error:
            warnings.append(f"title_unavailable:{error}")
        else:
            warnings.extend(notes)
            title_spend = spend
            if found is not None:
                title_text, title_reason = found, reason
                title_source = "auto"
    elif want_title and title is not None:
        title_text = title.strip()
        title_source = "explicit"
        if not title_text:
            raise CaptionsError("captions: refusing an empty --title")
    if title_text is not None:
        font_path, font_label = resolve_title_font(fonts_dir)
        if not font_path.exists():
            warnings.append(f"title_unavailable:missing font {font_path}")
            title_text, title_source = None, None
        else:
            try:
                rendered = render_title_png(
                    title_text, out_dir / "title.png", width=1080,
                    height=1920, font_path=font_path)
            except (TitleUnavailable, OSError) as error:
                warnings.append(f"title_unavailable:{error}")
                title_text, title_source = None, None
            else:
                title_png = out_dir / "title.png"
                title_info = {"text": title_text, "reason": title_reason,
                              "source": title_source, "font": font_label,
                              "file": "title.png", **rendered}

    vertical_ass = build_ass(lines, preset=preset, width=1080, height=1920)
    (out_dir / "captions.srt").write_text(build_srt(lines), encoding="utf-8")
    (out_dir / "captions.ass").write_text(vertical_ass, encoding="utf-8")
    horizontal_ass_name: str | None = None

    vertical = out_dir / "vertical.mp4"
    horizontal = out_dir / "cut.mp4"
    targets: list[tuple[Path, str]] = []
    if vertical.exists() or (vertical.with_name(
            f"{vertical.stem}.clean{vertical.suffix}").exists()):
        targets.append((vertical, "captions.ass"))
    elif horizontal.exists() or horizontal.with_name(
            f"{horizontal.stem}.clean{horizontal.suffix}").exists():
        # A --no-vertical job has nothing else to burn into.
        warnings.append("captions_no_vertical:burning horizontal")
        targets.append((horizontal, "captions-horizontal.ass"))
    else:
        warnings.append("captions_no_video:nothing to burn into")
    if burn_horizontal and all(path != horizontal for path, _ in targets) \
            and (horizontal.exists() or horizontal.with_name(
                f"{horizontal.stem}.clean{horizontal.suffix}").exists()):
        targets.append((horizontal, "captions-horizontal.ass"))
    if any(name == "captions-horizontal.ass" for _, name in targets):
        (out_dir / "captions-horizontal.ass").write_text(
            build_ass(lines, preset=preset, width=1920, height=1080),
            encoding="utf-8")
        horizontal_ass_name = "captions-horizontal.ass"

    fontsdir = stage_fonts_dir(out_dir, fonts_dir)
    burned: list[str] = []
    clean_kept: list[str] = []
    if not lines:
        warnings.append("captions_no_lines:nothing heard; nothing burned")
    for target, ass_name in targets:
        if not lines:
            break
        ass_path = out_dir / ass_name
        clean = _clean_source(target)
        if clean is None:
            warnings.append(f"captions_missing:{target.name}")
            continue
        per_target_title = title_png
        if title_text is not None and target == horizontal:
            # The horizontal master gets its own scaled title card.
            font_path = Path(str((title_info or {}).get(
                "font_path") or SHIPPED_POPPINS))
            try:
                render_title_png(title_text, out_dir / "title-1920x1080.png",
                                 width=1920, height=1080,
                                 font_path=font_path)
            except (TitleUnavailable, OSError) as error:
                warnings.append(f"title_unavailable:{error}")
                per_target_title = None
            else:
                per_target_title = out_dir / "title-1920x1080.png"
        size = _probe_size(clean)
        if size is not None and size != ((1080, 1920)
                                        if target == vertical else (1920, 1080)):
            warnings.append(f"captions_size:{target.name} is "
                            f"{size[0]}x{size[1]}")
        run_command(burn_command(clean_path=clean, ass_path=ass_path,
                                 fonts_dir=fontsdir, out_path=target,
                                 title_png=per_target_title))
        burned.append(target.name)
        clean_kept.append(clean.name)

    payload = {
        "version": 1,
        "preset": preset,
        "words_file": str(words_path),
        "angles_file": str(angles_path),
        "tighten_file": str(tighten_path) if tighten_path is not None else None,
        "words_in": len(words),
        "words_kept": len(caption_words),
        "lines_n": len(lines),
        "lines": [{"id": line.id, "start_s": round(line.start_s, 3),
                   "end_s": round(line.end_s, 3), "text": line.text,
                   "words": [{"text": word.text,
                              "start_s": round(word.start_s, 3),
                              "end_s": round(word.end_s, 3)}
                             for word in line.words]}
                  for line in lines],
        "duration_out_s": round(duration, 3) if duration is not None else None,
        "srt": "captions.srt",
        "ass": "captions.ass",
        "ass_horizontal": horizontal_ass_name,
        "title": title_info,
        "model": model,
        "spend_usd": round(title_spend, 4),
        "burned": burned,
        "clean": clean_kept,
        "warnings": warnings,
    }
    write_json(out_dir / "captions.json", payload)
    return payload
