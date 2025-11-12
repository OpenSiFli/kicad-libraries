from __future__ import annotations

import copy
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

import yaml

from .footprint_loader import FootprintLibrary, FootprintPackageDefinition
from .schema_loader import ChipSeries
from .upstream import ensure_footprint_repo_on_sys_path

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class FootprintArtifact:
    name: str
    namespace: str
    library: str
    path: Path
    package: str

    @property
    def qualified_name(self) -> str:
        return f"{self.namespace}:{self.name}"


@dataclass(frozen=True)
class FootprintGenerationResult:
    namespace: str
    artifacts: Mapping[str, FootprintArtifact]
    missing: Sequence[str]
    manifest_path: Path | None
    package_map: Mapping[str, str]

    def footprint_for_package(self, package: str) -> FootprintArtifact | None:
        name = self.package_map.get(package)
        if not name:
            return None
        return self.artifacts.get(name)


class NoLeadGeneratorAdapter:
    """Thin wrapper around the upstream kicad-footprint-generator no-lead generator."""

    def __init__(self, repo_root: Path, density: str = "N") -> None:
        ensure_footprint_repo_on_sys_path(repo_root)
        from scripts.Packages.no_lead import ipc_noLead_generator  # type: ignore
        from scripts.tools.global_config_files import global_config as GC  # type: ignore
        from kilibs.ipc_tools import ipc_rules  # type: ignore

        self.repo_root = repo_root
        self.generator_cls = ipc_noLead_generator.NoLeadGenerator
        self.global_config = GC.DefaultGlobalConfig()
        config_path = repo_root / "scripts/Packages/package_config_KLCv3.yaml"
        with config_path.open("r", encoding="utf-8") as handle:
            self.configuration = yaml.safe_load(handle)
        self.ipc_defs = ipc_rules.IpcRules.from_file("ipc_7351b")
        self.density = density

    def generate(
        self,
        output_dir: Path,
        package_name: str,
        parameters: Mapping[str, object],
        header_info: Mapping[str, object] | None,
    ) -> list[Path]:
        header = dict(header_info or {})
        library_name = str(header.get("library") or "Generated")

        generator_cls = self.generator_cls

        class TrackingGenerator(generator_cls):  # type: ignore
            def __init__(self, *args, **kwargs):
                self._generated: list[Path] = []
                super().__init__(*args, **kwargs)

            def write_footprint(self, kicad_mod, library_name: str):  # type: ignore
                super().write_footprint(kicad_mod, library_name)
                base = self.output_path or output_dir
                library_dir = Path(base) / f"{library_name}.pretty"
                self._generated.append(library_dir / f"{kicad_mod.name}.kicad_mod")

        generator = TrackingGenerator(
            output_dir=output_dir,
            global_config=self.global_config,
            configuration=copy.deepcopy(self.configuration),
            ipc_defs=self.ipc_defs,
        )

        header = dict(header_info or {})
        generator.generateFootprint(
            dict(parameters),
            pkg_id=package_name,
            header_info=header,
        )

        generated = getattr(generator, "_generated", [])
        return sorted(Path(path) for path in generated)


class FootprintGenerator:
    """Produces KiCad footprints via the upstream generators."""

    def __init__(self, output_dir: Path, namespace: str, footprint_repo: Path) -> None:
        self.output_dir = output_dir
        self.namespace = namespace
        self.adapter = NoLeadGeneratorAdapter(footprint_repo)

    def _planned_packages(self, series: Sequence[ChipSeries]) -> list[str]:
        packages = {variant.package for item in series for variant in item.variants}
        return sorted(packages)

    def generate(
        self,
        series: Sequence[ChipSeries],
        library: FootprintLibrary,
    ) -> FootprintGenerationResult:
        required_packages = self._planned_packages(series)
        footprints_root = self.output_dir / "footprints"
        footprints_root.mkdir(parents=True, exist_ok=True)

        manifest_entries: list[dict[str, object]] = []
        artifacts: dict[str, FootprintArtifact] = {}
        package_map: dict[str, str] = {}
        missing: list[str] = []

        for package_name in required_packages:
            definition = library.get(package_name)
            if not definition:
                LOGGER.error("Package %s missing from footprint library.", package_name)
                missing.append(package_name)
                continue

            header = definition.metadata.get("file_header") or {}
            generated_paths = self.adapter.generate(
                output_dir=footprints_root,
                package_name=package_name,
                parameters=definition.parameters,
                header_info=header,
            )

            if not generated_paths:
                LOGGER.error("Footprint generator produced no files for %s", package_name)
                missing.append(package_name)
                continue

            library_name = str(header.get("library") or "Generated")
            for file_path in generated_paths:
                artifact = FootprintArtifact(
                    name=file_path.stem,
                    namespace=self.namespace,
                    library=library_name,
                    path=file_path,
                    package=package_name,
                )
                artifacts[artifact.name] = artifact
                manifest_entries.append(
                    {
                        "name": artifact.name,
                        "package": package_name,
                        "library": library_name,
                        "qualified_name": artifact.qualified_name,
                        "path": str(artifact.path.relative_to(self.output_dir)),
                    }
                )

            canonical = next((p.stem for p in generated_paths if p.stem == package_name), None)
            if canonical:
                package_map[package_name] = canonical
            else:
                LOGGER.warning(
                    "No canonical footprint named %s was generated; defaulting to %s.",
                    package_name,
                    generated_paths[0].stem,
                )
                package_map[package_name] = generated_paths[0].stem

        manifest_path = footprints_root / "manifest.json"
        manifest_payload = {
            "namespace": self.namespace,
            "packages": manifest_entries,
        }
        manifest_path.write_text(json.dumps(manifest_payload, indent=2), encoding="utf-8")

        if missing:
            LOGGER.warning("Missing %d footprint definitions.", len(missing))

        return FootprintGenerationResult(
            namespace=self.namespace,
            artifacts=artifacts,
            missing=missing,
            manifest_path=manifest_path,
            package_map=package_map,
        )


def load_footprint_manifest(output_dir: Path, namespace: str) -> FootprintGenerationResult:
    manifest_path = output_dir / "footprints" / "manifest.json"
    if not manifest_path.is_file():
        LOGGER.warning("Footprint manifest not found at %s", manifest_path)
        return FootprintGenerationResult(
            namespace=namespace,
            artifacts={},
            missing=[],
            manifest_path=None,
            package_map={},
        )

    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    artifacts: dict[str, FootprintArtifact] = {}
    package_map: dict[str, str] = {}
    for entry in data.get("packages", []):
        path = output_dir / entry["path"]
        artifact = FootprintArtifact(
            name=entry["name"],
            namespace=namespace,
            library=entry.get("library", ""),
            path=path,
            package=entry.get("package", ""),
        )
        artifacts[artifact.name] = artifact
        package = entry.get("package")
        if package and package not in package_map:
            if entry.get("name") == package:
                package_map[package] = entry["name"]
            elif package not in package_map:
                package_map[package] = entry["name"]

    return FootprintGenerationResult(
        namespace=namespace,
        artifacts=artifacts,
        missing=[],
        manifest_path=manifest_path,
        package_map=package_map,
    )
