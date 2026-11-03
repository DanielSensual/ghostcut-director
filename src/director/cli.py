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
        tighten_path=Path(args.tighten) if getattr(args, "tighten", None) else None,
    )
    for shot in payload.get("shots", []):
        for overrule in shot.get("overrules", []):
            print(f"  shot {shot['id']} {shot['camera']}: {overrule}")
    n_lines, n_shots = len(payload.get("lines", [])), len(payload.get("shots", []))
    summary = (f"{n_lines} lines → {n_shots} shots ({payload.get('method')}, "
               f"${payload.get('spend_usd', 0.0):.4f})")
    if payload.get("tighten"):
        section = payload["tighten"]
        shots = payload.get("shots", [])
        if shots:
            out_s = float(shots[-1].get("end_s", 0.0)) - float(shots[0].get("start_s", 0.0))
        else:
            out_s = 0.0
        in_s = float(section.get("kept_s", 0.0)) + float(section.get("removed_s", 0.0))
        summary += f" tightened {in_s:.1f}s → {out_s:.1f}s"
    print(summary)
    return payload


def command_tighten(args: argparse.Namespace) -> dict:
    from .tighten import parse_target, run as run_tighten

    require_ffmpeg_binaries()
    payload = run_tighten(
        sync_path=Path(args.sync),
        words_path=Path(args.words),
        out_path=Path(args.out),
        audio_label=args.audio,
        pause_limit_s=float(args.pause_limit),
        target_s=parse_target(args.target) if args.target else None,
        model=args.model,
        no_muse=bool(args.no_muse),
        max_usd=float(args.max_usd),
    )
    for beat in payload.get("beats", []):
        attempts = beat.get("attempts", [])
        pick = beat.get("pick_attempt", -1)
        if beat.get("status") == "kept" and 0 <= pick < len(attempts):
            chosen = attempts[pick]
            duration = float(chosen.get("end_s", 0.0)) - float(chosen.get("start_s", 0.0))
            n_alternates = sum(1 for attempt in attempts
                               if attempt.get("verdict") == "alternate")
            if n_alternates == 1:
                suffix = " (+1 alternate)"
            elif n_alternates:
                suffix = f" (+{n_alternates} alternates)"
            else:
                suffix = ""
            print(f"B{beat.get('id')} {beat.get('topic', '')}: "
                  f"kept A{chosen.get('id')} ({duration:.1f}s) — "
                  f"{chosen.get('reason', '')}{suffix}")
        else:
            if 0 <= pick < len(attempts):
                best = attempts[pick]
            elif attempts:
                best = max(attempts,
                           key=lambda a: (float(a.get("confidence", 0.0) or 0.0),
                                          -int(a.get("id", 0))))
            else:
                best = {}
            print(f"B{beat.get('id')} {beat.get('topic', '')}: "
                  f"dropped ({best.get('kind', '')})")
    print(f"{float(payload.get('duration_in_s', 0.0)):.1f}s → "
          f"{float(payload.get('duration_out_s', 0.0)):.1f}s, "
          f"{len(payload.get('beats', []))} beats, "
          f"{len(payload.get('removed_spans', []))} removed spans "
          f"(${float(payload.get('spend_usd', 0.0)):.4f}, "
          f"{payload.get('method')})")
    print(f"Wrote {args.out}")
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
        whisper_model=args.whisper_model,
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


