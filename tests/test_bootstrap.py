"""Dependency setup tests use local fixtures and mocked downloads/installers."""

import hashlib
import io
import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from scripts import bootstrap
from src.runtime import PipelineError


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "requirements.txt").write_text("fixture==1.0\n", encoding="utf-8")
        self.logger = logging.Logger("setup-tests")
        self.logger.addHandler(logging.NullHandler())

    def test_python_setup_reuses_matching_successful_state(self):
        with patch("scripts.bootstrap.subprocess.run", return_value=Mock(returncode=0)), \
                patch("scripts.bootstrap.run_logged") as run:
            bootstrap.prepare_python(self.root, self.logger)
            self.assertEqual(run.call_count, 3)
            run.reset_mock()
            bootstrap.prepare_python(self.root, self.logger)
            run.assert_not_called()
            (self.root / "requirements.txt").write_text("fixture==2.0\n", encoding="utf-8")
            bootstrap.prepare_python(self.root, self.logger)
            self.assertEqual(run.call_count, 3)

    def test_missing_report_package_reinstalls_even_when_prepared(self):
        def missing_reportlab(command, **kwargs):
            # pip check passes, but the import check fails when reportlab is absent.
            return Mock(returncode=1 if "reportlab" in command[-1] else 0)
        with patch("scripts.bootstrap.subprocess.run", return_value=Mock(returncode=0)), \
                patch("scripts.bootstrap.run_logged"):
            bootstrap.prepare_python(self.root, self.logger)               # state recorded
        with patch("scripts.bootstrap.subprocess.run", side_effect=missing_reportlab), \
                patch("scripts.bootstrap.run_logged") as run:
            bootstrap.prepare_python(self.root, self.logger)
            self.assertEqual(run.call_count, 3)                             # reinstalled, not "already prepared"
        self.assertIn("reportlab", bootstrap.IMPORT_CHECK)

    def test_failed_python_install_is_not_cached(self):
        with patch("scripts.bootstrap.subprocess.run", return_value=Mock(returncode=0)), \
                patch("scripts.bootstrap.run_logged", side_effect=PipelineError("download failed")):
            with self.assertRaises(PipelineError):
                bootstrap.prepare_python(self.root, self.logger)
        self.assertFalse((self.root / ".runtime" / "python-setup.json").exists())

    def test_bad_download_checksum_prevents_running_installer(self):
        checksum = f"{'0' * 32} *R-{bootstrap.R_VERSION}-win.exe\n".encode()
        with patch("scripts.bootstrap.urllib.request.urlopen", side_effect=[io.BytesIO(checksum), io.BytesIO(b"bad installer")]), \
                patch("scripts.bootstrap.run_logged") as run:
            with self.assertRaisesRegex(PipelineError, "checksum"):
                bootstrap.install_r(self.root, self.logger)
            run.assert_not_called()

    def test_verified_r_download_installs_into_project(self):
        installer = b"fixture installer bytes"
        checksum = f"{hashlib.md5(installer).hexdigest()} *R-{bootstrap.R_VERSION}-win.exe\n".encode()
        def installed(*args, **kwargs):
            target = self.root / ".runtime" / f"R-{bootstrap.R_VERSION}" / "bin" / "Rscript.exe"
            target.parent.mkdir(parents=True)
            target.write_bytes(b"fixture")
        with patch("scripts.bootstrap.urllib.request.urlopen", side_effect=[io.BytesIO(checksum), io.BytesIO(installer)]), \
                patch("scripts.bootstrap.run_logged", side_effect=installed) as run:
            executable = bootstrap.install_r(self.root, self.logger)
            self.assertTrue(executable.is_relative_to(self.root))
            command = run.call_args.args[0]
            self.assertIn("/CURRENTUSER", command)
            self.assertIn("/NOICONS", command)
            self.assertIn(f"/DIR={self.root / '.runtime' / ('R-' + bootstrap.R_VERSION)}", command)

    def test_installed_r_is_reused_without_downloading(self):
        with patch("scripts.bootstrap.prepare_python"), \
                patch("scripts.bootstrap.find_rscript", return_value=self.root / "Rscript.exe"), \
                patch("scripts.bootstrap.r_environment", return_value={"R_LIBS_USER": "test-library"}), \
                patch("scripts.bootstrap.run_logged") as run, \
                patch("scripts.bootstrap.install_r") as installer:
            bootstrap.prepare_environment(self.root, self.logger)
            installer.assert_not_called()
            self.assertEqual(run.call_count, 1)
            self.assertIn("install", run.call_args.args[0])
