from __future__ import annotations

from dataclasses import dataclass
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping

import yaml


@dataclass(frozen=True)
class FootprintPackageDefinition:
    name: str
    family: str
    description: str | None
    source_file: Path
    parameters: Mapping[str, Any]
    metadata: Mapping[str, Any]


class FootprintLibrary:
    """Loads and provides lookup helpers for footprint parameter files."""

    def __init__(self, packages: Mapping[str, FootprintPackageDefinition]) -> None:
        self._packages = dict(packages)

    @classmethod
    def from_directory(cls, directory: Path) -> "FootprintLibrary":
        if not directory.is_dir():
            msg = f"Footprint directory {directory} does not exist."
            raise FileNotFoundError(msg)

        packages: dict[str, FootprintPackageDefinition] = {}
        for file_path in sorted(directory.glob("*.yml")):
            packages.update(cls._load_file(file_path))
        return cls(packages)

    @classmethod
    def _load_file(cls, path: Path) -> Mapping[str, FootprintPackageDefinition]:
        with path.open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle)

        family = raw.get("family", path.stem)
        defaults = raw.get("defaults", {}) or {}
        generator_reference = raw.get("generator_reference")
        file_header = raw.get("file_header")
        shared_metadata = {
            "schema_version": raw.get("schema_version"),
            "units": raw.get("units"),
            "generator_reference": generator_reference,
            "defaults": defaults,
            "file_header": file_header,
        }

        packages: dict[str, FootprintPackageDefinition] = {}
        for package in raw.get("packages", []):
            entry = deepcopy(package)
            name = entry.pop("name", None)
            if not name:
                msg = f"Footprint definition in {path} is missing a 'name' field."
                raise ValueError(msg)

            description = entry.get("description")
            parameters: dict[str, Any] = deepcopy(defaults)
            parameters.update(entry)

            metadata = {
                "family": family,
                **shared_metadata,
                **parameters,
            }
            packages[name] = FootprintPackageDefinition(
                name=name,
                family=family,
                description=description,
                source_file=path,
                parameters=parameters,
                metadata=metadata,
            )

        return packages

    def get(self, name: str) -> FootprintPackageDefinition | None:
        return self._packages.get(name)

    def required_packages(self, names: Iterable[str]) -> Mapping[str, FootprintPackageDefinition]:
        found: dict[str, FootprintPackageDefinition] = {}
        for name in names:
            package = self.get(name)
            if package:
                found[name] = package
        return found
