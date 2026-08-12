"""Engram · infra/build.py — prepares each Lambda's deployment package.  [PLUMBER]

CDK's usual answer for a Python Lambda with dependencies is `aws_lambda_python_alpha
.PythonFunction`, which bundles via Docker by default. No Docker is available in this dev
environment (confirmed: `docker --version` -> command not found) -- same constraint
`workers/common/db.py`'s own docstring explains for why it uses `pg8000` instead of `psycopg3`.

Since `pg8000` (and its own dependencies, `scramp`/`asn1crypto`) are pure Python with no native
extension, a plain `pip install --target` from ANY platform, including this Windows dev machine,
produces a working Lambda package -- no cross-compilation, no Docker, no Lambda Layer needed.
This module does that: copies a given handler directory + `workers/common/` (which includes the
bundled CA cert, `workers/common/certs/memory-ca.crt`) into a build directory, `pip install`s
`workers/requirements.txt` alongside them, and returns the path for `Code.from_asset()`.

One function per Lambda (`build_approvals_package`/`build_webhooks_package`/
`build_metrics_package`) rather than one parameterized entry point some stacks call directly --
each is a one-line call site in its own stack construct, and naming them after what they build
reads better at the CDK call site than a generic `build_package("webhooks")` would.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
WORKERS_DIR = ROOT / "workers"
BUILD_ROOT = pathlib.Path(__file__).resolve().parent / ".build"


def _build_package(handler_dir_name: str) -> str:
    build_dir = BUILD_ROOT / handler_dir_name
    if build_dir.exists():
        shutil.rmtree(build_dir)
    build_dir.mkdir(parents=True)

    for name in (handler_dir_name, "common"):
        shutil.copytree(
            WORKERS_DIR / name,
            build_dir / name,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )

    subprocess.run(
        [
            sys.executable, "-m", "pip", "install",
            "-r", str(WORKERS_DIR / "requirements.txt"),
            "--target", str(build_dir),
            "--no-cache-dir",
            "--quiet",
        ],
        check=True,
    )
    return str(build_dir)


def build_approvals_package() -> str:
    return _build_package("approvals")


def build_webhooks_package() -> str:
    return _build_package("webhooks")


def build_metrics_package() -> str:
    return _build_package("metrics")


def build_sweep_enumerator_package() -> str:
    return _build_package("sweep_enumerator")


if __name__ == "__main__":
    for build_fn in (
        build_approvals_package, build_webhooks_package, build_metrics_package, build_sweep_enumerator_package,
    ):
        path = build_fn()
        print(f"built {path}")
