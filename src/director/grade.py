"""Per-camera grading and matching: every camera gets a conversion to Rec.709.

Sony cameras record S-Log3 (flat log, wide gamut), the iPhone records HLG
(BT.2020), and sometimes a camera already records Rec.709. Cutting between
them ungraded jumps from grey log to bright phone, so each camera first gets
a 33-point .cube converting it to Rec.709 (generated here in pure Python),
then the cameras are matched to a reference camera on the speaker's skin:
statistics measured inside the face box at the shared moments, a per-channel
gain as before plus contrast from the spreads, baked into the same cube.
Without a usable face on both cameras the match falls back to whole-frame
centre measurements with a warning. `ghostcut angles --grade grade.json`
applies each shot's cube.
"""
from __future__ import annotations

import json
import math
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ghostcut.config import read_json, write_json
from ghostcut.ffmpeg_tools import ToolingError, run_command

LUT_SIZE = 33
MEASURE_W, MEASURE_H = 64, 36
SKIN_W, SKIN_H = 480, 270
SKIN_INSET = 0.2  # the match measures the central 60% of the face box
MAX_MOMENTS = 8
CHECK_MOMENTS = 3
GAIN_MIN, GAIN_MAX, OFFSET_MAX = 0.7, 1.4, 0.10
WB_MIN, WB_MAX = 0.8, 1.25
CONTRAST_MIN, CONTRAST_MAX = 0.8, 1.25
LUMA = (0.2126, 0.7152, 0.0722)  # BT.709 luma weights

OUT_W, OUT_H = 1920, 1080


class GradeError(RuntimeError):
    """A grading step failed (bad LUT, undecodable frame, unplaced reference)."""


class CheckUnavailable(RuntimeError):
    """guestlist (the Meta API client) cannot be imported."""


@dataclass(slots=True)
class Cube:
    size: int
    data: list[tuple[float, float, float]]  # [(b * size + g) * size + r], red fastest
    domain_min: tuple[float, float, float] = (0.0, 0.0, 0.0)
    domain_max: tuple[float, float, float] = (1.0, 1.0, 1.0)
    title: str = ""


@dataclass(slots=True)
class SourceColour:
    range: str = "tv"
    space: str = "bt709"
    primaries: str = "bt709"
    transfer: str = ""
    pix_fmt: str = ""
    bits: int = 8


@dataclass(slots=True)
class GradeClip:
    label: str
    path: str
    gamma: str
    offset_s: float | None
    duration_s: float
    width: int = 0
    height: int = 0
    fps: float = 0.0
    colour: SourceColour = field(default_factory=SourceColour)


@dataclass(slots=True)
class ChannelStats:
    mean: tuple[float, float, float]
    std: tuple[float, float, float]
    moments: int  # Rec.709-encoded, post-conversion


@dataclass(slots=True)
class MatchResult:
    gain: tuple[float, float, float]
    offset: tuple[float, float, float]
    gain_clamped: tuple[bool, bool, bool]
    offset_clamped: tuple[bool, bool, bool]
    matched: bool


@dataclass(slots=True)
class GradeShotInfo:
    label: str
    lut: Path | None
    colour: SourceColour  # used by angles


def clamp01(x: float) -> float:
    return 0.0 if x <= 0.0 else (1.0 if x >= 1.0 else x)


# ---------------------------------------------------------------- colour math
#
# Per pixel, full-range RGB 0..1: (1) per-channel inverse transfer to scene
# linear in the source gamut; (2) 3x3 gamut matrix to Rec.709 primaries;
# (3) clamp negative linear to 0; (4) per-channel shoulder; (5) Rec.709
# encode; (6) clamp 0..1.

# S-Log3 (Sony S-Log3 Technical Summary; same constants as colour-science
# eotf_inverse_SLog3 / the ACES S-Log3 IDT). 10-bit codes: 0% black is 95,
# 18% grey is 420, the log/linear cut sits at linear 0.01125.
_SLOG3_CUT = 171.21029469287395
_SLOG3_BLACK_CODE = 95.0
_SLOG3_GREY_CODE = 420.0
_SLOG3_CUT_LINEAR = 0.01125


def slog3_to_linear(s: float, range_: str = "full") -> float:
    """S-Log3 signal to scene linear.

    Sony defines the curve on the stored 10-bit code (black 95, grey 420), not on
    a level-expanded signal. A full-range decode hands the LUT code/1023; a
    limited-range decode has already expanded 64..940 to 0..1, so undo that.
    """
    code = 64.0 + 876.0 * s if range_ == "tv" else 1023.0 * s
    if code >= _SLOG3_CUT:  # log branch through the 18% grey point
        return 10.0 ** ((code - _SLOG3_GREY_CODE) / 261.5) * 0.19 - 0.01
    # linear branch: straight line through black and the cut point
    return (code - _SLOG3_BLACK_CODE) * _SLOG3_CUT_LINEAR / (_SLOG3_CUT - _SLOG3_BLACK_CODE)


# HLG inverse OETF (ITU-R BT.2100 Table 5). No BT.2100 OOTF: it is
# display-side brightness adaptation and would move 18% grey off the S-Log3
# value; the shoulder below is the only display adaptation.
_HLG_A = 0.17883277
_HLG_B = 1.0 - 4.0 * 0.17883277  # 0.28466892
_HLG_C = 0.5 - 0.17883277 * math.log(4.0 * 0.17883277)  # 0.55991073


def hlg_to_linear(s: float) -> float:
    """HLG signal to BT.2020 scene linear (s=1 -> L=1)."""
    if s <= 0.0:  # out-of-domain guard: the OETF is only defined for s >= 0
        return 0.0
    if s <= 0.5:
        return s * s / 3.0
    return (math.exp((s - _HLG_C) / _HLG_A) + _HLG_B) / 12.0


# HLG exposure (ITU-R BT.2408): reference white (diffuse 100%) is signal
# 0.75, so scene linear is normalised by E at 0.75: reference white -> 1.0,
# 18% grey -> 0.18, highlights above reference white roll off in the shoulder.
_HLG_REF_WHITE = hlg_to_linear(0.75)


def _hlg_scene(s: float) -> float:
    return hlg_to_linear(s) / _HLG_REF_WHITE


# Shoulder (GhostCut design, unpublished): identity up to the 18% grey knee
# so the grey anchors are exact, then an exponential approach to 1.0 that
# never hard-clips. Continuous with derivative 1 at the knee.
_SHOULDER_KNEE = 0.18
_SHOULDER_HEADROOM = 0.82  # 1 - knee


def shoulder(l: float) -> float:
    if l <= _SHOULDER_KNEE:
        return l
    return _SHOULDER_KNEE + _SHOULDER_HEADROOM * (
        1.0 - math.exp(-(l - _SHOULDER_KNEE) / _SHOULDER_HEADROOM))


def rec709_encode(l: float) -> float:
    """Scene linear to Rec.709 signal (ITU-R BT.709 OETF)."""
    if l < 0.018:
        return 4.5 * l
    return 1.099 * l ** 0.45 - 0.099


def _rec709_decode(s: float) -> float:
    if s <= 0.0:  # out-of-domain guard: negative signals are black
        return 0.0
    if s < 0.081:
        return s / 4.5
    return ((s + 0.099) / 1.099) ** (1.0 / 0.45)


