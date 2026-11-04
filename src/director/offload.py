"""Card offload: mounted volumes -> verified copies on the destination drive.

`list_volumes` guesses what each mounted volume is from its layout,
`scan_card` inventories every camera clip and audio recording on a card,
`plan_offload` lays out `<destination>/<YYYY-MM-DD> <project>/<camera>/`
copies (sidecars beside their clips, identical files skipped), and
`run_offload` copies through `*.part` files that are renamed only after
a streamed checksum verifies against the source. The source card is
only ever read. `offload.json` in the shoot folder records every file.

The Ghost intent parser at the bottom turns a spoken or typed request
("the footage is on the SD card, transfer it to my SSD") into the same
plan inputs; Muse Spark is tried first when the Meta client is
available, and a deterministic rule parser covers the demo offline.
Deterministic code does all file work in both paths.

Built by Muse Code on muse-wonder-1.0 from
docs/muse-code/OFFLOAD-BRIEF.md, after the owner's review of the app
on submission day.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
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

def scan_card(volume: Path | str) -> CardScan:
    """Every camera clip and audio recording on a volume (read-only)."""
    vol = Path(volume)
    if not vol.exists():
        raise OffloadError(f"offload: no such card or folder: {vol}")
    kind, detail = guess_volume_kind(vol)
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
    clips: list[ClipInfo] = []
    for path in found:
        try:
            size = path.stat().st_size
            mtime = path.stat().st_mtime
        except OSError:
            continue
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
        clips.append(ClipInfo(source=path, size_bytes=size,
                              duration_s=probe.get("duration_s"),
                              recorded_at=recorded, time_source=source,
                              make=make, model=model, camera_label=label,
                              kind=clip_kind, sidecars=sidecars, volume=vol))
    clips.sort(key=lambda c: (c.recorded_at or datetime.max.replace(tzinfo=timezone.utc),
                              str(c.source).lower()))
    return CardScan(volume=vol, clips=clips, kind=kind, detail=detail)


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


def plan_offload(cards, destination, project: str | None = None) -> OffloadPlan:
    """Plan copies into <destination>/<date> <project>/<camera label>/."""
    dest_root = Path(destination)
    scans = [_as_scan(c) for c in list(cards or [])]
    all_clips: list[ClipInfo] = []
    for scan in scans:
        all_clips.extend(scan.clips)
    times = [c.recorded_at for c in all_clips if c.recorded_at]
    if times:
        date_str = min(times).date().isoformat()
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
    free = _free_bytes(dest_root)
    plan = OffloadPlan(shoot_dir=shoot_dir, destination=dest_root,
                       project=project_name, date_str=date_str,
                       entries=entries, free_bytes=free)
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
                still = dest.is_file() and dest.stat().st_size == entry.size_bytes
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
                 "open_job", "confirm", "reply"],
    "properties": {
        "cards": {"type": "array", "items": {"type": "string"}},
        "destination": {"type": "string"},
        "project": {"type": "string"},
        "make_cut": {"type": "boolean"},
        "open_job": {"type": "string"},
        "confirm": {"type": "boolean"},
        "reply": {"type": "string"},
    },
}
GHOST_DEVELOPER = (
    "You are Ghost, the import agent inside GhostCut Director. The editor "
    "asks to move footage from camera cards to a destination drive, or to "
    "open an existing Director job. You are given the live mounted volumes "
    "(path, kind, camera guess, free space) and the request. Choose cards "
    "only from those volumes, choose the destination only from those "
    "volumes, extract any project name the editor gives, and say whether "
    "they want the cut started afterwards. Reply in one plain sentence. "
    "Never invent a volume, a path or footage that is not listed."
)


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

    def as_dict(self) -> dict:
        return {"cards": list(self.cards), "destination": self.destination,
                "project": self.project, "make_cut": self.make_cut,
                "open_job": self.open_job, "confirm": self.confirm,
                "reply": self.reply, "method": self.method}

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
                       method="rule")


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
    return GhostIntent(cards=cards, destination=dest, project=project,
                       make_cut=bool(data.get("make_cut")),
                       open_job=(data.get("open_job") or None),
                       confirm=bool(data.get("confirm")),
                       reply=(data.get("reply") or "").strip()
                       or "Here is the transfer to plan.",
                       method="spark")


def parse_intent(text: str, volumes) -> GhostIntent:
    """Ghost's reading of a request: Spark when available, else rules."""
    spark_intent = _intent_from_spark(text, volumes)
    if spark_intent is not None:
        return spark_intent
    return parse_intent_rule(text, volumes)


parse_ghost_intent = parse_intent
