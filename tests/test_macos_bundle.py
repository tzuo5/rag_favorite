from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import tarfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
VERSION = "1.2.0"
SPEC = importlib.util.spec_from_file_location(
    "build_macos_bundle", ROOT / "scripts/build_macos_bundle.py"
)
assert SPEC and SPEC.loader
BUILD_MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILD_MODULE)
BundleError = BUILD_MODULE.BundleError
build = BUILD_MODULE.build


def _fake_uv_archive(tmp_path: Path, *, unsafe: bool = False) -> Path:
    archive = tmp_path / "uv-aarch64-apple-darwin.tar.gz"
    executable = tmp_path / "uv"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    with tarfile.open(archive, "w:gz") as target:
        target.add(
            executable,
            arcname=("../../unsafe/uv" if unsafe else "uv-aarch64-apple-darwin/uv"),
        )
    return archive


def test_macos_bundle_contains_reviewed_payload_and_installers(tmp_path: Path) -> None:
    wheel = tmp_path / f"rag_favorite-{VERSION}-py3-none-any.whl"
    wheel.write_bytes(b"test-wheel")

    archive = build(
        wheel=wheel,
        uv_archive=_fake_uv_archive(tmp_path),
        architecture="arm64",
        output=tmp_path / "dist",
    )
    checksum = archive.with_name(archive.name + ".sha256")
    assert (
        checksum.read_text(encoding="utf-8").split()[0]
        == hashlib.sha256(archive.read_bytes()).hexdigest()
    )

    extracted = tmp_path / "extracted"
    with tarfile.open(archive, "r:gz") as source:
        source.extractall(extracted, filter="data")
    bundle = extracted / f"rag-favorite-{VERSION}-macos-arm64"
    assert (bundle / "payload" / wheel.name).is_file()
    assert (bundle / "payload/ingestion/backend/ingestion/cli.py").is_file()
    assert (bundle / "payload/ingestion/openclaw-plugin/index.js").is_file()
    assert not (bundle / "payload/ingestion/tests").exists()
    assert not tuple((bundle / "payload/ingestion").rglob("__pycache__"))
    metadata = json.loads(
        (bundle / "payload/bundle-metadata.json").read_text(encoding="utf-8")
    )
    assert metadata["version"] == VERSION
    assert metadata["architecture"] == "arm64"
    assert metadata["uv_version"] == "0.11.16"
    assert "@VERSION@" not in (bundle / "README.md").read_text(encoding="utf-8")
    assert (
        subprocess.run(
            ["bash", "-n", str(bundle / "install.command")], check=False
        ).returncode
        == 0
    )
    assert (
        subprocess.run(
            ["bash", "-n", str(bundle / "uninstall.command")], check=False
        ).returncode
        == 0
    )

    checksums = (bundle / "SHA256SUMS").read_text(encoding="utf-8")
    assert f"payload/{wheel.name}" in checksums
    assert "payload/uv" in checksums


def test_macos_bundle_rejects_unexpected_wheel_and_unsafe_uv_archive(
    tmp_path: Path,
) -> None:
    wrong_wheel = tmp_path / "rag_favorite-0.0.0-py3-none-any.whl"
    wrong_wheel.write_bytes(b"wrong")
    with pytest.raises(BundleError, match="Expected release wheel"):
        build(
            wheel=wrong_wheel,
            uv_archive=_fake_uv_archive(tmp_path),
            architecture="arm64",
            output=tmp_path / "dist",
        )

    wheel = tmp_path / f"rag_favorite-{VERSION}-py3-none-any.whl"
    wheel.write_bytes(b"test-wheel")
    with pytest.raises(BundleError, match="unsafe path"):
        build(
            wheel=wheel,
            uv_archive=_fake_uv_archive(tmp_path, unsafe=True),
            architecture="arm64",
            output=tmp_path / "dist",
        )


def test_macos_installer_templates_are_bash_32_compatible() -> None:
    for name in ("install.command", "uninstall.command"):
        assert (
            subprocess.run(
                ["bash", "-n", str(ROOT / "packaging/macos" / name)], check=False
            ).returncode
            == 0
        )
