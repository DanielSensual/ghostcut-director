from __future__ import annotations

import json
import math
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from director.grade import (
    CHECK_DEVELOPER,
    CheckUnavailable,
    ChannelStats,
    Cube,
    GradeClip,
    GradeError,
    GradeShotInfo,
    SourceColour,
    apply_stats,
    centre_stats,
    check_prompt,
    check_schema,
    check_with_muse,
    compose_gain_offset,
    conversion_kind,
    convert_pixel,
    cube_at,
    format_cube,
    gamut_matrix,
    generate_conversion_cube,
    hlg_to_linear,
    identity_cube,
    lut_filter_chain,
    parse_cube,
    parse_lut_specs,
    pairwise_moments,
    read_cube,
    rec709_encode,
    resample_cube,
    sample_command,
    shared_moments,
    shoulder,
    skin_sample_command,
    skin_stats,
    solve_match,
    solve_skin_match,
    write_cube,
)


REC709_GREY = rec709_encode(0.18)  # ~0.4089: where 18% grey must land
SLOG3_GREY = 420.0 / 1023.0
# HLG grey: the signal whose scene linear is 0.18 of reference white. That
# energy sits below the OETF's 0.5 hinge, so the low branch inverts it.
HLG_GREY = math.sqrt(3.0 * 0.18 * hlg_to_linear(0.75))


class ColourAnchorTests(unittest.TestCase):
    def test_slog3_black_is_black(self) -> None:
        for gamma in ("s-log3-cine", "s-log3"):
            for value in convert_pixel(gamma, 95.0 / 1023.0, 95.0 / 1023.0, 95.0 / 1023.0):
                self.assertLessEqual(value, 0.01)

    def test_greys_land_on_rec709_grey(self) -> None:
        slog = convert_pixel("s-log3-cine", SLOG3_GREY, SLOG3_GREY, SLOG3_GREY)
        hlg = convert_pixel("hlg", HLG_GREY, HLG_GREY, HLG_GREY)
        for got in (*slog, *hlg):
            self.assertAlmostEqual(got, REC709_GREY, delta=0.03)
        for a, b in zip(slog, hlg):
            self.assertAlmostEqual(a, b, delta=0.03)

    def test_neutral_stays_neutral(self) -> None:
        for gamma in ("s-log3-cine", "s-log3", "hlg", "rec709"):
            for v in (0.05, 0.2, SLOG3_GREY, 0.6, 0.9):
                got = convert_pixel(gamma, v, v, v)
                self.assertLessEqual(max(got) - min(got), 0.002, (gamma, v))

    def test_neutral_ramp_clamped_and_monotonic(self) -> None:
        for gamma in ("s-log3-cine", "s-log3", "hlg", "rec709"):
            prev = None
            v = -0.1
            while v <= 1.5:
                got = convert_pixel(gamma, v, v, v)
                for value in got:
                    self.assertGreaterEqual(value, 0.0)
                    self.assertLessEqual(value, 1.0)
                if prev is not None:
                    for c in range(3):
                        self.assertGreaterEqual(got[c], prev[c] - 1e-12, (gamma, v, c))
                prev = got
                v += 0.05

    def test_each_channel_responds_monotonically(self) -> None:
        for gamma in ("s-log3-cine", "hlg"):
            for c in range(3):
                prev = -1.0
                v = 0.0
                while v <= 1.0:
                    args = [0.0, 0.0, 0.0]
                    args[c] = v
                    got = convert_pixel(gamma, *args)[c]
                    self.assertGreaterEqual(got, prev - 1e-12, (gamma, v, c))
                    prev = got
                    v += 0.05

    def test_hlg_zero_is_zero(self) -> None:
        self.assertEqual(convert_pixel("hlg", 0.0, 0.0, 0.0), (0.0, 0.0, 0.0))

    def test_slog3_reads_the_stored_code_in_either_decode_range(self) -> None:
        # grey is stored as code 420 whatever the range flag: a full-range decode gives 420/1023,
        # a limited-range decode gives (420-64)/876; both must land on the same Rec.709 grey
        full = convert_pixel("s-log3-cine", 420 / 1023, 420 / 1023, 420 / 1023, "full")
        tv = convert_pixel("s-log3-cine", 356 / 876, 356 / 876, 356 / 876, "tv")
        shadow_full = convert_pixel("s-log3-cine", 200 / 1023, 200 / 1023, 200 / 1023, "full")
        shadow_tv = convert_pixel("s-log3-cine", 136 / 876, 136 / 876, 136 / 876, "tv")
        for a, b in ((full, tv), (shadow_full, shadow_tv)):
            for x, y in zip(a, b):
                self.assertAlmostEqual(x, y, places=9)
        self.assertAlmostEqual(full[1], 0.4089, delta=0.002)

    def test_hlg_reference_white_matches_slog3_white(self) -> None:
        hlg = convert_pixel("hlg", 0.75, 0.75, 0.75)
        slog = convert_pixel("s-log3", 609.8 / 1023.0, 609.8 / 1023.0, 609.8 / 1023.0)
        for a, b in zip(hlg, slog):
            self.assertAlmostEqual(a, b, delta=0.005)

    def test_shoulder_is_identity_at_the_knee(self) -> None:
        self.assertEqual(shoulder(0.18), 0.18)
        self.assertEqual(shoulder(0.05), 0.05)
        self.assertLess(shoulder(2.0), 1.0)
        self.assertGreater(shoulder(0.5), 0.18)

    def test_no_conversion_raises(self) -> None:
        for gamma in ("pq", "", "weird"):
            with self.assertRaises(ValueError, msg=gamma):
                convert_pixel(gamma, 0.5, 0.5, 0.5)
            with self.assertRaises(ValueError, msg=gamma):
                gamut_matrix(gamma)


