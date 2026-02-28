from __future__ import annotations

import copy
import json
import logging
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, Mapping, Sequence, Tuple

from .footprints import FootprintGenerationResult
from .schema_loader import ChipPad, ChipSeries, ChipVariant, PinmuxEntry
from .upstream import ensure_symbol_repo_on_sys_path

LOGGER = logging.getLogger(__name__)

PIN_TYPE_MAP: Mapping[str, str] = {
    "input": "input",
    "output": "output",
    "bidirectional": "bidirectional",
    "tri-state": "tri_state",
    "passive": "passive",
    "free": "free",
    "unspecified": "unspecified",
    "power_input": "power_in",
    "power_output": "power_out",
    "open_collector": "open_collector",
    "open_emitter": "open_emitter",
    "unconnected": "no_connect",
}

LEFT_TYPES = {"input", "power_input"}
RIGHT_TYPES = {"output", "power_output"}
PIN_PITCH = 2.54  # 100 mil vertical spacing
BODY_HALF_WIDTH = 5.0
PIN_CLEARANCE = 1.0
GRID = 1.27  # 50 mil grid for horizontal alignment
MIN_PIN_LENGTH = 2 * GRID  # 100 mil (minimum)
MAX_PIN_LENGTH = 6 * GRID  # 300 mil (maximum)
SYS_SPLIT_MAX_PINS = 40
# Each tuple defines a priority level. Subsystems within the same level may be
# packed together into a SYS unit; subsystems from different levels are never
# mixed into the same SYS unit.
#
# Special marker:
# - Include "over" in a level tuple to force a part break after each subsystem
#   in that level (i.e. disable greedy packing within that level).
SYS_SUBSYSTEM_PRIORITY_LEVELS: Tuple[Tuple[str, ...], ...] = (
    ("power", "analog", "over"),
    ("crystal", "rf", "strapping", "audio", "mipi", "usb"),
)


@dataclass(frozen=True)
class SymbolGenerationResult:
    output_path: Path


@dataclass(frozen=True)
class SymbolPinSpec:
    number: str
    name: str
    pad_type: str
    electrical_type: str
    pinmux: Tuple[PinmuxEntry, ...]
    pad_name: str
    subsystem: str | None


@dataclass
class SysTemplateUnit:
    """Cached snapshot of a SYS unit layout."""

    name: str | None
    pins: Tuple[object, ...]
    rectangles: Tuple[object, ...]
    circles: Tuple[object, ...]
    arcs: Tuple[object, ...]
    polylines: Tuple[object, ...]
    beziers: Tuple[object, ...]
    texts: Tuple[object, ...]
    pin_lookup: Dict[Tuple[str, str], object] = field(init=False, default_factory=dict)

    def __post_init__(self) -> None:
        self.pin_lookup = {
            (getattr(pin, "name", f"__idx_{idx}"), getattr(pin, "number", f"__num_{idx}")): pin
            for idx, pin in enumerate(self.pins)
        }

    def get_pin(self, spec: SymbolPinSpec) -> object | None:
        return self.pin_lookup.get((spec.name, spec.number))


@dataclass
class SysTemplate:
    """Represents a SYS template library keyed by template id."""

    template_id: str
    path: Path
    units: Mapping[str, SysTemplateUnit]

    def unit_for_name(self, name: str | None) -> SysTemplateUnit | None:
        if not name:
            return None
        return self.units.get(name)


