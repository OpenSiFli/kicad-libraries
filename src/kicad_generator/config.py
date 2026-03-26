from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Tuple


@dataclass(frozen=True)
class GeneratorTargets:
    """Flags that describe which artifacts should be emitted."""

    footprints: bool
    symbols: bool

    @classmethod
    def from_flags(cls, footprints_only: bool, symbols_only: bool) -> "GeneratorTargets":
        if footprints_only and symbols_only:
            msg = "Cannot request --footprints-only and --symbols-only simultaneously."
            raise ValueError(msg)

        if footprints_only:
            return cls(footprints=True, symbols=False)

        if symbols_only:
            return cls(footprints=False, symbols=True)

        return cls(footprints=True, symbols=True)


@dataclass(frozen=True)
class GeneratorOptions:
    """Runtime configuration derived from the CLI."""

    schema_dir: Path
    footprint_data_dir: Path | None
    module_data_dir: Path | None
    module_footprint_dir: Path | None
    output_dir: Path
    targets: GeneratorTargets
    series_filter: Tuple[str, ...] = ()
    variant_filter: Tuple[str, ...] = ()
    footprint_namespace: str = "SiFli_MOD"
    kicad_footprint_root: Path | None = None
    kicad_library_utils_root: Path | None = None

    def filtered_series(self) -> Tuple[str, ...]:
        return self.series_filter

    def filtered_variants(self) -> Tuple[str, ...]:
        return self.variant_filter

    @classmethod
    def normalize_names(cls, values: Iterable[str]) -> Tuple[str, ...]:
        return tuple(dict.fromkeys(v.strip() for v in values if v and v.strip()))