class CubeTests(unittest.TestCase):
    def test_generate_round_trip(self) -> None:
        cube = generate_conversion_cube("s-log3-cine")
        self.assertEqual(cube.size, 33)
        self.assertEqual(len(cube.data), 33 ** 3)
        text = format_cube(cube, "ghostcut A generated_slog3_cine")
        lines = text.splitlines()
        self.assertEqual(lines[0], 'TITLE "ghostcut A generated_slog3_cine"')
        self.assertEqual(lines[1], "LUT_3D_SIZE 33")
        self.assertEqual(lines[2], "DOMAIN_MIN 0.000000 0.000000 0.000000")
        self.assertEqual(lines[3], "DOMAIN_MAX 1.000000 1.000000 1.000000")
        # second data line is input (1/32, 0, 0): red changes fastest
        want = convert_pixel("s-log3-cine", 1.0 / 32.0, 0.0, 0.0)
        for got, expected in zip([float(v) for v in lines[5].split()], want):
            self.assertAlmostEqual(got, expected, delta=2e-6)
        back = parse_cube(text, source="roundtrip")
        self.assertEqual((back.size, back.title), (33, "ghostcut A generated_slog3_cine"))
        for a, b in zip(back.data, cube.data):
            for x, y in zip(a, b):
                self.assertAlmostEqual(x, y, delta=1e-6)

    def _small(self, size: int = 2, domain_max: float = 1.0) -> str:
        rows = "\n".join("0.000000 0.000000 0.000000" for _ in range(size ** 3))
        return (f'TITLE "tiny"\nLUT_3D_SIZE {size}\n'
                f"DOMAIN_MIN 0.000000 0.000000 0.000000\n"
                f"DOMAIN_MAX {domain_max:.6f} {domain_max:.6f} {domain_max:.6f}\n{rows}\n")

    def test_parse_accepts_tab_separated_keywords(self) -> None:
        # DJI's D-Log cube (made in Resolve): "# comments", a blank line, then "LUT_3D_SIZE\t33".
        rows = "\n".join("0.0066 0.0071 0.0066" for _ in range(8))
        cube = parse_cube(f"# DaVinci Resolve Cube (3D LUT).\n#\n\nLUT_3D_SIZE\t2\n\n{rows}\n", source="dji")
        self.assertEqual(cube.size, 2)

    def test_parse_rejections(self) -> None:
        tiny = self._small()
        with self.assertRaises(ValueError):
            parse_cube(tiny.replace("LUT_3D_SIZE", "LUT_1D_SIZE"), source="t")
        with self.assertRaises(ValueError):
            parse_cube(tiny + "0.000000 0.000000 0.000000\n", source="t")
        with self.assertRaises(ValueError):
            parse_cube(tiny.replace("0.000000 0.000000 0.000000\n", "nope nope nope\n", 1),
                       source="t")
        with self.assertRaises(ValueError):
            parse_cube(self._small(domain_max=0.0), source="t")
        with self.assertRaises(ValueError):
            parse_cube('TITLE "no size"\n0.0 0.0 0.0\n', source="t")

    def test_resample_halving_cube(self) -> None:
        # 17-point cube over domain 0..2 halving its input: node r holds r/16
        n = 17
        supplied = Cube(size=n, data=[
            (r / (n - 1), 0.0, 0.0)
            for b in range(n) for g in range(n) for r in range(n)],
            domain_min=(0.0, 0.0, 0.0), domain_max=(2.0, 2.0, 2.0))
        back = resample_cube(supplied)
        self.assertEqual(back.size, 33)
        for b in range(33):
            for g in range(33):
                for r in range(33):
                    got = back.data[(b * 33 + g) * 33 + r]
                    self.assertAlmostEqual(got[0], (r / 32.0) / 2.0, delta=1e-9)
                    self.assertAlmostEqual(got[1], 0.0, delta=1e-9)
                    self.assertAlmostEqual(got[2], 0.0, delta=1e-9)

    def test_compose_gain_offset_clamps(self) -> None:
        cube = identity_cube(5)
        out = compose_gain_offset(cube, (1.2, 0.9, 1.1), (0.03, -0.02, 0.01))
        self.assertEqual(out.data[(4 * 5 + 4) * 5 + 4], (1.0, 0.88, 1.0))
        self.assertEqual(out.data[0], (0.03, 0.0, 0.01))
        got = out.data[(2 * 5 + 2) * 5 + 2]
        self.assertAlmostEqual(got[0], 1.2 * 0.5 + 0.03)
        self.assertAlmostEqual(cube_at(out, 0.5, 0.5, 0.5)[1], 0.9 * 0.5 - 0.02, delta=1e-9)

    def test_write_read_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "A.cube"
            cube = generate_conversion_cube("hlg")
            cube.title = "ghostcut A generated_hlg"
            write_cube(path, cube)
            back = read_cube(path)
        self.assertEqual(back.size, 33)
        self.assertEqual(back.title, "ghostcut A generated_hlg")


class ConversionKindTests(unittest.TestCase):
    def test_table(self) -> None:
        cases = {
            "s-log3-cine": "generated_slog3_cine",
            "s-log3": "generated_slog3",
            "hlg": "generated_hlg",
            "rec709": "none_rec709",
            "pq": "none_pq",
            "": "none_unknown",
            "something-else": "none_unknown",
        }
        for gamma, kind in cases.items():
            self.assertEqual(conversion_kind(gamma, None), (kind, None), gamma)
        # a supplied LUT wins, even over pq
        self.assertEqual(conversion_kind("pq", Path("house.cube")), ("supplied", "house.cube"))
        self.assertEqual(conversion_kind("s-log3", Path("house.cube")), ("supplied", "house.cube"))