class SymbolGenerator:
    """Writes KiCad symbol libraries plus metadata manifests."""

    def __init__(
        self,
        output_dir: Path,
        footprint_namespace: str,
        library_utils_root: Path,
    ) -> None:
        self.output_dir = output_dir
        self.footprint_namespace = footprint_namespace
        self.library_utils_root = library_utils_root
        self.library_name = "MCU_SiFli"
        ensure_symbol_repo_on_sys_path(library_utils_root)
        from kicad_sym import (  # type: ignore
            AltFunction,
            KicadLibrary,
            KicadSymbol,
            Pin,
            Property,
            Rectangle,
        )

        self.AltFunction = AltFunction
        self.KicadLibrary = KicadLibrary
        self.KicadSymbol = KicadSymbol
        self.Pin = Pin
        self.Property = Property
        self.Rectangle = Rectangle
        self.sys_template_dir = self._resolve_sys_template_dir()
        self._sys_template_cache: Dict[str, SysTemplate | None] = {}

    def generate(
        self,
        series: Sequence[ChipSeries],
        footprints: FootprintGenerationResult,
    ) -> SymbolGenerationResult:
        symbol_dir = self.output_dir / "symbols"
        metadata_dir = symbol_dir / "metadata"
        library_dir = symbol_dir / "libs"
        metadata_dir.mkdir(parents=True, exist_ok=True)
        library_dir.mkdir(parents=True, exist_ok=True)

        library_path = library_dir / f"{self.library_name}.kicad_sym"
        library_rel_path = library_path.relative_to(self.output_dir)
        library = self.KicadLibrary(str(library_path))
        library.symbols = []

        manifest_entries: list[dict[str, str]] = []

        for entry in series:
            metadata_file = metadata_dir / f"{entry.model_id}.json"
            variants_payload: list[dict[str, object]] = []
            pin_group_bases: dict[int, str] = {}
            pin_group_ids = {
                (variant.pin_group_id or id(variant)) for variant in entry.variants
            }
            multiple_pin_groups = len(pin_group_ids) > 1

            for variant in entry.variants:
                footprint_artifact = footprints.footprint_for_package(variant.package)
                if not footprint_artifact:
                    raise RuntimeError(
                        f"Variant {variant.part_number} references missing footprint {variant.package}."
                    )

                group_id = variant.pin_group_id or id(variant)
                base_symbol_name = pin_group_bases.get(group_id)
                sys_template_id: str | None = None
                sys_template: SysTemplate | None = None
                if base_symbol_name is None:
                    sys_template_id = f"{entry.model_id}__{variant.part_number}"
                    sys_template = self._load_sys_template(
                        sys_template_id,
                        symbol_name_hint=sys_template_id,
                    )
                    if sys_template is None and not multiple_pin_groups:
                        sys_template = self._load_sys_template(
                            entry.model_id,
                            symbol_name_hint=entry.model_id,
                        )

                symbol = self._build_symbol(
                    series=entry,
                    variant=variant,
                    footprint_ref=footprint_artifact.qualified_name,
                    extends=base_symbol_name,
                    sys_template=sys_template,
                    sys_template_id=sys_template_id,
                )

                if base_symbol_name is None:
                    pin_group_bases[group_id] = symbol.name

                library.symbols.append(symbol)
                variants_payload.append(
                    {
                        "part_number": variant.part_number,
                        "package": variant.package,
                        "footprint": footprint_artifact.qualified_name,
                        "pin_count": len(variant.pins),
                        "symbol": f"{self.library_name}:{symbol.name}",
                        "extends": base_symbol_name,
                    }
                )

            payload = {
                "model_id": entry.model_id,
                "lifecycle": entry.lifecycle,
                "variants": variants_payload,
                "footprint_namespace": self.footprint_namespace,
                "symbol_library": str(library_rel_path),
            }
            metadata_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            manifest_entries.append(
                {
                    "model_id": entry.model_id,
                    "metadata": str(metadata_file.relative_to(self.output_dir)),
                    "library": str(library_rel_path),
                }
            )

        library.write()

        manifest = {
            "namespace": self.footprint_namespace,
            "symbols": manifest_entries,
            "library": str(library_rel_path),
        }
        manifest_path = symbol_dir / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        LOGGER.info("Wrote symbol libraries for %d series.", len(series))
        return SymbolGenerationResult(output_path=manifest_path)

    def _build_symbol(
        self,
        series: ChipSeries,
        variant: ChipVariant,
        footprint_ref: str,
        extends: str | None = None,
        sys_template: SysTemplate | None = None,
        sys_template_id: str | None = None,
    ):
        symbol = self.KicadSymbol.new(variant.part_number, self.library_name)
        if extends:
            symbol.extends = extends
        symbol.add_default_properties()

        reference = symbol.get_property("Reference")
        reference.value = "U"

        value = symbol.get_property("Value")
        value.value = variant.part_number

        footprint = symbol.get_property("Footprint")
        footprint.value = footprint_ref

        datasheet = symbol.get_property("Datasheet")
        datasheet.value = self._extract_datasheet(series.docs)

        if extends:
            return symbol

        lock = self.Property("ki_locked", "")
        lock.effects.is_hidden = True
        symbol.properties.append(lock)

        pins = self._collect_pin_specs(series.pads, variant)
        units = self._group_units(pins)
        symbol.unit_count = len(units)
        symbol.unit_names = {idx + 1: unit.name for idx, unit in enumerate(units)}

        sys_unit_names = [unit.name for unit in units if unit.name.upper().startswith("SYS")]
        export_template = bool(sys_unit_names) and sys_template is None
        if export_template:
            expected_path = self._sys_template_path(sys_template_id or series.model_id)
            LOGGER.warning(
                "SYS template %s missing for series %s (part %s); exporting suggestion template.",
                expected_path,
                series.model_id,
                variant.part_number,
            )

        unit_bounds: list[tuple[int, float] | None] = []
        for idx, unit in enumerate(units, start=1):
            if sys_template and unit.name.upper().startswith("SYS"):
                applied, mismatch = self._apply_sys_template_unit(symbol, idx, unit, sys_template)
                if applied:
                    unit_bounds.append(None)
                    continue
                export_template = export_template or mismatch

            max_rows, half_width = self._place_pins(
                symbol,
                unit=idx,
                pins=unit.pins,
                pair_mode=unit.pair_mode,
            )
            unit_bounds.append((max_rows, half_width))

        for idx, bounds in enumerate(unit_bounds, start=1):
            if bounds is None:
                continue
            rows, half_width = bounds
            half_height = (rows - 1) * PIN_PITCH / 2 + PIN_PITCH
            rect = self.Rectangle(
                half_width, -half_height, -half_width, half_height, stroke_width=0.254
            )
            rect.unit = idx
            symbol.rectangles.append(rect)

        if export_template:
            self._export_sys_template_suggestion(
                model_id=series.model_id,
                part_number=variant.part_number,
                symbol=symbol,
            )

        return symbol

    def _resolve_sys_template_dir(self) -> Path:
        current = Path(__file__).resolve()
        try:
            root = current.parents[2]
        except IndexError:
            root = current.parent
        return root / "templates" / "sys"

    def _sys_template_path(self, model_id: str) -> Path:
        return self.sys_template_dir / f"{model_id}.kicad_sym"

    def _find_sys_template_file(self, template_id: str) -> Path | None:
        candidate = self.sys_template_dir / f"{template_id}.kicad_sym"
        if candidate.is_file():
            return candidate
        return None

    def _load_sys_template(
        self,
        template_id: str,
        symbol_name_hint: str | None = None,
    ) -> SysTemplate | None:
        if template_id in self._sys_template_cache:
            return self._sys_template_cache[template_id]

        path = self._find_sys_template_file(template_id)
        if not path:
            self._sys_template_cache[template_id] = None
            return None

        try:
            library = self.KicadLibrary.from_file(str(path))
        except Exception as exc:  # pragma: no cover - defensive
            LOGGER.warning("Failed to load SYS template %s: %s", path, exc)
            self._sys_template_cache[template_id] = None
            return None

        preferred_names = [name for name in (symbol_name_hint, template_id) if name]
        symbol = None
        for name in preferred_names:
            symbol = next((sym for sym in library.symbols if sym.name == name), None)
            if symbol is not None:
                break
        if symbol is None and library.symbols:
            symbol = library.symbols[0]

        if symbol is None:
            LOGGER.warning("SYS template %s does not contain any symbol data.", path)
            self._sys_template_cache[template_id] = None
            return None

        template = self._snapshot_sys_template(template_id, path, symbol)
        self._sys_template_cache[template_id] = template
        return template

    def _snapshot_sys_template(self, template_id: str, path: Path, symbol) -> SysTemplate:
        units: Dict[str, SysTemplateUnit] = {}
        unit_indices: set[int] = set(idx for idx in symbol.unit_names.keys() if isinstance(idx, int))

        def collect_units(items: Iterable[object]) -> None:
            for item in items:
                unit_value = getattr(item, "unit", None)
                if isinstance(unit_value, int) and unit_value > 0:
                    unit_indices.add(unit_value)

        collect_units(symbol.pins)
        collect_units(symbol.rectangles)
        collect_units(symbol.circles)
        collect_units(symbol.arcs)
        collect_units(symbol.polylines)
        collect_units(symbol.beziers)
        collect_units(symbol.texts)

        if not unit_indices and symbol.unit_count:
            unit_indices.update(range(1, symbol.unit_count + 1))

        for unit_idx in sorted(unit_indices):
            name = symbol.unit_names.get(unit_idx) or f"UNIT{unit_idx}"
            pins = tuple(copy.deepcopy(pin) for pin in symbol.pins if pin.unit == unit_idx)
            rectangles = tuple(
                copy.deepcopy(rect) for rect in symbol.rectangles if rect.unit == unit_idx
            )
            circles = tuple(
                copy.deepcopy(circle) for circle in symbol.circles if circle.unit == unit_idx
            )
            arcs = tuple(copy.deepcopy(arc) for arc in symbol.arcs if arc.unit == unit_idx)
            polylines = tuple(
                copy.deepcopy(poly) for poly in symbol.polylines if poly.unit == unit_idx
            )
            beziers = tuple(
                copy.deepcopy(bezier) for bezier in symbol.beziers if bezier.unit == unit_idx
            )
            texts = tuple(copy.deepcopy(text) for text in symbol.texts if text.unit == unit_idx)

            if not (pins or rectangles or circles or arcs or polylines or beziers or texts):
                continue

            units[name] = SysTemplateUnit(
                name=name,
                pins=pins,
                rectangles=rectangles,
                circles=circles,
                arcs=arcs,
                polylines=polylines,
                beziers=beziers,
                texts=texts,
            )

        return SysTemplate(template_id=template_id, path=path, units=units)

    def _apply_sys_template_unit(
        self,
        symbol,
        unit_index: int,
        unit: "SymbolGenerator.Unit",
        template: SysTemplate,
    ) -> tuple[bool, bool]:
        template_unit = template.unit_for_name(unit.name)
        if not template_unit:
            LOGGER.warning("SYS template %s missing unit %s.", template.path, unit.name)
            return False, True

        missing = [spec.name for spec in unit.pins if not template_unit.get_pin(spec)]
        if missing:
            LOGGER.warning(
                "SYS template %s missing pins for unit %s: %s",
                template.path,
                unit.name,
                ", ".join(missing),
            )
            return False, True

        self._extend_graphics(symbol, "rectangles", template_unit.rectangles, unit_index)
        self._extend_graphics(symbol, "circles", template_unit.circles, unit_index)
        self._extend_graphics(symbol, "arcs", template_unit.arcs, unit_index)
        self._extend_graphics(symbol, "polylines", template_unit.polylines, unit_index)
        self._extend_graphics(symbol, "beziers", template_unit.beziers, unit_index)
        self._extend_graphics(symbol, "texts", template_unit.texts, unit_index)

        for spec in unit.pins:
            layout = template_unit.get_pin(spec)
            if layout is None:
                continue
            pin = self.Pin(
                name=spec.name,
                number=spec.number,
                etype=spec.electrical_type,
                posx=layout.posx,
                posy=layout.posy,
                rotation=layout.rotation,
                length=layout.length,
                unit=unit_index,
                shape=layout.shape,
            )
            pin.is_hidden = layout.is_hidden
            pin.is_global = layout.is_global
            pin.name_effect = copy.deepcopy(layout.name_effect)
            pin.number_effect = copy.deepcopy(layout.number_effect)
            pin.demorgan = 0
            emit_altfuncs = (
                spec.pad_type not in {"power_input", "power_output"}
                and len(spec.pinmux) > 1
            )
            if emit_altfuncs:
                for mux in spec.pinmux:
                    pin.altfuncs.append(self.AltFunction(mux.function, pin.etype))
            symbol.pins.append(pin)

        LOGGER.info(
            "Applied SYS template %s for series %s unit %s.",
            template.path,
            template.template_id,
            unit.name,
        )
        return True, False

    def _extend_graphics(
        self,
        symbol,
        attribute: str,
        items: Tuple[object, ...],
        unit_index: int,
    ) -> None:
        if not items:
            return
        target = getattr(symbol, attribute)
        for item in items:
            clone = copy.deepcopy(item)
            if hasattr(clone, "unit"):
                setattr(clone, "unit", unit_index)
            if hasattr(clone, "demorgan"):
                setattr(clone, "demorgan", 0)
            target.append(clone)

    def _export_sys_template_suggestion(self, model_id: str, part_number: str, symbol) -> None:
        """Write a SYS template suggestion into the output directory.

        Repository templates under templates/sys/ are treated as read-only inputs.
        When a template is missing or does not match the generated SYS units, a
        suggestion template is emitted under:

            output_dir/template/<model_id>__<part_number>.kicad_sym

        Args:
            model_id: SiliconSchema series identifier (used as the template symbol name).
            part_number: Variant part number used to disambiguate output filenames.
            symbol: Generated symbol that contains the SYS units to export.
        """

        sys_units = [
            idx
            for idx, name in symbol.unit_names.items()
            if isinstance(name, str) and name.upper().startswith("SYS")
        ]
        if not sys_units:
            return

        mapping = {src: dst for dst, src in enumerate(sys_units, start=1)}
        template_id = f"{model_id}__{part_number}"
        template_symbol = self.KicadSymbol.new(template_id, self.library_name)
        template_symbol.unit_count = len(sys_units)
        template_symbol.demorgan_count = 0
        template_symbol.unit_names = {
            dst: symbol.unit_names.get(src) for src, dst in mapping.items()
        }
        template_symbol.pins = []
        template_symbol.rectangles = []
        template_symbol.circles = []
        template_symbol.arcs = []
        template_symbol.polylines = []
        template_symbol.beziers = []
        template_symbol.texts = []

        def transfer(source, target):
            for item in source:
                if getattr(item, "unit", None) in mapping:
                    clone = copy.deepcopy(item)
                    clone.unit = mapping[clone.unit]
                    if hasattr(clone, "demorgan"):
                        clone.demorgan = 0
                    target.append(clone)

        transfer(symbol.pins, template_symbol.pins)
        transfer(symbol.rectangles, template_symbol.rectangles)
        transfer(symbol.circles, template_symbol.circles)
        transfer(symbol.arcs, template_symbol.arcs)
        transfer(symbol.polylines, template_symbol.polylines)
        transfer(symbol.beziers, template_symbol.beziers)
        transfer(symbol.texts, template_symbol.texts)

        template_dir = self.output_dir / "template"
        template_dir.mkdir(parents=True, exist_ok=True)
        path = template_dir / f"{model_id}__{part_number}.kicad_sym"
        library = self.KicadLibrary(str(path))
        library.symbols = [template_symbol]
        library.write()
        LOGGER.warning("Wrote SYS template suggestion to %s", path)

    def _collect_pin_specs(
        self,
        pads: Mapping[str, ChipPad],
        variant: ChipVariant,
    ) -> list[SymbolPinSpec]:
        specs: list[SymbolPinSpec] = []
        for pin in variant.pins:
            pad_name = pin.pads[0] if pin.pads else pin.number
            pad = pads.get(pad_name)
            pad_type = pad.type if pad else "passive"
            electrical = PIN_TYPE_MAP.get(pad_type, "passive")
            pin_name = pin.description or "/".join(pin.pads) or pad_name
            pinmux = pad.pinmux if pad else ()
            subsystem = pad.subsystem if pad else None
            specs.append(
                SymbolPinSpec(
                    number=pin.number,
                    name=pin_name,
                    pad_type=pad_type,
                    electrical_type=electrical,
                    pinmux=pinmux,
                    pad_name=pad_name,
                    subsystem=subsystem,
                )
            )
        return specs

    @dataclass
    class Unit:
        name: str
        pins: list[SymbolPinSpec]
        pair_mode: bool

    def _group_units(
        self,
        pins: list[SymbolPinSpec],
    ) -> list["SymbolGenerator.Unit"]:
        io_groups: dict[str, list[SymbolPinSpec]] = {}
        misc: list[SymbolPinSpec] = []

        for spec in pins:
            port = self._extract_port_prefix(spec.pad_name)
            if port:
                io_groups.setdefault(port, []).append(spec)
            else:
                misc.append(spec)

        sys_units = self._group_sys_units(misc)

        io_units: list[SymbolGenerator.Unit] = []
        for port in sorted(io_groups):
            entries = self._sort_port_pins(io_groups[port])
            for idx, chunk in enumerate(self._chunks(entries, 64), start=1):
                label = port if len(entries) <= 64 else f"{port}{idx}"
                io_units.append(self.Unit(label, chunk, True))

        units: list[SymbolGenerator.Unit] = [*sys_units, *io_units]
        if not units:
            units.append(self.Unit("UNIT1", [], False))
        return units

    def _group_sys_units(self, misc: list[SymbolPinSpec]) -> list["SymbolGenerator.Unit"]:
        """Group non-port pins into SYS units, splitting large sets by subsystem.

        Args:
            misc: Pins that do not belong to a GPIO port prefix (PA/PB/...).

        Returns:
            A list of SYS units in display order (highest-priority subsystems first).

            When the SYS pins exceed ``SYS_SPLIT_MAX_PINS``, pins are first grouped by
            ``subsystem`` and each subsystem is treated as atomic (it must not span
            multiple SYS units). Subsystems are packed greedily within the same
            priority level; subsystems from different priority levels are never mixed
            into the same SYS unit (even if there is remaining capacity). If a priority
            level contains the "over" marker, greedy packing within that level is
            disabled (each subsystem in that level starts a new SYS unit).
        """

        if not misc:
            return []

        sorted_misc = self._sort_misc_pins(misc)
        if len(sorted_misc) <= SYS_SPLIT_MAX_PINS:
            return [self.Unit("SYS", sorted_misc, False)]

        # If no subsystem classification is available, fall back to a simple size split.
        if all(spec.subsystem is None for spec in misc):
            units: list[SymbolGenerator.Unit] = []
            for idx, chunk in enumerate(self._chunks(sorted_misc, SYS_SPLIT_MAX_PINS), start=1):
                units.append(self.Unit(f"SYS{idx}", chunk, False))
            return units

        groups: dict[str | None, list[SymbolPinSpec]] = {}
        for spec in misc:
            groups.setdefault(spec.subsystem, []).append(spec)

        for key, items in list(groups.items()):
            groups[key] = self._sort_misc_pins(items)

        configured: set[str] = set()
        priority_levels: list[tuple[list[str | None], bool]] = []
        for level in SYS_SUBSYSTEM_PRIORITY_LEVELS:
            over_marker = "over" in level
            level_keys = tuple(key for key in level if key != "over")
            configured.update(level_keys)
            present = [key for key in level_keys if key in groups]
            if present:
                priority_levels.append((present, over_marker))

        unconfigured = sorted(
            key for key in groups if isinstance(key, str) and key not in configured
        )
        if unconfigured or None in groups:
            tail: list[str | None] = list(unconfigured)
            if None in groups:
                tail.append(None)
            priority_levels.append((tail, False))

        bins: list[list[str | None]] = []
        current: list[str | None] = []
        current_count = 0

        def flush() -> None:
            nonlocal current, current_count
            if not current:
                return
            bins.append(current)
            current = []
            current_count = 0

        for level, over_marker in priority_levels:
            flush()
            for key in level:
                count = len(groups[key])
                if count > SYS_SPLIT_MAX_PINS:
                    flush()
                    bins.append([key])
                    continue

                if not current or current_count + count > SYS_SPLIT_MAX_PINS:
                    flush()
                    current = [key]
                    current_count = count
                else:
                    current.append(key)
                    current_count += count

                if over_marker:
                    flush()
            flush()

        units: list[SymbolGenerator.Unit] = []
        for idx, bin_keys in enumerate(bins, start=1):
            unit_pins: list[SymbolPinSpec] = []
            for key in bin_keys:
                unit_pins.extend(groups[key])
            units.append(self.Unit(f"SYS{idx}", unit_pins, False))
        return units

    def _sort_port_pins(self, pins: list[SymbolPinSpec]) -> list[SymbolPinSpec]:
        def key(spec: SymbolPinSpec) -> tuple[int, str]:
            match = re.search(r"(\d+)", spec.pad_name)
            if match:
                return (int(match.group(1)), spec.pad_name)
            match_num = re.search(r"(\d+)", spec.number)
            if match_num:
                return (int(match_num.group(1)), spec.pad_name)
            return (10**9, spec.pad_name)

        return sorted(pins, key=key)

    def _sort_misc_pins(self, pins: list[SymbolPinSpec]) -> list[SymbolPinSpec]:
        def key(spec: SymbolPinSpec) -> tuple[int, str]:
            match = re.search(r"(\d+)", spec.number)
            if match:
                return (int(match.group(1)), spec.name)
            return (10**9, spec.name)

        return sorted(pins, key=key)

    def _partition_by_type(
        self,
        pins: list[SymbolPinSpec],
    ) -> tuple[list[SymbolPinSpec], list[SymbolPinSpec]]:
        left: list[SymbolPinSpec] = []
        right: list[SymbolPinSpec] = []
        neutral: list[SymbolPinSpec] = []
        for spec in pins:
            if spec.pad_type in LEFT_TYPES:
                left.append(spec)
            elif spec.pad_type in RIGHT_TYPES:
                right.append(spec)
            else:
                neutral.append(spec)

        for spec in neutral:
            if len(left) <= len(right):
                left.append(spec)
            else:
                right.append(spec)

        return left, right

    def _pair_columns(
        self,
        pins: list[SymbolPinSpec],
    ) -> tuple[list[SymbolPinSpec], list[SymbolPinSpec]]:
        ordered = self._sort_port_pins(pins)
        right_count = math.ceil(len(ordered) / 2)
        right = ordered[: right_count]
        left = list(reversed(ordered[right_count:]))
        return left, right

    def _place_pins(
        self,
        symbol,
        unit: int,
        pins: list[SymbolPinSpec],
        pair_mode: bool,
    ) -> tuple[int, float]:
        """Place pins in one unit and return the unit bounds.

        Pin length is derived from the maximum pin number string length within the
        unit. A single character adds 50mil (1.27mm); the minimum is 100mil and
        the maximum is 300mil.

        Args:
            symbol: Target symbol object to mutate.
            unit: Unit index (1-based).
            pins: Pins belonging to this unit.
            pair_mode: True for port units (PA/PB/...), False for SYS/misc units.

        Returns:
            A tuple of (row_count, body_half_width_mm).
        """
        pin_length = self._pin_length_for_unit(pins)
        if pair_mode:
            left, right = self._pair_columns(pins)
        else:
            left, right = self._partition_by_type(pins)

        char_width = 0.75
        label_margin = 1.5

        def label_len(spec: SymbolPinSpec, include_alt: bool) -> int:
            if not include_alt:
                return len(spec.name)
            alt_len = max((len(mux.function) for mux in spec.pinmux), default=0)
            return max(len(spec.name), alt_len)

        include_alt = pair_mode
        left_label_len = max((label_len(spec, include_alt) for spec in left), default=0)
        right_label_len = max((label_len(spec, include_alt) for spec in right), default=0)
        left_extra = left_label_len * char_width + label_margin
        right_extra = right_label_len * char_width + label_margin
        base_half = max(BODY_HALF_WIDTH, PIN_PITCH)
        if pair_mode:
            body_half = base_half + (left_extra + right_extra) / 2
        else:
            body_half = base_half + max(left_extra, right_extra)
        body_half = self._snap(max(base_half, body_half))

        left_offset = self._snap(body_half + pin_length)
        right_offset = left_offset

        max_rows = max(len(left), len(right), 1)
        top_y = (max_rows - 1) * PIN_PITCH / 2

        def place(column: list[SymbolPinSpec], x: float, rotation: int) -> None:
            if not column:
                return
            for index, spec in enumerate(column):
                posy = top_y - index * PIN_PITCH
                pin = self.Pin(
                    name=spec.name,
                    number=spec.number,
                    etype=spec.electrical_type,
                    posx=x,
                    posy=posy,
                    rotation=rotation,
                    length=pin_length,
                    unit=unit,
                )
                emit_altfuncs = (
                    spec.pad_type not in {"power_input", "power_output"}
                    and len(spec.pinmux) > 1
                )
                if emit_altfuncs:
                    for mux in spec.pinmux:
                        pin.altfuncs.append(self.AltFunction(mux.function, pin.etype))
                symbol.pins.append(pin)

        place(left, x=-left_offset, rotation=0)
        place(right, x=right_offset, rotation=180)
        return max_rows, body_half

    def _pin_length_for_unit(self, pins: Sequence[SymbolPinSpec]) -> float:
        """Compute pin length for a unit based on pin number width.

        The length is derived from the maximum string length of ``pin.number`` in
        the unit. Each character adds 50mil (1.27mm). The value is clamped to the
        range 100mil..300mil.

        Args:
            pins: Pins in the unit.

        Returns:
            Pin length in millimeters, aligned to the 50mil grid.
        """
        max_chars = max((len(spec.number) for spec in pins), default=0)
        steps = max(2, max_chars)
        steps = min(steps, int(round(MAX_PIN_LENGTH / GRID)))
        length = steps * GRID
        return max(MIN_PIN_LENGTH, min(MAX_PIN_LENGTH, length))

    def _extract_datasheet(self, docs: Sequence[Mapping[str, object]]) -> str:
        for entry in docs:
            datasheet = entry.get("datasheet")
            if isinstance(datasheet, Mapping):
                if "en" in datasheet:
                    return str(datasheet["en"])
                if datasheet:
                    first = next(iter(datasheet.values()))
                    if isinstance(first, str):
                        return first
        return ""

    def _extract_port_prefix(self, pad_name: str) -> str | None:
        match = re.match(r"^(P[A-Z]+)\d", pad_name)
        if match:
            return match.group(1)
        return None

    def _chunks(self, items: Iterable[SymbolPinSpec], size: int) -> list[list[SymbolPinSpec]]:
        chunk: list[SymbolPinSpec] = []
        chunks: list[list[SymbolPinSpec]] = []
        for item in items:
            chunk.append(item)
            if len(chunk) >= size:
                chunks.append(chunk)
                chunk = []
        if chunk:
            chunks.append(chunk)
        return chunks
    @staticmethod
    def _snap(value: float, grid: float = GRID) -> float:
        if grid <= 0:
            return value
        return round(value / grid) * grid