def _cut_stage_line(name: str, record: dict, payload: dict) -> str:
    status = record.get("status")
    qc_section = payload.get("qc") or {}
    if name == "qc" and qc_section:
        status = qc_section.get("status")
    line = f"{name}: {status}"
    if float(record.get("spend_usd") or 0.0):
        line += f" (${float(record['spend_usd']):.4f})"
    if name == "words":
        audio = payload.get("audio") or {}
        if audio:
            extra = ""
            if audio.get("words_shifted_s"):
                extra = f", shifted {float(audio['words_shifted_s']):+.3f}s"
            transcribe = audio.get("transcribe") or {}
            if transcribe.get("method") == "muse":
                share = transcribe.get("matched_share")
                share_text = (f"{float(share) * 100:.1f}%"
                              if share is not None else "?")
                extra += (f", muse {share_text} matched, "
                          f"${float(transcribe.get('spend_usd') or 0.0):.4f}")
            line += (f" ({audio.get('words', 0)} words, "
                     f"{audio.get('words_source')}{extra})")
    elif name == "tighten":
        section = payload.get("tighten") or {}
        if section:
            method = section.get("method", "")
            prefix = f"{method}, " if method and method != "muse" else ""
            line += (f" ({prefix}{section.get('beats', 0)} beats, kept "
                     f"{float(section.get('duration_in_s', 0.0)):.1f}s → "
                     f"{float(section.get('duration_out_s', 0.0)):.1f}s")
            target = section.get("target") or None
            if target:
                requested = float(target.get("requested_s", 0.0))
                stamp = f"{int(requested // 60)}:{int(requested % 60):02d}"
                if target.get("met"):
                    line += f", target {stamp} met"
                else:
                    over = float(target.get("after_s", 0.0)) - requested
                    line += f", target {stamp} missed by {over:.1f}s"
            line += ")"
    elif name == "export":
        section = payload.get("export") or {}
        if section:
            n_shots = section.get("shots_exported", 0)
            n_gaps = len(section.get("gaps", []))
            line += (f" ({n_shots} shot{'s' if n_shots != 1 else ''}, "
                     f"{n_gaps} gap{'s' if n_gaps != 1 else ''})")
    elif name == "qc" and qc_section:
        checks = qc_section.get("checks", [])
        n_checks = len(checks)
        n_findings = sum(1 for check in checks
                         if check.get("source") == "muse"
                         and check.get("id") != "muse:overall")
        line += f" ({n_checks} checks, {n_findings} muse findings)"
    return line


def _cut_warnings(payload: dict) -> list[str]:
    from .cut import STAGES

    lines: list[str] = []
    stages = payload.get("stages", {})
    for name in STAGES:
        if name not in stages:
            continue
        for warning in stages[name].get("warnings", []):
            if name == "sync":
                lines.append(f"! {warning}")
            else:
                lines.append(f"! {name}: {warning}")
    return lines


def _print_cut_progress(payload: dict) -> None:
    from .cut import STAGES

    stages = payload.get("stages", {})
    for name in STAGES:
        if name in stages:
            print(_cut_stage_line(name, stages[name], payload))


def _print_cut_summary(payload: dict) -> None:
    out_dir = payload.get("out_dir", "")
    stages = payload.get("stages", {})
    _print_cut_progress(payload)
    for line in _cut_warnings(payload):
        print(line)
    print(f"Wrote {out_dir}/cut.json ({payload.get('status')}, "
          f"${float(payload.get('spend_usd') or 0.0):.4f})")
    angles_record = stages.get("angles", {})
    if "cut.mp4" in (angles_record.get("outputs") or []):
        print(f"cut: {out_dir}/cut.mp4")
    reframe_record = stages.get("reframe")
    if reframe_record and "vertical.mp4" in (reframe_record.get("outputs") or []):
        print(f"vertical: {out_dir}/vertical.mp4")
    export_record = stages.get("export")
    if export_record and export_record.get("outputs"):
        joined = ", ".join(f"{out_dir}/{name}" for name in export_record["outputs"])
        print(f"timelines: {joined}")


def _cut_manifest_mtime_ns(out_dir: Path) -> int | None:
    try:
        return Path(out_dir, "cut.json").stat().st_mtime_ns
    except OSError:
        return None


