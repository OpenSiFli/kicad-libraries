from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence, Tuple

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
PIN_PITCH = 2.54
PIN_LENGTH = 2.54
BODY_HALF_WIDTH = 5.0


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
        symbol.unit_names = {idx + 1: unit_name for idx, (unit_name, _) in enumerate(units)}

        unit_bounds: list[tuple[int, float]] = []
        for idx, (_, unit_pins) in enumerate(units, start=1):
            max_rows, half_width = self._place_pins(symbol, unit=idx, pins=unit_pins)
            unit_bounds.append((max_rows, half_width))

        for idx, (rows, half_width) in enumerate(unit_bounds, start=1):
            half_height = (rows - 1) * PIN_PITCH / 2 + PIN_PITCH
            rect = self.Rectangle(
                half_width, -half_height, -half_width, half_height, stroke_width=0.254
            )
            rect.unit = idx
            symbol.rectangles.append(rect)

        return symbol

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

    def _group_units(
        self,
        pins: list[SymbolPinSpec],
    ) -> list[tuple[str, list[SymbolPinSpec]]]:
        io_groups: dict[str, list[SymbolPinSpec]] = {}
        misc: list[SymbolPinSpec] = []

        for spec in pins:
            port = self._extract_port_prefix(spec.pad_name)
            if port:
                io_groups.setdefault(port, []).append(spec)
            else:
                misc.append(spec)

        units: list[tuple[str, list[SymbolPinSpec]]] = []
        for port in sorted(io_groups):
            entries = self._sort_port_pins(io_groups[port])
            for idx, chunk in enumerate(self._chunks(entries, 64), start=1):
                label = port if len(entries) <= 64 else f"{port}{idx}"
                units.append((label, chunk))

        if misc:
            sorted_misc = self._sort_misc_pins(misc)
            for idx, chunk in enumerate(self._chunks(sorted_misc, 64), start=1):
                label = "SYS" if len(misc) <= 64 and idx == 1 else f"SYS{idx}"
                units.append((label, chunk))

        if not units:
            units.append(("UNIT1", []))
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

    def _partition_pins(
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

    def _place_pins(
        self,
        symbol,
        unit: int,
        pins: list[SymbolPinSpec],
    ) -> tuple[int, float]:
        left, right = self._partition_pins(pins)

        char_width = 0.6
        label_margin = 1.0
        left_label_len = max((len(spec.name) for spec in left), default=0)
        right_label_len = max((len(spec.name) for spec in right), default=0)
        label_width = max(left_label_len, right_label_len) * char_width + label_margin
        body_half = max(BODY_HALF_WIDTH, label_width)
        pin_offset = body_half + PIN_LENGTH / 2 + 0.5

        def place(column: list[SymbolPinSpec], x: float, rotation: int) -> None:
            if not column:
                return
            offset = (len(column) - 1) / 2
            for index, spec in enumerate(column):
                posy = (offset - index) * PIN_PITCH
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

        place(left, x=-pin_offset, rotation=0)
        place(right, x=pin_offset, rotation=180)
        return max(len(left), len(right), 1), body_half

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