# Gamut primaries (CIE x,y), all D65 white so no chromatic adaptation:
# S-Gamut3.Cine and S-Gamut3 (Sony spec), BT.2020 (ITU-R BT.2020),
# Rec.709 (ITU-R BT.709).
_PRIMARIES: dict[str, tuple[float, ...]] = {
    "sgamut3cine": (0.766, 0.275, 0.225, 0.800, 0.089, -0.087),
    "sgamut3": (0.730, 0.280, 0.165, 0.840, 0.100, -0.030),
    "bt2020": (0.708, 0.292, 0.170, 0.797, 0.131, 0.046),
    "bt709": (0.640, 0.330, 0.300, 0.600, 0.150, 0.060),
}
_GAMMA_PRIMARIES = {"s-log3-cine": "sgamut3cine", "s-log3": "sgamut3",
                    "hlg": "bt2020", "rec709": "bt709"}
_D65_WHITE = (0.3127 / 0.3290, 1.0, (1.0 - 0.3127 - 0.3290) / 0.3290)


def _invert_3x3(m: tuple[float, ...]) -> tuple[float, ...]:
    a, b, c, d, e, f, g, h, i = m
    A = e * i - f * h
    B = f * g - d * i
    C = d * h - e * g
    det = a * A + b * B + c * C
    if det == 0.0:
        raise ValueError("singular matrix")
    return (A / det, (c * h - b * i) / det, (b * f - c * e) / det,
            B / det, (a * i - c * g) / det, (c * d - a * f) / det,
            C / det, (b * g - a * h) / det, (a * e - b * d) / det)


def _mul_3x3(a: tuple[float, ...], b: tuple[float, ...]) -> tuple[float, ...]:
    return tuple(sum(a[3 * r + k] * b[3 * k + c] for k in range(3))
                 for r in range(3) for c in range(3))


def _primaries_to_xyz(primaries: tuple[float, ...]) -> tuple[float, ...]:
    """RGB-to-XYZ matrix for (rx, ry, gx, gy, bx, by) under D65."""
    rx, ry, gx, gy, bx, by = primaries
    cols = [(rx / ry, 1.0, (1.0 - rx - ry) / ry),
            (gx / gy, 1.0, (1.0 - gx - gy) / gy),
            (bx / by, 1.0, (1.0 - bx - by) / by)]
    flat = (cols[0][0], cols[1][0], cols[2][0],
            cols[0][1], cols[1][1], cols[2][1],
            cols[0][2], cols[1][2], cols[2][2])
    inv = _invert_3x3(flat)
    s = [sum(inv[3 * r + c] * _D65_WHITE[c] for c in range(3)) for r in range(3)]
    return tuple(flat[3 * r + c] * s[c] for r in range(3) for c in range(3))


_XYZ_BT709 = _primaries_to_xyz(_PRIMARIES["bt709"])
_INV_XYZ_BT709 = _invert_3x3(_XYZ_BT709)
_GAMUTS = {name: _mul_3x3(_INV_XYZ_BT709, _primaries_to_xyz(p))
           for name, p in _PRIMARIES.items()}


def gamut_matrix(gamma: str) -> tuple[float, ...]:
    """Source-linear to Rec.709-linear 3x3 for a recording gamma."""
    key = _GAMMA_PRIMARIES.get((gamma or "").strip().lower(), "")
    if not key or key == "bt709":
        raise ValueError(f"no gamut conversion for gamma {gamma!r}")
    return _GAMUTS[key]


def convert_pixel(gamma: str, r: float, g: float, b: float,
                  range_: str = "full") -> tuple[float, float, float]:
    """One decoded source pixel (full-range RGB 0..1) to Rec.709 (the scalar colour pipeline).
    range_ is how the stream was decoded; only S-Log3 depends on it."""
    gamma = (gamma or "").strip().lower()
    if gamma in ("s-log3-cine", "s-log3"):
        lin = [slog3_to_linear(v, range_) for v in (r, g, b)]
        matrix = gamut_matrix(gamma)
    elif gamma == "hlg":
        lin = [_hlg_scene(v) for v in (r, g, b)]
        matrix = gamut_matrix(gamma)
    elif gamma == "rec709":
        lin = [_rec709_decode(v) for v in (r, g, b)]
        matrix = (1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0)
    else:
        raise ValueError(f"no conversion for gamma {gamma!r}")
    # gamut rotation can push saturated colours negative: hard clip, no mapping
    mapped = [max(0.0, sum(matrix[3 * i + k] * lin[k] for k in range(3)))
              for i in range(3)]
    return tuple(clamp01(rec709_encode(shoulder(v))) for v in mapped)  # type: ignore[return-value]


def conversion_kind(gamma: str, supplied: Path | None) -> tuple[str, str | None]:
    """(conversion kind, supplied LUT path as given). A supplied LUT wins."""
    if supplied is not None:
        return "supplied", str(supplied)
    gamma = (gamma or "").strip().lower()
    if gamma == "s-log3-cine":
        return "generated_slog3_cine", None
    if gamma == "s-log3":
        return "generated_slog3", None
    if gamma == "hlg":
        return "generated_hlg", None
    if gamma == "rec709":
        return "none_rec709", None
    if gamma == "pq":
        return "none_pq", None
    return "none_unknown", None


# ---------------------------------------------------------------- cubes

def identity_cube(size: int = LUT_SIZE) -> Cube:
    """A pass-through cube: node values are the canonical grid itself."""
    step = 1.0 / (size - 1)
    return Cube(size=size, data=[
        (r * step, g * step, b * step)
        for b in range(size) for g in range(size) for r in range(size)])


def generate_conversion_cube(gamma: str, range_: str = "full") -> Cube:
    """convert_pixel evaluated on the 33^3 canonical grid, for one decode range."""
    step = 1.0 / (LUT_SIZE - 1)
    return Cube(size=LUT_SIZE, data=[
        convert_pixel(gamma, r * step, g * step, b * step, range_)
        for b in range(LUT_SIZE) for g in range(LUT_SIZE) for r in range(LUT_SIZE)])


def format_cube(cube: Cube, title: str) -> str:
    """Canonical .cube text: header, then size^3 lines, red fastest."""
    lines = [f'TITLE "{title}"', f"LUT_3D_SIZE {cube.size}",
             "DOMAIN_MIN 0.000000 0.000000 0.000000",
             "DOMAIN_MAX 1.000000 1.000000 1.000000"]
    lines += [f"{r:.6f} {g:.6f} {b:.6f}" for r, g, b in cube.data]
    return "\n".join(lines) + "\n"


def write_cube(path: Path, cube: Cube) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(format_cube(cube, cube.title), encoding="utf-8")


