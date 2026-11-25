"""What every runner test starts from: none of the caller's settings, and a scratch
directory of its own for the shells' state."""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def hermetic_environment(monkeypatch, tmp_path) -> None:
    # The settings read the process environment for every field a test does not pass: a
    # shell with the compose `.env` exported would otherwise change what the tests see.
    for name in list(os.environ):
        if name.upper().startswith("SANDBOX_") or name.upper() == "APP_ENV":
            monkeypatch.delenv(name, raising=False)
    # The supervisors make their state directories under TMPDIR, and a shell killed with
    # SIGKILL never removes its own: here they go with the test's directory.
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    monkeypatch.setenv("TMPDIR", str(scratch))
