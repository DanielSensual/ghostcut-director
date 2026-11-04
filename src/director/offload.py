"""Card offload: mounted volumes -> verified copies on the destination drive.

`list_volumes` guesses what each mounted volume is from its layout,
`scan_card` inventories every camera clip and audio recording on a card
(cached, keyed by the card's clip files' names, sizes and mtimes, so a
repeat scan probes only what changed), `plan_offload` lays out
`<destination>/<YYYY-MM-DD> <project>/<camera>/` copies (sidecars beside
their clips, identical files skipped), and `run_offload` copies through
`*.part` files that are renamed only after a streamed checksum verifies
against the source. The source card is only ever read. `offload.json`
in the shoot folder records every file.

By default a plan covers only the most recent shoot day found on the
cards (the local date of each clip's recorded time) and names the
folder by that day; what was left out rides on the plan so the caller
can offer to include it. `shoot_date` plans one named day instead and
`include_all` plans the whole cards.

The Ghost intent parser at the bottom turns a spoken or typed request
("the footage is on the SD card, transfer it to my SSD") into the same
plan inputs; Muse Spark is tried first when the Meta client is
available, and a deterministic rule parser covers the demo offline.
Deterministic code does all file work in both paths.

Built by Muse Code on muse-wonder-1.0 from
docs/muse-code/OFFLOAD-BRIEF.md, after the owner's review of the app
on submission day. Latest-day planning, the scan cache and the
OFFLOAD-FIX-BRIEF.md fixes are in the same build.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date as _Date
from datetime import datetime, timedelta, timezone
from pathlib import Path

VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".mxf"}
AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".flac", ".aif", ".aiff"}
MEDIA_EXTENSIONS = VIDEO_EXTENSIONS | AUDIO_EXTENSIONS
PROXY_EXTENSIONS = {".lrf", ".thm"}
SIDECAR_EXTENSIONS = {".xml", ".srt"}
# Directories that never hold camera originals worth copying.
SKIP_DIR_NAMES = {
    "AVF_INFO", "THMBNL", ".Spotlight-V100", ".Trashes", ".fseventsd",
    "System Volume Information", ".TemporaryItems", "DATABASE",
}
CHUNK_SIZE = 1024 * 1024
KIND_CAMERA = "camera_card"
KIND_DESTINATION = "destination"
KIND_OTHER = "other"


class OffloadError(RuntimeError):
    """An offload step failed in a way the operator must see.

    The message always starts with 'offload: '.
    """


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt is not None else None


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    # ffprobe prints e.g. 2026-10-29T14:41:00.000000Z
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M:%S"):
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def local_day(moment: datetime | None) -> _Date | None:
    """The local calendar date of a recorded time (None stays None)."""
    if moment is None:
        return None
    if moment.tzinfo is None:
        return moment.date()
    return moment.astimezone().date()


def clip_day(clip) -> _Date | None:
    """The local shoot day of one clip, from its recorded time."""
    return local_day(getattr(clip, "recorded_at", None))


def format_day_short(day: _Date | None) -> str:
    """Nov 3-style label for one day (empty string for no day)."""
    if day is None:
        return ""
    return f"{day.strftime('%b')} {day.day}"


format_day = format_day_short


def format_day_range(start: _Date | None, end: _Date | None) -> str:
    """Oct 19 – Oct 29-style label for a span of days."""
    if start is None or end is None:
        return ""
    if start == end:
        return format_day_short(start)
    return f"{format_day_short(start)} – {format_day_short(end)}"


def older_summary_text(count: int, start: _Date | None,
                       end: _Date | None) -> str | None:
    """The plan card's left-out line, or None when nothing was left out."""
    if count <= 0:
        return None
    span = format_day_range(start, end)
    clips = f"{count} older clip{'s' if count != 1 else ''}"
    if span:
        return f"Also on the cards: {clips}, {span} (not copied)"
    return f"Also on the cards: {clips} (not copied)"