def parse_cube(text: str, source: str = "<cube>") -> Cube:
    """A .cube file: comments and blanks ignored, TITLE optional and quoted,
    LUT_3D_SIZE required with exactly n^3 data rows, DOMAIN lines optional in
    any order before the data. No 1D support."""
    size: int | None = None
    dmin = [0.0, 0.0, 0.0]
    dmax = [1.0, 1.0, 1.0]
    title = ""
    rows: list[tuple[float, float, float]] = []

    def fail(line_no: int, what: str) -> ValueError:
        return ValueError(f"{source}:{line_no}: {what}")

    for line_no, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        # Any whitespace separates a keyword from its value: Resolve-made cubes (DJI's) use a tab.
        head, _, rest = line.replace("\t", " ").partition(" ")
        key = head.strip().upper()
        if key == "TITLE":
            if rows:
                raise fail(line_no, "TITLE after data")
            quoted = rest.strip()
            if len(quoted) >= 2 and quoted.startswith('"') and quoted.endswith('"'):
                title = quoted[1:-1]
            elif quoted:
                title = quoted
            else:
                raise fail(line_no, "TITLE needs a value")
        elif key == "LUT_1D_SIZE":
            raise fail(line_no, "1D LUTs are not supported")
        elif key == "LUT_3D_SIZE":
            if size is not None:
                raise fail(line_no, "second LUT_3D_SIZE")
            if rows:
                raise fail(line_no, "LUT_3D_SIZE after data")
            try:
                size = int(rest.strip())
            except ValueError:
                raise fail(line_no, f"bad LUT_3D_SIZE {rest.strip()!r}") from None
            if size < 2 or size > 256:
                raise fail(line_no, f"LUT_3D_SIZE {size} outside 2..256")
        elif key in ("DOMAIN_MIN", "DOMAIN_MAX"):
            if rows:
                raise fail(line_no, f"{key} after data")
            try:
                vals = [float(v) for v in rest.split()]
            except ValueError:
                raise fail(line_no, f"bad {key} {rest.strip()!r}") from None
            if len(vals) != 3:
                raise fail(line_no, f"{key} needs 3 floats")
            if key == "DOMAIN_MIN":
                dmin = vals
            else:
                dmax = vals
        else:
            if size is None:
                raise fail(line_no, "data before LUT_3D_SIZE")
            try:
                vals = [float(v) for v in line.split()]
            except ValueError:
                raise fail(line_no, f"bad data row {line!r}") from None
            if len(vals) != 3:
                raise fail(line_no, f"data row needs 3 floats: {line!r}")
            rows.append((vals[0], vals[1], vals[2]))
    if size is None:
        raise ValueError(f"{source}:0: LUT_3D_SIZE is required")
    if len(rows) != size ** 3:
        raise ValueError(f"{source}:0: want {size ** 3} data rows, have {len(rows)}")
    if any(hi <= lo for lo, hi in zip(dmin, dmax)):
        raise ValueError(f"{source}:0: DOMAIN_MAX must exceed DOMAIN_MIN")
    return Cube(size=size, data=rows,
                domain_min=(dmin[0], dmin[1], dmin[2]),
                domain_max=(dmax[0], dmax[1], dmax[2]), title=title)


def read_cube(path: Path) -> Cube:
    return parse_cube(Path(path).read_text(encoding="utf-8"), source=str(path))


def cube_at(cube: Cube, r: float, g: float, b: float) -> tuple[float, float, float]:
    """Trilinear sample: clamp into the cube's domain, normalise, interpolate."""
    n = cube.size
    ts = []
    for v, lo, hi in zip((r, g, b), cube.domain_min, cube.domain_max):
        ts.append(clamp01((v - lo) / (hi - lo)))
    idx: list[tuple[int, int, float]] = []
    for t in ts:
        pos = t * (n - 1)
        i0 = min(int(pos), n - 2) if n > 1 else 0
        i1 = min(i0 + 1, n - 1)
        idx.append((i0, i1, pos - i0))
    out = []
    for c in range(3):
        acc = 0.0
        for db in (0, 1):
            wb = idx[2][2] if db else 1.0 - idx[2][2]
            for dg in (0, 1):
                wg = idx[1][2] if dg else 1.0 - idx[1][2]
                for dr in (0, 1):
                    wr = idx[0][2] if dr else 1.0 - idx[0][2]
                    node = cube.data[(idx[2][db] * n + idx[1][dg]) * n + idx[0][dr]]
                    acc += wb * wg * wr * node[c]
        out.append(acc)
    return (out[0], out[1], out[2])


def resample_cube(cube: Cube, size: int = LUT_SIZE) -> Cube:
    """A supplied cube canonicalised to the 33^3 0..1 grid (cube_at clamps
    into the supplied domain, so other sizes and domains compose the same)."""
    step = 1.0 / (size - 1)
    return Cube(size=size, data=[
        cube_at(cube, r * step, g * step, b * step)
        for b in range(size) for g in range(size) for r in range(size)],
        title=cube.title)


def compose_gain_offset(cube: Cube, gain: tuple[float, float, float],
                        offset: tuple[float, float, float]) -> Cube:
    """Bake the match into a canonical cube, node by node, clamped."""
    return Cube(size=cube.size, data=[
        (clamp01(gain[0] * r + offset[0]),
         clamp01(gain[1] * g + offset[1]),
         clamp01(gain[2] * b + offset[2]))
        for r, g, b in cube.data],
        domain_min=cube.domain_min, domain_max=cube.domain_max, title=cube.title)


def parse_lut_specs(items: list[str]) -> dict[str, Path]:
    """--lut LABEL=PATH entries; malformed or duplicate labels are an error."""
    out: dict[str, Path] = {}
    for item in items:
        label, sep, given = item.partition("=")
        if not sep or not label or not given:
            raise ValueError(f"Bad --lut {item!r}: want LABEL=PATH")
        if label in out:
            raise ValueError(f"Duplicate --lut label: {label}")
        out[label] = Path(given)
    return out


# ---------------------------------------------------------------- sync views, probe, sampling, match

def choose_reference(clips: list[GradeClip], sync_label: str,
                     reference_label: str | None) -> str:
    """The camera to match colour to: --reference, else the sync reference, unless that has
    no picture (an audio recorder), in which case the first placed camera. Pure."""
    if reference_label:
        return reference_label
    by_label = {c.label: c for c in clips}
    if sync_label in by_label and by_label[sync_label].width <= 0:
        cameras = sorted((c for c in clips if c.width > 0 and c.offset_s is not None),
                         key=lambda c: c.label)
        if cameras:
            return cameras[0].label
    return sync_label


def load_sync_for_grade(path: Path) -> tuple[str, list[GradeClip]]:
    """sync.json reference clip's label plus one GradeClip view per clip."""
    data = read_json(Path(path))
    if not isinstance(data, dict) or not isinstance(data.get("clips"), list):
        raise ValueError(f"Not a sync.json: {path}")
    reference = str(data.get("reference") or "")
    clips: list[GradeClip] = []
    seen: set[str] = set()
    for item in data["clips"]:
        label = str(item.get("label") or "")
        if not label:
            raise ValueError(f"Sync clip without a label in {path}")
        if label in seen:
            raise ValueError(f"Duplicate label {label} in {path}")
        seen.add(label)
        cam = item.get("camera") or {}
        offset = item.get("offset_s")
        gamma = str(cam.get("gamma") or "").strip().lower()
        clips.append(GradeClip(
            label=label,
            path=str(cam.get("path") or ""),
            gamma=gamma,
            offset_s=None if offset is None else float(offset),
            duration_s=float(cam.get("duration_s") or 0.0),
            width=int(cam.get("width") or 0),
            height=int(cam.get("height") or 0),
            fps=float(cam.get("fps") or 0.0),
            colour=default_colour(gamma),
        ))
    reference_label = next((c.label for c in clips if c.path == reference), "")
    if not reference_label:
        raise ValueError(f"No sync clip matches the reference in {path}")
    return reference_label, clips


def default_colour(gamma: str) -> SourceColour:
    """Decode assumptions before the probe: HLG is BT.2020, else BT.709."""
    if (gamma or "").strip().lower() == "hlg":
        return SourceColour(range="tv", space="bt2020nc", primaries="bt2020")
    return SourceColour()


