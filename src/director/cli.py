from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ghostcut.ffmpeg_tools import ToolingError, require_ffmpeg_binaries


def command_grade(args: argparse.Namespace) -> dict:
    from .grade import run as run_grade

    if not bool(args.no_match) or bool(args.check):
        require_ffmpeg_binaries()
    payload = run_grade(
        sync_path=Path(args.sync),
        out_path=Path(args.out),
        reference_label=args.reference,
        lut_specs=list(args.lut or []),
        no_match=bool(args.no_match),
        check=bool(args.check),
        model=args.model,
    )
    for camera in payload.get("cameras", []):
        match = camera.get("match", {})
        line = f"{camera['label']} {camera.get('gamma', '')} {camera['conversion']['kind']}"
        if match.get("matched"):
            gains = ",".join(str(value) for value in match["gain"])
            offsets = ",".join(str(value) for value in match["offset"])
            line += f" gain {gains} offset {offsets}"
            if any(match["gain_clamped"]) or any(match["offset_clamped"]):
                line += " clamped"
        print(line)
    for warning in payload.get("warnings", []):
        print(f"! {warning}")
    print(f"Wrote {args.out}")
    return payload


def command_angles(args: argparse.Namespace) -> dict:
    from .angles import run as run_angles

    if (not bool(getattr(args, "no_muse", False)) or getattr(args, "render", None)
            or getattr(args, "grade", None)):
        require_ffmpeg_binaries()
    payload = run_angles(
        sync_path=Path(args.sync),
        words_path=Path(args.words),
        out_path=Path(args.out),
        audio_label=args.audio,
        render_path=Path(args.render) if args.render else None,
        min_shot_s=float(args.min_shot),
        model=args.model,
        no_muse=bool(args.no_muse),
        grade_path=Path(args.grade) if getattr(args, "grade", None) else None,
    )
    for shot in payload.get("shots", []):
        for overrule in shot.get("overrules", []):
            print(f"  shot {shot['id']} {shot['camera']}: {overrule}")
    n_lines, n_shots = len(payload.get("lines", [])), len(payload.get("shots", []))
    print(f"{n_lines} lines → {n_shots} shots ({payload.get('method')}, ${payload.get('spend_usd', 0.0):.4f})")
    return payload


def command_sync(args: argparse.Namespace) -> dict:
    from . import sync as sync_mod

    require_ffmpeg_binaries()
    payload = sync_mod.run(
        clip_paths=[Path(p) for p in args.clips],
        reference_path=Path(args.reference) if args.reference else None,
        methods=sync_mod.parse_methods(args.methods),
        transcripts_dir=Path(args.transcripts) if args.transcripts else None,
        out_path=Path(args.out),
    )
    for item in payload.get("clips", []):
        cam = item.get("camera", {})
        parts = [str(item.get("label") or ""), Path(str(cam.get("path") or "")).name]
        if cam.get("make"):
            parts.append(str(cam["make"]))
        if cam.get("model"):
            parts.append(str(cam["model"]))
        parts.append(str(cam.get("gamma") or "-"))
        parts.append(f"{float(cam.get('fps') or 0.0):.2f}fps")
        offset = item.get("offset_s")
        parts.append("offset")
        parts.append(f"{float(offset):+.3f}s" if offset is not None else "--")
        parts.append("method")
        parts.append(str(item.get("method") or "unplaced"))
        print(" ".join(part for part in parts if part))
        for check in item.get("checks", []):
            verdict = "agree" if check.get("agree") else "DISAGREE"
            print(f"  check {check['a']} vs {check['b']}: "
                  f"{verdict} (delta {float(check['delta_s']):.3f}s)")
        for warning in item.get("warnings", []):
            print(f"  ! {warning}")
    print(f"Wrote {args.out}")
    return payload