def _parse_day(value) -> _Date | None:
    """A YYYY-MM-DD string (or a date) as a date; None when unusable."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return local_day(value)
    if isinstance(value, _Date):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


@dataclass(slots=True)
class Volume:
    path: Path
    name: str
    kind: str
    free_bytes: int
    total_bytes: int
    detail: str = ""

    def as_dict(self) -> dict:
        return {"path": str(self.path), "name": self.name, "kind": self.kind,
                "free_bytes": self.free_bytes, "total_bytes": self.total_bytes,
                "detail": self.detail}

    __getitem__ = lambda self, k: self.as_dict()[k]  # noqa: E731

    def get(self, k, default=None):
        return self.as_dict().get(k, default)


@dataclass(slots=True)
class ClipInfo:
    source: Path
    size_bytes: int
    duration_s: float | None
    recorded_at: datetime | None
    time_source: str  # "ffprobe" | "xml" | "mtime"
    make: str = ""
    model: str = ""
    camera_label: str = ""
    kind: str = "video"  # "video" | "audio"
    sidecars: list[Path] = field(default_factory=list)
    volume: Path | None = None

    @property
    def path(self) -> Path:
        return self.source

    @property
    def name(self) -> str:
        return self.source.name

    def as_dict(self) -> dict:
        return {"source": str(self.source), "path": str(self.source),
                "name": self.source.name, "size_bytes": self.size_bytes,
                "size": self.size_bytes,
                "duration_s": self.duration_s,
                "recorded_at": _iso(self.recorded_at),
                "time_source": self.time_source,
                "make": self.make, "model": self.model,
                "camera_label": self.camera_label, "kind": self.kind,
                "sidecars": [str(p) for p in self.sidecars],
                "volume": str(self.volume) if self.volume else None}

    __getitem__ = lambda self, k: self.as_dict()[k]  # noqa: E731

    def get(self, k, default=None):
        return self.as_dict().get(k, default)


@dataclass(slots=True)
class CardScan:
    volume: Path
    clips: list[ClipInfo] = field(default_factory=list)
    kind: str = KIND_OTHER
    detail: str = ""

    def __iter__(self):
        return iter(self.clips)

    def __len__(self):
        return len(self.clips)

    def __getitem__(self, i):
        return self.clips[i]

    @property
    def total_bytes(self) -> int:
        total = 0
        for clip in self.clips:
            total += clip.size_bytes
            for side in clip.sidecars:
                try:
                    total += side.stat().st_size
                except OSError:
                    pass
        return total

    @property
    def date_range(self) -> tuple[datetime | None, datetime | None]:
        times = [c.recorded_at for c in self.clips if c.recorded_at]
        if not times:
            return None, None
        return min(times), max(times)

    def as_dict(self) -> dict:
        lo, hi = self.date_range
        return {"volume": str(self.volume), "kind": self.kind,
                "detail": self.detail, "clips": [c.as_dict() for c in self.clips],
                "clip_count": len(self.clips), "total_bytes": self.total_bytes,
                "date_start": _iso(lo), "date_end": _iso(hi)}


@dataclass(slots=True)
class PlanEntry:
    source: Path
    destination: Path
    size_bytes: int
    checksum: str | None = None
    checksum_algo: str = ""
    skipped: bool = False
    is_sidecar: bool = False
    camera_label: str = ""
    verified: bool | None = None
    error: str | None = None

    def as_dict(self) -> dict:
        return {"source": str(self.source), "destination": str(self.destination),
                "size_bytes": self.size_bytes, "size": self.size_bytes,
                "checksum": self.checksum, "checksum_algo": self.checksum_algo,
                "skipped": self.skipped, "is_sidecar": self.is_sidecar,
                "camera_label": self.camera_label,
                "verified": self.verified, "error": self.error}

    __getitem__ = lambda self, k: self.as_dict()[k]  # noqa: E731

    def get(self, k, default=None):
        return self.as_dict().get(k, default)


@dataclass(slots=True)
class OffloadPlan:
    shoot_dir: Path
    destination: Path
    project: str
    date_str: str
    entries: list[PlanEntry] = field(default_factory=list)
    free_bytes: int = 0
    # Latest-day planning: the day the folder is named by, what was
    # left out, and which cards had nothing from that day.
    shoot_day: str = ""
    include_all: bool = False
    older_count: int = 0
    older_bytes: int = 0
    older_start: str | None = None
    older_end: str | None = None
    older_summary: str | None = None
    skipped_volumes: list[str] = field(default_factory=list)
    cards_info: list[dict] = field(default_factory=list)
    selected_count: int = 0
    total_count: int = 0

    @property
    def total_bytes(self) -> int:
        return sum(e.size_bytes for e in self.entries)

    @property
    def bytes_to_copy(self) -> int:
        return sum(e.size_bytes for e in self.entries if not e.skipped)

    # Alias the space-check wording tests and the UI both reach for.
    @property
    def needed_bytes(self) -> int:
        return self.bytes_to_copy

    def as_dict(self) -> dict:
        return {"shoot_dir": str(self.shoot_dir),
                "destination": str(self.destination),
                "project": self.project, "date": self.date_str,
                "date_str": self.date_str,
                "shoot_day": self.shoot_day,
                "target_date": self.shoot_day,
                "include_all": self.include_all,
                "older_count": self.older_count,
                "excluded_count": self.older_count,
                "older_bytes": self.older_bytes,
                "older_start": self.older_start,
                "older_end": self.older_end,
                "older_summary": self.older_summary,
                "skipped_volumes": list(self.skipped_volumes),
                "skipped_cards": list(self.skipped_volumes),
                "cards": [dict(c) for c in self.cards_info],
                "cards_info": [dict(c) for c in self.cards_info],
                "selected_count": self.selected_count,
                "total_count": self.total_count,
                "entries": [e.as_dict() for e in self.entries],
                "files": [e.as_dict() for e in self.entries],
                "total_bytes": self.total_bytes,
                "bytes_to_copy": self.bytes_to_copy,
                "needed_bytes": self.bytes_to_copy,
                "free_bytes": self.free_bytes}

    __getitem__ = lambda self, k: self.as_dict()[k]  # noqa: E731

    def get(self, k, default=None):
        return self.as_dict().get(k, default)


@dataclass(slots=True)
class OffloadResult:
    shoot_dir: Path
    entries: list[PlanEntry] = field(default_factory=list)
    manifest: Path | None = None

    @property
    def verified_count(self) -> int:
        return sum(1 for e in self.entries if e.verified and not e.skipped)

    @property
    def skipped_count(self) -> int:
        return sum(1 for e in self.entries if e.skipped)

    @property
    def failed(self) -> list[PlanEntry]:
        return [e for e in self.entries if e.verified is False]

    @property
    def verified_bytes(self) -> int:
        return sum(e.size_bytes for e in self.entries
                   if e.verified or e.skipped)

    def as_dict(self) -> dict:
        return {"shoot_dir": str(self.shoot_dir),
                "manifest": str(self.manifest) if self.manifest else None,
                "offload_json": str(self.manifest) if self.manifest else None,
                "entries": [e.as_dict() for e in self.entries],
                "files": [e.as_dict() for e in self.entries],
                "verified_count": self.verified_count,
                "skipped_count": self.skipped_count,
                "failed_count": len(self.failed),
                "failed": [e.as_dict() for e in self.failed],
                "verified_bytes": self.verified_bytes,
                "total_bytes": sum(e.size_bytes for e in self.entries)}

    __getitem__ = lambda self, k: self.as_dict()[k]  # noqa: E731

    def get(self, k, default=None):
        return self.as_dict().get(k, default)


# ---------------------------------------------------------- checksums

def _hasher():
    """(algorithm name, new-hash callable). xxhash wins when present."""
    try:
        import xxhash  # type: ignore

        return "xxhash", lambda: xxhash.xxh64()
    except ImportError:
        return "blake2b", lambda: hashlib.blake2b()


CHECKSUM_ALGO, _new_hash = _hasher()


def file_checksum(path: Path, chunk_size: int = CHUNK_SIZE) -> str:
    """Streamed hex checksum of one file (never loads it whole)."""
    h = _new_hash()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


checksum_file = file_checksum


def _free_bytes(path: Path) -> int:
    probe = Path(path)
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        return shutil.disk_usage(probe).free
    except OSError:
        return 0


def _disk_total(path: Path) -> int:
    probe = Path(path)
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        return shutil.disk_usage(probe).total
    except OSError:
        return 0


# ---------------------------------------------------------- volume kinds

def _has_layout(volume: Path, *rel: str) -> bool:
    try:
        return volume.joinpath(*rel).exists()
    except OSError:
        return False


def guess_volume_kind(volume: Path) -> tuple[str, str]:
    """(kind, detail) from layout first, then name, then size."""
    vol = Path(volume)
    if _has_layout(vol, "PRIVATE", "M4ROOT", "CLIP") or _has_layout(vol, "M4ROOT", "CLIP"):
        return KIND_CAMERA, "Sony"
    if _has_layout(vol, "PRIVATE", "M4ROOT") or _has_layout(vol, "M4ROOT"):
        return KIND_CAMERA, "Sony"
    if _has_layout(vol, "AVF_INFO") and _has_layout(vol, "PRIVATE"):
        return KIND_CAMERA, "Sony"
    dcim = vol / "DCIM"
    try:
        if dcim.is_dir():
            names = [p.name for p in dcim.iterdir()]
            upper = [n.upper() for n in names]
            if any("DJI" in n for n in upper):
                return KIND_CAMERA, "DJI"
            if any("GOPRO" in n for n in upper):
                return KIND_CAMERA, "GoPro"
            if any(n.startswith("100APPLE") or n.startswith("101APPLE") for n in upper):
                return KIND_CAMERA, "iPhone"
            if names:
                return KIND_CAMERA, "DCIM"
    except OSError:
        pass
    try:
        roots = list(vol.iterdir())
    except OSError:
        roots = []
    wavs = [p for p in roots if p.is_file() and p.suffix.lower() == ".wav"]
    if wavs and not any(p.is_dir() and p.name not in SKIP_DIR_NAMES for p in roots):
        return KIND_CAMERA, "Recorder"
    name = vol.name.lower()
    if "ssd" in name or "extreme" in name or "destination" in name:
        return KIND_DESTINATION, "SSD"
    if _disk_total(vol) >= 1_000_000_000_000:
        return KIND_DESTINATION, "drive"
    return KIND_OTHER, ""


_guess_kind = guess_volume_kind


def list_volumes(volumes_root: Path | str | None = None) -> list[Volume]:
    """Mounted volumes under /Volumes, boot/installer/hidden skipped."""
    root = Path(volumes_root) if volumes_root is not None else Path("/Volumes")
    if not root.is_dir():
        return []
    out: list[Volume] = []
    try:
        entries = sorted(root.iterdir(), key=lambda p: p.name.lower())
    except OSError:
        return []
    for entry in entries:
        name = entry.name
        if name.startswith("."):
            continue
        if "installer" in name.lower():
            continue
        try:
            if not entry.is_dir():
                continue
        except OSError:
            continue
        try:
            if entry.is_symlink() and Path(os.readlink(entry)) == Path("/"):
                continue
        except OSError:
            pass
        if (entry / "System").exists() and (entry / "Applications").exists():
            continue
        try:
            kids = [p for p in entry.iterdir() if not p.name.startswith(".")]
        except OSError:
            kids = []
        plain_files = [p for p in kids if p.is_file()]
        if plain_files and all(p.suffix.lower() == ".dmg" for p in plain_files):
            continue
        kind, detail = guess_volume_kind(entry)
        if kind == KIND_CAMERA and detail in ("DCIM", "DJI", "GoPro", "iPhone"):
            if _disk_total(entry) >= 1_000_000_000_000 and len(kids) > 12:
                kind, detail = KIND_DESTINATION, "drive"
        try:
            resolved = entry.resolve()
        except OSError:
            resolved = entry
        out.append(Volume(path=resolved, name=name, kind=kind,
                          free_bytes=_free_bytes(entry),
                          total_bytes=_disk_total(entry), detail=detail))
    return out


# ---------------------------------------------------------- Sony sidecars

def _local(tag: str) -> str:
    return tag.split("}", 1)[-1]


def parse_sony_xml(path: Path) -> tuple[str, str, str | None]:
    """(make, model, CreationDate raw) from a Sony M01.XML; never raises."""
    try:
        root = ET.parse(path).getroot()
    except Exception:
        return "", "", None
    make = model = ""
    created: str | None = None
    try:
        for elem in root.iter():
            name = _local(elem.tag)
            if name == "CreationDate" and created is None:
                raw = elem.get("value") or (elem.text or "").strip()
                created = raw.strip() if raw else None
            elif name == "Device":
                make = make or (elem.get("manufacturer") or "")
                model = model or (elem.get("modelName") or elem.get("model") or "")
    except Exception:
        pass
    return make.strip(), model.strip(), created


def find_sidecar(clip_path: Path) -> Path | None:
    """The Sony `<stem>M01.XML` beside a clip, if present (exact case)."""
    cand = clip_path.parent / f"{clip_path.stem}M01.XML"
    return cand if cand.exists() else None


def _find_sidecars(clip_path: Path) -> list[Path]:
    """Sidecars that travel with a clip: Sony XML and subtitle files."""
    out: list[Path] = []
    sony = find_sidecar(clip_path)
    if sony is not None:
        out.append(sony)
    try:
        siblings = list(clip_path.parent.iterdir())
    except OSError:
        siblings = []
    for sib in siblings:
        if sib == clip_path or sib.name.startswith("."):
            continue
        if sib.suffix.lower() not in SIDECAR_EXTENSIONS:
            continue
        if sib == sony:
            continue
        # DJI/GoPro subtitles share the clip stem exactly.
        if sib.stem == clip_path.stem:
            out.append(sib)
    return sorted(out, key=lambda p: p.name)


# ---------------------------------------------------------- probing

def _probe_info(path: Path) -> dict:
    """ffprobe duration/creation/make/model; {} when unprobeable."""
    cmd = ["ffprobe", "-v", "error", "-show_entries",
           "format=duration:format_tags:stream=codec_type,codec_name,"
           "width,height:stream_tags", "-of", "json", str(path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return {}
    if proc.returncode != 0:
        return {}
    try:
        data = json.loads(proc.stdout or "{}")
    except ValueError:
        return {}
    fmt = data.get("format") or {}
    duration = None
    try:
        duration = float(fmt.get("duration"))
    except (TypeError, ValueError):
        duration = None
    scopes: list[dict] = []
    streams = [s for s in (data.get("streams") or []) if isinstance(s, dict)]
    videos = [s for s in streams if s.get("codec_type") == "video"]
    for scope in videos[:1]:
        tags = scope.get("tags")
        if isinstance(tags, dict):
            scopes.append({str(k).lower(): v for k, v in tags.items()})
    ftags = fmt.get("tags")
    if isinstance(ftags, dict):
        scopes.append({str(k).lower(): v for k, v in ftags.items()})
    for scope in streams:
        tags = scope.get("tags")
        if isinstance(tags, dict):
            scopes.append({str(k).lower(): v for k, v in tags.items()})

    def first(*names: str) -> str:
        for scope in scopes:
            for n in names:
                v = scope.get(n)
                if v is not None and str(v).strip():
                    return str(v).strip()
        return ""

    creation = first("creation_time", "com.apple.quicktime.creationdate")
    return {"duration_s": duration, "creation_time": creation or None,
            "make": first("make", "manufacturer", "com.apple.quicktime.make"),
            "model": first("model", "com.apple.quicktime.model")}


def _short_model(make: str, model: str) -> str:
    m = (model or "").strip()
    for prefix in ("ILME-", "ILCE-"):
        if m.upper().startswith(prefix):
            m = m[len(prefix):]
            break
    return m


def suggest_camera_label(make: str, model: str, volume: Path,
                         clip_path: Path, kind: str) -> str:
    """Folder-friendly camera label for one clip. Pure."""
    if kind == "audio":
        return "Audio"
    short = _short_model(make, model)
    if short:
        return re.sub(r"[\\/:*?\"<>|]+", "-", short).strip() or "Camera"
    parts = [p.upper() for p in clip_path.parts]
    name = clip_path.name.upper()
    if any("DJI" in p for p in parts) or name.startswith("DJI"):
        return "DJI"
    if any("GOPRO" in p for p in parts) or name.startswith("GOPR") or name.startswith("GX"):
        return "GoPro"
    if any(p in ("100APPLE", "101APPLE") for p in parts) or name.startswith("IMG_"):
        return "iPhone"
    if any(p in ("PRIVATE", "M4ROOT") for p in parts):
        return "Sony"
    vol = re.sub(r"[\\/:*?\"<>|]+", "-", Path(volume).name).strip()
    return vol or "Camera"


# Kept as a short alias: the label rule is the same per clip and per card.
_camera_label = suggest_camera_label


# ---------------------------------------------------------- scanning

_SCAN_CACHE: dict[str, tuple[tuple, "CardScan"]] = {}
_CLIP_CACHE: dict[tuple[str, int, int], "ClipInfo"] = {}
_SCAN_LOCK = threading.Lock()


def clear_scan_cache() -> None:
    """Forget every cached scan (tests; a card remounted fresh)."""
    with _SCAN_LOCK:
        _SCAN_CACHE.clear()
        _CLIP_CACHE.clear()


def _media_file_list(vol: Path) -> list[Path]:
    """Camera clips and audio recordings under a volume (walk only)."""
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(vol):
        # Never descend into system/thumbnail trees or hidden folders.
        dirnames[:] = [d for d in dirnames
                       if not d.startswith(".") and d not in SKIP_DIR_NAMES]
        for name in filenames:
            if name.startswith("."):
                continue
            path = Path(dirpath) / name
            suffix = path.suffix.lower()
            if suffix in PROXY_EXTENSIONS:
                continue
            if suffix not in MEDIA_EXTENSIONS:
                continue
            found.append(path)
    found.sort(key=lambda p: str(p).lower())
    return found


def _fingerprint_of(files: list[Path]) -> tuple:
    """(path, size, mtime_ns) per clip file: the scan cache key."""
    rows = []
    for path in files:
        try:
            st = path.stat()
            rows.append((str(path), st.st_size, st.st_mtime_ns))
        except OSError:
            rows.append((str(path), -1, -1))
    return tuple(rows)


def card_fingerprint(volume: Path | str) -> tuple:
    """The cache key for a volume's current clip files (walk + stat)."""
    vol = Path(volume)
    if not vol.exists():
        return ()
    return _fingerprint_of(_media_file_list(vol))