def probe_source_colour(clip: GradeClip) -> SourceColour:
    """What the stream says: range, matrix, transfer, depth. Raises on failure
    and the caller falls back to the default with a probe_failed warning."""
    result = run_command([
        "ffprobe", "-v", "error", "-show_entries",
        "stream=codec_type,pix_fmt,color_range,color_space,color_transfer,color_primaries",
        "-of", "json", clip.path], check=False)
    if result.returncode != 0:
        raise GradeError(f"ffprobe failed for {clip.label}: {result.stderr.strip()[-300:]}")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise GradeError(f"ffprobe JSON failed for {clip.label}: {error}") from error
    video = next((s for s in payload.get("streams", [])
                  if s.get("codec_type") == "video"), None)
    if video is None:
        raise GradeError(f"ffprobe found no video stream for {clip.label}")
    space = str(video.get("color_space") or "")
    if space not in ("bt2020nc", "bt709"):
        space = default_colour(clip.gamma).space
    pix_fmt = str(video.get("pix_fmt") or "")
    return SourceColour(
        range="full" if str(video.get("color_range") or "") == "pc" else "tv",
        space=space,
        primaries=str(video.get("color_primaries") or "") or default_colour(clip.gamma).primaries,
        transfer=str(video.get("color_transfer") or ""),
        pix_fmt=pix_fmt,
        bits=10 if "10" in pix_fmt else 8,
    )


def _lut_path_text(lut: Path) -> str:
    text = str(lut)
    if "'" in text or ":" in text:
        raise GradeError(f"LUT path cannot contain ' or :: {text}")
    return text


def _input_scale(colour: SourceColour) -> str:
    # scale has no rgb out_color_matrix here, and converting to an RGB format
    # ignores it anyway: only the input matrix and range are forced, so an
    # untagged stream cannot pick another matrix. out_range=full feeds the
    # LUT full-range RGB; 8-bit upscales exactly, 10-bit is never truncated.
    return (f"scale=in_range={colour.range}:in_color_matrix={colour.space}"
            f":out_range=full:flags=accurate_rnd+full_chroma_int")


# Frame a camera into the 16:9 master. Near-widescreen sources (aspect 1.4 to 2.4: a DJI
# 5120x2700 at 1.9:1, a 2.39 anamorphic) fill the frame with a small centre crop: letterbox
# bars that come and go on every angle change are worse. Vertical or 4:3 footage keeps its
# whole picture, pillarboxed. The single quotes keep the commas inside the expressions.
FILL_OR_FIT = (
    f"scale=w='if(between(a,1.4,2.4),if(gte(a,16/9),-2,{OUT_W}),if(gte(a,16/9),{OUT_W},-2))'"
    f":h='if(between(a,1.4,2.4),if(gte(a,16/9),{OUT_H},-2),if(gte(a,16/9),-2,{OUT_H}))'"
    f":flags=lanczos,crop='min(iw,{OUT_W})':'min(ih,{OUT_H})',"
    f"pad={OUT_W}:{OUT_H}:(ow-iw)/2:(oh-ih)/2,setsar=1")


def lut_filter_chain(colour: SourceColour, lut: Path | None, fps: str = "30") -> str:
    """The -vf for one graded shot. lut=None is today's legacy look verbatim.

    The LUT sees full-resolution full-range source-matrix RGB at 16-bit
    (rgb48le), straight after the decode, before any spatial scaling. The
    second scale works in graded Rec.709 RGB; an explicit out matrix picks
    BT.709 (swscale defaults to BT.601) and setparams tags the part, which
    matters because the join is stream copy.
    """
    if lut is None:
        return f"{FILL_OR_FIT},fps={fps},format=yuv420p"
    graded = (f"{FILL_OR_FIT},fps={fps},"
              f"scale=out_color_matrix=bt709:out_range=tv,format=yuv420p,"
              f"setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=tv")
    return (f"{_input_scale(colour)},format=rgb48le,"
            f"lut3d=file='{_lut_path_text(lut)}':interp=tetrahedral,{graded}")


def _convert_lut3d(conversion_lut: Path | None) -> str:
    if conversion_lut is None:
        return ""
    return f",lut3d=file='{_lut_path_text(conversion_lut)}':interp=tetrahedral"


def sample_command(clip: GradeClip, at_local_s: float,
                   conversion_lut: Path | None) -> list[str]:
    """One converted frame as small raw RGB: the centre half (the speaker),
    bilinearly down to 64x36. -ss sits before -i, as in angles."""
    vf = (f"{_input_scale(clip.colour)},format=rgb48le{_convert_lut3d(conversion_lut)},"
          f"crop=w=iw/2:h=ih/2:x=iw/4:y=ih/4,scale={MEASURE_W}:{MEASURE_H}:flags=bilinear,"
          f"format=rgb24")
    return ["ffmpeg", "-v", "error", "-y", "-ss", f"{at_local_s:.3f}", "-i", clip.path,
            "-map", "0:v:0", "-frames:v", "1", "-vf", vf,
            "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"]


def decode_centre_frame(clip: GradeClip, at_local_s: float,
                        conversion_lut: Path | None) -> bytes:
    """Raw rgb24 bytes off the pipe (binary, so not ffmpeg_tools.run_command)."""
    proc = subprocess.run(sample_command(clip, at_local_s, conversion_lut),
                          capture_output=True, check=False)
    want = MEASURE_W * MEASURE_H * 3
    if proc.returncode != 0 or len(proc.stdout) != want:
        tail = proc.stderr.decode("utf-8", errors="replace")[-300:]
        raise GradeError(f"sample failed for {clip.label} at {at_local_s:.3f}s: {tail}")
    return proc.stdout


def skin_sample_command(clip: GradeClip, at_local_s: float,
                        conversion_lut: Path | None, jpg_path: Path,
                        width: int = SKIN_W, height: int = SKIN_H) -> list[str]:
    """One converted full frame as raw RGB plus a helper JPEG for face boxes.

    Both outputs go through the camera's conversion LUT and bilinearly
    down to width x height; the raw pipe feeds skin_stats and the JPEG
    feeds the Vision helper, whose boxes are normalised so one size
    serves both. -ss sits before -i, as in angles.
    """
    vf = (f"{_input_scale(clip.colour)},format=rgb48le{_convert_lut3d(conversion_lut)},"
          f"scale={width}:{height}:flags=bilinear,format=rgb24")
    vf_jpg = (f"{_input_scale(clip.colour)},format=rgb48le{_convert_lut3d(conversion_lut)},"
              f"scale={width}:{height}:flags=bilinear")
    return ["ffmpeg", "-v", "error", "-y", "-ss", f"{at_local_s:.3f}", "-i", clip.path,
            "-map", "0:v:0", "-frames:v", "1", "-vf", vf,
            "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
            "-map", "0:v:0", "-frames:v", "1", "-vf", vf_jpg,
            "-q:v", "4", str(jpg_path)]


def decode_skin_frame(clip: GradeClip, at_local_s: float,
                      conversion_lut: Path | None, jpg_path: Path,
                      width: int = SKIN_W, height: int = SKIN_H) -> bytes:
    """Raw rgb24 bytes off the pipe; the helper JPEG lands at jpg_path."""
    Path(jpg_path).parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(skin_sample_command(clip, at_local_s, conversion_lut,
                                              jpg_path, width, height),
                          capture_output=True, check=False)
    want = width * height * 3
    if proc.returncode != 0 or len(proc.stdout) != want:
        tail = proc.stderr.decode("utf-8", errors="replace")[-300:]
        raise GradeError(f"skin sample failed for {clip.label} at {at_local_s:.3f}s: {tail}")
    return proc.stdout


