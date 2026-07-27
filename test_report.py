#!/usr/bin/env python3
"""Tests for report.py (stdlib unittest)."""
import csv
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import report  # noqa: E402


def _write_csv(path, vin, rows, header):
    with open(path, "w", newline="") as f:
        if vin is not None:
            f.write(f"# vin: {vin}\n")
        w = csv.writer(f)
        w.writerow(header)
        for r in rows:
            w.writerow(r)


class ReadDriveLogTest(unittest.TestCase):
    def test_vin_comment_line_parsed(self):
        fd, path = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        try:
            _write_csv(path, "WBAUF11030PT67600",
                      [["00:00:00", "1", "", "", "{}", "800", "5", "1"]],
                      ["time", "epoch", "event", "pull_id", "event_data",
                       "rpm", "maf", "throttle"])
            vin, rows = report.read_drive_log(path)
            self.assertEqual(vin, "WBAUF11030PT67600")
            self.assertEqual(len(rows), 1)
        finally:
            os.unlink(path)

    def test_missing_vin_line_still_parses_rows(self):
        """Older recordings predate the '# vin:' header line (see
        CLAUDE.md's note on the two E87s) -- must degrade gracefully."""
        fd, path = tempfile.mkstemp(suffix=".csv")
        os.close(fd)
        try:
            _write_csv(path, None,
                      [["00:00:00", "1", "", "", "{}", "800", "5", "1"]],
                      ["time", "epoch", "event", "pull_id", "event_data",
                       "rpm", "maf", "throttle"])
            vin, rows = report.read_drive_log(path)
            self.assertIsNone(vin)
            self.assertEqual(len(rows), 1)
        finally:
            os.unlink(path)


class FindPullsTest(unittest.TestCase):
    def test_groups_rows_between_start_and_end_markers(self):
        header = ["time", "epoch", "event", "pull_id", "event_data",
                  "rpm", "maf", "throttle", "stft", "ltft"]
        rows = [
            dict(zip(header, ["t0", "0", "pull_start", "1", "{}",
                              "", "", "", "", ""])),
            dict(zip(header, ["t1", "1", "", "", "{}",
                              "3000", "50", "80", "1", "-2"])),
            dict(zip(header, ["t2", "2", "", "", "{}",
                              "6000", "120", "90", "2", "-3"])),
            dict(zip(header, ["t3", "3", "pull_end", "1", "{}",
                              "", "", "", "", ""])),
            # a row outside the window shouldn't be counted
            dict(zip(header, ["t4", "10", "", "", "{}",
                              "8000", "999", "100", "9", "9"])),
        ]
        pulls = report.find_pulls(rows)
        self.assertEqual(len(pulls), 1)
        p = pulls[0]
        self.assertEqual(p["pull_id"], "1")
        self.assertEqual(p["duration_s"], 3.0)
        self.assertEqual(p["peak_rpm"], 6000.0)
        self.assertEqual(p["peak_maf_gps"], 120.0)
        self.assertIsNotNone(p["power_kw"])
        self.assertIsNotNone(p["torque_nm"])

    def test_no_markers_means_no_pulls(self):
        header = ["time", "epoch", "event", "pull_id", "rpm"]
        rows = [dict(zip(header, ["t0", "0", "", "", "3000"]))]
        self.assertEqual(report.find_pulls(rows), [])

    def test_unmatched_start_without_end_is_ignored(self):
        header = ["time", "epoch", "event", "pull_id", "rpm"]
        rows = [dict(zip(header, ["t0", "0", "pull_start", "1", ""]))]
        self.assertEqual(report.find_pulls(rows), [])


class BuildReportTest(unittest.TestCase):
    def test_mismatched_vins_across_files_warn_and_dont_merge(self):
        paths_ = []
        for vin in ("WBAAAAAAAAAAAAAAA", "WBBBBBBBBBBBBBBBB"):
            fd, path = tempfile.mkstemp(suffix=".csv")
            os.close(fd)
            _write_csv(path, vin,
                      [["t0", "0", "pull_start", "1", "{}", "", "", ""],
                       ["t1", "1", "pull_end", "1", "{}", "3000", "40", "80"]],
                      ["time", "epoch", "event", "pull_id", "event_data",
                       "rpm", "maf", "throttle"])
            paths_.append(path)
        try:
            rep = report.build_report(paths_)
            self.assertTrue(rep["warnings"])
            self.assertIsNone(rep["vin"])
        finally:
            for p in paths_:
                os.unlink(p)

    def test_vin_override_used_when_no_csvs_given(self):
        rep = report.build_report([], vin_override="WBAUF11030PT67600")
        self.assertEqual(rep["vin"], "WBAUF11030PT67600")
        self.assertEqual(rep["files"], [])


class MarkdownRenderTest(unittest.TestCase):
    def test_renders_without_crashing_on_minimal_report(self):
        rep = report.build_report([], vin_override=None)
        md = report.to_markdown(rep)
        self.assertIn("GarageDiag Diagnostic Report", md)
        self.assertIn("Not identified", md)


if __name__ == "__main__":
    unittest.main(verbosity=2)