def _build_clip(path: Path, vol: Path, size: int, mtime: float) -> ClipInfo:
    """Probe and sidecar facts for one clip file (the expensive step)."""
    sidecars = _find_sidecars(path)
    probe = _probe_info(path)
    xml_make = xml_model = ""
    xml_created: str | None = None
    sony_xml = find_sidecar(path)
    if sony_xml is not None:
        xml_make, xml_model, xml_created = parse_sony_xml(sony_xml)
    make = probe.get("make") or xml_make
    model = probe.get("model") or xml_model
    recorded: datetime | None = None
    source = "mtime"
    probe_dt = _parse_iso(probe.get("creation_time"))
    if probe_dt is not None:
        recorded, source = probe_dt, "ffprobe"
    else:
        xml_dt = _parse_iso(xml_created)
        if xml_dt is not None:
            recorded, source = xml_dt, "xml"
        else:
            recorded = datetime.fromtimestamp(mtime, tz=timezone.utc)
            source = "mtime"
    clip_kind = "audio" if path.suffix.lower() in AUDIO_EXTENSIONS else "video"
    label = suggest_camera_label(make, model, vol, path, clip_kind)
    return ClipInfo(source=path, size_bytes=size,
                    duration_s=probe.get("duration_s"),
                    recorded_at=recorded, time_source=source,
                    make=make, model=model, camera_label=label,
                    kind=clip_kind, sidecars=sidecars, volume=vol)