def centre_stats(raw: bytes) -> ChannelStats:
    """Mean and spread per channel over one 64x36 centre frame."""
    if len(raw) != MEASURE_W * MEASURE_H * 3:
        raise GradeError(f"want {MEASURE_W * MEASURE_H * 3} sample bytes, have {len(raw)}")
    n = MEASURE_W * MEASURE_H
    sums = [0.0, 0.0, 0.0]
    for i in range(0, len(raw), 3):
        sums[0] += raw[i]
        sums[1] += raw[i + 1]
        sums[2] += raw[i + 2]
    means = [s / (n * 255.0) for s in sums]
    variances = [0.0, 0.0, 0.0]
    for i in range(0, len(raw), 3):
        for c in range(3):
            diff = raw[i + c] / 255.0 - means[c]
            variances[c] += diff * diff
    stds = [math.sqrt(v / n) for v in variances]
    return ChannelStats(mean=(means[0], means[1], means[2]),
                        std=(stds[0], stds[1], stds[2]), moments=1)


def skin_stats(raw: bytes, width: int, height: int,
               box: tuple[float, float, float, float] | None,
               inset: float = SKIN_INSET) -> ChannelStats | None:
    """Mean and spread per channel inside the face box's inner region.

    The region is the central 1 - 2 * inset of the Vision box (normalised,
    origin lower-left), so hair and background stay out; pixels come from
    a full-frame decode `width` x `height` rgb24. None when there is no
    box or the region is empty. Pure.
    """
    if box is None:
        return None
    if len(raw) != width * height * 3:
        raise GradeError(f"want {width * height * 3} sample bytes, have {len(raw)}")
    x, y, w, h = (float(v) for v in box)
    c0 = max(0, min(width, math.floor((x + inset * w) * width)))
    c1 = max(0, min(width, math.ceil((x + (1.0 - inset) * w) * width)))
    # Vision's y grows upwards, pixel rows grow downwards
    r0 = max(0, min(height, math.floor(height - (y + (1.0 - inset) * h) * height)))
    r1 = max(0, min(height, math.ceil(height - (y + inset * h) * height)))
    if c1 <= c0 or r1 <= r0:
        return None
    n = (c1 - c0) * (r1 - r0)
    sums = [0.0, 0.0, 0.0]
    for r in range(r0, r1):
        base = (r * width + c0) * 3
        for c in range(c1 - c0):
            sums[0] += raw[base + 3 * c]
            sums[1] += raw[base + 3 * c + 1]
            sums[2] += raw[base + 3 * c + 2]
    means = [s / (n * 255.0) for s in sums]
    variances = [0.0, 0.0, 0.0]
    for r in range(r0, r1):
        base = (r * width + c0) * 3
        for c in range(c1 - c0):
            for ch in range(3):
                diff = raw[base + 3 * c + ch] / 255.0 - means[ch]
                variances[ch] += diff * diff
    stds = [math.sqrt(v / n) for v in variances]
    return ChannelStats(mean=(means[0], means[1], means[2]),
                        std=(stds[0], stds[1], stds[2]), moments=1)


def _average_stats(frames: list[ChannelStats]) -> ChannelStats:
    """Mean of per-frame means, mean of per-frame stds."""
    n = len(frames)
    mean = tuple(sum(f.mean[c] for f in frames) / n for c in range(3))  # type: ignore[misc]
    std = tuple(sum(f.std[c] for f in frames) / n for c in range(3))  # type: ignore[misc]
    return ChannelStats(mean=mean, std=std, moments=n)


def measure_camera(clip: GradeClip, moments: list[float],
                   conversion_lut: Path | None) -> ChannelStats:
    """Converted centre stats at clip-local moments, averaged."""
    return _average_stats([centre_stats(decode_centre_frame(clip, t, conversion_lut))
                           for t in moments])


def _overlap_moments(lo: float, hi: float) -> list[float]:
    span = hi - lo
    span_lo, span_hi = lo + 0.05 * span, hi - 0.05 * span
    if span_hi - span_lo < 0.5:
        return []
    # 5% trimmed off each end so dissolves and whip-pans cannot drive the match
    n = min(MAX_MOMENTS, max(1, int(span_hi - span_lo)))
    return [span_lo + (i + 0.5) / n * (span_hi - span_lo) for i in range(n)]


def pairwise_moments(a: GradeClip, b: GradeClip) -> list[float]:
    """Timeline moments both clips cover; unplaced clips never overlap."""
    if a.offset_s is None or b.offset_s is None:
        return []
    return _overlap_moments(max(a.offset_s, b.offset_s),
                            min(a.offset_s + a.duration_s, b.offset_s + b.duration_s))


def shared_moments(clips: list[GradeClip]) -> list[float]:
    """Timeline moments every placed clip covers (the Muse check's grid)."""
    placed = [c for c in clips if c.offset_s is not None]
    if not placed:
        return []
    return _overlap_moments(max(c.offset_s or 0.0 for c in placed),
                            min((c.offset_s or 0.0) + c.duration_s for c in placed))


def solve_match(cam: ChannelStats, ref: ChannelStats) -> MatchResult:
    """Exposure from the luma ratio, white balance from red and blue against green.

    Multicam angles never frame the same thing (a wide and a punch-in), so the
    spread of a channel says more about the framing than the colour; matching
    spreads swung the gains and pinned the offsets on a real pair. Ratios of
    means survive a change of framing far better, and pure gains keep black
    at black. Offsets are always 0. Exposure clamps to GAIN_MIN..GAIN_MAX,
    each white-balance ratio to WB_MIN..WB_MAX; a clamp marks its channels.
    """
    if min(cam.mean) <= 1e-6 or min(ref.mean) <= 1e-6:  # a black channel carries no ratio
        return MatchResult(gain=(1.0, 1.0, 1.0), offset=(0.0, 0.0, 0.0),
                           gain_clamped=(False, False, False),
                           offset_clamped=(False, False, False), matched=True)

    def luma(m: tuple[float, ...]) -> float:
        return LUMA[0] * m[0] + LUMA[1] * m[1] + LUMA[2] * m[2]

    wb, wb_hit = [1.0, 1.0, 1.0], [False, False, False]
    for c in (0, 2):
        raw = (ref.mean[c] / ref.mean[1]) / (cam.mean[c] / cam.mean[1])
        wb[c] = min(max(raw, WB_MIN), WB_MAX)
        wb_hit[c] = raw < WB_MIN or raw > WB_MAX
    # white balance alone leaves the camera's luma where it was
    keep = luma(cam.mean) / luma(tuple(wb[c] * cam.mean[c] for c in range(3)))
    raw_exposure = luma(ref.mean) / luma(cam.mean)
    exposure = min(max(raw_exposure, GAIN_MIN), GAIN_MAX)
    exposure_hit = raw_exposure < GAIN_MIN or raw_exposure > GAIN_MAX
    gain = tuple(exposure * keep * wb[c] for c in range(3))
    return MatchResult(gain=(gain[0], gain[1], gain[2]), offset=(0.0, 0.0, 0.0),
                       gain_clamped=tuple(exposure_hit or wb_hit[c] for c in range(3)),  # type: ignore[arg-type]
                       offset_clamped=(False, False, False), matched=True)


