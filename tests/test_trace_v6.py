import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from pff_parser import process_pff, TRACE_FULL, TRACE_NORMAL, TRACE_COMPACT

REFERENCE_PFF = ROOT / "tests" / "reference.pff"
SCRIPT_PATH = ROOT / "src" / "pff_parser.py"


class TraceV6Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.trace_reports = {
            TRACE_FULL: process_pff(
                str(REFERENCE_PFF),
                mode="TRACE",
                trace_detail=TRACE_FULL,
                include_model_prompt=False,
            ),
            TRACE_NORMAL: process_pff(
                str(REFERENCE_PFF),
                mode="TRACE",
                trace_detail=TRACE_NORMAL,
                include_model_prompt=False,
            ),
            TRACE_COMPACT: process_pff(
                str(REFERENCE_PFF),
                mode="TRACE",
                trace_detail=TRACE_COMPACT,
                include_model_prompt=False,
            ),
        }
        cls.perf_report = process_pff(
            str(REFERENCE_PFF),
            mode="PERF",
            include_model_prompt=False,
        )

    @staticmethod
    def _coverage_value(report: str, key: str) -> int:
        match = re.search(rf"{re.escape(key)}=(\d+)", report)
        if not match:
            raise AssertionError(f"Coverage key not found: {key}")
        return int(match.group(1))

    def test_trace_and_perf_smoke(self):
        trace = self.trace_reports[TRACE_NORMAL]
        self.assertIn("=== TRACE [NORMAL] ===", trace)
        self.assertIn("=== CALL MAP ===", trace)
        self.assertIn("=== MODULES (справочник модулей) ===", trace)

        perf = self.perf_report
        self.assertIn("=== HOTSPOTS", perf)

    def test_detail_invariant_full_ge_normal_ge_compact(self):
        full = self.trace_reports[TRACE_FULL]
        normal = self.trace_reports[TRACE_NORMAL]
        compact = self.trace_reports[TRACE_COMPACT]

        events_full = self._coverage_value(full, "events_shown")
        events_normal = self._coverage_value(normal, "events_shown")
        events_compact = self._coverage_value(compact, "events_shown")

        call_full = self._coverage_value(full, "call_map_shown_entries")
        call_normal = self._coverage_value(normal, "call_map_shown_entries")
        call_compact = self._coverage_value(compact, "call_map_shown_entries")

        self.assertGreaterEqual(events_full, events_normal)
        self.assertGreaterEqual(events_normal, events_compact)
        self.assertGreaterEqual(call_full, call_normal)
        self.assertGreaterEqual(call_normal, call_compact)

    def test_trace_required_sections_and_order(self):
        report = self.trace_reports[TRACE_NORMAL]
        required = [
            "=== TRACE [NORMAL] ===",
            "=== TRACE META ===",
            "=== TRACE COVERAGE ===",
            "=== EXECUTION FLOW (эвристическая реконструкция) ===",
            "=== CALL MAP ===",
            "=== MODULES (справочник модулей) ===",
            "=== TRACE REPRODUCE ===",
        ]

        cursor = -1
        for marker in required:
            idx = report.find(marker)
            self.assertGreater(idx, cursor, f"Missing or out of order: {marker}")
            cursor = idx

        self.assertIn("trace_format: TRACE v6", report)
        self.assertIn("trace_detail: normal", report)

    def test_deprecated_no_compact_alias(self):
        out_file = ROOT / "tests" / "_tmp_cli_trace.txt"
        cmd = [
            sys.executable,
            str(SCRIPT_PATH),
            str(REFERENCE_PFF),
            str(out_file),
            "--mode",
            "TRACE",
            "--trace-detail",
            "compact",
            "--no-compact",
            "--no-model-prompt",
        ]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(ROOT),
                check=True,
            )
            self.assertIn("deprecated", proc.stderr.lower())
            self.assertIn("=== TRACE [FULL] ===", proc.stdout or "")
        finally:
            out_file.unlink(missing_ok=True)

    def test_trace_report_has_no_perf_threshold_parameter(self):
        report = self.trace_reports[TRACE_NORMAL]
        self.assertNotIn("--threshold", report)
        self.assertNotIn("=== PERF", report)

    def test_eventid_and_fact_inferred_present(self):
        report = self.trace_reports[TRACE_FULL]
        self.assertRegex(report, r"E\d{5}")
        self.assertIn("[FACT ", report)
        self.assertIn("[INFERRED ", report)


if __name__ == "__main__":
    unittest.main()