def scan_card(volume: Path | str, progress=None) -> CardScan:
    """Every camera clip and audio recording on a volume (read-only).

    Scans are cached per volume, keyed by the clip files' names, sizes
    and mtimes: a repeat scan of an unchanged card probes nothing, and
    after a change only the new or changed clips are probed. The
    optional progress callable receives {"volume", "name", "done",
    "total", "file"} dicts as clips are inventoried.
    """
    vol = Path(volume)
    if not vol.exists():
        raise OffloadError(f"offload: no such card or folder: {vol}")
    kind, detail = guess_volume_kind(vol)
    found = _media_file_list(vol)
    fingerprint = _fingerprint_of(found)
    total = len(found)

    def emit(done: int, current: str = "") -> None:
        if progress is None:
            return
        event = {"volume": str(vol), "name": vol.name, "done": done,
                 "total": total, "file": current}
        try:
            progress(event)
        except TypeError:
            progress(done, total)

    with _SCAN_LOCK:
        hit = _SCAN_CACHE.get(str(vol))
    if hit is not None and hit[0] == fingerprint:
        emit(total)
        return hit[1]
    clips: list[ClipInfo] = []
    for index, path in enumerate(found):
        try:
            stat = path.stat()
            size, mtime_ns = stat.st_size, stat.st_mtime_ns
            mtime = stat.st_mtime
        except OSError:
            emit(index + 1, path.name)
            continue
        key = (str(path), size, mtime_ns)
        with _SCAN_LOCK:
            clip = _CLIP_CACHE.get(key)
        if clip is None:
            clip = _build_clip(path, vol, size, mtime)
            with _SCAN_LOCK:
                _CLIP_CACHE[key] = clip
        clips.append(clip)
        emit(index + 1, path.name)
    clips.sort(key=lambda c: (c.recorded_at or datetime.max.replace(tzinfo=timezone.utc),
                              str(c.source).lower()))
    scan = CardScan(volume=vol, clips=clips, kind=kind, detail=detail)
    with _SCAN_LOCK:
        _SCAN_CACHE[str(vol)] = (fingerprint, scan)
    return scan


scan_card_cached = scan_card


def _as_scan(item) -> CardScan:
    """A plan input (path / Volume / CardScan / clip list) as a CardScan."""
    if isinstance(item, CardScan):
        return item
    if isinstance(item, Volume):
        return scan_card(item.path)
    if isinstance(item, (str, Path)):
        return scan_card(Path(item))
    if isinstance(item, dict) and "clips" in item:
        clips = []
        for raw in item.get("clips") or []:
            if isinstance(raw, ClipInfo):
                clips.append(raw)
        vol = Path(item.get("volume") or ".")
        return CardScan(volume=vol, clips=clips,
                        kind=str(item.get("kind") or KIND_OTHER),
                        detail=str(item.get("detail") or ""))
    if isinstance(item, ClipInfo):
        vol = item.volume or item.source.parent
        return CardScan(volume=Path(vol), clips=[item])
    if isinstance(item, (list, tuple)):
        clips = [c for c in item if isinstance(c, ClipInfo)]
        vol = clips[0].volume if clips and clips[0].volume else Path(".")
        return CardScan(volume=Path(vol), clips=clips)
    raise OffloadError(f"offload: cannot plan from {type(item).__name__}")


