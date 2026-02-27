from __future__ import annotations

import json
import itertools
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from string import ascii_uppercase
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


@dataclass(frozen=True)
class SifliBgaPackage:
    """Parsed representation of a SiFli BGA package identifier.

    SiliconSchema uses stable, machine-readable package identifiers such as:

        SiFli_BGA-175_6.5x6.1mm_Layout16x15_P0.4mm

    Attributes:
        name: The original package identifier string.
        ball_count: Number of populated balls/pads in the package.
        body_size_x: Nominal body size in mm (X).
        body_size_y: Nominal body size in mm (Y).
        layout_x: Number of columns (numeric, 1..layout_x).
        layout_y: Number of rows (alphabetic, A..).
        pitch: Ball pitch in mm.
    """

    name: str
    ball_count: int
    body_size_x: float
    body_size_y: float
    layout_x: int
    layout_y: int
    pitch: float


_SIFLI_BGA_RE = re.compile(
    r"^SiFli_BGA-(?P<balls>\d+)"
    r"_(?P<body_x>\d+(?:\.\d+)?)x(?P<body_y>\d+(?:\.\d+)?)mm"
    r"_Layout(?P<layout_x>\d+)x(?P<layout_y>\d+)"
    r"_P(?P<pitch>\d+(?:\.\d+)?)mm$"
)


def is_sifli_bga_package(package_name: str) -> bool:
    """Return True if the package name matches the SiFli BGA naming convention."""

    return bool(_SIFLI_BGA_RE.match(package_name))


def parse_sifli_bga_package_name(package_name: str) -> SifliBgaPackage:
    """Parse a SiFli BGA package identifier.

    Args:
        package_name: Package identifier from SiliconSchema.

    Returns:
        Parsed `SifliBgaPackage`.

    Raises:
        ValueError: If the package name does not match the expected format.
    """

    match = _SIFLI_BGA_RE.match(package_name)
    if not match:
        msg = f"Unsupported SiFli BGA package name: {package_name}"
        raise ValueError(msg)

    return SifliBgaPackage(
        name=package_name,
        ball_count=int(match.group("balls")),
        body_size_x=float(match.group("body_x")),
        body_size_y=float(match.group("body_y")),
        layout_x=int(match.group("layout_x")),
        layout_y=int(match.group("layout_y")),
        pitch=float(match.group("pitch")),
    )


def bga_row_names(count: int) -> list[str]:
    """Generate BGA row names compatible with the upstream grid_array generator.

    The upstream implementation skips visually confusing letters (I, O, Q, S, X, Z)
    and then continues with multi-letter row names (AA, AB, ...).

    Args:
        count: Number of row names to generate.

    Returns:
        List of row names, length == count.
    """

    if count < 0:
        msg = "Row count must be non-negative."
        raise ValueError(msg)
    if count == 0:
        return []

    alphabet = [ch for ch in ascii_uppercase if ch not in "IOQSXZ"]

    def row_name_generator() -> Iterable[str]:
        for n in itertools.count(1):
            for item in itertools.product(alphabet, repeat=n):
                yield "".join(item)

    return list(itertools.islice(row_name_generator(), count))


def iter_bga_balls(layout_x: int, layout_y: int) -> Iterable[str]:
    """Iterate over all ball coordinates in a rectangular grid."""

    rows = bga_row_names(layout_y)
    for row in rows:
        for col in range(1, layout_x + 1):
            yield f"{row}{col}"


def infer_sifli_bga_present_balls(
    series: Sequence[ChipSeries], package_name: str
) -> frozenset[str]:
    """Infer populated ball coordinates for a given BGA package from series data.

    Args:
        series: Loaded SiliconSchema build artifacts.
        package_name: BGA package identifier.

    Returns:
        A frozenset of ball coordinates (e.g. {"A1", "B2"}).

    Raises:
        ValueError: If multiple variants share the same package but disagree on the ball list.
    """

    pin_sets: list[tuple[str, str, frozenset[str]]] = []
    for item in series:
        for variant in item.variants:
            if variant.package != package_name:
                continue
            pins = frozenset(pin.number.strip().upper() for pin in variant.pins)
            pin_sets.append((item.model_id, variant.part_number, pins))

    if not pin_sets:
        return frozenset()

    base = pin_sets[0][2]
    mismatched = [(model, part, pins) for model, part, pins in pin_sets[1:] if pins != base]
    if mismatched:
        details = ", ".join(
            f"{model}/{part} (pins={len(pins)})" for model, part, pins in mismatched
        )
        msg = (
            f"BGA package {package_name} is used by multiple variants with different ball lists. "
            f"First variant pins={len(base)}; mismatches: {details}"
        )
        raise ValueError(msg)

    return base


_BALL_RE = re.compile(r"^(?P<row>[A-Z]+)(?P<col>\d+)$")


