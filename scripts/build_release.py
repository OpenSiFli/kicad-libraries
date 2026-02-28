#!/usr/bin/env python3
"""Build KiCad release package and upstream metadata."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build KiCad release artifacts")
    parser.add_argument(
        "--source-dir",
        type=Path,
        default=Path("."),
        help="Directory containing metadata.json/symbols/footprints/resources for packaging.",
    )
    parser.add_argument(
        "--tag",
        help="Release tag. If omitted, use GITHUB_REF_NAME or exact git tag on HEAD.",
    )
    return parser.parse_args()


def get_all_tags() -> list[str]:
    result = subprocess.run(
        ["git", "tag", "--sort=-version:refname"],
        capture_output=True,
        text=True,
        check=True,
    )
    return [tag.strip() for tag in result.stdout.splitlines() if tag.strip()]


def get_current_tag(explicit_tag: str | None) -> str:
    if explicit_tag:
        return explicit_tag

    github_tag = os.environ.get("GITHUB_REF_NAME", "").strip()
    if github_tag:
        return github_tag

    result = subprocess.run(
        ["git", "describe", "--tags", "--exact-match", "HEAD"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError("Not on a tagged commit and --tag not provided")
    return result.stdout.strip()


def get_repo_info() -> dict[str, str]:
    repo_url = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
    repo_name = os.environ.get("GITHUB_REPOSITORY", "OpenSiFli/kicad-libraries")
    return {
        "download_base": f"{repo_url}/{repo_name}/releases/download",
    }


def calculate_sha256(file_path: Path) -> str:
    digest = hashlib.sha256()
    with file_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def calculate_directory_size(directory: Path) -> int:
    size = 0
    for path in directory.rglob("*"):
        if path.is_file():
            size += path.stat().st_size
    return size


def read_metadata(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_metadata(path: Path, content: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(content, handle, indent=2, ensure_ascii=False)


def build_versions(tags: list[str], current_tag: str) -> list[dict[str, Any]]:
    versions: list[dict[str, Any]] = []
    for tag in tags:
        versions.append(
            {
                "version": tag,
                "status": "stable",
                "kicad_version": "9.0",
            }
        )

    if not versions:
        versions.append(
            {
                "version": current_tag,
                "status": "stable",
                "kicad_version": "9.0",
            }
        )
    return versions


def create_zip_from_dir(source_dir: Path, zip_path: Path) -> None:
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for file_path in sorted(source_dir.rglob("*")):
            if file_path.is_file():
                archive.write(file_path, file_path.relative_to(source_dir))


def write_output_files(
    package_path: Path,
    metadata_path: Path,
    package_size: int,
    install_size: int,
    package_sha256: str,
) -> None:
    Path("package_path.txt").write_text(str(package_path.resolve()), encoding="utf-8")
    Path("metadata_path.txt").write_text(str(metadata_path.resolve()), encoding="utf-8")
    Path("package_size.txt").write_text(str(package_size), encoding="utf-8")
    Path("install_size.txt").write_text(str(install_size), encoding="utf-8")
    Path("package_sha256.txt").write_text(package_sha256, encoding="utf-8")


def main() -> int:
    args = parse_args()

    source_dir = args.source_dir.resolve()
    metadata_path = source_dir / "metadata.json"
    if not metadata_path.is_file():
        print(f"Error: metadata.json not found under {source_dir}", file=sys.stderr)
        return 1

    current_tag = get_current_tag(args.tag)
    tags = get_all_tags()
    if current_tag not in tags:
        tags.insert(0, current_tag)

    package_metadata = read_metadata(metadata_path)
    package_metadata["versions"] = build_versions(tags, current_tag)

    temp_dir = Path("temp_package")
    if temp_dir.exists():
        shutil.rmtree(temp_dir)
    temp_dir.mkdir(parents=True)

    try:
        write_metadata(temp_dir / "metadata.json", package_metadata)

        for name in ("symbols", "footprints", "resources", "3dmodels"):
            src = source_dir / name
            if src.exists():
                dest = temp_dir / name
                if src.is_dir():
                    shutil.copytree(src, dest)
                else:
                    shutil.copy2(src, dest)

        install_size = calculate_directory_size(temp_dir)

        package_name = f"sifli-kicad-libraries-{current_tag}.zip"
        package_path = Path(package_name)
        create_zip_from_dir(temp_dir, package_path)

        package_size = package_path.stat().st_size
        package_sha256 = calculate_sha256(package_path)

        upstream_metadata = read_metadata(metadata_path)
        upstream_versions = build_versions(tags, current_tag)
        repo_info = get_repo_info()
        for entry in upstream_versions:
            if entry["version"] == current_tag:
                entry.update(
                    {
                        "download_url": f"{repo_info['download_base']}/{current_tag}/{package_name}",
                        "download_sha256": package_sha256,
                        "download_size": package_size,
                        "install_size": install_size,
                    }
                )
        upstream_metadata["versions"] = upstream_versions

        upstream_metadata_path = Path("metadata-upstream.json")
        write_metadata(upstream_metadata_path, upstream_metadata)

        write_output_files(
            package_path=package_path,
            metadata_path=upstream_metadata_path,
            package_size=package_size,
            install_size=install_size,
            package_sha256=package_sha256,
        )

        print(f"Package: {package_path}")
        print(f"Package size: {package_size}")
        print(f"Install size: {install_size}")
        print(f"SHA256: {package_sha256}")
        print(f"Upstream metadata: {upstream_metadata_path}")
        return 0
    finally:
        if temp_dir.exists():
            shutil.rmtree(temp_dir)


if __name__ == "__main__":
    raise SystemExit(main())
