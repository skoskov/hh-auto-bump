"""Synthetic subprocess tests: no HH, browser or real Task Scheduler access."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class LauncherTests(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / '.tmp' / 'launcher-tests'
        scratch.mkdir(parents=True, exist_ok=True)
        self.folder = tempfile.TemporaryDirectory(dir=scratch)
        self.root = Path(self.folder.name)
        shutil.copyfile(ROOT / 'run-scheduled.ps1', self.root / 'run-scheduled.ps1')
        self.shells = [Path(os.environ.get('WINDIR', 'C:/Windows')) /
                       'System32/WindowsPowerShell/v1.0/powershell.exe']
        if shutil.which('pwsh'):
            self.shells.append(Path(shutil.which('pwsh')))

    def tearDown(self):
        self.folder.cleanup()

    def launch(self, shell, code, scheduled=True, missing=False, nested=False):
        (self.root / 'hh_bump.py').write_text(
            'import sys\nprint("Резюме: подтверждено", flush=True)\nsys.exit(%d)\n' % code,
            encoding='utf-8')
        cmd = [str(shell), '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
               str(self.root / 'run-scheduled.ps1'), '-NoPause', '-PythonPath',
               str(self.root / 'missing.exe') if missing else sys.executable]
        if scheduled:
            cmd.append('-Scheduled')
        if nested:
            quote = lambda value: "'" + str(value).replace("'", "''") + "'"
            command = '& ' + quote(self.root / 'run-scheduled.ps1') + ' -NoPause -PythonPath ' + quote(sys.executable)
            if scheduled:
                command += ' -Scheduled'
            command += '; exit $global:LASTEXITCODE'
            cmd = [str(shell), '-NoProfile', '-ExecutionPolicy', 'Bypass', '-Command', command]
        result = subprocess.run(cmd, capture_output=True, timeout=30)
        logs = sorted((self.root / '.state' / 'console').glob('*.log'))
        self.assertTrue(logs)
        raw = logs[-1].read_bytes()
        self.assertNotIn(b'\x00', raw)
        return result.returncode, raw.decode('utf-8')

    def test_success_and_failure_codes_and_utf8(self):
        for shell in self.shells:
            if not shell.exists():
                continue
            for code in (0, 1, 2):
                with self.subTest(shell=shell, code=code):
                    actual, log = self.launch(shell, code)
                    self.assertEqual(code, actual)
                    self.assertIn('Резюме: подтверждено', log)
                    self.assertIn('EXIT ' + str(code), log)

    def test_missing_python_is_failure_without_prompt(self):
        for shell in self.shells:
            if shell.exists():
                actual, log = self.launch(shell, 0, missing=True)
                self.assertEqual(1, actual)
                self.assertIn('environment is missing', log)

    def test_busy_is_distinct_manually_and_success_for_scheduler(self):
        for shell in self.shells:
            if shell.exists():
                self.assertEqual(0, self.launch(shell, 3)[0])
                self.assertEqual(3, self.launch(shell, 3, scheduled=False)[0])

    def test_call_from_existing_powershell_scope_preserves_native_exit(self):
        for shell in self.shells:
            if shell.exists():
                for code in (0, 1, 3):
                    with self.subTest(shell=shell, code=code):
                        self.assertEqual(code, self.launch(shell, code, scheduled=False, nested=True)[0])


if __name__ == '__main__':
    unittest.main()