def command_reframe(args: argparse.Namespace) -> dict:
    from .reframe import run as run_reframe

    require_ffmpeg_binaries()
    payload = run_reframe(
        video_path=Path(args.video),
        out_path=Path(args.out),
        render_path=Path(args.render) if args.render else None,
        shots_path=Path(args.shots) if args.shots else None,
        no_sam=bool(args.no_sam),
        max_usd=float(args.max_usd),
    )
    for shot in payload.get("shots", []):
        if shot.get("method") == "subject":
            subject = f"object {shot['object_id']} presence {shot['presence']:.2f}"
        else:
            subject = "-"
        print(f"shot {shot['id']} {shot['start_s']:.3f}-{shot['end_s']:.3f}s "
              f"{shot['method']} {subject} {shot['reason']}")
    for warning in payload.get("warnings", []):
        print(f"! {warning}")
    print(f"spend ${payload.get('spend_usd', 0.0):.4f} "
          f"({payload.get('frames_billed', 0)} frames billed, "
          f"method {payload.get('method')})")
    if payload.get("render"):
        print(f"Rendered {args.render} ({payload['render']['crop_moves']} crop moves)")
    print(f"Wrote {args.out}")
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="GhostCut Director: multicam cutting on Meta's Muse models")
    sub = parser.add_subparsers(dest="command", required=True)

    angles_parser = sub.add_parser("angles", help="Pick the camera angle for each spoken line")
    angles_parser.add_argument("--sync", required=True, help="sync.json from director sync")
    angles_parser.add_argument("--words", required=True, help="Word-timed Whisper JSON for the audio clip")
    angles_parser.add_argument("--out", required=True, help="Where to write angles.json")
    angles_parser.add_argument("--audio", default=None, help="Audio clip label (default: the reference clip)")
    angles_parser.add_argument("--render", default=None, help="Render the cut to this .mp4 path")
    angles_parser.add_argument("--min-shot", type=float, default=2.0, help="Shorter shots merge into a neighbour")
    angles_parser.add_argument("--model", default="muse-spark-1.3")
    angles_parser.add_argument("--no-muse", action="store_true", help="Use the deterministic rule fallback")
    angles_parser.add_argument("--grade", default=None, help="grade.json from director grade")
    angles_parser.set_defaults(func=command_angles)

    grade_parser = sub.add_parser("grade", help="Grade each camera to Rec.709 and match to a reference")
    grade_parser.add_argument("--sync", required=True, help="sync.json from director sync")
    grade_parser.add_argument("--out", required=True, help="Where to write grade.json")
    grade_parser.add_argument("--reference", default=None, help="Reference camera label (default: the sync reference)")
    grade_parser.add_argument("--lut", action="append", default=[],
                              help="Supplied .cube as LABEL=PATH (repeatable)")
    grade_parser.add_argument("--no-match", action="store_true", help="Write conversions only, skip matching")
    grade_parser.add_argument("--check", action="store_true", help="Ask Muse Spark to check the match")
    grade_parser.add_argument("--model", default="muse-spark-1.3")
    grade_parser.set_defaults(func=command_grade)

    sync_parser = sub.add_parser("sync", help="Place every clip on one shared timeline")
    sync_parser.add_argument("clips", nargs="+", help="Clips to place on the timeline")
    sync_parser.add_argument("--reference", default=None,
                             help="Reference clip (default: the longest clip)")
    sync_parser.add_argument("--methods", default=None,
                             help="Subset of timecode,clock,waveform,whisper")
    sync_parser.add_argument("--transcripts", default=None,
                             help="Directory of word-timed Whisper JSON transcripts")
    sync_parser.add_argument("--out", default="sync.json", help="Where to write sync.json")
    sync_parser.set_defaults(func=command_sync)

    reframe_parser = sub.add_parser("reframe", help="Reframe a 16:9 cut to 9:16 following the speaker")
    reframe_parser.add_argument("video", help="Finished 16:9 cut to reframe")
    reframe_parser.add_argument("--out", required=True, help="Where to write reframe.json")
    reframe_parser.add_argument("--render", default=None, help="Render the vertical cut to this .mp4 path")
    reframe_parser.add_argument("--shots", default=None, help="angles.json from director angles")
    reframe_parser.add_argument("--no-sam", action="store_true", help="Centre every shot, no tracking")
    reframe_parser.add_argument("--max-usd", type=float, default=2.0,
                                help="Refuse above this estimated SAM spend")
    reframe_parser.set_defaults(func=command_reframe)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        args.func(args)
    except (FileNotFoundError, ValueError, RuntimeError, ToolingError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
