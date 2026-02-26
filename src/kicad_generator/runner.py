from __future__ import annotations

import logging
from dataclasses import replace
from typing import Sequence

from .config import GeneratorOptions
from .footprint_loader import FootprintLibrary
from .footprints import FootprintGenerationResult, FootprintGenerator, load_footprint_manifest
from .schema_loader import ChipSeries, SiliconSchemaRepository
from .symbols import SymbolGenerator

LOGGER = logging.getLogger(__name__)


def apply_variant_filter(series: Sequence[ChipSeries], allowed: Sequence[str]) -> list[ChipSeries]:
    if not allowed:
        return list(series)

    allowed_set = {item.upper() for item in allowed}
    filtered: list[ChipSeries] = []
    for item in series:
        variants = tuple(v for v in item.variants if v.part_number.upper() in allowed_set)
        if not variants:
            continue
        filtered.append(replace(item, variants=variants))

    return filtered


def run(options: GeneratorOptions) -> int:
    try:
        repo = SiliconSchemaRepository(options.schema_dir)
        series = repo.load_series(options.series_filter or None)
    except FileNotFoundError as exc:
        LOGGER.error("%s", exc)
        return 2
    series = apply_variant_filter(series, options.variant_filter)

    if not series:
        LOGGER.warning("No series matched the provided filters.")
        return 1

    if options.targets.footprints:
        footprint_library = FootprintLibrary.from_directory(options.footprint_data_dir)
        repo_root = options.kicad_footprint_root
        if not repo_root or not repo_root.exists():
            LOGGER.error(
                "kicad-footprint-generator repository not found at %s",
                repo_root,
            )
            return 2

        footprint_generator = FootprintGenerator(
            output_dir=options.output_dir,
            namespace=options.footprint_namespace,
            footprint_repo=repo_root,
        )

        footprint_result = footprint_generator.generate(
            series=series,
            library=footprint_library,
        )
    else:
        footprint_result = load_footprint_manifest(
            output_dir=options.output_dir,
            namespace=options.footprint_namespace,
        )

    if options.targets.symbols:
        repo_root = options.kicad_library_utils_root
        if not repo_root or not repo_root.exists():
            LOGGER.error(
                "kicad-library-utils repository not found at %s",
                repo_root,
            )
            return 2

        symbol_generator = SymbolGenerator(
            output_dir=options.output_dir,
            footprint_namespace=options.footprint_namespace,
            library_utils_root=repo_root,
        )
        symbol_generator.generate(series=series, footprints=footprint_result)

    LOGGER.info("Generation complete.")
    return 0
