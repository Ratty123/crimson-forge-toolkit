from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from cdmw.core.common import hidden_subprocess_kwargs


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_HKXPACK_JAR = REPO_ROOT / ".tools" / "hkxpack-cli.jar"
HKXPACK_TIMEOUT_SECONDS = 120


def _existing_file_from_env_or_default(env_name: str, default: Path | None = None) -> Path | None:
    raw_value = os.environ.get(env_name, "").strip()
    candidate = Path(raw_value) if raw_value else default
    if candidate is None:
        return None
    candidate = candidate.expanduser()
    return candidate if candidate.is_file() else None


def _run_hkxpack(jar_path: Path, *args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["java", "-jar", str(jar_path), *args],
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=HKXPACK_TIMEOUT_SECONDS,
        **hidden_subprocess_kwargs(),
    )


def _assert_success(testcase: unittest.TestCase, result: subprocess.CompletedProcess[str], action: str) -> None:
    combined_output = "\n".join((result.stdout or "", result.stderr or ""))
    logged_failure = "[SEVERE]" in combined_output or "[Fatal Error]" in combined_output
    if result.returncode == 0 and not logged_failure:
        return
    output = "\n".join(
        part
        for part in (
            f"HKXPack {action} failed with exit code {result.returncode}.",
            "stdout:",
            result.stdout.strip(),
            "stderr:",
            result.stderr.strip(),
        )
        if part
    )
    testcase.fail(output)


class HKXPackIntegrationTests(unittest.TestCase):
    def test_hkxpack_unpacks_and_repacks_sample_hkx(self) -> None:
        if shutil.which("java") is None:
            self.skipTest("Java is not available on PATH.")

        jar_path = _existing_file_from_env_or_default("HKXPACK_CLI_JAR", DEFAULT_HKXPACK_JAR)
        if jar_path is None:
            self.skipTest("Set HKXPACK_CLI_JAR or place hkxpack-cli.jar under .tools to run this test.")

        sample_path = _existing_file_from_env_or_default("HKXPACK_SAMPLE_HKX")
        if sample_path is None:
            self.skipTest("Set HKXPACK_SAMPLE_HKX to a compatible .hkx file to run this test.")

        with tempfile.TemporaryDirectory() as temp_dir:
            work_dir = Path(temp_dir)
            input_hkx = work_dir / "sample_input.hkx"
            shutil.copyfile(sample_path, input_hkx)

            unpack_result = _run_hkxpack(jar_path, "unpack", input_hkx.name, cwd=work_dir)
            _assert_success(self, unpack_result, "unpack")

            unpacked_xml = input_hkx.with_suffix(".xml")
            self.assertTrue(unpacked_xml.is_file(), "HKXPack unpack did not create sample_input.xml.")
            self.assertGreater(unpacked_xml.stat().st_size, 0, "HKXPack unpack created an empty XML file.")

            roundtrip_xml = work_dir / "sample_roundtrip.xml"
            roundtrip_hkx = roundtrip_xml.with_suffix(".hkx")
            shutil.copyfile(unpacked_xml, roundtrip_xml)

            pack_result = _run_hkxpack(jar_path, "pack", roundtrip_xml.name, cwd=work_dir)
            _assert_success(self, pack_result, "pack")

            self.assertTrue(roundtrip_hkx.is_file(), "HKXPack pack did not create sample_roundtrip.hkx.")
            self.assertGreater(roundtrip_hkx.stat().st_size, 0, "HKXPack pack created an empty HKX file.")


if __name__ == "__main__":
    unittest.main()