def command_cut(args: argparse.Namespace) -> dict:
    from . import cut as cut_mod
    from .sync import parse_methods
    from .tighten import parse_target

    require_ffmpeg_binaries()
    methods = parse_methods(args.methods)
    out_dir = Path(args.out_dir)
    before_ns = _cut_manifest_mtime_ns(out_dir)
    try:
        payload = cut_mod.run(
            clips=[Path(entry) for entry in args.inputs],
            out_dir=out_dir,
            reference_path=Path(args.reference) if args.reference else None,
            audio_path=Path(args.audio) if args.audio else None,
            words_path=Path(args.words) if args.words else None,
            transcripts_dir=Path(args.transcripts) if args.transcripts else None,
            methods=methods,
            no_grade=bool(args.no_grade),
            grade_check=bool(args.grade_check),
            no_muse=bool(args.no_muse),
            model=args.model,
            min_shot_s=float(args.min_shot),
            no_vertical=bool(args.no_vertical),
            no_sam=bool(args.no_sam),
            no_export=bool(args.no_export),
            no_qc=bool(args.no_qc),
            max_usd=float(args.max_usd),
            force=bool(args.force),
            project_name=args.name,
            no_tighten=bool(args.no_tighten),
            pause_limit_s=float(args.pause_limit),
            target_s=parse_target(args.target) if args.target else None,
            no_transcribe=bool(args.no_transcribe),
            whisper_model=args.whisper_model,
        )
    except Exception:
        # The stage lines are the progress log: on failure, show the stages
        # this run completed before cli.main prints the error. The mtime
        # guard keeps a stale manifest from a previous run silent, and this
        # must never mask the original exception.
        try:
            if _cut_manifest_mtime_ns(out_dir) != before_ns:
                import json

                payload = json.loads(Path(out_dir, "cut.json").read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    _print_cut_progress(payload)
        except Exception:
            pass
        raise
    _print_cut_summary(payload)
    return payload


def command_transcribe(args: argparse.Namespace) -> dict:
    from .transcribe import run as run_transcribe

    require_ffmpeg_binaries()
    out = Path(args.out)
    transcript = (Path(args.transcript) if args.transcript
                  else out.parent / "transcript.json")
    payload = run_transcribe(
        sync_path=Path(args.sync),
        words_path=Path(args.words),
        out_path=out,
        transcript_path=transcript,
        audio_label=args.audio,
        max_usd=float(args.max_usd),
        no_muse=bool(args.no_muse),
    )
    if payload.get("method") == "muse":
        for speaker in payload.get("speakers", []):
            print(f"{speaker['id']}: {float(speaker['duration_s']):.1f}s, "
                  f"{speaker['turns']} turns")
        alignment = payload.get("alignment") or {}
        share = float(alignment.get("matched_share") or 0.0)
        print(f"aligned {alignment.get('matched', 0)}/"
              f"{alignment.get('whisper_words', 0)} words "
              f"({share * 100:.1f}%), {alignment.get('muse_only', 0)} "
              f"muse-only, {alignment.get('whisper_only', 0)} whisper-only, "
              f"${float(payload.get('spend_usd') or 0.0):.4f}")
    for warning in payload.get("warnings", []):
        print(f"! {warning}")
    print(f"Wrote {args.out}")
    if payload.get("method") == "muse":
        print(f"Wrote {transcript}")
    return payload


def build_parser() -> argparse.ArgumentParser:
    from .sync import WHISPER_MODEL

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
    angles_parser.add_argument("--tighten", default=None, help="tighten.json from director tighten")
    angles_parser.set_defaults(func=command_angles)

    tighten_parser = sub.add_parser("tighten", help="Pick the takes, lose the flubs")
    tighten_parser.add_argument("--sync", required=True, help="sync.json from director sync")
    tighten_parser.add_argument("--words", required=True, help="Word-timed Whisper JSON for the audio clip")
    tighten_parser.add_argument("--out", required=True, help="Where to write tighten.json")
    tighten_parser.add_argument("--audio", default=None, help="Audio clip label (default: the reference clip)")
    tighten_parser.add_argument("--pause-limit", type=float, default=0.8,
                                help="Pauses longer than this inside a kept attempt are shortened to 0.35 s")
    tighten_parser.add_argument("--target", default=None,
                                help="Target kept length, M:SS or seconds (whole beats only)")
    tighten_parser.add_argument("--model", default="muse-spark-1.3")
    tighten_parser.add_argument("--no-muse", action="store_true", help="Keep the last full pass of each beat")
    tighten_parser.add_argument("--max-usd", type=float, default=5.0,
                                help="Refuse above this estimated Spark spend")
    tighten_parser.set_defaults(func=command_tighten)

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
                             help="Subset of timecode,clock,waveform,flash,whisper")
    sync_parser.add_argument("--transcripts", default=None,
                             help="Directory of word-timed Whisper JSON transcripts")
    sync_parser.add_argument("--whisper-model", default=WHISPER_MODEL,
                             help="Local Whisper model for word timings (mlx_whisper --model)")
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

    cut_parser = sub.add_parser("cut", help="Cut a folder of cards to a finished cut in one step")
    cut_parser.add_argument("inputs", metavar="INPUT", nargs="+",
                            help="Media files and/or folders")
    cut_parser.add_argument("--out-dir", default="director-cut",
                            help="Job directory; created by the first stage that writes")
    cut_parser.add_argument("--reference", default=None,
                            help="Reference clip (default: the longest clip)")
    cut_parser.add_argument("--audio", default=None,
                            help="Production-audio clip file (default: the reference clip)")
    cut_parser.add_argument("--words", default=None,
                            help="Word-timed Whisper JSON in timeline times "
                            "(used in place, never transcribed)")
    cut_parser.add_argument("--transcripts", default=None,
                            help="Directory of per-clip word-timed Whisper JSON transcripts")
    cut_parser.add_argument("--methods", default=None,
                            help="Subset of timecode,clock,waveform,flash,whisper")
    cut_parser.add_argument("--whisper-model", default=WHISPER_MODEL,
                            help="Local Whisper model for word timings (mlx_whisper --model)")
    cut_parser.add_argument("--no-grade", action="store_true",
                            help="Skip grading; render cameras as recorded")
    cut_parser.add_argument("--grade-check", action="store_true",
                            help="Ask Muse Spark to check the grade match")
    cut_parser.add_argument("--no-muse", action="store_true",
                            help="No Meta model calls for picks or checks (SAM still runs; see --no-sam)")
    cut_parser.add_argument("--no-transcribe", action="store_true",
                            help="Skip Muse Voice Transcribe; keep Whisper words only")
    cut_parser.add_argument("--model", default="muse-spark-1.3")
    cut_parser.add_argument("--min-shot", type=float, default=2.0,
                            help="Shorter shots merge into a neighbour")
    cut_parser.add_argument("--no-vertical", action="store_true", help="Skip the 9:16 reframe")
    cut_parser.add_argument("--no-sam", action="store_true",
                            help="Centre every vertical shot, no SAM tracking")
    cut_parser.add_argument("--no-export", action="store_true",
                            help="Skip the FCPXML/OTIO timeline export")
    cut_parser.add_argument("--no-qc", action="store_true", help="Skip the whole-cut check")
    cut_parser.add_argument("--no-tighten", action="store_true",
                            help="Skip take picking; keep the whole recording")
    cut_parser.add_argument("--pause-limit", type=float, default=0.8,
                            help="Pauses longer than this inside a kept attempt are shortened to 0.35 s")
    cut_parser.add_argument("--target", default=None,
                            help="Target kept length, M:SS or seconds (whole beats only)")
    cut_parser.add_argument("--max-usd", type=float, default=5.0,
                            help="Refuse above this estimated Meta spend")
    cut_parser.add_argument("--force", action="store_true",
                            help="Ignore fingerprints; re-run every enabled stage")
    cut_parser.add_argument("--name", default="Director cut",
                            help="Project name written into both timelines")
    cut_parser.set_defaults(func=command_cut)

    transcribe_parser = sub.add_parser("transcribe", help="Transcribe one recording with Muse Voice Transcribe")
    transcribe_parser.add_argument("--sync", required=True, help="sync.json from director sync")
    transcribe_parser.add_argument("--words", required=True,
                                   help="Word-timed Whisper JSON for the audio clip, "
                                   "in clip-local times (not timeline times)")
    transcribe_parser.add_argument("--out", default="words.json",
                                   help="Where to write the timeline words")
    transcribe_parser.add_argument("--transcript", default=None,
                                   help="Where to write transcript.json "
                                   "(default: transcript.json beside --out)")
    transcribe_parser.add_argument("--audio", default=None,
                                   help="Audio clip label (default: the reference clip)")
    transcribe_parser.add_argument("--no-muse", action="store_true",
                                   help="Shift the Whisper words only, no transcription")
    transcribe_parser.add_argument("--max-usd", type=float, default=2.0,
                                   help="Refuse above this estimated transcribe spend")
    transcribe_parser.set_defaults(func=command_transcribe)

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
