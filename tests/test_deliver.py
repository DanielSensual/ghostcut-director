"""Deliver page: the real files a cut job wrote, and revealing one in Finder."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

APP = Path(__file__).resolve().parents[1] / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))
import engine  # noqa: E402


class DeliverFilesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.job = Path(self.tmp.name) / "my job"
        self.job.mkdir()
        (self.job / "cut.mp4").write_bytes(b"x" * 2_000_000)
        (self.job / "timeline.fcpxml").write_text("<fcpxml/>")
        self.fake = SimpleNamespace(job_dir=self.job, JOB_FILES=engine.Engine.JOB_FILES)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_lists_only_files_that_exist(self) -> None:
        out = engine.Engine.job_files(self.fake)
        self.assertEqual([f["name"] for f in out["files"]], ["cut.mp4", "timeline.fcpxml"])
        self.assertEqual(out["files"][0]["size"], "2.0 MB")
        self.assertEqual(out["files"][1]["size"], "1 KB")

    def test_no_job_lists_nothing(self) -> None:
        self.assertEqual(engine.Engine.job_files(SimpleNamespace(job_dir=None)), {"files": []})

    def test_reveal_selects_the_file_and_refuses_other_names(self) -> None:
        with mock.patch("engine.subprocess.Popen") as popen:
            engine.Engine.job_reveal(self.fake, "cut.mp4")
            popen.assert_called_once_with(["open", "-R", str(self.job / "cut.mp4")])
            engine.Engine.job_reveal(self.fake, "../../etc/passwd")
            popen.assert_called_with(["open", str(self.job)])
        with self.assertRaises(ValueError):
            engine.Engine.job_reveal(self.fake, "vertical.mp4")


if __name__ == "__main__":
    unittest.main()
