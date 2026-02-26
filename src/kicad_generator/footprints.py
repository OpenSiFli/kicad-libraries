from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

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
    """Wrapper around the upstream kicad-footprint-generator no-lead generator."""

    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root
        ensure_footprint_repo_on_sys_path(repo_root)
        self._spec_index: dict[str, tuple[Mapping[str, Any], str]] | None = None
        self._output_dir: Path | None = None
        self._create_footprints = None
        self._NoLeadSpec = None
        self._spec_generator = None

    def _ensure_upstream_loaded(self, output_dir_footprints: Path) -> None:
        """Initialise the upstream generator environment.

        The upstream package generator relies on a global argparse Namespace
        (CLI_ARGS) for configuration. We set the minimal fields required for
        footprint generation and then import the generator modules.

        Args:
            output_dir_footprints: Root directory where `.kicad_mod` files will be written.
        """
        if self._create_footprints is not None and self._NoLeadSpec is not None:
            # Still update the output directory if it changes between calls.
            if self._output_dir != output_dir_footprints:
                from argparse import Namespace

                from generators.tools import cli_args  # type: ignore

                cli_args.init(
                    Namespace(
                        dry_run=False,
                        separate_outputs=False,
                        output_dir_footprints=output_dir_footprints,
                        package_config=str(
                            self.repo_root
                            / "src"
                            / "generators"
                            / "package"
                            / "package_config_KLCv3.yaml"
                        ),
                    )
                )
                self._output_dir = output_dir_footprints
            return

        from argparse import Namespace

        from generators.tools import cli_args  # type: ignore

        cli_args.init(
            Namespace(
                dry_run=False,
                separate_outputs=False,
                output_dir_footprints=output_dir_footprints,
                package_config=str(
                    self.repo_root
                    / "src"
                    / "generators"
                    / "package"
                    / "package_config_KLCv3.yaml"
                ),
            )
        )
        self._output_dir = output_dir_footprints

        # Imports below depend on CLI_ARGS.package_config being set.
        from generators.package.no_lead.footprint import create_footprints  # type: ignore
        from generators.package.no_lead.spec import NoLeadSpec  # type: ignore
        from generators.tools.spec import spec_generator  # type: ignore

        self._create_footprints = create_footprints
        self._NoLeadSpec = NoLeadSpec
        self._spec_generator = spec_generator

    def _load_builtin_specs(self) -> Mapping[str, tuple[Mapping[str, Any], str]]:
        if self._spec_index is not None:
            return self._spec_index
        if self._spec_generator is None:
            msg = "Upstream spec generator is not available yet."
            raise RuntimeError(msg)

        specs: dict[str, tuple[Mapping[str, Any], str]] = {}
        for file_name, entries in self._spec_generator.get_spec_dicts("package/no_lead"):
            for spec_id, spec in entries.items():
                specs[spec_id] = (spec, file_name)
        self._spec_index = specs
        return specs

    def generate(
        self,
        output_dir: Path,
        package_name: str,
        definition: FootprintPackageDefinition | None = None,
    ) -> list[Path]:
        self._ensure_upstream_loaded(output_dir)
        assert self._NoLeadSpec is not None
        assert self._create_footprints is not None

        if definition is not None:
            spec: dict[str, Any] = dict(definition.parameters)
            header = definition.metadata.get("file_header") or {}
            if isinstance(header, Mapping):
                library = header.get("library")
                if library:
                    spec.setdefault("library", str(library))
            file_name = str(definition.source_file)
        else:
            specs = self._load_builtin_specs()
            match = specs.get(package_name)
            if match is None:
                return []
            spec, file_name = match

        spec_obj = self._NoLeadSpec(package_name, spec, file_name)
        generated_count = int(self._create_footprints(spec_obj, "package/no_lead"))
        if generated_count <= 0:
            return []

        library_dir_name = str(spec_obj.lib_name)
        if not library_dir_name.endswith(".pretty"):
            library_dir_name = f"{library_dir_name}.pretty"
        library_dir = output_dir / library_dir_name

        expected_names = [str(spec_obj.fp_name_without_vias)]
        if spec_obj.has_ep and "thermal_vias" in spec_obj.spec:
            expected_names.append(str(spec_obj.fp_name_with_vias))

        paths = [library_dir / f"{name}.kicad_mod" for name in expected_names]
        return [path for path in paths if path.is_file()]


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
            generated_paths = self.adapter.generate(
                output_dir=footprints_root,
                package_name=package_name,
                definition=library.get(package_name),
            )

            if not generated_paths:
                LOGGER.error(
                    "No footprint spec found for %s (not in local overrides, not in upstream kicad-footprint-generator).",
                    package_name,
                )
                missing.append(package_name)
                continue

            for file_path in generated_paths:
                library_dir = file_path.parent.name
                library_name = (
                    library_dir[: -len(".pretty")]
                    if library_dir.endswith(".pretty")
                    else library_dir
                )
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
