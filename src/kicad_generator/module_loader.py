from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import yaml

from .schema_loader import (
    ChipPad,
    ChipSeries,
    ChipVariant,
    ChipVariantPin,
    PinmuxEntry,
    SiliconSchemaRepository,
)

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModuleInclude:
    """Represents an include source for module pins.

    Attributes:
        alias: The include alias used by pins.yml (e.g. "soc").
        kind: Include kind identifier (currently only "silicon_schema").
        series: SiliconSchema series/model identifier to load.
    """

    alias: str
    kind: str
    series: str


@dataclass(frozen=True)
class IncludedPadRef:
    """Reference to a pad defined by an include source."""

    include: str
    name: str


@dataclass(frozen=True)
class ModulePadSpec:
    """Local pad specification for non-SoC module pins."""

    name: str
    type: str
    description: str | None
    notes: str | None
    pinmux: tuple[PinmuxEntry, ...]

    def to_chip_pad(self) -> ChipPad:
        return ChipPad(
            name=self.name,
            type=self.type,
            description=self.description,
            notes=self.notes,
            pinmux=self.pinmux,
        )


@dataclass(frozen=True)
class ModulePin:
    """A module pin definition loaded from pins.yml."""

    number: str
    pad: str | IncludedPadRef
    name: str | None = None
    notes: str | None = None


@dataclass(frozen=True)
class ModuleVariantDefinition:
    """A module variant definition."""

    part_number: str
    package: str
    pins_file: Path
    description: str | None = None


@dataclass(frozen=True)
class ModuleDefinition:
    """Loaded module definition (module.yml + pins.yml + footprint.yml path).

    This structure is the source of truth for generating:

    - module footprints (package/layout driven)
    - module symbols (pins driven, with optional pinmux inherited from includes)
    """

    module_id: str
    schema_version: str
    docs: Sequence[Mapping[str, Any]]
    includes: Mapping[str, ModuleInclude]
    variants: Sequence[ModuleVariantDefinition]
    pads: Mapping[str, ModulePadSpec]
    pins_by_variant: Mapping[str, Sequence[ModulePin]]
    footprint_file: Path
    root_dir: Path
    source_path: Path

    def variant_for_package(self, package: str) -> ModuleVariantDefinition | None:
        for variant in self.variants:
            if variant.package == package:
                return variant
        return None