def solve_skin_match(cam: ChannelStats, ref: ChannelStats) -> MatchResult:
    """Today's gain from the skin means, plus contrast from the skin spreads.

    The base gain (exposure plus white balance) is solve_match on the skin
    statistics, so skin brightness and warmth agree as a whole-frame match
    would. Contrast then scales each channel about its matched mean:
    k = ref_std / matched_std, clamped to CONTRAST_MIN..CONTRAST_MAX, so
    skin contrast agrees without moving the means. The final per-channel
    transform is gain k * base with a lift holding the matched mean; the
    gain clamps to GAIN_MIN..GAIN_MAX and the lift to ±OFFSET_MAX, and any
    bound hit on the path marks its channel.
    """
    base = solve_match(cam, ref)
    gains: list[float] = []
    offsets: list[float] = []
    gain_hit: list[bool] = []
    offset_hit: list[bool] = []
    for c in range(3):
        matched_std = base.gain[c] * cam.std[c]
        if matched_std <= 1e-6 or ref.std[c] <= 1e-6:
            k, contrast_hit = 1.0, False
        else:
            raw = ref.std[c] / matched_std
            k = min(max(raw, CONTRAST_MIN), CONTRAST_MAX)
            contrast_hit = raw < CONTRAST_MIN or raw > CONTRAST_MAX
        gain = base.gain[c] * k
        if gain < GAIN_MIN:
            gain, final_hit = GAIN_MIN, True
        elif gain > GAIN_MAX:
            gain, final_hit = GAIN_MAX, True
        else:
            final_hit = False
        lift = base.gain[c] * cam.mean[c] * (1.0 - k)
        if lift < -OFFSET_MAX:
            lift, lift_hit = -OFFSET_MAX, True
        elif lift > OFFSET_MAX:
            lift, lift_hit = OFFSET_MAX, True
        else:
            lift_hit = False
        gains.append(gain)
        offsets.append(lift)
        gain_hit.append(base.gain_clamped[c] or contrast_hit or final_hit)
        offset_hit.append(lift_hit)
    return MatchResult(gain=(gains[0], gains[1], gains[2]),
                       offset=(offsets[0], offsets[1], offsets[2]),
                       gain_clamped=(gain_hit[0], gain_hit[1], gain_hit[2]),
                       offset_clamped=(offset_hit[0], offset_hit[1], offset_hit[2]),
                       matched=True)


def apply_stats(stats: ChannelStats, gain: tuple[float, float, float],
                offset: tuple[float, float, float]) -> ChannelStats:
    """What the match does to reported numbers: means shift, spreads scale."""
    return ChannelStats(
        mean=tuple(clamp01(gain[c] * stats.mean[c] + offset[c]) for c in range(3)),  # type: ignore[misc]
        std=tuple(gain[c] * stats.std[c] for c in range(3)),  # type: ignore[misc]
        moments=stats.moments)


# ---------------------------------------------------------------- Muse check (advisory; changes nothing)

CHECK_DEVELOPER = (
    "You are Muse, the colour-match checker inside GhostCut. You are given one frame from every "
    "camera at the same moment of the same event, after each camera has been converted to Rec.709 "
    "and matched to the reference camera. The frames show the same scene from different angles, "
    "so composition, framing and who is facing the lens will differ; ignore those. Judge only "
    "colour: exposure, white balance, saturation and contrast. For each of the four, name the one "
    "camera that stands out most from the others, the direction it stands out in, and how strongly. "
    "If no camera stands out on a dimension, say so with camera \"none\", direction \"none\" and "
    "strength \"none\". Never invent a camera that is not listed. This check is advisory: nothing "
    "is adjusted from your answer."
)

_CHECK_DIMENSIONS = ("exposure", "white_balance", "saturation", "contrast")
_CHECK_DIRECTIONS = {
    "exposure": ["brighter", "darker", "none"],
    "white_balance": ["warmer", "cooler", "none"],
    "saturation": ["more_saturated", "less_saturated", "none"],
    "contrast": ["higher_contrast", "lower_contrast", "none"],
}


def check_schema(labels: list[str]) -> dict:
    """Strict tuple-style schema: four fixed assessments, only the cameras sent."""
    options = [*labels, "none"]
    items = [{
        "type": "object",
        "additionalProperties": False,
        "required": ["dimension", "standout_camera", "direction", "strength", "note"],
        "properties": {
            "dimension": {"type": "string", "enum": [dim]},
            "standout_camera": {"type": "string", "enum": list(options)},
            "direction": {"type": "string", "enum": list(_CHECK_DIRECTIONS[dim])},
            "strength": {"type": "string", "enum": ["none", "slight", "moderate", "strong"]},
            "note": {"type": "string"},
        },
    } for dim in _CHECK_DIMENSIONS]
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["moment_s", "overall_standout", "assessments"],
        "properties": {
            "moment_s": {"type": "number"},
            "overall_standout": {"type": "string", "enum": list(options)},
            "assessments": {
                "type": "array",
                "prefixItems": items,
                "items": False,
                "minItems": len(items),
                "maxItems": len(items),
            },
        },
    }


def check_prompt(moment_s: float, labels: list[str]) -> str:
    listed = ", ".join(f"{n} = camera {label}" for n, label in enumerate(labels, 1))
    return (f"Check the colour match at t={moment_s:.1f}s. Cameras, in image order: {listed}. "
            "For each of exposure, white balance, saturation and contrast, reply with the camera "
            "that stands out (or \"none\"), the direction, and the strength. Also give the single "
            "overall standout camera (or \"none\").")


def check_frame_command(clip: GradeClip, at_local_s: float, final_lut: Path | None,
                        out: Path) -> list[str]:
    """One 512-wide JPEG through the camera's final (matched) cube; a Rec.709
    camera with no cube (an unmatched reference) is shown as recorded."""
    vf = (f"{_input_scale(clip.colour)},format=rgb48le{_convert_lut3d(final_lut)}"
          f",scale=512:-2"  # JPEG is full range, so the way out is full too
          f",scale=out_color_matrix=bt709:out_range=full,format=yuv420p")
    return ["ffmpeg", "-v", "error", "-y", "-ss", f"{at_local_s:.3f}", "-i", clip.path,
            "-map", "0:v:0", "-frames:v", "1", "-vf", vf, "-q:v", "3", str(out)]


def check_with_muse(frames: list[tuple[float, dict[str, Path]]], labels: list[str], *,
                    model: str) -> tuple[dict, float, list[str]]:
    """One spark.image_json per moment; a failed moment is dropped, never fatal."""
    try:
        from guestlist.meta import cache, spark
    except ImportError as error:
        raise CheckUnavailable(str(error)) from error
    moments: list[dict] = []
    warnings: list[str] = []
    spend = 0.0
    cached = 0
    for i, (moment_s, by_label) in enumerate(frames):
        sent = sorted(l for l in labels if l in by_label and Path(by_label[l]).exists())
        if len(sent) < 2:  # one camera cannot stand out from itself
            continue
        images = [by_label[label] for label in sent]
        try:
            cache.guard(0.25)
            reply = spark.image_json(images, schema=check_schema(sent), name="grade_check",
                                     developer=CHECK_DEVELOPER, prompt=check_prompt(moment_s, sent),
                                     model=model, effort="medium", max_output_tokens=6000)
            spend += float(reply.usd or 0.0)
            if getattr(reply, "cached", False):
                cached += 1
            moments.append({"moment_s": moment_s,
                            "frames": {label: f"check-frames/{Path(by_label[label]).name}"
                                       for label in sent},
                            "reply": reply.data})
        except Exception as error:  # say why, or a dropped moment can't be fixed
            warnings.append(f"check_moment_failed:{i}: {type(error).__name__}: {str(error)[:160]}")
    if cached:
        warnings.append(f"{cached} check(s) served from cache")
    return {"moments": moments}, round(spend, 4), warnings


