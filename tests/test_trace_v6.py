import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from pff_parser import process_pff, TRACE_FULL, TRACE_COMPACT

REFERENCE_PFF = ROOT / "tests" / "reference.pff"
SCRIPT_PATH = ROOT / "src" / "pff_parser.py"


class TraceV7Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.trace_reports = {
            TRACE_FULL: process_pff(
                str(REFERENCE_PFF),
                mode="TRACE",
                trace_detail=TRACE_FULL,
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
        line = next((x for x in report.splitlines() if x.startswith("events: ")), "")
        if key == "events_shown":
            m = re.search(r"events:\s*(\d+)/\d+", line)
        elif key == "events_total":
            m = re.search(r"events:\s*\d+/(\d+)", line)
        elif key == "modules_shown":
            m = re.search(r"modules:\s*(\d+)/\d+", line)
        elif key == "modules_total":
            m = re.search(r"modules:\s*\d+/(\d+)", line)
        else:
            raise AssertionError(f"Unknown coverage key: {key}")
        if not m:
            raise AssertionError(f"Coverage key not found: {key}")
        return int(m.group(1))

    def test_trace_and_perf_smoke(self):
        compact = self.trace_reports[TRACE_COMPACT]
        self.assertIn("=== TRACE [COMPACT] ===", compact)
        self.assertIn("=== MODULES MAP ===", compact)
        self.assertIn("=== CALL INDEX ===", compact)
        self.assertIn("=== MODULES ===", compact)

        perf = self.perf_report
        self.assertIn("=== HOTSPOTS", perf)

    def test_detail_invariant_full_ge_compact(self):
        full = self.trace_reports[TRACE_FULL]
        compact = self.trace_reports[TRACE_COMPACT]

        self.assertGreaterEqual(
            self._coverage_value(full, "events_shown"),
            self._coverage_value(compact, "events_shown"),
        )
        self.assertGreaterEqual(
            self._coverage_value(full, "modules_shown"),
            self._coverage_value(compact, "modules_shown"),
        )

    def test_trace_required_sections_and_order(self):
        report = self.trace_reports[TRACE_COMPACT]
        required = [
            "=== TRACE [COMPACT] ===",
            "=== TRACE META ===",
            "=== TRACE COVERAGE ===",
            "=== MODULES MAP ===",
            "=== EXECUTION FLOW ===",
            "=== CALL INDEX ===",
            "=== MODULES ===",
            "=== TRACE REPRODUCE ===",
        ]
        cursor = -1
        for marker in required:
            idx = report.find(marker)
            self.assertGreater(idx, cursor, f"Missing or out of order: {marker}")
            cursor = idx

        self.assertIn("format: TRACE v7", report)
        self.assertIn("detail: compact", report)

    def test_compact_trace_has_no_threshold_stub_lines(self):
        compact = self.trace_reports[TRACE_COMPACT]
        self.assertNotIn("[свёрнуто по threshold]", compact)

    def test_trace_has_no_legacy_time_or_budget_markers(self):
        full = self.trace_reports[TRACE_FULL]
        compact = self.trace_reports[TRACE_COMPACT]
        for report in (full, compact):
            self.assertNotIn("Budget:", report)
            self.assertNotIn("[Self:", report)
            self.assertNotRegex(report, r"\[\d+ms\s*/\s*\d+ms\]")

    def test_compact_link_format_is_hash_or_question(self):
        report = self.trace_reports[TRACE_COMPACT]
        self.assertRegex(report, r"#\d+")
        self.assertRegex(report, r"\?\d+")
        self.assertNotIn("[FACT E", report)
        self.assertNotIn("[INFERRED E", report)

    def test_trace_meta_is_compact(self):
        report = self.trace_reports[TRACE_COMPACT]
        self.assertNotIn("flow_threshold_ms:", report)
        self.assertNotIn("flags:", report)
        self.assertNotIn("entry:", report)
        self.assertNotIn("main_block:", report)
        self.assertIn("sessions:", report)

    def test_cli_no_compact_alias_switches_to_full(self):
        auto_out = ROOT / "tests" / "reference_TRACE_FULL.txt"
        auto_out.unlink(missing_ok=True)
        cmd = [
            sys.executable,
            str(SCRIPT_PATH),
            str(REFERENCE_PFF),
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
            self.assertIn("=== TRACE [FULL] ===", proc.stdout)
        finally:
            auto_out.unlink(missing_ok=True)

    def test_cli_normal_fallback_to_compact(self):
        auto_out = ROOT / "tests" / "reference_TRACE_COMPACT.txt"
        auto_out.unlink(missing_ok=True)
        cmd = [
            sys.executable,
            str(SCRIPT_PATH),
            str(REFERENCE_PFF),
            "--mode",
            "TRACE",
            "--trace-detail",
            "normal",
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
            self.assertIn("=== TRACE [COMPACT] ===", proc.stdout)
        finally:
            auto_out.unlink(missing_ok=True)

    def test_cli_default_output_name_depends_on_trace_detail(self):
        full_out = ROOT / "tests" / "reference_TRACE_FULL.txt"
        compact_out = ROOT / "tests" / "reference_TRACE_COMPACT.txt"
        full_out.unlink(missing_ok=True)
        compact_out.unlink(missing_ok=True)

        cmd_full = [
            sys.executable,
            str(SCRIPT_PATH),
            str(REFERENCE_PFF),
            "--mode",
            "TRACE",
            "--trace-detail",
            "full",
            "--no-model-prompt",
        ]
        cmd_compact = [
            sys.executable,
            str(SCRIPT_PATH),
            str(REFERENCE_PFF),
            "--mode",
            "TRACE",
            "--trace-detail",
            "compact",
            "--no-model-prompt",
        ]
        try:
            subprocess.run(
                cmd_full,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(ROOT),
                check=True,
            )
            subprocess.run(
                cmd_compact,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=str(ROOT),
                check=True,
            )
            self.assertTrue(full_out.exists())
            self.assertTrue(compact_out.exists())
        finally:
            full_out.unlink(missing_ok=True)
            compact_out.unlink(missing_ok=True)

    def test_removed_cli_args_are_not_available(self):
        proc = subprocess.run(
            [sys.executable, str(SCRIPT_PATH), "--help"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(ROOT),
            check=True,
        )
        self.assertNotIn("--entry", proc.stdout)
        self.assertNotIn("--main-block", proc.stdout)


if __name__ == "__main__":
    unittest.main()
