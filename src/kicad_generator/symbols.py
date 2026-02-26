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
PIN_LENGTH = 2.54
BODY_HALF_WIDTH = 5.0
PIN_CLEARANCE = 1.0
GRID = 1.27  # 50 mil grid for horizontal alignment


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
    """Represents a per-series SYS template library."""

    model_id: str
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
        from kicad_sym import AltFunction, KicadLibrary, KicadSymbol, Pin, Rectangle  # type: ignore

        self.AltFunction = AltFunction
        self.KicadLibrary = KicadLibrary
        self.KicadSymbol = KicadSymbol
        self.Pin = Pin
        self.Rectangle = Rectangle
        self.sys_template_dir = self._resolve_sys_template_dir()
        self._sys_template_cache: Dict[str, SysTemplate | None] = {}
        self._sys_template_missing: set[str] = set()
        self._sys_template_written: set[str] = set()

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
            sys_template = self._load_sys_template(entry.model_id)
            metadata_file = metadata_dir / f"{entry.model_id}.json"
            variants_payload: list[dict[str, object]] = []
            pin_group_bases: dict[int, str] = {}

            for variant in entry.variants:
                footprint_artifact = footprints.footprint_for_package(variant.package)
                if not footprint_artifact:
                    raise RuntimeError(
                        f"Variant {variant.part_number} references missing footprint {variant.package}."
                    )

                group_id = variant.pin_group_id or id(variant)
                base_symbol_name = pin_group_bases.get(group_id)

                symbol = self._build_symbol(
                    series=entry,
                    variant=variant,
                    footprint_ref=footprint_artifact.qualified_name,
                    extends=base_symbol_name,
                    sys_template=sys_template,
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

        pins = self._collect_pin_specs(series.pads, variant)
        units = self._group_units(pins)
        symbol.unit_count = len(units)
        symbol.unit_names = {idx + 1: unit.name for idx, unit in enumerate(units)}

        unit_bounds: list[tuple[int, float] | None] = []
        for idx, unit in enumerate(units, start=1):
            if (
                sys_template
                and unit.name
                and unit.name.upper().startswith("SYS")
                and self._apply_sys_template_unit(symbol, idx, unit, sys_template)
            ):
                unit_bounds.append(None)
                continue

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

        if self._should_export_sys_template(series.model_id):
            self._export_sys_template(series.model_id, symbol)

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

    def _find_sys_template_file(self, model_id: str) -> Path | None:
        candidate = self.sys_template_dir / f"{model_id}.kicad_sym"
        if candidate.is_file():
            return candidate
        return None

    def _load_sys_template(self, model_id: str) -> SysTemplate | None:
        if model_id in self._sys_template_cache:
            return self._sys_template_cache[model_id]

        path = self._find_sys_template_file(model_id)
        if not path:
            self._sys_template_missing.add(model_id)
            self._sys_template_cache[model_id] = None
            return None

        try:
            library = self.KicadLibrary.from_file(str(path))
        except Exception as exc:  # pragma: no cover - defensive
            LOGGER.warning("Failed to load SYS template %s: %s", path, exc)
            self._sys_template_cache[model_id] = None
            return None

        symbol = next((sym for sym in library.symbols if sym.name == model_id), None)
        if symbol is None and library.symbols:
            symbol = library.symbols[0]

        if symbol is None:
            LOGGER.warning("SYS template %s does not contain any symbol data.", path)
            self._sys_template_cache[model_id] = None
            return None

        template = self._snapshot_sys_template(model_id, path, symbol)
        self._sys_template_cache[model_id] = template
        return template

    def _snapshot_sys_template(self, model_id: str, path: Path, symbol) -> SysTemplate:
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

        return SysTemplate(model_id=model_id, path=path, units=units)

    def _apply_sys_template_unit(
        self,
        symbol,
        unit_index: int,
        unit: "SymbolGenerator.Unit",
        template: SysTemplate,
    ) -> bool:
        template_unit = template.unit_for_name(unit.name)
        if not template_unit:
            return False

        missing = [spec.name for spec in unit.pins if not template_unit.get_pin(spec)]
        if missing:
            LOGGER.warning(
                "SYS template %s missing pins for unit %s: %s",
                template.path,
                unit.name,
                ", ".join(missing),
            )
            return False

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
            for mux in spec.pinmux:
                pin.altfuncs.append(self.AltFunction(mux.function, pin.etype))
            symbol.pins.append(pin)

        LOGGER.info(
            "Applied SYS template %s for series %s unit %s.",
            template.path,
            template.model_id,
            unit.name,
        )
        return True

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

    def _should_export_sys_template(self, model_id: str) -> bool:
        return (
            model_id in self._sys_template_missing
            and model_id not in self._sys_template_written
        )

    def _export_sys_template(self, model_id: str, symbol) -> None:
        if model_id not in self._sys_template_missing or model_id in self._sys_template_written:
            return

        sys_units = [
            idx
            for idx, name in symbol.unit_names.items()
            if isinstance(name, str) and name.upper().startswith("SYS")
        ]
        if not sys_units:
            return

        mapping = {src: dst for dst, src in enumerate(sys_units, start=1)}
        template_symbol = self.KicadSymbol.new(model_id, self.library_name)
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

        path = self._sys_template_path(model_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        library = self.KicadLibrary(str(path))
        library.symbols = [template_symbol]
        library.write()
        self._sys_template_written.add(model_id)
        LOGGER.info("Wrote default SYS template to %s", path)

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
            specs.append(
                SymbolPinSpec(
                    number=pin.number,
                    name=pin_name,
                    pad_type=pad_type,
                    electrical_type=electrical,
                    pinmux=pinmux,
                    pad_name=pad_name,
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

        units: list[SymbolGenerator.Unit] = []
        for port in sorted(io_groups):
            entries = self._sort_port_pins(io_groups[port])
            for idx, chunk in enumerate(self._chunks(entries, 64), start=1):
                label = port if len(entries) <= 64 else f"{port}{idx}"
                units.append(self.Unit(label, chunk, True))

        if misc:
            sorted_misc = self._sort_misc_pins(misc)
            for idx, chunk in enumerate(self._chunks(sorted_misc, 64), start=1):
                label = "SYS" if len(misc) <= 64 and idx == 1 else f"SYS{idx}"
                units.insert(0, self.Unit(label, chunk, False))

        if not units:
            units.append(self.Unit("UNIT1", [], False))
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
        if pair_mode:
            left, right = self._pair_columns(pins)
        else:
            left, right = self._partition_by_type(pins)

        char_width = 0.75
        label_margin = 1.5
        left_label_len = max((len(spec.name) for spec in left), default=0)
        right_label_len = max((len(spec.name) for spec in right), default=0)
        left_extra = left_label_len * char_width + label_margin
        right_extra = right_label_len * char_width + label_margin
        base_half = max(BODY_HALF_WIDTH, PIN_PITCH)
        left_offset = base_half + PIN_CLEARANCE + PIN_LENGTH / 2 + left_extra
        right_offset = base_half + PIN_CLEARANCE + PIN_LENGTH / 2 + right_extra
        left_offset = self._snap(left_offset)
        right_offset = self._snap(right_offset)
        body_half = max(
            base_half,
            left_offset - PIN_CLEARANCE - PIN_LENGTH / 2,
            right_offset - PIN_CLEARANCE - PIN_LENGTH / 2,
        )
        body_half = self._snap(body_half)

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
                    length=PIN_LENGTH,
                    unit=unit,
                )
                for mux in spec.pinmux:
                    pin.altfuncs.append(self.AltFunction(mux.function, pin.etype))
                symbol.pins.append(pin)

        place(left, x=-left_offset, rotation=0)
        place(right, x=right_offset, rotation=180)
        return max_rows, body_half

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
