"""Guardrails for the opt-in production load helpers."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from load.target import checked_target

RUN_ID = "00000000-0000-0000-0000-000000000001"


@pytest.mark.parametrize(
    "url",
    ["https://flickpond.com", "https://flickpond.com.evil.invalid", "https://flickpond.com/health"],
)
def test_local_mode_rejects_nonlocal_targets(monkeypatch, url):
    monkeypatch.delenv("LOAD_MODE", raising=False)
    monkeypatch.setenv("LOAD_BASE_URL", url)
    with pytest.raises(RuntimeError, match="not authorized"):
        checked_target()


@pytest.mark.parametrize(
    "url",
    ["https://flickpond.com.evil.invalid", "https://flickpond.com:443", "http://flickpond.com"],
)
def test_production_mode_rejects_alternate_origins(monkeypatch, url):
    monkeypatch.setenv("LOAD_MODE", "production")
    monkeypatch.setenv("LOAD_RUN_ID", RUN_ID)
    monkeypatch.setenv("LOAD_CONFIRM", f"flickpond-production:{RUN_ID}")
    monkeypatch.setenv("LOAD_BASE_URL", url)
    with pytest.raises(RuntimeError, match="not authorized"):
        checked_target()


def test_production_mode_requires_matching_confirmation(monkeypatch):
    monkeypatch.setenv("LOAD_MODE", "production")
    monkeypatch.setenv("LOAD_RUN_ID", RUN_ID)
    monkeypatch.setenv("LOAD_BASE_URL", "https://flickpond.com")
    monkeypatch.delenv("LOAD_CONFIRM", raising=False)
    with pytest.raises(RuntimeError, match="not authorized"):
        checked_target()
    monkeypatch.setenv("LOAD_CONFIRM", f"flickpond-production:{RUN_ID}")
    assert checked_target() == "https://flickpond.com"


def test_production_fixture_requires_upload_permission_without_writing_evidence(tmp_path):
    video = tmp_path / "fixture.mp4"
    video.write_bytes(b"offline validation")
    evidence = Path("load/results") / f"cloud-{RUN_ID}" / "fixture.json"
    environment = {
        **os.environ,
        "LOAD_MODE": "production",
        "LOAD_BASE_URL": "https://flickpond.com",
        "LOAD_RUN_ID": RUN_ID,
        "LOAD_CONFIRM": f"flickpond-production:{RUN_ID}",
    }
    environment.pop("LOAD_ALLOW_UPLOAD", None)
    result = subprocess.run(
        [sys.executable, "load/fixture.py", "prepare", "--video", str(video),
         "--evidence", str(evidence)],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "LOAD_ALLOW_UPLOAD=1" in result.stderr
    assert not evidence.exists()