#!/usr/bin/env python3
"""Build a versioned macOS bootstrap bundle from reviewed release inputs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import tarfile
import tempfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MACOS_TEMPLATES = ROOT / "packaging" / "macos"
ARCHITECTURES = {"arm64": "aarch64-apple-darwin", "x86_64": "x86_64-apple-darwin"}
UV_VERSION = "0.11.16"
IGNORED_NAMES = {
    ".env",
    ".venv",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
    "node_modules",
    "secrets",
    "staging",
    "temp",
    "tests",
}


class BundleError(RuntimeError):
    pass


def _version() -> str:
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return str(metadata["project"]["version"])


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_extract(archive: Path, destination: Path) -> None:
    with tarfile.open(archive, "r:gz") as source:
        root = destination.resolve()
        for member in source.getmembers():
            target = (destination / member.name).resolve()
            if not target.is_relative_to(root):
                raise BundleError("The uv archive contains an unsafe path.")
        source.extractall(destination, filter="data")


def _copy_ingestion(destination: Path) -> tuple[str, ...]:
    def ignore(_directory: str, names: list[str]) -> set[str]:
        return {
            name
            for name in names
            if name in IGNORED_NAMES or name.endswith((".pyc", ".pyo"))
        }

    shutil.copytree(ROOT / "ingestion", destination, ignore=ignore)
    files = tuple(
        path.relative_to(destination).as_posix()
        for path in sorted(destination.rglob("*"))
        if path.is_file()
    )
    (destination / ".managed-files").write_text(
        "\n".join(files) + "\n", encoding="utf-8"
    )
    return files


def _copy_uv(archive: Path, destination: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="rag-favorite-uv-") as temporary:
        extracted = Path(temporary)
        _safe_extract(archive, extracted)
        candidates = [
            path
            for path in extracted.rglob("uv")
            if path.is_file() and os.access(path, os.X_OK)
        ]
        if len(candidates) != 1:
            raise BundleError(
                "The uv archive does not contain one executable named uv."
            )
        shutil.copy2(candidates[0], destination)
        destination.chmod(0o755)


def _write_checksums(bundle: Path) -> None:
    payload = bundle / "payload"
    rows = [
        f"{_sha256(path)}  {path.relative_to(bundle).as_posix()}"
        for path in sorted(payload.rglob("*"))
        if path.is_file()
    ]
    (bundle / "SHA256SUMS").write_text("\n".join(rows) + "\n", encoding="utf-8")


def build(*, wheel: Path, uv_archive: Path, architecture: str, output: Path) -> Path:
    version = _version()
    expected_wheel = f"rag_favorite-{version}-py3-none-any.whl"
    if wheel.name != expected_wheel or not wheel.is_file():
        raise BundleError(f"Expected release wheel {expected_wheel}.")
    if architecture not in ARCHITECTURES:
        raise BundleError(f"Unsupported macOS architecture: {architecture}.")
    if not uv_archive.is_file():
        raise BundleError("The reviewed uv archive is missing.")

    output.mkdir(parents=True, exist_ok=True)
    bundle_name = f"rag-favorite-{version}-macos-{architecture}"
    with tempfile.TemporaryDirectory(prefix="rag-favorite-macos-") as temporary:
        bundle = Path(temporary) / bundle_name
        payload = bundle / "payload"
        payload.mkdir(parents=True)
        shutil.copy2(wheel, payload / wheel.name)
        shutil.copy2(ROOT / "LICENSE", payload / "rag-favorite-LICENSE")
        shutil.copy2(ROOT / "LICENSE", payload / "uv-LICENSE-APACHE")
        shutil.copy2(MACOS_TEMPLATES / "UV-LICENSE-MIT", payload / "uv-LICENSE-MIT")
        _copy_uv(uv_archive, payload / "uv")
        _copy_ingestion(payload / "ingestion")
        (payload / "bundle-metadata.json").write_text(
            json.dumps(
                {
                    "product": "rag-favorite",
                    "version": version,
                    "architecture": architecture,
                    "uv_version": UV_VERSION,
                    "uv_target": ARCHITECTURES[architecture],
                    "uv_binary_sha256": _sha256(payload / "uv"),
                    "uv_release": f"https://github.com/astral-sh/uv/releases/tag/{UV_VERSION}",
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )

        replacements = {
            "@VERSION@": version,
            "@ARCHITECTURE@": architecture,
            "@WHEEL_NAME@": wheel.name,
        }
        for name in ("install.command", "uninstall.command", "README.md"):
            content = (MACOS_TEMPLATES / name).read_text(encoding="utf-8")
            for token, value in replacements.items():
                content = content.replace(token, value)
            if re.search(r"@[A-Z][A-Z_]*@", content):
                raise BundleError(f"Unresolved bundle template token in {name}.")
            destination = bundle / name
            destination.write_text(content, encoding="utf-8")
            destination.chmod(0o755 if name.endswith(".command") else 0o644)

        _write_checksums(bundle)
        archive = output / f"{bundle_name}.tar.gz"
        with tarfile.open(archive, "w:gz", format=tarfile.PAX_FORMAT) as target:
            target.add(bundle, arcname=bundle.name, recursive=True)
        checksum = output / f"{archive.name}.sha256"
        checksum.write_text(f"{_sha256(archive)}  {archive.name}\n", encoding="utf-8")
        return archive


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheel", required=True, type=Path)
    parser.add_argument("--uv-archive", required=True, type=Path)
    parser.add_argument("--architecture", required=True, choices=tuple(ARCHITECTURES))
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    archive = build(
        wheel=arguments.wheel,
        uv_archive=arguments.uv_archive,
        architecture=arguments.architecture,
        output=arguments.output,
    )
    print(archive)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