# ---------------------------------------------------------------- orchestrator

def _round6(values: tuple[float, ...]) -> list[float]:
    return [round(v, 6) for v in values]


def _stats_json(stats: ChannelStats | None) -> dict | None:
    if stats is None:
        return None
    return {"mean": _round6(stats.mean), "std": _round6(stats.std)}


def _unmatched(is_reference: bool) -> dict:
    return {"matched": False, "is_reference": is_reference, "moments": [],
            "region": None, "skin_moments": [],
            "gain": [1, 1, 1], "offset": [0, 0, 0],
            "gain_clamped": [False, False, False], "offset_clamped": [False, False, False],
            "before": None, "reference": None, "after": None,
            "diff_before": None, "diff_after": None}


def run(*, sync_path: Path, out_path: Path, reference_label: str | None = None,
        lut_specs: list[str] | tuple[str, ...] = (), no_match: bool = False,
        check: bool = False, model: str = "muse-spark-1.3") -> dict:
    """Grade every camera: conversion cubes, match to the reference, grade.json."""
    sync_path, out_path = Path(sync_path), Path(out_path)
    out_dir = out_path.parent
    warnings: list[str] = []
    sync_label, clips = load_sync_for_grade(sync_path)
    by_label = {c.label: c for c in clips}
    label = choose_reference(clips, sync_label, reference_label)
    if label not in by_label:
        raise ValueError(f"Unknown --reference label: {label}")
    reference = by_label[label]
    if reference.offset_s is None:
        raise GradeError(f"Reference clip {label} is not placed on the timeline")
    # supplied LUTs are read before anything is written: a bad file aborts clean
    supplied = parse_lut_specs(list(lut_specs))
    for lut_label in supplied:
        if lut_label not in by_label:
            raise ValueError(f"--lut label {lut_label} is not in {sync_path}")
    supplied_cubes = {lut_label: resample_cube(read_cube(given))
                      for lut_label, given in supplied.items()}
    kinds = {c.label: conversion_kind(c.gamma, supplied.get(c.label)) for c in clips}
    for c in clips:
        kind = kinds[c.label][0]
        if kind == "none_pq":
            warnings.append(f"pq_not_supported:{c.label}")
        elif kind == "none_unknown":
            seen_gamma = c.gamma or "empty"
            warnings.append(f"unknown_gamma:{c.label}:{seen_gamma}")
    # probe even under --no-match: the cube and the render's decode must agree on range and matrix
    for c in clips:
        if not Path(c.path).exists():
            c.colour = default_colour(c.gamma)
            warnings.append(f"probe_failed:{c.label}")
            continue
        try:
            c.colour = probe_source_colour(c)
        except (GradeError, ToolingError, OSError):
            c.colour = default_colour(c.gamma)
            warnings.append(f"probe_failed:{c.label}")
    base: dict[str, Cube | None] = {}
    for c in clips:
        kind = kinds[c.label][0]
        if kind == "supplied":
            base[c.label] = supplied_cubes[c.label]
        elif kind.startswith("generated"):
            base[c.label] = generate_conversion_cube(c.gamma, c.colour.range)
        else:
            base[c.label] = None
    ref_unconverted = kinds[reference.label][0] in ("none_pq", "none_unknown")
    if ref_unconverted:
        warnings.append(f"reference_unconverted:{reference.label}")
    matching = not no_match and not ref_unconverted
    # conversion cubes go to disk first: sampling measures through them, so the
    # numbers being matched are post-conversion; matched cubes overwrite these.
    conversion_paths: dict[str, Path | None] = {}
    if matching:
        for c in clips:
            if base[c.label] is not None:
                base[c.label].title = f"ghostcut {c.label} {kinds[c.label][0]}"
                lut_path = out_dir / f"{c.label}.cube"
                write_cube(lut_path, base[c.label])
                conversion_paths[c.label] = lut_path
            else:
                conversion_paths[c.label] = None
    final: dict[str, Cube | None] = {reference.label: base[reference.label]}
    matches: dict[str, dict] = {reference.label: _unmatched(True)}
    ref_warnings: dict[str, list[str]] = {c.label: [] for c in clips}
    skin_cache: dict = {}

    def note(cam_label: str, warning: str) -> None:
        ref_warnings[cam_label].append(warning)
        warnings.append(warning)

    for cam in clips:
        if cam.label == reference.label:
            continue
        kind = kinds[cam.label][0]
        # rec709 has no conversion cube but still matches (onto an identity cube)
        if not matching or (base[cam.label] is None and kind != "none_rec709"):
            final[cam.label] = base[cam.label]
            matches[cam.label] = _unmatched(False)
            continue
        moments = pairwise_moments(reference, cam)
        skin: tuple[ChannelStats, ChannelStats, list[float]] | None = None
        if moments and cam.width > 0 and reference.width > 0:
            skin = _take_skin_pair(reference, cam, moments, conversion_paths,
                                   out_dir / "skin-frames", skin_cache, note)
        if skin is not None:
            ref_stats, cam_stats, used = skin
            region: str | None = "skin"
            solve = solve_skin_match
        else:
            ref_local = [t - (reference.offset_s or 0.0) for t in moments]
            cam_local = [t - (cam.offset_s or 0.0) for t in moments]
            ref_frames = _take_frames(reference, ref_local,
                                      conversion_paths[reference.label], note)
            cam_frames = _take_frames(cam, cam_local, conversion_paths[cam.label], note)
            if not ref_frames or not cam_frames:
                final[cam.label] = base[cam.label]
                matches[cam.label] = _unmatched(False)
                note(cam.label, f"no_shared_moment:{cam.label}")
                continue
            ref_stats = _average_stats(ref_frames)
            cam_stats = _average_stats(cam_frames)
            used = moments
            region = "frame"
            solve = solve_match
        if any(s <= 1e-6 for s in cam_stats.std + ref_stats.std):
            note(cam.label, f"degenerate_spread:{cam.label}")
        solved = solve(cam_stats, ref_stats)
        after = apply_stats(cam_stats, solved.gain, solved.offset)
        final[cam.label] = compose_gain_offset(
            base[cam.label] if kind != "none_rec709" else identity_cube(),
            solved.gain, solved.offset)
        matches[cam.label] = {
            "matched": True, "is_reference": False,
            "moments": [round(t, 3) for t in used],
            "region": region,
            "skin_moments": [round(t, 3) for t in used] if region == "skin" else [],
            "gain": _round6(solved.gain), "offset": _round6(solved.offset),
            "gain_clamped": list(solved.gain_clamped),
            "offset_clamped": list(solved.offset_clamped),
            "before": _stats_json(cam_stats), "reference": _stats_json(ref_stats),
            "after": _stats_json(after),
            "diff_before": [round(a - b, 6) for a, b in zip(cam_stats.mean, ref_stats.mean)],
            "diff_after": [round(a - b, 6) for a, b in zip(after.mean, ref_stats.mean)],
        }
    for cam_label, cube in final.items():
        if cube is not None:
            cube.title = f"ghostcut {cam_label} {kinds[cam_label][0]}"
            write_cube(out_dir / f"{cam_label}.cube", cube)
    spend_usd = 0.0
    check_payload: dict | None = None
    if check:
        check_cams = [c for c in clips if c.offset_s is not None
                      and (final.get(c.label) is not None or kinds[c.label][0] == "none_rec709")]
        grid = shared_moments(check_cams)[:CHECK_MOMENTS]
        if not grid:
            check_payload = None
            warnings.append("no_shared_moment_for_check")
        else:
            frames_dir = out_dir / "check-frames"
            frames_dir.mkdir(parents=True, exist_ok=True)
            frames: list[tuple[float, dict[str, Path]]] = []
            for i, moment in enumerate(grid):
                by_path = {}
                for cam in check_cams:
                    out = frames_dir / f"M{i}_{cam.label}.jpg"
                    if not (out.exists() and out.stat().st_size > 0):
                        try:
                            run_command(check_frame_command(
                                cam, moment - (cam.offset_s or 0.0),
                                out_dir / f"{cam.label}.cube" if final.get(cam.label) is not None else None,
                                out))
                        except Exception:
                            continue  # too few frames left: the moment is skipped
                    if out.exists():
                        by_path[cam.label] = out
                frames.append((round(moment, 3), by_path))
            try:
                payload, spend_usd, check_warnings = check_with_muse(
                    frames, [c.label for c in check_cams], model=model)
                warnings.extend(check_warnings)
                check_payload = {"model": model, "spend_usd": spend_usd,
                                 "moments": payload["moments"]}
            except CheckUnavailable as error:
                warnings.append(f"muse unavailable: {error}")
                check_payload = None
    payload = {
        "version": 1,
        "sync": str(sync_path),
        "reference": reference.path,
        "reference_label": reference.label,
        "model": model,
        "no_match": no_match,
        "spend_usd": spend_usd,
        "warnings": warnings,
        "check": check_payload,
        "cameras": [{
            "label": c.label,
            "path": c.path,
            "gamma": c.gamma,
            "conversion": {"kind": kinds[c.label][0],
                           "source_lut": kinds[c.label][1]},
            "lut": f"{c.label}.cube" if final.get(c.label) is not None else None,
            "source_colour": {"range": c.colour.range, "space": c.colour.space,
                              "primaries": c.colour.primaries,
                              "transfer": c.colour.transfer,
                              "pix_fmt": c.colour.pix_fmt, "bits": c.colour.bits},
            "match": matches[c.label],
            "warnings": ref_warnings[c.label],
        } for c in clips],
    }
    write_json(out_path, payload)
    return payload


