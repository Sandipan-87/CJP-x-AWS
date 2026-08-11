"""Engram · infra/build.py — prepares the approvals Lambda's deployment package.  [PLUMBER]

CDK's usual answer for a Python Lambda with dependencies is `aws_lambda_python_alpha
.PythonFunction`, which bundles via Docker by default. No Docker is available in this dev
environment (confirmed: `docker --version` -> command not found) -- same constraint
`workers/common/db.py`'s own docstring explains for why it uses `pg8000` instead of `psycopg3`.

Since `pg8000` (and its own dependencies, `scramp`/`asn1crypto`) are pure Python with no native
extension, a plain `pip install --target` from ANY platform, including this Windows dev machine,
produces a working Lambda package -- no cross-compilation, no Docker, no Lambda Layer needed.
This module does that: copies `workers/approvals/` + `workers/common/` (which includes the
bundled CA cert, `workers/common/certs/memory-ca.crt`) into a build directory, `pip install`s
`workers/requirements.txt` alongside them, and returns the path for `Code.from_asset()`.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
WORKERS_DIR = ROOT / "workers"
BUILD_DIR = pathlib.Path(__file__).resolve().parent / ".build" / "approvals"


def build_approvals_package() -> str:
    if BUILD_DIR.exists():
        shutil.rmtree(BUILD_DIR)
    BUILD_DIR.mkdir(parents=True)

    for name in ("approvals", "common"):
        shutil.copytree(
            WORKERS_DIR / name,
            BUILD_DIR / name,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )

    subprocess.run(
        [
            sys.executable, "-m", "pip", "install",
            "-r", str(WORKERS_DIR / "requirements.txt"),
            "--target", str(BUILD_DIR),
            "--no-cache-dir",
            "--quiet",
        ],
        check=True,
    )
    return str(BUILD_DIR)


if __name__ == "__main__":
    path = build_approvals_package()
    print(f"built Lambda package at {path}")
