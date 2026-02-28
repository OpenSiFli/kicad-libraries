from __future__ import annotations

import logging
from dataclasses import replace
from typing import Sequence

from .config import GeneratorOptions
from .footprint_loader import FootprintLibrary
from .footprints import FootprintGenerationResult, FootprintGenerator, load_footprint_manifest
from .module_loader import ModuleLibrary
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
    LOGGER.info("Starting generation with targets: footprints=%s symbols=%s", options.targets.footprints, options.targets.symbols)
    LOGGER.debug("Generation options: output_dir=%s schema_dir=%s", options.output_dir, options.schema_dir)
    try:
        repo = SiliconSchemaRepository(options.schema_dir)
    except FileNotFoundError as exc:
        LOGGER.error("%s", exc)
        return 2

    chips: list[ChipSeries] = []
    if options.series_filter:
        chip_ids = {model_id for model_id, _ in repo.iter_series_paths()}
        chips_allowed = [item for item in options.series_filter if item in chip_ids]
        if chips_allowed:
            try:
                chips = repo.load_series(chips_allowed)
            except FileNotFoundError as exc:
                LOGGER.error("%s", exc)
                return 2
    else:
        try:
            chips = repo.load_series(None)
        except FileNotFoundError as exc:
            LOGGER.error("%s", exc)
            return 2

    module_library: ModuleLibrary | None = None
    module_series: list[ChipSeries] = []
    if options.module_data_dir is not None:
        try:
            module_library = ModuleLibrary.from_directory(options.module_data_dir)
        except FileNotFoundError as exc:
            LOGGER.error("%s", exc)
            return 2
        module_series = module_library.to_chip_series(
            repo,
            schema_cache={entry.model_id: entry for entry in chips},
            allowed_modules=options.series_filter or None,
        )

    series = apply_variant_filter([*chips, *module_series], options.variant_filter)
    LOGGER.info("Loaded %d series after filters (schema=%d, modules=%d).", len(series), len(chips), len(module_series))

    if not series:
        LOGGER.warning("No series matched the provided filters.")
        return 1

    if options.targets.footprints:
        LOGGER.info("Generating footprints...")
        try:
            footprint_library = (
                FootprintLibrary.from_directory(options.footprint_data_dir)
                if options.footprint_data_dir is not None
                else FootprintLibrary({})
            )
        except FileNotFoundError as exc:
            LOGGER.error("%s", exc)
            return 2

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
            module_library=module_library,
        )
        LOGGER.info(
            "Footprints generated: %d artifacts, %d missing definitions.",
            len(footprint_result.artifacts),
            len(footprint_result.missing),
        )
    else:
        footprint_result = load_footprint_manifest(
            output_dir=options.output_dir,
            namespace=options.footprint_namespace,
        )

    if options.targets.symbols:
        LOGGER.info("Generating symbols...")
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
        LOGGER.info("Symbols generated for %d series.", len(series))

    LOGGER.info("Generation complete.")
    return 0