def _sanitize_project(project: str | None) -> str:
    text = re.sub(r"[\\/:*?\"<>|]+", "-", (project or "").strip())
    text = re.sub(r"\s+", " ", text).strip(" .-")
    return text or "Shoot"


def _human(n: int) -> str:
    value = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1000 or unit == "TB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1000.0
    return f"{value:.1f} TB"


def plan_offload(cards, destination, project: str | None = None,
                 shoot_date=None, include_all: bool = False) -> OffloadPlan:
    """Plan copies into <destination>/<date> <project>/<camera label>/.

    By default only the most recent shoot day on the cards is planned
    (the local date of each clip's recorded time) and the folder is
    named by that day. `shoot_date` (a date or YYYY-MM-DD) plans that
    day instead; `include_all` plans every clip. Cards with no clips
    in scope are listed as skipped on the plan, and the clips left out
    are counted on it (`older_count`, `older_summary`).
    """
    dest_root = Path(destination)
    scans = [_as_scan(c) for c in list(cards or [])]
    rows: list[tuple[CardScan, ClipInfo, _Date | None]] = []
    for scan in scans:
        for clip in scan.clips:
            rows.append((scan, clip, clip_day(clip)))
    dated_days = [day for _, _, day in rows if day is not None]
    target: _Date | None = None
    if not include_all:
        if shoot_date is not None:
            target = _parse_day(shoot_date)
            if target is None:
                raise OffloadError(
                    f"offload: cannot read shoot date {shoot_date!r} "
                    "(want YYYY-MM-DD)")
        elif dated_days:
            target = max(dated_days)
    if include_all or target is None:
        selected_rows = list(rows)
    else:
        selected_rows = [row for row in rows if row[2] == target]
    selected_ids = {id(clip) for _, clip, _ in selected_rows}
    excluded_rows = [row for row in rows if id(row[1]) not in selected_ids]
    all_clips = [clip for _, clip, _ in selected_rows]
    older_days = [day for _, _, day in excluded_rows if day is not None]
    older_start = min(older_days) if older_days else None
    older_end = max(older_days) if older_days else None
    if target is not None:
        date_str = target.isoformat()
    elif dated_days:
        date_str = min(dated_days).isoformat()
    else:
        date_str = datetime.now(timezone.utc).date().isoformat()
    project_name = _sanitize_project(project)
    shoot_dir = dest_root / f"{date_str} {project_name}"
    # Final folder label per (volume, base label); duplicates numbered.
    groups: dict[tuple[str, str], list[ClipInfo]] = {}
    for clip in all_clips:
        key = (str(clip.volume or ""), clip.camera_label or "Camera")
        groups.setdefault(key, []).append(clip)
    base_counts: dict[str, int] = {}
    for _, base in groups:
        base_counts[base] = base_counts.get(base, 0) + 1
    seen_base: dict[str, int] = {}
    label_for: dict[tuple[str, str], str] = {}
    for key in sorted(groups, key=lambda k: (k[1], k[0])):
        base = key[1]
        if base_counts[base] > 1:
            seen_base[base] = seen_base.get(base, 0) + 1
            label_for[key] = base if seen_base[base] == 1 else f"{base} {seen_base[base]}"
        else:
            label_for[key] = base
    entries: list[PlanEntry] = []
    for clip in all_clips:
        key = (str(clip.volume or ""), clip.camera_label or "Camera")
        label = label_for[key]
        folder = shoot_dir / label
        jobs: list[tuple[Path, bool]] = [(clip.source, False)]
        jobs += [(s, True) for s in clip.sidecars]
        for src, is_side in jobs:
            try:
                size = src.stat().st_size
            except OSError as e:
                raise OffloadError(f"offload: cannot read source {src}: {e}") from e
            dest = folder / src.name
            entries.append(PlanEntry(source=src, destination=dest,
                                     size_bytes=size, is_sidecar=is_side,
                                     camera_label=label,
                                     checksum_algo=CHECKSUM_ALGO))
    entries.sort(key=lambda e: (e.camera_label, str(e.destination).lower()))
    # Skip-if-identical: same size + same streamed checksum already there.
    for entry in entries:
        try:
            if not entry.destination.is_file():
                continue
            if entry.destination.stat().st_size != entry.size_bytes:
                continue
            src_sum = file_checksum(entry.source)
            dst_sum = file_checksum(entry.destination)
            entry.checksum = src_sum
            if src_sum == dst_sum:
                entry.skipped = True
        except OSError:
            entry.skipped = False
    # Per-card summary for the plan card: what each card contributes,
    # and which cards have nothing from the planned day (skipped).
    cards_info: list[dict] = []
    skipped_volumes: list[str] = []
    for scan in scans:
        mine = [clip for s, clip, _ in selected_rows if s is scan]
        mine_days = [clip_day(c) for c in mine]
        mine_days = [d for d in mine_days if d is not None]
        vol_entries = [e for e in entries
                       if any(e.source == c.source for c in mine)
                       or any(e.source == side for c in mine
                              for side in c.sidecars)]
        skipped = not mine
        if skipped:
            skipped_volumes.append(str(scan.volume))
        cards_info.append({
            "volume": str(scan.volume), "name": Path(scan.volume).name,
            "clip_count": len(mine), "total_clips": len(scan.clips),
            "total_bytes": sum(e.size_bytes for e in vol_entries),
            "date_start": min(mine_days).isoformat() if mine_days else None,
            "date_end": max(mine_days).isoformat() if mine_days else None,
            "skipped": skipped})
    free = _free_bytes(dest_root)
    plan = OffloadPlan(
        shoot_dir=shoot_dir, destination=dest_root,
        project=project_name, date_str=date_str,
        entries=entries, free_bytes=free,
        shoot_day=target.isoformat() if target is not None else date_str,
        include_all=bool(include_all),
        older_count=len(excluded_rows),
        older_bytes=sum(clip.size_bytes for _, clip, _ in excluded_rows),
        older_start=older_start.isoformat() if older_start else None,
        older_end=older_end.isoformat() if older_end else None,
        older_summary=older_summary_text(len(excluded_rows), older_start,
                                         older_end),
        skipped_volumes=skipped_volumes, cards_info=cards_info,
        selected_count=len(selected_rows), total_count=len(rows))
    if plan.bytes_to_copy > free:
        raise OffloadError(
            f"offload: not enough space on {dest_root}: need "
            f"{plan.bytes_to_copy} bytes ({_human(plan.bytes_to_copy)}) "
            f"but only {free} bytes ({_human(free)}) free")
    return plan


# ---------------------------------------------------------- copying

