from __future__ import annotations

import argparse
import logging
from pathlib import Path
from typing import Sequence

from .config import GeneratorOptions, GeneratorTargets
from .runner import run


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kicad-generator",
        description=(
            "Generate SiFli KiCad footprints and symbols based on SiliconSchema input data."
        ),
    )
    parser.add_argument(
        "--schema-dir",
        type=Path,
        required=True,
        help=(
            "Path to the SiliconSchema checkout containing out/<chip>/series.yaml build artifacts "
            "(generated via 'uv run build-schema')."
        ),
    )
    parser.add_argument(
        "--footprint-data-dir",
        type=Path,
        help="Directory that contains footprint parameter YAML files (defaults to <schema-dir>/footprint).",
    )
    parser.add_argument(
        "--kicad-footprint-root",
        type=Path,
        help="Path to the local kicad-footprint-generator repository (defaults to sibling of --schema-dir).",
    )
    parser.add_argument(
        "--kicad-library-utils-root",
        type=Path,
        help="Path to the local kicad-library-utils repository (defaults to sibling of --schema-dir).",
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
    schema_dir = args.schema_dir.expanduser().resolve()
    footprint_data_dir = (
        args.footprint_data_dir.expanduser().resolve()
        if args.footprint_data_dir
        else schema_dir / "footprint"
    )
    output_dir = args.output_dir.expanduser().resolve()
    workspace_root = schema_dir.parent

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
