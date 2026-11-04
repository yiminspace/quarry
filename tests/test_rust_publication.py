"""Release version gate must reject accidental mismatches before publication."""
import pathlib, subprocess, sys, tomllib
ROOT=pathlib.Path(__file__).resolve().parents[1]
def test_release_version_gate_accepts_exact_current_version():
    version=tomllib.loads((ROOT/'Cargo.toml').read_text())['workspace']['package']['version']
    assert subprocess.run([sys.executable,str(ROOT/'scripts/validate_release.py'),'v'+version]).returncode==0

def test_release_version_gate_rejects_mismatched_version():
    assert subprocess.run([sys.executable,str(ROOT/'scripts/validate_release.py'),'v999.0.0'],capture_output=True).returncode!=0