class MatchTests(unittest.TestCase):
    def test_recovers_a_colour_cast_and_exposure(self) -> None:
        # a camera that is a touch warm and a little dark: pure per-channel gains, recovered exactly
        g0 = (1.1, 1.0, 0.95)
        ref = ChannelStats(mean=(0.4, 0.5, 0.45), std=(0.1, 0.12, 0.09), moments=3)
        cam = ChannelStats(mean=tuple(m / g for m, g in zip(ref.mean, g0)),
                           std=(0.3, 0.01, 0.2), moments=3)  # spreads don't matter: framing differs
        solved = solve_match(cam, ref)
        self.assertTrue(solved.matched)
        for got, want in zip(solved.gain, g0):
            self.assertAlmostEqual(got, want, delta=1e-9)
        self.assertEqual(solved.offset, (0.0, 0.0, 0.0))
        self.assertEqual(solved.gain_clamped, (False, False, False))
        self.assertEqual(solved.offset_clamped, (False, False, False))

    def test_clamps_exposure_and_white_balance(self) -> None:
        ref = ChannelStats(mean=(0.5, 0.5, 0.5), std=(0.2, 0.2, 0.2), moments=2)
        cam = ChannelStats(mean=(0.1, 0.1, 0.1), std=(0.1, 0.1, 0.1), moments=2)
        solved = solve_match(cam, ref)
        # exposure 5x clamps to 1.4 on every channel
        for got in solved.gain:
            self.assertAlmostEqual(got, 1.4)
        self.assertEqual(solved.gain_clamped, (True, True, True))
        # red 2x too strong against green: the ratio clamps to 1.25, and only red is marked
        cam = ChannelStats(mean=(0.25, 0.5, 0.5), std=(0.1, 0.1, 0.1), moments=2)
        solved = solve_match(cam, ref)
        self.assertEqual(solved.gain_clamped, (True, False, False))
        self.assertAlmostEqual(solved.gain[0] / solved.gain[1], 1.25)

    def test_black_channel_holds_gain_at_one(self) -> None:
        ref = ChannelStats(mean=(0.5, 0.5, 0.5), std=(0.1, 0.0, 0.1), moments=1)
        cam = ChannelStats(mean=(0.4, 0.0, 0.4), std=(0.0, 0.1, 0.1), moments=1)
        solved = solve_match(cam, ref)
        self.assertEqual(solved.gain, (1.0, 1.0, 1.0))
        self.assertEqual(solved.gain_clamped, (False, False, False))

    def test_apply_stats(self) -> None:
        stats = ChannelStats(mean=(0.4, 0.5, 0.6), std=(0.1, 0.1, 0.1), moments=4)
        after = apply_stats(stats, (1.2, 0.9, 1.1), (0.03, -0.02, 0.01))
        self.assertEqual(after.moments, 4)
        for got, want in zip(after.mean, (0.51, 0.43, 0.67)):
            self.assertAlmostEqual(got, want)
        for got, want in zip(after.std, (0.12, 0.09, 0.11)):
            self.assertAlmostEqual(got, want)


def _grade_clip(label: str, offset: float | None, duration: float,
                gamma: str = "s-log3-cine") -> GradeClip:
    return GradeClip(label=label, path=f"/media/{label}.mp4", gamma=gamma,
                     offset_s=offset, duration_s=duration)