def run_offload(plan: OffloadPlan, progress=None) -> OffloadResult:
    """Copy a plan via *.part files, verifying each before its rename."""
    shoot_dir = Path(plan.shoot_dir)
    shoot_dir.mkdir(parents=True, exist_ok=True)
    entries = list(plan.entries)
    total_bytes = sum(e.size_bytes for e in entries if not e.skipped)
    done_bytes = 0
    started = time.monotonic()
    results: list[PlanEntry] = []

    def emit(entry: PlanEntry, file_done: int) -> None:
        if progress is None:
            return
        elapsed = max(time.monotonic() - started, 1e-6)
        rate = done_bytes / elapsed if done_bytes else 0.0
        eta = (total_bytes - done_bytes) / rate if rate > 0 else None
        idx = next((i for i, e in enumerate(entries) if e is entry),
                   len(results))
        event = {"file": entry.destination.name,
                 "source": str(entry.source),
                 "destination": str(entry.destination),
                 "file_index": idx,
                 "file_count": len(entries),
                 "index": idx, "total_files": len(entries),
                 "file_bytes_done": file_done,
                 "file_bytes_total": entry.size_bytes,
                 "bytes_done": done_bytes, "bytes_total": total_bytes,
                 "bytes": done_bytes, "total": total_bytes,
                 "eta_s": eta, "eta": eta,
                 "fraction": (done_bytes / total_bytes) if total_bytes else 1.0}
        try:
            progress(event)
        except TypeError:
            # Tolerate simple progress(done, total, file) callables.
            try:
                progress(done_bytes, total_bytes, entry.destination.name)
            except Exception:
                raise

    for entry in entries:
        dest = Path(entry.destination)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if entry.skipped:
            # Re-confirm cheaply: a same-size file is still there.
            try:
                still = dest.is_file() and dest.stat().st_size == entry.size_bytes and file_checksum(dest) == file_checksum(entry.source)
            except OSError:
                still = False
            if still:
                if not entry.checksum:
                    try:
                        entry.checksum = file_checksum(entry.source)
                    except OSError:
                        entry.checksum = None
                entry.verified = True
                results.append(entry)
                emit(entry, entry.size_bytes)
                continue
            entry.skipped = False
        # Already copied and identical (resume after a finished run).
        try:
            if dest.is_file() and dest.stat().st_size == entry.size_bytes:
                src_sum = entry.checksum or file_checksum(entry.source)
                if file_checksum(dest) == src_sum:
                    entry.checksum = src_sum
                    entry.skipped = True
                    entry.verified = True
                    results.append(entry)
                    emit(entry, entry.size_bytes)
                    continue
        except OSError:
            pass
        part = Path(str(dest) + ".part")
        src_hash = _new_hash()
        copied = 0
        emit(entry, 0)
        # Stream source -> .part, hashing the source as it goes. Any
        # failure leaves the partial .part for the next attempt.
        with open(entry.source, "rb") as fin, open(part, "wb") as fout:
            while True:
                chunk = fin.read(CHUNK_SIZE)
                if not chunk:
                    break
                fout.write(chunk)
                src_hash.update(chunk)
                copied += len(chunk)
                # Progress per chunk keeps the bar live on big clips;
                # done_bytes only advances when the file verifies.
                if progress is not None:
                    elapsed = max(time.monotonic() - started, 1e-6)
                    live = done_bytes + copied
                    rate = live / elapsed if live else 0.0
                    eta = (total_bytes - live) / rate if rate > 0 else None
                    idx = next((i for i, e in enumerate(entries) if e is entry),
                               len(results))
                    event = {"file": dest.name, "source": str(entry.source),
                             "destination": str(dest),
                             "file_index": idx,
                             "file_count": len(entries),
                             "index": idx, "total_files": len(entries),
                             "file_bytes_done": copied,
                             "file_bytes_total": entry.size_bytes,
                             "bytes_done": live, "bytes_total": total_bytes,
                             "bytes": live, "total": total_bytes,
                             "eta_s": eta, "eta": eta,
                             "fraction": (live / total_bytes) if total_bytes else 1.0}
                    try:
                        progress(event)
                    except TypeError:
                        progress(live, total_bytes, dest.name)
        entry.checksum = src_hash.hexdigest()
        entry.checksum_algo = CHECKSUM_ALGO
        try:
            dst_sum = file_checksum(part)
        except OSError as e:
            entry.verified = False
            entry.error = f"cannot verify copy: {e}"
            results.append(entry)
            continue
        if dst_sum != entry.checksum:
            entry.verified = False
            entry.error = "checksum mismatch"
            results.append(entry)
            continue
        os.replace(part, dest)
        entry.verified = True
        entry.error = None
        done_bytes += entry.size_bytes
        results.append(entry)
        emit(entry, entry.size_bytes)
    result = OffloadResult(shoot_dir=shoot_dir, entries=results)
    payload = {"version": 1, "shoot_dir": str(shoot_dir),
               "destination": str(plan.destination),
               "project": plan.project, "date": plan.date_str,
               "checksum_algo": CHECKSUM_ALGO,
               "created_at": datetime.now(timezone.utc).isoformat(),
               "files": [e.as_dict() for e in results],
               "entries": [e.as_dict() for e in results],
               "sources": [str(e.source) for e in results],
               "destinations": [str(e.destination) for e in results],
               "sizes": [e.size_bytes for e in results],
               "checksums": [e.checksum for e in results],
               "verified": [bool(e.verified) for e in results],
               "skipped": [bool(e.skipped) for e in results],
               "verified_count": result.verified_count,
               "skipped_count": result.skipped_count,
               "failed_count": len(result.failed),
               "total_bytes": sum(e.size_bytes for e in results),
               "verified_bytes": result.verified_bytes}
    manifest = shoot_dir / "offload.json"
    manifest.write_text(json.dumps(payload, indent=1), encoding="utf-8")
    result.manifest = manifest
    return result


# ---------------------------------------------------------- Ghost intent

GHOST_MODEL = "muse-spark-1.3"
INTENT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["cards", "destination", "project", "make_cut",
                 "open_job", "confirm", "reply", "date", "include_all"],
    "properties": {
        "cards": {"type": "array", "items": {"type": "string"}},
        "destination": {"type": "string"},
        "project": {"type": "string"},
        "make_cut": {"type": "boolean"},
        "open_job": {"type": "string"},
        "confirm": {"type": "boolean"},
        "reply": {"type": "string"},
        "date": {"type": "string"},
        "include_all": {"type": "boolean"},
    },
}
GHOST_DEVELOPER = (
    "You are Ghost, the import agent inside GhostCut Director. The editor "
    "asks to move footage from camera cards to a destination drive, or to "
    "open an existing Director job. You are given the live mounted volumes "
    "(path, kind, camera guess, free space) and the request. Choose cards "
    "only from those volumes, choose the destination only from those "
    "volumes, extract any project name the editor gives, and say whether "
    "they want the cut started afterwards. By default only the most "
    "recent shoot day on the cards is copied: leave date empty and "
    "include_all false unless the editor names a shoot date (return it "
    "as date in YYYY-MM-DD) or asks for everything / all of it (set "
    "include_all true). Reply in one plain sentence. "
    "Never invent a volume, a path or footage that is not listed."
)

