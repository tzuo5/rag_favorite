"""The installer preserves independent services and restores a prior drop-in."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def installer(tmp_path, monkeypatch):
    path = Path(__file__).parents[1] / "examples/install_video_services.py"
    spec = importlib.util.spec_from_file_location("install_video_services", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "UNITS", tmp_path / "units")
    monkeypatch.setattr(module, "BACKUP", tmp_path / "backups")
    monkeypatch.setattr(
        module.subprocess, "run", lambda *a, **kw: SimpleNamespace(stdout="0")
    )
    return module


def test_guard_reinstall_and_uninstall_restore_only_owned_file(installer):
    target = installer.UNITS / installer.MEMORY_DROP_IN
    target.parent.mkdir(parents=True)
    target.write_text("# previous owner configuration\n[Service]\nMemoryMax=9G\n")
    previous = target.read_bytes()
    unrelated = target.parent / "90-owner.conf"
    unrelated.write_text("[Service]\nEnvironment=OWNER=1\n")
    installer.install_memory_guard()
    installer.install_memory_guard()
    assert target.read_text() == installer.MEMORY_GUARD
    assert "MemoryHigh=6G" in target.read_text()
    assert "MemoryMax=8G" in target.read_text()
    assert "MemorySwapMax=1G" in target.read_text()
    assert "StartLimitBurst=3" in target.read_text()
    assert "StartLimitIntervalSec=10min" in target.read_text()
    assert "OOMPolicy=kill" in target.read_text()
    assert not (installer.UNITS / "rag-favorite-imagebind.service.d").exists()
    installer.uninstall_memory_guard()
    assert target.read_bytes() == previous and unrelated.exists()


def test_guard_does_not_apply_hard_cap_to_oversized_live_worker(installer, monkeypatch):
    monkeypatch.setattr(
        installer.subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(stdout=str(21 * 1024**3)),
    )
    with pytest.raises(RuntimeError, match="Drain and stop"):
        installer.install_memory_guard()
    assert not installer.UNITS.exists()