def _take_frames(clip: GradeClip, local: list[float], conversion_lut: Path | None,
                 note) -> list[ChannelStats]:
    """Converted centre stats, dropping moments whose decode fails."""
    out: list[ChannelStats] = []
    for t in local:
        try:
            out.append(centre_stats(decode_centre_frame(clip, t, conversion_lut)))
        except GradeError:
            note(clip.label, f"sample_failed:{clip.label}:{t:.3f}")
    return out


def _skin_helper_cached(skin_cache: dict):
    """The built Vision helper, building once; None with a cached reason when not."""
    if "helper" not in skin_cache:
        try:
            from .sync import build_lips_helper
        except ImportError:
            skin_cache["helper"] = None
            skin_cache["helper_reason"] = "sync unavailable"
        else:
            try:
                helper, reason = build_lips_helper()
            except Exception:
                helper, reason = None, "the Vision helper could not be built"
            skin_cache["helper"] = helper
            skin_cache["helper_reason"] = reason
    return skin_cache["helper"]


def _take_skin_pair(reference: GradeClip, cam: GradeClip, moments: list[float],
                    conversion_paths: dict[str, Path | None], frames_dir: Path,
                    skin_cache: dict, note):
    """Paired skin stats at the moments with a face on both cameras.

    Each side decodes a skin frame (raw plus helper JPEG) per moment; one
    helper run reads the pair's JPEGs and the moments with a face box on
    both sides average into (reference, camera, paired moments). None
    when no moment pairs — the caller falls back to whole-frame matching.
    Skin decode failures drop that side's moment silently; the
    whole-frame path reports its own failures.
    """
    if not moments:
        return None
    helper = _skin_helper_cached(skin_cache)
    if helper is None:
        if not skin_cache.get("helper_warned"):
            skin_cache["helper_warned"] = True
            reason = skin_cache.get("helper_reason") or "unavailable"
            note(cam.label, f"skin_helper_unavailable:{reason}")
        return None
    try:
        from .sync import run_lips_helper
    except ImportError:
        return None
    ref_off = reference.offset_s or 0.0
    cam_off = cam.offset_s or 0.0
    raw: dict[tuple[str, int], bytes] = {}
    names: dict[str, tuple[str, int]] = {}
    for i, t in enumerate(moments):
        for side, clip, off in (("ref", reference, ref_off),
                                ("cam", cam, cam_off)):
            jpg = Path(frames_dir) / f"{cam.label}_{i:02d}_{side}.jpg"
            try:
                raw[(side, i)] = decode_skin_frame(
                    clip, t - off, conversion_paths.get(clip.label), jpg)
            except (GradeError, OSError):
                continue
            names[jpg.name] = (side, i)
    if not names:
        note(cam.label, f"no_face:{cam.label}")
        return None
    try:
        found = run_lips_helper(helper, Path(frames_dir))
    except Exception:
        found = None
    if found is None:
        note(cam.label, f"no_face:{cam.label}")
        return None
    try:
        ordered = sorted(p.name for p in Path(frames_dir).iterdir()
                         if not p.name.startswith(".")
                         and p.suffix.lower() in (".jpg", ".jpeg"))
    except OSError:
        note(cam.label, f"no_face:{cam.label}")
        return None
    by_index = {frame.index: frame for frame in found}
    boxes: dict[tuple[str, int], tuple[float, float, float, float] | None] = {}
    for pos, name in enumerate(ordered):
        if name not in names:
            continue
        frame = by_index.get(pos)
        boxes[names[name]] = frame.box if frame is not None else None
    ref_stats: list[ChannelStats] = []
    cam_stats: list[ChannelStats] = []
    paired: list[float] = []
    for i, t in enumerate(moments):
        if ("ref", i) not in raw or ("cam", i) not in raw:
            continue
        ref_box = boxes.get(("ref", i))
        cam_box = boxes.get(("cam", i))
        if ref_box is None or cam_box is None:
            continue
        try:
            ref_skin = skin_stats(raw[("ref", i)], SKIN_W, SKIN_H, ref_box)
            cam_skin = skin_stats(raw[("cam", i)], SKIN_W, SKIN_H, cam_box)
        except GradeError:
            continue
        if ref_skin is None or cam_skin is None:
            continue
        ref_stats.append(ref_skin)
        cam_stats.append(cam_skin)
        paired.append(t)
    if not paired:
        note(cam.label, f"no_face:{cam.label}")
        return None
    return (_average_stats(ref_stats), _average_stats(cam_stats), paired)

