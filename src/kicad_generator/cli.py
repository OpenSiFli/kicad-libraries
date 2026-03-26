from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Sequence

from .config import GeneratorOptions, GeneratorTargets
from .runner import run

LOGGER = logging.getLogger(__name__)


def _is_workspace_root(path: Path) -> bool:
    """Check whether the given path looks like the repository workspace root.

    Args:
        path: Candidate directory.

    Returns:
        True if the directory contains the expected project root layout.
    """
    return (path / "pyproject.toml").is_file() and (path / "src" / "kicad_generator").is_dir()


def _resolve_workspace_root() -> Path:
    """Locate the workspace root directory.

    The generator is expected to run from a repository checkout that contains
    the upstream repositories as submodules.

    Returns:
        The resolved workspace root directory.

    Raises:
        FileNotFoundError: If the workspace root cannot be located.
    """
    # Prefer the repository layout, but allow running from subdirectories.
    candidates = [Path(__file__).resolve(), Path.cwd().resolve()]
    for start in candidates:
        for parent in (start, *start.parents):
            if _is_workspace_root(parent):
                return parent
    msg = (
        "Could not locate KiCAD-Generator workspace root (expected to contain "
        "pyproject.toml and src/kicad_generator)."
    )
    raise FileNotFoundError(msg)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kicad-generator",
        description=(
            "Generate SiFli KiCad footprints and symbols based on SiliconSchema input data."
        ),
    )
    parser.add_argument(
        "--module-data-dir",
        type=Path,
        help=(
            "Optional directory that contains module definitions (modules/*/module.yml). "
            "If omitted, defaults to ./modules when present."
        ),
    )
    parser.add_argument(
        "--module-footprint-dir",
        type=Path,
        help=(
            "Optional directory that contains manually maintained module footprints "
            "structured as *.pretty/*.kicad_mod. If omitted, defaults to "
            "./module-footprints when present."
        ),
    )
    parser.add_argument(
        "--footprint-data-dir",
        type=Path,
        help=(
            "Optional directory that contains footprint parameter YAML files. "
            "If omitted, defaults to ./SiliconSchema/footprint or ./footprint when present; "
            "otherwise, the upstream kicad-footprint-generator package specs are used."
        ),
    )
    parser.add_argument(
        "--kicad-footprint-root",
        type=Path,
        help=(
            "Path to the local kicad-footprint-generator repository "
            "(defaults to ./kicad-footprint-generator)."
        ),
    )
    parser.add_argument(
        "--kicad-library-utils-root",
        type=Path,
        help=(
            "Path to the local kicad-library-utils repository "
            "(defaults to ./kicad-library-utils)."
        ),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        required=True,
        help="Directory where generated footprints and symbols will be written.",
    )
    target_group = parser.add_mutually_exclusive_group()
    target_group.add_argument(
        "--footprints-only",
        action="store_true",
        help="Only generate footprints.",
    )
    target_group.add_argument(
        "--symbols-only",
        action="store_true",
        help="Only generate symbols (expects footprints to exist already).",
    )
    parser.add_argument(
        "--series",
        action="append",
        default=[],
        metavar="MODEL_ID",
        help="Limit generation to the given series/model identifier. Can be repeated.",
    )
    parser.add_argument(
        "--variant",
        action="append",
        default=[],
        metavar="PART_NUMBER",
        help="Limit generation to the specified part number. Can be repeated.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="Increase log verbosity (can be specified multiple times).",
    )
    return parser


def configure_logging(verbosity: int) -> None:
    level = logging.WARNING
    if verbosity == 1:
        level = logging.INFO
    elif verbosity >= 2:
        level = logging.DEBUG

    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def options_from_args(args: argparse.Namespace) -> GeneratorOptions:
    workspace_root = _resolve_workspace_root()
    schema_dir = (workspace_root / "SiliconSchema").resolve()
    if not schema_dir.exists():
        msg = f"SiliconSchema submodule not found at {schema_dir}."
        raise FileNotFoundError(msg)

    if args.footprint_data_dir:
        footprint_data_dir = args.footprint_data_dir.expanduser().resolve()
    else:
        # Optional local overrides. If absent, we can still generate footprints using
        # the upstream kicad-footprint-generator spec data.
        candidates = [schema_dir / "footprint", workspace_root / "footprint"]
        footprint_data_dir = next((path for path in candidates if path.is_dir()), None)

    if args.module_data_dir:
        module_data_dir = args.module_data_dir.expanduser().resolve()
    else:
        candidate = workspace_root / "modules"
        module_data_dir = candidate.resolve() if candidate.is_dir() else None

    if args.module_footprint_dir:
        module_footprint_dir = args.module_footprint_dir.expanduser().resolve()
    else:
        candidate = workspace_root / "module-footprints"
        module_footprint_dir = candidate.resolve() if candidate.is_dir() else None

    output_dir = args.output_dir.expanduser().resolve()

    targets = GeneratorTargets.from_flags(args.footprints_only, args.symbols_only)
    series_filter = GeneratorOptions.normalize_names(args.series)
    variant_filter = GeneratorOptions.normalize_names(args.variant)

    def resolve_repo(provided: Path | None, fallback_name: str) -> Path:
        if provided:
            return provided.expanduser().resolve()
        return (workspace_root / fallback_name).expanduser().resolve()

    kicad_footprint_root = resolve_repo(args.kicad_footprint_root, "kicad-footprint-generator")
    if targets.footprints and not kicad_footprint_root.exists():
        msg = f"kicad-footprint-generator repository not found at {kicad_footprint_root}."
        raise FileNotFoundError(msg)

    kicad_library_utils_root = resolve_repo(args.kicad_library_utils_root, "kicad-library-utils")
    if targets.symbols and not kicad_library_utils_root.exists():
        msg = f"kicad-library-utils repository not found at {kicad_library_utils_root}."
        raise FileNotFoundError(msg)

    return GeneratorOptions(
        schema_dir=schema_dir,
        footprint_data_dir=footprint_data_dir,
        module_data_dir=module_data_dir,
        module_footprint_dir=module_footprint_dir,
        output_dir=output_dir,
        targets=targets,
        series_filter=series_filter,
        variant_filter=variant_filter,
        kicad_footprint_root=kicad_footprint_root,
        kicad_library_utils_root=kicad_library_utils_root,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    configure_logging(args.verbose)

    options = options_from_args(args)
    return run(options)


if __name__ == "__main__":
    raise SystemExit(main())