class ModuleLibrary:
    """Loads and resolves module definitions from a directory tree."""

    def __init__(self, modules: Mapping[str, ModuleDefinition]) -> None:
        self._modules = dict(modules)
        self._package_index: dict[str, tuple[ModuleDefinition, ModuleVariantDefinition]] = {}
        for module in self._modules.values():
            for variant in module.variants:
                self._package_index[variant.package] = (module, variant)

    @classmethod
    def from_directory(cls, directory: Path) -> "ModuleLibrary":
        """Load all module definitions under the given directory.

        Expected layout:
            modules/<module_id>/module.yml
            modules/<module_id>/pins.yml
            modules/<module_id>/footprint.yml

        Args:
            directory: Root directory that contains per-module subdirectories.

        Returns:
            ModuleLibrary containing all parsed module definitions.

        Raises:
            FileNotFoundError: If directory does not exist.
            ValueError: If module YAML files are malformed.
        """

        if not directory.is_dir():
            msg = f"Module directory {directory} does not exist."
            raise FileNotFoundError(msg)

        modules: dict[str, ModuleDefinition] = {}
        for child in sorted(directory.iterdir()):
            if not child.is_dir():
                continue
            module_file = child / "module.yml"
            if not module_file.is_file():
                continue
            definition = cls._load_module_dir(child, module_file)
            modules[definition.module_id] = definition
        return cls(modules)

    def module_for_id(self, module_id: str) -> ModuleDefinition | None:
        return self._modules.get(module_id)

    def package_entry(
        self, package: str
    ) -> tuple[ModuleDefinition, ModuleVariantDefinition] | None:
        return self._package_index.get(package)

    def is_module_package(self, package: str) -> bool:
        return package in self._package_index

    def modules(self) -> Sequence[ModuleDefinition]:
        return tuple(self._modules.values())

    def to_chip_series(
        self,
        schema_repo: SiliconSchemaRepository,
        *,
        schema_cache: Mapping[str, ChipSeries] | None = None,
        allowed_modules: Sequence[str] | None = None,
    ) -> list[ChipSeries]:
        """Convert loaded modules into ChipSeries for symbol generation.

        Args:
            schema_repo: SiliconSchema repository loader used to resolve includes.
            schema_cache: Optional mapping of already loaded SiliconSchema series keyed by model_id.
            allowed_modules: Optional list of module_ids to include.

        Returns:
            List of ChipSeries, one per module definition.

        Raises:
            FileNotFoundError: If an included SiliconSchema series cannot be loaded.
            ValueError: If pins reference unknown includes or unknown pads.
        """

        allowed_set = {item for item in (allowed_modules or []) if item}
        include_cache: dict[str, ChipSeries] = dict(schema_cache or {})
        out: list[ChipSeries] = []

        for module in self._modules.values():
            if allowed_set and module.module_id not in allowed_set:
                continue

            resolved_includes: dict[str, ChipSeries] = {}
            for alias, include in module.includes.items():
                if include.kind != "silicon_schema":
                    raise ValueError(
                        f"Unsupported include kind {include.kind!r} in {module.source_path}"
                    )
                series = include_cache.get(include.series)
                if series is None:
                    series = schema_repo.load_series_by_id(include.series)
                    include_cache[include.series] = series
                resolved_includes[alias] = series

            pads: dict[str, ChipPad] = {}
            local_pad_specs = dict(module.pads)

            def ensure_local_pad(name: str) -> ChipPad:
                spec = local_pad_specs.get(name)
                if spec is None:
                    LOGGER.warning(
                        "Module %s references local pad %s without a pads entry; defaulting to passive.",
                        module.module_id,
                        name,
                    )
                    spec = ModulePadSpec(
                        name=name,
                        type="passive",
                        description=None,
                        notes=None,
                        pinmux=(),
                    )
                return spec.to_chip_pad()

            for variant in module.variants:
                pins = module.pins_by_variant.get(variant.part_number) or ()
                for pin in pins:
                    if isinstance(pin.pad, IncludedPadRef):
                        series = resolved_includes.get(pin.pad.include)
                        if series is None:
                            raise ValueError(
                                f"Module {module.module_id} pin {pin.number} references unknown include "
                                f"{pin.pad.include!r} (in {variant.pins_file})."
                            )
                        pad_name = pin.pad.name
                        referenced = series.pads.get(pad_name)
                        if referenced is None:
                            raise ValueError(
                                f"Module {module.module_id} pin {pin.number} references unknown pad {pad_name!r} "
                                f"in include {pin.pad.include!r} series {series.model_id}."
                            )
                        if pad_name in local_pad_specs:
                            raise ValueError(
                                f"Module {module.module_id} defines local pad {pad_name!r} that conflicts with an "
                                "included pad of the same name."
                            )
                        pads.setdefault(pad_name, referenced)
                    else:
                        pad_name = pin.pad
                        pads.setdefault(pad_name, ensure_local_pad(pad_name))

            variants: list[ChipVariant] = []
            for variant in module.variants:
                pins = module.pins_by_variant.get(variant.part_number) or ()
                pin_group_id = id(pins) if pins else None
                variant_pins = tuple(
                    ChipVariantPin(
                        number=str(pin.number),
                        pads=(pin.pad.name if isinstance(pin.pad, IncludedPadRef) else str(pin.pad),),
                        description=pin.name,
                        notes=pin.notes,
                    )
                    for pin in pins
                )
                variants.append(
                    ChipVariant(
                        part_number=variant.part_number,
                        package=variant.package,
                        description=variant.description,
                        pins=variant_pins,
                        pin_group_id=pin_group_id,
                    )
                )

            out.append(
                ChipSeries(
                    model_id=module.module_id,
                    lifecycle="module",
                    docs=tuple(module.docs),
                    pads=pads,
                    variants=tuple(variants),
                    schema_version=module.schema_version,
                    source_path=module.source_path,
                )
            )

        return out

    @classmethod
    def _load_module_dir(cls, root: Path, module_file: Path) -> ModuleDefinition:
        with module_file.open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}

        module_id = raw.get("module_id") or root.name
        schema_version = str(raw.get("schema_version", "1"))

        docs_raw = raw.get("docs") or ()
        docs: Sequence[Mapping[str, Any]]
        if isinstance(docs_raw, Mapping):
            docs = (docs_raw,)
        elif isinstance(docs_raw, list):
            docs = tuple(item for item in docs_raw if isinstance(item, Mapping))
        else:
            docs = ()

        includes = cls._parse_includes(raw.get("includes") or {}, module_file)
        variants = cls._parse_variants(raw.get("variants") or [], root, module_file)
        footprint_file_raw = raw.get("footprint_file")
        if not footprint_file_raw:
            raise ValueError(f"Module {module_id} is missing footprint_file in {module_file}")
        footprint_file = (root / str(footprint_file_raw)).resolve()

        pads: dict[str, ModulePadSpec] = {}
        pins_by_variant: dict[str, Sequence[ModulePin]] = {}
        pins_cache: dict[Path, tuple[Sequence[ModulePin], Mapping[str, ModulePadSpec]]] = {}
        for variant in variants:
            pins_path = variant.pins_file
            cached = pins_cache.get(pins_path)
            if cached is None:
                cached = cls._load_pins_file(pins_path, includes)
                pins_cache[pins_path] = cached
            pins, local_pads = cached
            pins_by_variant[variant.part_number] = pins
            for name, spec in local_pads.items():
                if name in pads:
                    continue
                pads[name] = spec

        return ModuleDefinition(
            module_id=str(module_id),
            schema_version=schema_version,
            docs=docs,
            includes=includes,
            variants=variants,
            pads=pads,
            pins_by_variant=pins_by_variant,
            footprint_file=footprint_file,
            root_dir=root,
            source_path=module_file,
        )

    @classmethod
    def _parse_includes(
        cls, payload: Any, source: Path
    ) -> Mapping[str, ModuleInclude]:
        if not payload:
            return {}
        if not isinstance(payload, Mapping):
            raise ValueError(f"Module includes must be a mapping in {source}")

        includes: dict[str, ModuleInclude] = {}
        for alias, spec in payload.items():
            if not isinstance(spec, Mapping):
                raise ValueError(f"Include {alias!r} must be a mapping in {source}")
            kind = str(spec.get("kind", "silicon_schema"))
            series = spec.get("series")
            if not series:
                raise ValueError(f"Include {alias!r} is missing series in {source}")
            includes[str(alias)] = ModuleInclude(alias=str(alias), kind=kind, series=str(series))
        return includes

    @classmethod
    def _parse_variants(
        cls, payload: Any, root: Path, source: Path
    ) -> Sequence[ModuleVariantDefinition]:
        if not isinstance(payload, list) or not payload:
            raise ValueError(f"Module variants must be a non-empty list in {source}")

        variants: list[ModuleVariantDefinition] = []
        for item in payload:
            if not isinstance(item, Mapping):
                raise ValueError(f"Variant entry must be a mapping in {source}")
            part_number = item.get("part_number")
            package = item.get("package") or part_number
            pins_file = item.get("pins_file")
            if not part_number or not pins_file:
                raise ValueError(f"Variant is missing part_number or pins_file in {source}: {item!r}")
            variants.append(
                ModuleVariantDefinition(
                    part_number=str(part_number),
                    package=str(package),
                    pins_file=(root / str(pins_file)).resolve(),
                    description=item.get("description"),
                )
            )
        return tuple(variants)

    @classmethod
    def _load_pins_file(
        cls,
        path: Path,
        includes: Mapping[str, ModuleInclude],
    ) -> tuple[Sequence[ModulePin], Mapping[str, ModulePadSpec]]:
        if not path.is_file():
            raise FileNotFoundError(f"Module pins file not found: {path}")

        with path.open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}

        local_pads_payload = raw.get("pads") or {}
        if local_pads_payload and not isinstance(local_pads_payload, Mapping):
            raise ValueError(f"pins.yml pads must be a mapping in {path}")

        local_pads: dict[str, ModulePadSpec] = {}
        for pad_name, spec in (local_pads_payload or {}).items():
            if not isinstance(spec, Mapping):
                raise ValueError(f"Local pad {pad_name!r} must be a mapping in {path}")
            pad_type = str(spec.get("type") or "passive")
            pinmux = cls._parse_functions(spec.get("functions"))
            local_pads[str(pad_name)] = ModulePadSpec(
                name=str(pad_name),
                type=pad_type,
                description=spec.get("description"),
                notes=spec.get("notes"),
                pinmux=pinmux,
            )

        pins_payload = raw.get("pins")
        if not isinstance(pins_payload, list) or not pins_payload:
            raise ValueError(f"pins.yml pins must be a non-empty list in {path}")

        pins: list[ModulePin] = []
        seen_numbers: set[str] = set()
        for entry in pins_payload:
            if not isinstance(entry, Mapping):
                raise ValueError(f"pins.yml pin entry must be a mapping in {path}")
            number = entry.get("number")
            pad_value = entry.get("pad")
            if not number or pad_value is None:
                raise ValueError(f"pins.yml pin entry is missing number/pad in {path}: {entry!r}")

            number_str = str(number)
            if number_str in seen_numbers:
                raise ValueError(f"Duplicate pin number {number_str} in {path}")
            seen_numbers.add(number_str)

            pad: str | IncludedPadRef
            if isinstance(pad_value, str):
                pad = pad_value
            elif isinstance(pad_value, Mapping):
                include_alias = pad_value.get("include")
                pad_name = pad_value.get("name")
                if not include_alias or not pad_name:
                    raise ValueError(f"Included pad ref must have include/name in {path}: {pad_value!r}")
                include_alias = str(include_alias)
                if include_alias not in includes:
                    raise ValueError(
                        f"pins.yml references include {include_alias!r} not declared in module.yml"
                    )
                pad = IncludedPadRef(include=include_alias, name=str(pad_name))
            else:
                raise ValueError(f"Unsupported pad reference type in {path}: {type(pad_value)!r}")

            pins.append(
                ModulePin(
                    number=number_str,
                    pad=pad,
                    name=entry.get("name"),
                    notes=entry.get("notes"),
                )
            )

        return tuple(pins), local_pads

    @staticmethod
    def _parse_functions(value: Any) -> tuple[PinmuxEntry, ...]:
        if not value:
            return ()
        if not isinstance(value, list):
            raise ValueError(f"Pad functions must be a list, got {type(value)!r}")

        entries: list[PinmuxEntry] = []
        for item in value:
            if isinstance(item, str):
                entries.append(PinmuxEntry(function=item))
                continue
            if isinstance(item, Mapping):
                function = item.get("function") or item.get("name")
                if not function:
                    raise ValueError(f"Pad function entry is missing a function name: {item!r}")
                entries.append(
                    PinmuxEntry(
                        function=str(function),
                        description=item.get("description"),
                        notes=item.get("notes"),
                    )
                )
                continue
            raise ValueError(f"Unsupported pad function entry type: {type(item)!r}")
        return tuple(entries)
