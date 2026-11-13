"""Pre-deploy boot checks: main:app must import even if insurance/ shadows bare `dialog`."""
import importlib
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / 'web'


def test_sector_dialog_exports_required_attributes():
    sys.path.insert(0, str(WEB))
    for name in list(sys.modules):
        if name in ('sector_dialog', 'dialog') or name.startswith('sector_dialog.'):
            del sys.modules[name]
    import sector_dialog
    for attr in ('BusinessSectorError', 'InsuranceDisabledSectorError', 'process', 'sector_of'):
        assert hasattr(sector_dialog, attr), attr
    assert issubclass(sector_dialog.InsuranceDisabledSectorError, sector_dialog.BusinessSectorError)


def test_main_imports_when_insurance_dir_shadows_bare_dialog():
    """Reproduce the Railway ImportError class of bug, then prove main still boots."""
    # cwd must NOT be web/: an empty '' path entry would prefer web/dialog.py over insurance/.
    code = f"""
import sys
web = {str(WEB)!r}
ins = {str(WEB / 'insurance')!r}
sys.path = [ins, web] + [p for p in sys.path if p not in ('', web, ins)]
import dialog, sector_dialog, main
assert dialog.__file__.replace('\\\\','/').endswith('insurance/dialog.py'), dialog.__file__
assert not hasattr(dialog, 'BusinessSectorError')
assert hasattr(sector_dialog, 'BusinessSectorError')
assert hasattr(sector_dialog, 'InsuranceDisabledSectorError')
assert hasattr(sector_dialog, 'process') and hasattr(sector_dialog, 'sector_of')
assert main.app is not None
print('boot_ok', main.app.name)
"""
    proc = subprocess.run(
        [sys.executable, '-c', code],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + '\n' + proc.stderr
    assert 'boot_ok' in proc.stdout


def test_main_app_importable_like_gunicorn_target():
    """Same object Gunicorn loads: main:app from the web root directory."""
    code = f"""
import sys
web = {str(WEB)!r}
sys.path = [web] + [p for p in sys.path if p not in ('', web)]
import main
assert main.app is not None
print('app_ok', type(main.app).__name__, main.app.name)
"""
    proc = subprocess.run(
        [sys.executable, '-c', code],
        cwd=str(WEB),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + '\n' + proc.stderr
    assert 'Flask' in proc.stdout


@pytest.mark.skipif(sys.platform.startswith('win'), reason='gunicorn requires fcntl (Linux/Railway)')
def test_gunicorn_check_config_linux():
    env = os.environ.copy()
    env['PYTHONPATH'] = str(WEB)
    proc = subprocess.run(
        [sys.executable, '-m', 'gunicorn', 'main:app', '--check-config', '--bind', '127.0.0.1:0'],
        cwd=str(WEB),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + '\n' + proc.stderr


@pytest.mark.skipif(sys.platform.startswith('win'), reason='gunicorn requires fcntl (Linux/Railway)')
def test_gunicorn_worker_boots_briefly_linux():
    env = os.environ.copy()
    env['PYTHONPATH'] = str(WEB)
    env.setdefault('PORT', '18087')
    gun = subprocess.Popen(
        [
            sys.executable, '-m', 'gunicorn', 'main:app',
            '--bind', f"127.0.0.1:{env['PORT']}",
            '--workers', '1', '--threads', '1', '--timeout', '30',
        ],
        cwd=str(WEB),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.time() + 25
        started = False
        err = ''
        while time.time() < deadline:
            if gun.poll() is not None:
                err = (gun.stderr.read() if gun.stderr else '') or ''
                break
            probe = subprocess.run(
                [sys.executable, '-c',
                 'from urllib.request import urlopen\n'
                 f'urlopen("http://127.0.0.1:{env["PORT"]}/", timeout=2)\n'],
                capture_output=True, text=True, timeout=10,
            )
            if probe.returncode == 0 or 'HTTP Error' in (probe.stderr or ''):
                started = True
                break
            time.sleep(0.4)
        assert gun.poll() is None or started, err or 'gunicorn exited before accepting connections'
    finally:
        gun.terminate()
        try:
            gun.wait(timeout=10)
        except subprocess.TimeoutExpired:
            gun.kill()