# ONBOARD: Ghost's briefing, shared by the help reply, the tour and Spark.
GHOST_BRIEFING = (
    "How an import goes: the editor inserts one or more camera cards, "
    "names a destination drive (usually the SSD) and, if they like, a "
    "project name. Ghost scans each card, then plans the copy. Only the "
    "most recent shoot day is planned unless the editor names a date or "
    "asks for everything; older footage is listed so it can be included. "
    "Transfer copies every file and keeps it only when its checksum "
    "matches the card. Afterwards, Make the cut runs sync, words, grade "
    "and angles on the copied shoot. Ghost understands which card, which "
    "drive, a project name, a shoot date, 'all of it', 'go ahead' and "
    "'open the last job'. Ghost never copies without the editor's "
    "confirm, never invents a volume, and never writes to a card."
)
GHOST_DEVELOPER = GHOST_DEVELOPER + " " + GHOST_BRIEFING  # ONBOARD

_HELP_RE = re.compile(  # ONBOARD
    r"\b(what can you do|what do you do|how does (this|it) work|"
    r"tutorial|walk me through)\b", re.IGNORECASE)
_HELP_WEAK_RE = re.compile(r"\b(help|get(ting)? started)\b", re.IGNORECASE)  # ONBOARD
# ONBOARD: "help me copy the card to my SSD" is a request, not a help question.
_ACTION_RE = re.compile(
    r"\b(copy|transfer|move|offload|import|back ?up|put|send|open|go ahead|"
    r"include|plan)\b", re.IGNORECASE)


def is_help(text: str) -> bool:  # ONBOARD
    """True when the editor asks how Ghost works rather than for a copy."""
    t = str(text or "")
    if _HELP_RE.search(t):
        return True
    return bool(_HELP_WEAK_RE.search(t)) and not _ACTION_RE.search(t)


@dataclass(slots=True)
class GhostIntent:
    cards: list[str] = field(default_factory=list)
    destination: str | None = None
    project: str | None = None
    make_cut: bool = False
    open_job: str | None = None
    confirm: bool = False
    reply: str = ""
    method: str = "rule"
    # Which shoot to plan: one named day (YYYY-MM-DD), everything on
    # the cards, or (both unset) the most recent shoot day.
    date: str | None = None
    include_all: bool = False

    @property
    def everything(self) -> bool:
        return self.include_all

    @property
    def shoot_date(self) -> str | None:
        return self.date

    def as_dict(self) -> dict:
        return {"cards": list(self.cards), "destination": self.destination,
                "project": self.project, "make_cut": self.make_cut,
                "open_job": self.open_job, "confirm": self.confirm,
                "reply": self.reply, "method": self.method,
                "date": self.date, "shoot_date": self.date,
                "include_all": self.include_all,
                "everything": self.include_all}

    __getitem__ = lambda self, k: self.as_dict()[k]  # noqa: E731

    def get(self, k, default=None):
        return self.as_dict().get(k, default)


def _vol_get(vol, key: str, default=None):
    if isinstance(vol, dict):
        return vol.get(key, default)
    return getattr(vol, key, default)


def _vol_path(vol) -> str:
    value = _vol_get(vol, "path", "")
    return str(value or "")


_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
           "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12}
_MONTH_RE = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*"


def _request_date(raw: str) -> str | None:
    """A shoot date named in a request, as YYYY-MM-DD (or None).

    Handles 2026-11-03, "Nov 3", "November 3rd, 2026" and "3 Nov"; a
    yearless date resolves to its most recent occurrence, since
    footage being copied was shot in the past. "yesterday" is the
    calendar day before today. "today" names no date: the default
    already plans the most recent shoot day, which is what an editor
    means by today's shoot even when the cards were shot yesterday.
    """
    text = raw or ""
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", text)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)),
                            int(m.group(3))).date().isoformat()
        except ValueError:
            return None
    today = datetime.now().astimezone().date()
    found: _Date | None = None
    m = re.search(_MONTH_RE + r"\s+(\d{1,2})(?:st|nd|rd|th)?"
                  r"(?:\s*,?\s*(\d{4}))?\b", text, re.I)
    if m:
        month, day_n = _MONTHS[m.group(1).lower()[:3]], int(m.group(2))
        year = int(m.group(3)) if m.group(3) else None
        found = _dated(month, day_n, year, today)
    if found is None:
        m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+" + _MONTH_RE +
                      r"(?:\s*,?\s*(\d{4}))?\b", text, re.I)
        if m:
            month = _MONTHS[m.group(2).lower()[:3]]
            year = int(m.group(3)) if m.group(3) else None
            found = _dated(month, int(m.group(1)), year, today)
    if found is not None:
        return found.isoformat()
    if re.search(r"\byesterday\b", text, re.I):
        return (today - timedelta(days=1)).isoformat()
    return None


def _dated(month: int, day_n: int, year: int | None,
           today: _Date) -> _Date | None:
    try:
        if year is not None:
            return _Date(year, month, day_n)
        candidate = _Date(today.year, month, day_n)
    except ValueError:
        return None
    if candidate > today:
        try:
            candidate = _Date(today.year - 1, month, day_n)
        except ValueError:
            return None
    return candidate


def wants_everything(low: str) -> bool:
    """True for "everything" / "all of it" / "include them" phrasings."""
    return bool(re.search(
        r"\beverything\b|\ball of it\b|\ball clips\b|\ball the clips\b|"
        r"\bwhole card|\bentire card|\ball dates\b|"
        r"\binclude (them|the older|the rest|older)\b", low))