def infer_sifli_bga_pad_skips(package: SifliBgaPackage, present_balls: Iterable[str]) -> list[str]:
    """Compute `pad_skips` for the upstream grid_array generator from present balls.

    Args:
        package: Parsed package metadata.
        present_balls: Iterable of ball coordinates present in the chip definition.

    Returns:
        Sorted list of ball coordinates that should be skipped (missing balls).

    Raises:
        ValueError: If the present balls do not match the package constraints.
    """

    present = {ball.strip().upper() for ball in present_balls if ball and ball.strip()}
    if not present:
        msg = f"No BGA balls found for package {package.name}."
        raise ValueError(msg)

    if len(present) != package.ball_count:
        msg = (
            f"Package {package.name} expects {package.ball_count} balls, "
            f"but SiliconSchema provides {len(present)}."
        )
        raise ValueError(msg)

    if package.layout_x <= 0 or package.layout_y <= 0:
        msg = f"Invalid grid layout in package name: {package.name}"
        raise ValueError(msg)

    all_balls = set(iter_bga_balls(package.layout_x, package.layout_y))

    invalid = []
    row_allow = set(bga_row_names(package.layout_y))
    for ball in sorted(present):
        match = _BALL_RE.match(ball)
        if not match:
            invalid.append(ball)
            continue
        row = match.group("row")
        col = int(match.group("col"))
        if row not in row_allow or not (1 <= col <= package.layout_x):
            invalid.append(ball)
            continue
        if ball not in all_balls:
            invalid.append(ball)

    if invalid:
        msg = f"Package {package.name} contains balls outside the grid: {', '.join(invalid)}"
        raise ValueError(msg)

    skips = sorted(all_balls - present)
    expected_skips = package.layout_x * package.layout_y - package.ball_count
    if len(skips) != expected_skips:
        msg = (
            f"Computed pad_skips length mismatch for {package.name}: "
            f"expected {expected_skips}, got {len(skips)}."
        )
        raise ValueError(msg)

    return skips


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


class GridArrayGeneratorAdapter:
    """Wrapper around the upstream kicad-footprint-generator grid array generator."""

    def __init__(
        self,
        repo_root: Path,
        *,
        pad_diameter: float = 0.225,
        mask_margin: float = 0.05,
        paste_margin: float = 0.000001,
    ) -> None:
        self.repo_root = repo_root
        self.pad_diameter = pad_diameter
        self.mask_margin = mask_margin
        self.paste_margin = paste_margin
        ensure_footprint_repo_on_sys_path(repo_root)
        self._output_dir: Path | None = None
        self._create_footprints = None
        self._GridArraySpec = None

    def _ensure_upstream_loaded(self, output_dir_footprints: Path) -> None:
        """Initialise the upstream generator environment.

        Args:
            output_dir_footprints: Root directory where `.kicad_mod` files will be written.
        """
        if self._create_footprints is not None and self._GridArraySpec is not None:
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
        from generators.package.grid_array.footprint import create_footprints  # type: ignore
        from generators.package.grid_array.spec import GridArraySpec  # type: ignore

        self._create_footprints = create_footprints
        self._GridArraySpec = GridArraySpec

    def generate(
        self,
        output_dir: Path,
        package: SifliBgaPackage,
        pad_skips: Sequence[str],
        definition: FootprintPackageDefinition | None = None,
    ) -> list[Path]:
        """Generate a BGA footprint and return the written `.kicad_mod` path.

        Args:
            output_dir: Root directory where `.kicad_mod` files will be written.
            package: Parsed SiFli BGA package metadata.
            pad_skips: Ball coordinates that should be skipped (missing balls).
            definition: Optional local override definition that can tweak generator parameters.

        Returns:
            A list containing the generated footprint path, or an empty list on failure.
        """
        self._ensure_upstream_loaded(output_dir)
        assert self._GridArraySpec is not None
        assert self._create_footprints is not None

        spec: dict[str, Any] = {
            "name_equal_to_key": True,
            "package_type": "BGA",
            "body_size_x": package.body_size_x,
            "body_size_y": package.body_size_y,
            "layout_x": package.layout_x,
            "layout_y": package.layout_y,
            "pitch": package.pitch,
            "pad_diameter": self.pad_diameter,
            "mask_margin": self.mask_margin,
            "paste_margin": self.paste_margin,
            "pad_skips": list(pad_skips),
        }

        file_name = "<generated>"
        if definition is not None:
            spec.update(dict(definition.parameters))
            file_name = str(definition.source_file)

        # Always keep the footprint key/name stable and ensure skips match the schema.
        spec["name_equal_to_key"] = True
        spec["pad_skips"] = list(pad_skips)

        spec_obj = self._GridArraySpec(package.name, spec, file_name)
        generated_count = int(self._create_footprints(spec_obj, "package/grid_array"))
        if generated_count <= 0:
            return []

        library_dir = output_dir / f"{spec_obj.lib_name}.pretty"
        path = library_dir / f"{spec_obj.name}.kicad_mod"
        return [path] if path.is_file() else []


class FootprintGenerator:
    """Produces KiCad footprints via the upstream generators."""

    def __init__(self, output_dir: Path, namespace: str, footprint_repo: Path) -> None:
        self.output_dir = output_dir
        self.namespace = namespace
        self.no_lead_adapter = NoLeadGeneratorAdapter(footprint_repo)
        self.grid_array_adapter = GridArrayGeneratorAdapter(footprint_repo)

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

        bga_present: dict[str, frozenset[str]] = {}
        for package_name in required_packages:
            if not is_sifli_bga_package(package_name):
                continue
            bga_present[package_name] = infer_sifli_bga_present_balls(series, package_name)

        for package_name in required_packages:
            generated_paths: list[Path] = []
            definition = library.get(package_name)
            if is_sifli_bga_package(package_name):
                package = parse_sifli_bga_package_name(package_name)
                present = bga_present.get(package_name) or frozenset()
                pad_skips = infer_sifli_bga_pad_skips(package, present)
                generated_paths = self.grid_array_adapter.generate(
                    output_dir=footprints_root,
                    package=package,
                    pad_skips=pad_skips,
                    definition=definition,
                )
            else:
                generated_paths = self.no_lead_adapter.generate(
                    output_dir=footprints_root,
                    package_name=package_name,
                    definition=definition,
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
