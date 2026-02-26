from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Sequence

from .config import GeneratorOptions, GeneratorTargets
from .runner import run


def _is_workspace_root(path: Path) -> bool:
    return (
        (path / "pyproject.toml").is_file()
        and (path / "src" / "kicad_generator").is_dir()
        and (path / "SiliconSchema").exists()
        and (path / "kicad-footprint-generator").exists()
        and (path / "kicad-library-utils").exists()
    )


def _resolve_workspace_root() -> Path:
    # Prefer the repository layout, but allow running from subdirectories.
    candidates = [Path(__file__).resolve(), Path.cwd().resolve()]
    for start in candidates:
        for parent in (start, *start.parents):
            if _is_workspace_root(parent):
                return parent
    msg = (
        "Could not locate KiCAD-Generator workspace root (expected to contain "
        "SiliconSchema/kicad-footprint-generator/kicad-library-utils submodules)."
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
        "--footprint-data-dir",
        type=Path,
        help=(
            "Directory that contains footprint parameter YAML files "
            "(defaults to ./SiliconSchema/footprint, falling back to ./footprint)."
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
        format="%(levelname)s: %(message)s",
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
        # SiliconSchema might optionally ship generator-specific footprint parameters;
        # otherwise keep them in this repository.
        candidate = schema_dir / "footprint"
        footprint_data_dir = candidate if candidate.is_dir() else workspace_root / "footprint"

    output_dir = args.output_dir.expanduser().resolve()

    targets = GeneratorTargets.from_flags(args.footprints_only, args.symbols_only)
    series_filter = GeneratorOptions.normalize_names(args.series)
    variant_filter = GeneratorOptions.normalize_names(args.variant)

    def resolve_repo(provided: Path | None, fallback_name: str) -> Path:
        if provided:
            return provided.expanduser().resolve()
        return (workspace_root / fallback_name).expanduser().resolve()

    kicad_footprint_root = resolve_repo(args.kicad_footprint_root, "kicad-footprint-generator")
    kicad_library_utils_root = resolve_repo(args.kicad_library_utils_root, "kicad-library-utils")

    return GeneratorOptions(
        schema_dir=schema_dir,
        footprint_data_dir=footprint_data_dir,
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