def parse_intent_rule(text: str, volumes) -> GhostIntent:
    """Deterministic Ghost: the demo path when no Meta key is present."""
    vols = list(volumes or [])
    raw = (text or "").strip()
    low = f" {(raw.lower())} "
    cameras = [v for v in vols if _vol_get(v, "kind") == KIND_CAMERA]
    dests = [v for v in vols if _vol_get(v, "kind") == KIND_DESTINATION]
    # Confirmation of a plan already on screen.
    if re.fullmatch(r"\s*(yes|yeah|yep|go|yes,?\s*go|do it|transfer|ok|okay)\s*[.!]?\s*",
                    raw.lower()):
        return GhostIntent(confirm=True, reply="On it — starting the transfer.",
                           method="rule")
    # Opening an existing job beats transferring.
    open_job: str | None = None
    path_hit = re.search(r"(/Volumes/[^\s\"']+|/Users/[^\s\"']+|~/[^\s\"']+)",
                         raw)
    if "open" in low and ("job" in low or path_hit):
        open_job = path_hit.group(1) if path_hit else "yesterday"
    if open_job:
        return GhostIntent(open_job=open_job,
                           reply="Opening that job.", method="rule")
    # Cards: a named source wins, else the generic card phrasings.
    cards: list = []
    if "drone" in low or "dji" in low or "osmo" in low:
        cards = [v for v in cameras if "dji" in str(_vol_get(v, "detail", "")).lower()
                 or "dji" in str(_vol_get(v, "name", "")).lower()]
        if not cards:
            cards = list(cameras)
    elif "gopro" in low:
        cards = [v for v in cameras if "gopro" in str(_vol_get(v, "detail", "")).lower()]
        if not cards:
            cards = list(cameras)
    elif "iphone" in low or "phone" in low:
        cards = [v for v in cameras if "iphone" in str(_vol_get(v, "detail", "")).lower()]
        if not cards:
            cards = list(cameras)
    elif "rode" in low or "recorder" in low or "lav" in low:
        cards = [v for v in cameras if "record" in str(_vol_get(v, "detail", "")).lower()]
        if not cards:
            cards = list(cameras)
    else:
        named = [v for v in cameras
                 if str(_vol_get(v, "name", "")).lower()
                 and str(_vol_get(v, "name", "")).lower() in low]
        if named:
            cards = named
        elif any(h in low for h in (" sd card", " sd cards", " the card", " cards",
                                    " footage", " transfer", " import", " offload")):
            cards = list(cameras)
        elif len(cameras) == 1:
            cards = list(cameras)
    # Destination: "my SSD" / "extreme" is the house phrasing.
    destination = None
    if dests:
        if "extreme" in low or "ssd" in low:
            extreme = [v for v in dests if "extreme" in str(_vol_get(v, "name", "")).lower()]
            destination = _vol_path((extreme or dests)[0])
        else:
            named_d = [v for v in dests
                       if str(_vol_get(v, "name", "")).lower()
                       and str(_vol_get(v, "name", "")).lower() in low]
            destination = _vol_path(named_d[0]) if named_d else _vol_path(dests[0])
    # Project name only after an explicit naming word.
    project: str | None = None
    m = re.search(r"(?:project|call it|called|named)\s+[\"']([^\"']+)[\"']", raw, re.I)
    if m:
        project = m.group(1).strip()
    else:
        m = re.search(r"(?:project|call it|called|named)\s+([A-Za-z0-9][A-Za-z0-9 _-]{1,40})",
                      raw, re.I)
        if m:
            cand = re.split(r"\b(to|on|then|and make|and start)\b", m.group(1),
                            flags=re.I)[0].strip(" .,")
            if cand and cand.lower() not in ("my ssd", "the ssd", "ssd"):
                project = cand
    make_cut = bool(re.search(
        r"make the cut|start the cut|cut afterwards|then cut|and cut|"
        r"make.*\bcut\b", low))
    # Scope: everything on the cards, one named day, or (default) the
    # most recent shoot day, which plan_offload picks after scanning.
    include_all = wants_everything(low)
    req_date = None if include_all else _request_date(raw)
    if include_all and not cards:
        cards = list(cameras)
    card_paths = [_vol_path(v) for v in cards]
    if card_paths and destination:
        names = ", ".join(str(_vol_get(v, "name", "")) for v in cards)
        reply = (f"Found {len(card_paths)} card{'s' if len(card_paths) != 1 else ''} "
                 f"({names}) — here is the transfer to plan.")
    elif card_paths:
        reply = f"Found {len(card_paths)} card{'s' if len(card_paths) != 1 else ''} — pick a destination to continue."
    elif destination:
        reply = "I don't see a camera card mounted — insert one and ask again."
    else:
        reply = "Tell me which card to copy and where to put it."
    return GhostIntent(cards=card_paths, destination=destination,
                       project=project, make_cut=make_cut, reply=reply,
                       method="rule", date=req_date,
                       include_all=include_all)


# Aliases: one parser, the spellings callers and tests reach for.
parse_rule_intent = parse_intent_rule
rule_parse_intent = parse_intent_rule


def _intent_from_spark(text: str, volumes) -> GhostIntent | None:
    """One Spark call; None when the Meta client is unavailable."""
    import os as _os

    if not _os.environ.get("MODEL_API_KEY"):
        return None
    try:
        from guestlist.meta import cache, spark
    except ImportError:
        return None
    vols = list(volumes or [])
    facts = [{"path": _vol_path(v), "name": str(_vol_get(v, "name", "")),
              "kind": str(_vol_get(v, "kind", "")),
              "detail": str(_vol_get(v, "detail", "")),
              "free_bytes": _vol_get(v, "free_bytes", 0),
              "total_bytes": _vol_get(v, "total_bytes", 0)} for v in vols]
    prompt = (f"VOLUMES: {json.dumps(facts)}\n\nREQUEST: {text.strip()}\n"
              "cards/destination must be volume paths from VOLUMES "
              "(empty string / empty list when the request names none).")
    try:
        model = "muse-spark-1.3"
        key = cache.key("ghost", model, GHOST_DEVELOPER, prompt,
                        INTENT_SCHEMA, "medium")
        hit = cache.get("spark", key)
        if hit:
            data = hit["data"]
        else:
            cache.guard(0.25)
            result = spark._structured(
                key, [{"type": "input_text", "text": prompt}],
                schema=INTENT_SCHEMA, name="ghost_intent",
                developer=GHOST_DEVELOPER, model=model, effort="medium",
                max_output_tokens=2000, record={}, detail={"request": text[:80]})
            data = result.data
    except Exception:
        return None
    allowed = {_vol_path(v) for v in vols}
    cards = [c for c in (data.get("cards") or []) if c in allowed]
    dest = data.get("destination") or None
    if dest not in allowed:
        dest = None
    project = (data.get("project") or "").strip() or None
    spark_date = (data.get("date") or "").strip() or None
    if spark_date is not None and _parse_day(spark_date) is None:
        spark_date = None
    return GhostIntent(cards=cards, destination=dest, project=project,
                       make_cut=bool(data.get("make_cut")),
                       open_job=(data.get("open_job") or None),
                       confirm=bool(data.get("confirm")),
                       reply=(data.get("reply") or "").strip()
                       or "Here is the transfer to plan.",
                       method="spark", date=spark_date,
                       include_all=bool(data.get("include_all")))


def parse_intent(text: str, volumes) -> GhostIntent:
    """Ghost's reading of a request: Spark when available, else rules."""
    spark_intent = _intent_from_spark(text, volumes)
    if spark_intent is not None:
        return spark_intent
    return parse_intent_rule(text, volumes)


parse_ghost_intent = parse_intent