class SamplingTests(unittest.TestCase):
    def test_pairwise_moments_cover_the_overlap(self) -> None:
        a = _grade_clip("A", 0.0, 100.0)
        b = _grade_clip("B", 10.0, 100.0)
        moments = pairwise_moments(a, b)
        self.assertEqual(len(moments), 8)
        # overlap [10, 100] shrunk 5% each end -> [14.5, 95.5]
        self.assertAlmostEqual(moments[0], 14.5 + 81.0 / 16)
        self.assertAlmostEqual(moments[-1], 95.5 - 81.0 / 16)
        gaps = [t2 - t1 for t1, t2 in zip(moments, moments[1:])]
        self.assertAlmostEqual(max(gaps), min(gaps))

    def test_pairwise_moments_empty(self) -> None:
        a = _grade_clip("A", 0.0, 100.0)
        self.assertEqual(pairwise_moments(a, _grade_clip("B", None, 100.0)), [])
        self.assertEqual(pairwise_moments(_grade_clip("B", None, 100.0), a), [])
        # 0.4 s of overlap shrinks below the 0.5 s floor
        self.assertEqual(pairwise_moments(a, _grade_clip("B", 99.6, 100.0)), [])

    def test_shared_moments(self) -> None:
        clips = [_grade_clip("A", 0.0, 100.0), _grade_clip("B", 5.0, 100.0),
                 _grade_clip("C", None, 100.0)]
        moments = shared_moments(clips)
        self.assertTrue(moments)
        self.assertGreaterEqual(min(moments), 5.0)
        self.assertEqual(shared_moments([_grade_clip("C", None, 100.0)]), [])

    def test_centre_stats(self) -> None:
        n = 64 * 36
        black = centre_stats(bytes(n * 3))
        self.assertEqual((black.mean, black.std, black.moments),
                         ((0.0, 0.0, 0.0), (0.0, 0.0, 0.0), 1))
        white = centre_stats(bytes([255] * n * 3))
        self.assertEqual(white.mean, (1.0, 1.0, 1.0))
        half = centre_stats(bytes(([0, 0, 0, 255, 255, 255] * (n // 2))))
        self.assertAlmostEqual(half.mean[0], 0.5)
        self.assertAlmostEqual(half.std[0], 0.5)
        with self.assertRaises(GradeError):
            centre_stats(bytes(10))

    def test_sample_command_shape(self) -> None:
        clip = _grade_clip("A", 0.0, 100.0)
        clip.colour = SourceColour(range="tv", space="bt709", primaries="bt709")
        cmd = sample_command(clip, 12.5, Path("/tmp/A-conv.cube"))
        self.assertLess(cmd.index("-ss"), cmd.index("-i"))
        self.assertIn("pipe:1", cmd)
        self.assertIn("rgb24", cmd)
        vf = cmd[cmd.index("-vf") + 1]
        self.assertIn("lut3d=file='/tmp/A-conv.cube'", vf)
        self.assertLess(vf.index("crop="), vf.index("scale=64:36"))
        bare = sample_command(clip, 12.5, None)
        self.assertNotIn("lut3d", bare[bare.index("-vf") + 1])


class ChainTests(unittest.TestCase):
    def test_hlg_chain(self) -> None:
        colour = SourceColour(range="tv", space="bt2020nc", primaries="bt2020",
                              transfer="arib-std-b67", pix_fmt="yuv420p10le", bits=10)
        vf = lut_filter_chain(colour, Path("/g/A.cube"), "30")
        self.assertIn("in_range=tv", vf)
        self.assertIn("in_color_matrix=bt2020nc", vf)
        self.assertLess(vf.index("format=rgb48le"), vf.index("lut3d="))
        self.assertIn("interp=tetrahedral", vf)
        self.assertIn("scale=out_color_matrix=bt709:out_range=tv", vf)
        self.assertIn(",format=yuv420p,", vf)
        self.assertTrue(vf.endswith("setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range=tv"))
        self.assertNotIn("zscale", vf)
        self.assertNotIn("tonemap", vf)

    def test_sony_8_and_10_bit_chains_match(self) -> None:
        vf8 = lut_filter_chain(SourceColour(range="tv", space="bt709", primaries="bt709",
                                            pix_fmt="yuv420p", bits=8), Path("/g/B.cube"), "30")
        vf10 = lut_filter_chain(SourceColour(range="tv", space="bt709", primaries="bt709",
                                             transfer="bt709", pix_fmt="yuv422p10le", bits=10),
                                Path("/g/B.cube"), "30")
        # depth lives in rgb48le: the chain is identical for 8- and 10-bit Sony
        self.assertEqual(vf8, vf10)
        self.assertIn("in_color_matrix=bt709", vf8)

    def test_full_range(self) -> None:
        vf = lut_filter_chain(SourceColour(range="full", space="bt709", primaries="bt709"),
                              Path("/g/B.cube"), "30")
        self.assertIn("in_range=full", vf)

    def test_no_lut_is_the_legacy_look(self) -> None:
        from director.grade import FILL_OR_FIT
        self.assertEqual(lut_filter_chain(SourceColour(), None, "30"),
                         f"{FILL_OR_FIT},fps=30,format=yuv420p")
        # near-widescreen fills (no letterbox), vertical keeps its whole picture
        self.assertIn("between(a,1.4,2.4)", FILL_OR_FIT)
        self.assertIn("pad=1920:1080", FILL_OR_FIT)

    def test_bad_lut_path(self) -> None:
        with self.assertRaises(GradeError):
            lut_filter_chain(SourceColour(), Path("/g/a'b.cube"))
        with self.assertRaises(GradeError):
            lut_filter_chain(SourceColour(), Path("/g/a:b.cube"))


class AnglesGradeTests(unittest.TestCase):
    def _shots(self):
        from director.angles import Shot, SyncClip
        clips = {
            "A": SyncClip(label="A", path="/media/A.mp4", offset_s=0.0,
                          fps=30.0, width=1920, height=1080, duration_s=100.0),
            "B": SyncClip(label="B", path="/media/B.mp4", offset_s=2.0,
                          fps=30.0, width=1920, height=1080, duration_s=100.0),
        }
        shots = [
            Shot(0, 2.0, 5.0, "A", clips["A"].path, 2.0, 5.0, [0], "medium", "ok", "muse", []),
            Shot(1, 5.0, 6.0, "black", "", 0.0, 1.0, [1], "", "", "rule", ["no_camera"]),
            Shot(2, 6.0, 9.0, "B", clips["B"].path, 4.0, 7.0, [2], "close", "ok", "muse", []),
        ]
        return clips, shots

    def test_render_with_grade(self) -> None:
        from director.angles import render_commands
        clips, shots = self._shots()
        hlg = SourceColour(range="tv", space="bt2020nc", primaries="bt2020")
        grade = {"A": GradeShotInfo("A", Path("/g/A.cube"), hlg),
                 "B": GradeShotInfo("B", None, SourceColour())}
        parts, _, _ = render_commands(shots, clips, clips["A"], Path("/tmp/cut.mp4"),
                                      grade=grade)
        graded = parts[0][parts[0].index("-vf") + 1]
        self.assertIn("lut3d=file='/g/A.cube'", graded)
        self.assertIn("in_color_matrix=bt2020nc", graded)
        black = parts[1][parts[1].index("-vf") + 1]
        self.assertIn("color=c=black", " ".join(parts[1]))
        self.assertNotIn("lut3d", black)
        ungraded = parts[2][parts[2].index("-vf") + 1]
        self.assertNotIn("lut3d", ungraded)
        self.assertIn("pad=1920:1080", ungraded)
        for cmd in parts:
            self.assertEqual(cmd[cmd.index("-color_primaries") + 1], "bt709")

    def test_render_without_grade_is_legacy(self) -> None:
        from director.angles import render_commands
        clips, shots = self._shots()
        parts, _, _ = render_commands(shots, clips, clips["A"], Path("/tmp/cut.mp4"))
        for cmd in parts:
            self.assertNotIn("-color_primaries", cmd)
            self.assertNotIn("lut3d", cmd[cmd.index("-vf") + 1])

    def test_render_rejects_unknown_grade_camera(self) -> None:
        from director.angles import render_commands
        clips, shots = self._shots()
        grade = {"Z": GradeShotInfo("Z", Path("/g/Z.cube"), SourceColour())}
        with self.assertRaises(ValueError):
            render_commands(shots, clips, clips["A"], Path("/tmp/cut.mp4"), grade=grade)

    def test_load_grade(self) -> None:
        from director.angles import load_grade
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "A.cube").write_text("LUT_3D_SIZE 2\n" + "0 0 0\n" * 8, encoding="utf-8")
            (base / "grade.json").write_text(json.dumps({
                "version": 1,
                "cameras": [
                    {"label": "A", "lut": "A.cube",
                     "source_colour": {"range": "tv", "space": "bt2020nc",
                                       "primaries": "bt2020", "transfer": "arib-std-b67",
                                       "pix_fmt": "yuv420p10le", "bits": 10}},
                    {"label": "B", "lut": None, "source_colour": {}},
                ]}), encoding="utf-8")
            grade = load_grade(base / "grade.json")
        self.assertEqual(grade["A"].lut, base / "A.cube")
        self.assertEqual(grade["A"].colour.space, "bt2020nc")
        self.assertIsNone(grade["B"].lut)
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / "grade.json").write_text(json.dumps({
                "version": 1,
                "cameras": [{"label": "A", "lut": "missing.cube", "source_colour": {}}]}),
                encoding="utf-8")
            with self.assertRaises(ValueError):
                load_grade(base / "grade.json")
            (base / "grade.json").write_text(json.dumps({"version": 2, "cameras": []}),
                                             encoding="utf-8")
            with self.assertRaises(ValueError):
                load_grade(base / "grade.json")
            (base / "grade.json").write_text(json.dumps({
                "version": 1,
                "cameras": [{"label": "A", "lut": "a'b.cube", "source_colour": {}}]}),
                encoding="utf-8")
            with self.assertRaises(GradeError):
                load_grade(base / "grade.json")


class FakeCheckTests(unittest.TestCase):
    """The Muse check end to end with a stand-in for the guestlist client."""

    def setUp(self) -> None:
        self.calls: list[dict] = []
        calls = self.calls

        class Result:
            def __init__(self, data, usd=0.02, cached=False):
                self.data, self.usd, self.cached = data, usd, cached

        def image_json(images, **kw):
            calls.append({"images": list(images), **kw})
            return Result({"moment_s": 12.5, "overall_standout": "none",
                           "assessments": []}, usd=0.02, cached=True)

        meta = types.ModuleType("guestlist.meta")
        meta.spark = types.SimpleNamespace(image_json=image_json)
        meta.cache = types.SimpleNamespace(guard=lambda usd: None)
        self.saved = {k: sys.modules.get(k) for k in ("guestlist", "guestlist.meta")}
        sys.modules["guestlist"] = types.ModuleType("guestlist")
        sys.modules["guestlist"].meta = meta
        sys.modules["guestlist.meta"] = meta

    def tearDown(self) -> None:
        for k, v in self.saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v

    def _frames(self, tmp: str):
        pa = Path(tmp) / "M0_A.jpg"
        pb = Path(tmp) / "M0_B.jpg"
        pa.write_bytes(b"a")
        pb.write_bytes(b"b")
        return [(12.5, {"A": pa, "B": pb})]

    def test_reply_recorded_verbatim_with_spend(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            payload, spend, warnings = check_with_muse(self._frames(tmp), ["A", "B"], model="m")
        call = self.calls[0]
        self.assertEqual(call["images"], [Path(tmp) / "M0_A.jpg", Path(tmp) / "M0_B.jpg"])
        self.assertIn("1 = camera A, 2 = camera B", call["prompt"])
        self.assertIn("t=12.5s", call["prompt"])
        self.assertEqual(call["name"], "grade_check")
        self.assertEqual(call["developer"], CHECK_DEVELOPER)
        self.assertEqual(call["schema"]["properties"]["overall_standout"]["enum"],
                         ["A", "B", "none"])
        moment = payload["moments"][0]
        self.assertEqual(moment["reply"], {"moment_s": 12.5, "overall_standout": "none",
                                           "assessments": []})
        self.assertEqual(moment["frames"], {"A": "check-frames/M0_A.jpg",
                                            "B": "check-frames/M0_B.jpg"})
        self.assertEqual(moment["moment_s"], 12.5)
        self.assertAlmostEqual(spend, 0.02)
        self.assertTrue(any("cache" in w for w in warnings))

    def test_short_moment_is_skipped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pa = Path(tmp) / "M0_A.jpg"
            pa.write_bytes(b"a")
            payload, _, _ = check_with_muse(
                [(12.5, {"A": pa, "B": Path(tmp) / "M0_B.jpg"})], ["A", "B"], model="m")
        self.assertEqual(payload["moments"], [])
        self.assertEqual(self.calls, [])

    def test_schema_shape(self) -> None:
        schema = check_schema(["A", "B"])
        assessments = schema["properties"]["assessments"]
        self.assertEqual(len(assessments["prefixItems"]), 4)
        self.assertEqual((assessments["minItems"], assessments["maxItems"]), (4, 4))
        self.assertIs(assessments["items"], False)
        dims = [item["properties"]["dimension"]["enum"] for item in assessments["prefixItems"]]
        self.assertEqual(dims, [["exposure"], ["white_balance"], ["saturation"], ["contrast"]])
        want = {"exposure": ["brighter", "darker", "none"],
                "white_balance": ["warmer", "cooler", "none"],
                "saturation": ["more_saturated", "less_saturated", "none"],
                "contrast": ["higher_contrast", "lower_contrast", "none"]}
        for item in assessments["prefixItems"]:
            dim = item["properties"]["dimension"]["enum"][0]
            self.assertEqual(item["properties"]["direction"]["enum"], want[dim])
            self.assertEqual(item["properties"]["standout_camera"]["enum"], ["A", "B", "none"])
        prompt = check_prompt(3.0, ["B", "A"])
        self.assertIn("t=3.0s", prompt)

    def test_check_unavailable_without_guestlist(self) -> None:
        hidden = {"guestlist": None, "guestlist.meta": None}  # None makes the import fail even when installed
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(sys.modules, hidden):
            with self.assertRaises(CheckUnavailable):
                check_with_muse(self._frames(tmp), ["A", "B"], model="m")


def _write_sync(base: Path) -> Path:
    sync = {
        "reference": "/media/A.mp4",
        "clips": [
            {"label": "A", "offset_s": 0.0,
             "camera": {"path": "/media/A.mp4", "gamma": "S-Log3-Cine",
                        "fps": 30.0, "width": 1920, "height": 1080, "duration_s": 100.0}},
            {"label": "B", "offset_s": 5.0,
             "camera": {"path": "/media/B.mp4", "gamma": "hlg",
                        "fps": 30.0, "width": 1920, "height": 1080, "duration_s": 100.0}},
            {"label": "C", "offset_s": 0.0,
             "camera": {"path": "/media/C.mp4", "gamma": "pq",
                        "fps": 30.0, "width": 1920, "height": 1080, "duration_s": 100.0}},
        ],
    }
    path = base / "sync.json"
    path.write_text(json.dumps(sync), encoding="utf-8")
    return path


class GradeRunTests(unittest.TestCase):
    def test_no_match_run_without_ffmpeg(self) -> None:
        from director.grade import run
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            sync = _write_sync(base)
            out = base / "grade" / "grade.json"
            payload = run(sync_path=sync, out_path=out, no_match=True)
            self.assertEqual(payload["version"], 1)
            self.assertEqual(payload["reference"], "/media/A.mp4")
            self.assertEqual(payload["reference_label"], "A")
            self.assertTrue(payload["no_match"])
            self.assertEqual(payload["spend_usd"], 0.0)
            self.assertIsNone(payload["check"])
            by_label = {c["label"]: c for c in payload["cameras"]}
            self.assertEqual(by_label["A"]["conversion"]["kind"], "generated_slog3_cine")
            self.assertEqual(by_label["B"]["conversion"]["kind"], "generated_hlg")
            self.assertEqual(by_label["C"]["conversion"]["kind"], "none_pq")
            self.assertEqual(by_label["A"]["lut"], "A.cube")
            self.assertEqual(by_label["C"]["lut"], None)
            self.assertIn("pq_not_supported:C", payload["warnings"])
            for cam in payload["cameras"]:
                self.assertFalse(cam["match"]["matched"])
                self.assertEqual(cam["match"]["moments"], [])
                self.assertIsNone(cam["match"]["before"])
            # cubes parse and the reference is gain 1 / offset 0
            for label in ("A", "B"):
                cube = read_cube(base / "grade" / f"{label}.cube")
                self.assertEqual(cube.size, 33)
            self.assertFalse((base / "grade" / "C.cube").exists())
            # defaults feed the chains: HLG decodes BT.2020, Sony BT.709
            self.assertEqual(by_label["B"]["source_colour"]["space"], "bt2020nc")
            self.assertEqual(by_label["A"]["source_colour"]["space"], "bt709")
            # grade.json round-trips through the angles loader
            from director.angles import load_grade
            grade = load_grade(out)
            self.assertEqual(set(grade), {"A", "B", "C"})
            self.assertIsNone(grade["C"].lut)

    def test_reference_override_and_unknown(self) -> None:
        from director.grade import run
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            sync = _write_sync(base)
            payload = run(sync_path=sync, out_path=base / "g.json",
                          reference_label="B", no_match=True)
            self.assertEqual(payload["reference_label"], "B")
            self.assertEqual(payload["reference"], "/media/B.mp4")
            with self.assertRaises(ValueError):
                run(sync_path=sync, out_path=base / "g.json",
                    reference_label="Z", no_match=True)

    def test_supplied_lut(self) -> None:
        from director.grade import run
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            sync = _write_sync(base)
            house = base / "house.cube"
            write_cube(house, identity_cube(2))
            out = base / "grade" / "grade.json"
            payload = run(sync_path=sync, out_path=out, no_match=True,
                          lut_specs=[f"B={house}"])
            by_label = {c["label"]: c for c in payload["cameras"]}
            self.assertEqual(by_label["B"]["conversion"]["kind"], "supplied")
            self.assertEqual(by_label["B"]["conversion"]["source_lut"], str(house))
            self.assertEqual(read_cube(base / "grade" / "B.cube").size, 33)

    def test_lut_spec_errors(self) -> None:
        with self.assertRaises(ValueError):
            parse_lut_specs(["ABC"])
        with self.assertRaises(ValueError):
            parse_lut_specs(["A="])
        with self.assertRaises(ValueError):
            parse_lut_specs(["=x.cube"])
        with self.assertRaises(ValueError):
            parse_lut_specs(["A=a.cube", "A=b.cube"])

    def test_angles_run_records_grade(self) -> None:
        from director.angles import run as run_angles
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            sync = _write_sync(base)
            words = base / "words.json"
            words.write_text(json.dumps({"words": [
                {"word": "hello", "start": 6.0, "end": 6.4},
                {"word": "world", "start": 6.5, "end": 6.9},
            ]}), encoding="utf-8")
            (base / "A.cube").write_text("LUT_3D_SIZE 2\n" + "0 0 0\n" * 8,
                                         encoding="utf-8")
            grade_json = base / "grade.json"
            grade_json.write_text(json.dumps({
                "version": 1,
                "cameras": [
                    {"label": "A", "lut": "A.cube", "source_colour": {}},
                    {"label": "B", "lut": None, "source_colour": {}},
                ]}), encoding="utf-8")
            out = base / "angles.json"
            payload = run_angles(sync_path=sync, words_path=words, out_path=out,
                                 audio_label=None, render_path=None, min_shot_s=2.0,
                                 model="m", no_muse=True, grade_path=grade_json)
            self.assertEqual(payload["grade"], str(grade_json))
            self.assertTrue(payload["shots"])
            for shot in payload["shots"]:
                if shot["camera"] == "A":
                    self.assertEqual(shot["lut"], "A.cube")
                else:
                    self.assertIsNone(shot["lut"])


class GradeCommandTests(unittest.TestCase):
    def test_grade_command_no_match(self) -> None:
        from director.cli import main
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            sync = _write_sync(base)
            out = base / "grade" / "grade.json"
            code = main(["grade", "--sync", str(sync), "--out", str(out), "--no-match"])
            self.assertEqual(code, 0)
            self.assertTrue(out.exists())
            self.assertTrue((base / "grade" / "A.cube").exists())

    def test_parser_defaults(self) -> None:
        from director.cli import build_parser
        args = build_parser().parse_args(["grade", "--sync", "s.json", "--out", "g.json"])
        self.assertEqual(args.model, "muse-spark-1.3")
        self.assertEqual(args.lut, [])
        self.assertFalse(args.no_match)
        self.assertFalse(args.check)
        self.assertIsNone(args.reference)
        angles = build_parser().parse_args(
            ["angles", "--sync", "s.json", "--words", "w.json",
             "--out", "a.json", "--grade", "g.json"])
        self.assertEqual(angles.grade, "g.json")


if __name__ == "__main__":
    unittest.main()


class ReferenceChoiceTests(unittest.TestCase):
    def test_audio_recorder_reference_falls_back_to_first_camera(self) -> None:
        from director.grade import GradeClip, choose_reference
        clips = [GradeClip("A", "lav.wav", "", 0.0, 692.0, width=0),
                 GradeClip("B", "sony.mp4", "s-log3-cine", 151.6, 152.1, width=3840),
                 GradeClip("C", "dji.mov", "rec709", 143.5, 162.0, width=5120)]
        self.assertEqual(choose_reference(clips, "A", None), "B")
        self.assertEqual(choose_reference(clips, "A", "C"), "C")
        self.assertEqual(choose_reference(clips[1:], "B", None), "B")


def _solid_frame(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    return bytes(rgb * (width * height))


class SkinStatsTests(unittest.TestCase):
    def test_measures_the_inner_region_only(self) -> None:
        # 10x10, box the whole frame: the central 60% is the middle 6x6
        width, height = 10, 10
        frame = bytearray(_solid_frame(width, height, (0, 0, 0)))
        for r in range(2, 8):
            for c in range(2, 8):
                base = (r * width + c) * 3
                frame[base:base + 3] = bytes((200, 100, 50))
        stats = skin_stats(bytes(frame), width, height, (0.0, 0.0, 1.0, 1.0))
        assert stats is not None
        self.assertEqual(stats.moments, 1)
        for got, want in zip(stats.mean, (200 / 255, 100 / 255, 50 / 255)):
            self.assertAlmostEqual(got, want)
        self.assertEqual(stats.std, (0.0, 0.0, 0.0))

    def test_vision_box_y_grows_upwards(self) -> None:
        # top half white, bottom half black: a Vision upper-half box reads white
        width, height = 8, 8
        frame = bytearray()
        for r in range(height):
            frame += bytes((255, 255, 255) * width) if r < 4 \
                else bytes((0, 0, 0) * width)
        top = skin_stats(bytes(frame), width, height, (0.0, 0.5, 1.0, 0.5),
                         inset=0.0)
        bottom = skin_stats(bytes(frame), width, height, (0.0, 0.0, 1.0, 0.5),
                            inset=0.0)
        assert top is not None and bottom is not None
        self.assertEqual(top.mean, (1.0, 1.0, 1.0))
        self.assertEqual(bottom.mean, (0.0, 0.0, 0.0))

    def test_spread_over_a_split_region(self) -> None:
        # left half of the inner region black, right half white
        width, height = 10, 10
        frame = bytearray(_solid_frame(width, height, (0, 0, 0)))
        for r in range(2, 8):
            for c in range(5, 8):
                base = (r * width + c) * 3
                frame[base:base + 3] = bytes((255, 255, 255))
        stats = skin_stats(bytes(frame), width, height, (0.0, 0.0, 1.0, 1.0))
        assert stats is not None
        self.assertAlmostEqual(stats.mean[0], 0.5)
        self.assertAlmostEqual(stats.std[0], 0.5)

    def test_no_box_or_empty_region_gives_none(self) -> None:
        frame = _solid_frame(10, 10, (9, 9, 9))
        self.assertIsNone(skin_stats(frame, 10, 10, None))
        self.assertIsNone(skin_stats(frame, 10, 10, (0.5, 0.5, 0.0, 0.0)))
        self.assertIsNone(skin_stats(frame, 10, 10, (2.0, 2.0, 0.5, 0.5)))
        with self.assertRaises(GradeError):
            skin_stats(bytes(10), 10, 10, (0.0, 0.0, 1.0, 1.0))

    def test_sample_command_shape(self) -> None:
        clip = _grade_clip("B", 5.0, 100.0)
        clip.colour = SourceColour(range="tv", space="bt709", primaries="bt709")
        cmd = skin_sample_command(clip, 12.5, Path("/tmp/B-conv.cube"),
                                  Path("/tmp/B_00_cam.jpg"))
        self.assertLess(cmd.index("-ss"), cmd.index("-i"))
        self.assertIn("pipe:1", cmd)
        self.assertIn("/tmp/B_00_cam.jpg", cmd)
        self.assertEqual(cmd.count("-map"), 2)
        first_vf = cmd[cmd.index("-vf") + 1]
        self.assertIn("lut3d=file='/tmp/B-conv.cube'", first_vf)
        self.assertIn("scale=480:270", first_vf)
        bare = skin_sample_command(clip, 12.5, None, Path("/tmp/x.jpg"))
        self.assertNotIn("lut3d", " ".join(bare))


class SkinSolveTests(unittest.TestCase):
    def test_recovers_gain_and_contrast(self) -> None:
        # neutral cast plus a flatter response: means and spreads both land
        cam = ChannelStats(mean=(0.4, 0.4, 0.4), std=(0.09, 0.09, 0.09),
                           moments=2)
        ref = ChannelStats(mean=(0.5, 0.5, 0.5), std=(0.1, 0.1, 0.1),
                           moments=2)
        solved = solve_skin_match(cam, ref)
        self.assertTrue(solved.matched)
        for gain in solved.gain:
            self.assertAlmostEqual(gain, 1.111111, places=5)
        for offset in solved.offset:
            self.assertAlmostEqual(offset, 0.0555556, places=5)
        self.assertEqual(solved.gain_clamped, (False, False, False))
        self.assertEqual(solved.offset_clamped, (False, False, False))
        after = apply_stats(cam, solved.gain, solved.offset)
        for got in after.mean:
            self.assertAlmostEqual(got, 0.5)
        for got in after.std:
            self.assertAlmostEqual(got, 0.1)

    def test_equal_spreads_are_gain_only(self) -> None:
        ref = ChannelStats(mean=(0.4, 0.5, 0.45), std=(0.1, 0.12, 0.09),
                           moments=3)
        cam = ChannelStats(mean=(0.4 / 1.1, 0.5, 0.45 / 0.95),
                           std=(0.1 / 1.1, 0.12, 0.09 / 0.95), moments=3)
        solved = solve_skin_match(cam, ref)
        plain = solve_match(cam, ref)
        for got, want in zip(solved.gain, plain.gain):
            self.assertAlmostEqual(got, want)
        for got in solved.offset:
            self.assertAlmostEqual(got, 0.0)
        self.assertEqual(solved.gain_clamped, plain.gain_clamped)
        self.assertEqual(solved.offset_clamped, (False, False, False))

    def test_clamps_contrast_gain_and_lift(self) -> None:
        cam = ChannelStats(mean=(0.4, 0.4, 0.4), std=(0.05, 0.05, 0.05),
                           moments=2)
        ref = ChannelStats(mean=(0.5, 0.5, 0.5), std=(0.1, 0.1, 0.1),
                           moments=2)
        solved = solve_skin_match(cam, ref)
        # contrast 1.6x clamps to 1.25, the product to 1.4, the lift to -0.10
        self.assertEqual(solved.gain, (1.4, 1.4, 1.4))
        self.assertEqual(solved.offset, (-0.1, -0.1, -0.1))
        self.assertEqual(solved.gain_clamped, (True, True, True))
        self.assertEqual(solved.offset_clamped, (True, True, True))

    def test_flat_spread_holds_contrast_at_one(self) -> None:
        cam = ChannelStats(mean=(0.4, 0.4, 0.4), std=(0.0, 0.09, 0.09),
                           moments=1)
        ref = ChannelStats(mean=(0.5, 0.5, 0.5), std=(0.1, 0.1, 0.1),
                           moments=1)
        solved = solve_skin_match(cam, ref)
        plain = solve_match(cam, ref)
        self.assertAlmostEqual(solved.gain[0], plain.gain[0])
        self.assertEqual(solved.offset[0], 0.0)

    def test_black_channel_holds(self) -> None:
        ref = ChannelStats(mean=(0.5, 0.5, 0.5), std=(0.1, 0.1, 0.1),
                           moments=1)
        cam = ChannelStats(mean=(0.4, 0.0, 0.4), std=(0.1, 0.1, 0.1),
                           moments=1)
        solved = solve_skin_match(cam, ref)
        self.assertEqual(solved.gain, (1.0, 1.0, 1.0))
        self.assertEqual(solved.offset, (0.0, 0.0, 0.0))


class SkinFallbackTests(unittest.TestCase):
    def test_run_falls_back_to_whole_frame_with_a_warning(self) -> None:
        from director.grade import run
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            sync = _write_sync(base)
            out = base / "grade.json"
            ref = ChannelStats(mean=(0.5, 0.5, 0.5), std=(0.1, 0.1, 0.1),
                               moments=1)
            cam = ChannelStats(mean=(0.4, 0.4, 0.4), std=(0.09, 0.09, 0.09),
                               moments=1)

            def no_skin(*args, **kwargs):
                note = args[-1]
                note("B", "no_face:B")
                return None

            with mock.patch("director.grade._take_skin_pair",
                            side_effect=no_skin), \
                    mock.patch("director.grade._take_frames",
                               side_effect=[[ref], [cam]]):
                payload = run(sync_path=sync, out_path=out)
            by_label = {c["label"]: c for c in payload["cameras"]}
            match = by_label["B"]["match"]
            self.assertTrue(match["matched"])
            self.assertEqual(match["region"], "frame")
            self.assertEqual(match["skin_moments"], [])
            self.assertIn("no_face:B", payload["warnings"])
            self.assertIn("no_face:B", by_label["B"]["warnings"])
            # whole-frame numbers, solved gain-only
            self.assertEqual(match["offset"], [0.0, 0.0, 0.0])
            self.assertAlmostEqual(match["before"]["mean"][0], 0.4)

    def test_run_matches_on_skin_when_faces_pair(self) -> None:
        from director.grade import run
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            sync = _write_sync(base)
            out = base / "grade.json"
            ref = ChannelStats(mean=(0.5, 0.5, 0.5), std=(0.1, 0.1, 0.1),
                               moments=2)
            cam = ChannelStats(mean=(0.4, 0.4, 0.4), std=(0.09, 0.09, 0.09),
                               moments=2)
            moments = [14.5, 20.0]
            with mock.patch("director.grade._take_skin_pair",
                            return_value=(ref, cam, moments)):
                payload = run(sync_path=sync, out_path=out)
            by_label = {c["label"]: c for c in payload["cameras"]}
            match = by_label["B"]["match"]
            self.assertTrue(match["matched"])
            self.assertEqual(match["region"], "skin")
            self.assertEqual(match["moments"], moments)
            self.assertEqual(match["skin_moments"], moments)
            self.assertNotIn("no_face:B", payload["warnings"])
            # the skin solve carries contrast in the offset
            self.assertNotEqual(match["offset"], [0.0, 0.0, 0.0])
            for got in match["after"]["mean"]:
                self.assertAlmostEqual(got, 0.5)
            for got in match["after"]["std"]:
                self.assertAlmostEqual(got, 0.1)

    def test_helper_unavailable_warns_once(self) -> None:
        from director.grade import _take_skin_pair
        seen: list[str] = []
        cache: dict = {}
        with mock.patch("director.grade._skin_helper_cached",
                        return_value=None):
            first = _take_skin_pair(_grade_clip("A", 0.0, 100.0),
                                    _grade_clip("B", 5.0, 100.0), [50.0],
                                    {}, Path("/tmp/nowhere"), cache,
                                    lambda label, warning: seen.append(warning))
            second = _take_skin_pair(_grade_clip("A", 0.0, 100.0),
                                     _grade_clip("C", 5.0, 100.0), [50.0],
                                     {}, Path("/tmp/nowhere"), cache,
                                     lambda label, warning: seen.append(warning))
        self.assertIsNone(first)
        self.assertIsNone(second)
        self.assertEqual(len(seen), 1)
        self.assertTrue(seen[0].startswith("skin_helper_unavailable:"))

    def test_pairing_needs_a_face_on_both_sides(self) -> None:
        from director.grade import SKIN_H, SKIN_W, _take_skin_pair
        from director.sync import LipsFrame
        with tempfile.TemporaryDirectory() as tmp:
            frames_dir = Path(tmp) / "skin-frames"
            frames_dir.mkdir()
            raw = _solid_frame(SKIN_W, SKIN_H, (128, 64, 32))
            box = (0.25, 0.25, 0.5, 0.5)
            # sorted names: B_00_cam, B_00_ref, B_01_cam, B_01_ref
            found = [LipsFrame(0, None, box),
                     LipsFrame(1, None, box),
                     LipsFrame(2, None, box),
                     LipsFrame(3, None, None)]
            cache: dict = {"helper": Path("/fake/helper")}

            def fake_decode(clip, at_local, lut, jpg_path, width=0, height=0):
                Path(jpg_path).write_bytes(b"fake-jpeg")
                return raw

            with mock.patch("director.grade.decode_skin_frame",
                            side_effect=fake_decode) as dec, \
                    mock.patch("director.sync.run_lips_helper",
                               return_value=found):
                out = _take_skin_pair(
                    _grade_clip("A", 0.0, 100.0), _grade_clip("B", 5.0, 100.0),
                    [50.0, 60.0], {"A": None, "B": None}, frames_dir, cache,
                    lambda label, warning: None)
            self.assertEqual(dec.call_count, 4)
            assert out is not None
            ref_stats, cam_stats, paired = out
            self.assertEqual(paired, [50.0])
            for stats in (ref_stats, cam_stats):
                self.assertEqual(stats.moments, 1)
                for got, want in zip(stats.mean, (128 / 255, 64 / 255, 32 / 255)):
                    self.assertAlmostEqual(got, want)
