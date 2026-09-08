"""Build two isolated wheels from frozen inputs and compare their exact bytes."""
import argparse
import hashlib
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output-dir", type=Path, default=ROOT / "work" / "repro-build")
    args = p.parse_args()
    pins = json.loads((ROOT / "build/toolchain.lock.json").read_text())
    version = subprocess.run(["uv", "--version"], capture_output=True, text=True, check=True).stdout.strip()
    if version.split()[:2] != ["uv", pins["uv_version"]]:
        raise ValueError("uv version differs from build/toolchain.lock.json. Install the pinned uv version and retry.")
    base = args.output_dir.resolve() / uuid.uuid4().hex
    env = os.environ.copy()
    env["SOURCE_DATE_EPOCH"] = pins["source_date_epoch"]
    wheels = []
    for label in ("first", "second"):
        target = base / label
        target.mkdir(parents=True)
        source = target/'source'
        source.mkdir()
        for name in ('pyproject.toml','uv.lock','LICENSE','.python-version'):
            shutil.copyfile(ROOT/name,source/name)
        shutil.copytree(ROOT/'src',source/'src',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        subprocess.run(["uv", "sync", "--frozen"], cwd=source, env=env, check=True)
        subprocess.run(["uv", "build", "--wheel", "--no-build-isolation", "--out-dir", str(target)], cwd=source, env=env, check=True)
        matches = list(target.glob("*.whl"))
        if len(matches) != 1:
            raise ValueError("Expected exactly one wheel per build.")
        wheels.append(matches[0])
        python = source/'.venv'/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
        # Install the built artifact over the build environment's editable package;
        # isolated Python must resolve the wheel's package and all runtime assets.
        subprocess.run(['uv','pip','install','--no-deps','--python',str(python),'--force-reinstall',str(matches[0])],cwd=target,check=True)
        check = "from pathlib import Path; import game_census; from game_census.cli import main; p=Path(game_census.__file__).resolve(); assert '.venv' in p.parts, p; assert (p.parent/'templates/_enrichment.html').is_file(); assert (p.parent/'migrations/011_archive_ownership.sql').is_file(); assert main(['--help']) == 0; print('PASS: isolated wheel install and runtime assets')"
        subprocess.run([str(python),'-I','-c',check],cwd=target,check=True)
    checksums = [hashlib.sha256(path.read_bytes()).hexdigest() for path in wheels]
    if checksums[0] != checksums[1]:
        raise ValueError("Canonical wheel bytes differ between identical-input builds.")
    with zipfile.ZipFile(wheels[0]) as wheel:
        names = wheel.namelist()
        required = ["game_census/migrations/001_initial.sql", "game_census/templates/index.html", "game_census/static/app.js"]
        if any(name not in names for name in required):
            raise ValueError("Wheel is missing runtime schema, templates or static assets.")
    print(json.dumps({"status": "succeeded", "canonical_artifact": wheels[0].name, "sha256": checksums[0],
                      "identical_builds": 2, "isolated_install_checks":2, "paths": [str(path) for path in wheels], "runtime_assets_present": True}, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        raise SystemExit(1)
