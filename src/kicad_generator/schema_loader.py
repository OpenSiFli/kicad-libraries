from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Sequence, Tuple

import yaml


@dataclass(frozen=True)
class PinmuxEntry:
    function: str
    select: int
    description: str | None = None
    notes: str | None = None


@dataclass(frozen=True)
class ChipPad:
    name: str
    type: str
    description: str | None
    notes: str | None
    pinmux: Tuple[PinmuxEntry, ...]


@dataclass(frozen=True)
class ChipVariantPin:
    number: str
    pads: Tuple[str, ...]
    description: str | None = None
    notes: str | None = None


@dataclass(frozen=True)
class ChipVariant:
    part_number: str
    package: str
    description: str | None
    pins: Tuple[ChipVariantPin, ...]
    pin_group_id: int | None = None


@dataclass(frozen=True)
class ChipSeries:
    model_id: str
    lifecycle: str
    docs: Tuple[Mapping[str, Any], ...]
    pads: Mapping[str, ChipPad]
    variants: Tuple[ChipVariant, ...]
    schema_version: str
    source_path: Path


class SiliconSchemaRepository:
    """Utility that loads chip data from the SiliconSchema repository layout."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.chips_dir = root / "chips"
        if not self.chips_dir.is_dir():
            msg = f"chips directory not found under {root}"
            raise FileNotFoundError(msg)

    def iter_series_paths(self) -> Iterator[Tuple[str, Path]]:
        for entry in sorted(self.chips_dir.iterdir()):
            if not entry.is_dir():
                continue
            series_file = entry / "series.yaml"
            if series_file.is_file():
                yield entry.name, series_file

    def load_series(self, allowed: Sequence[str] | None = None) -> List[ChipSeries]:
        allowed_set = {item.strip() for item in allowed or [] if item.strip()}
        series_list: list[ChipSeries] = []
        for model_id, path in self.iter_series_paths():
            if allowed_set and model_id not in allowed_set:
                continue
            series_list.append(self._load_series_file(path))
        return series_list

    def _load_series_file(self, path: Path) -> ChipSeries:
        with path.open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle)

        schema_version = str(raw["schema_version"])
        model_id = str(raw["model_id"])
        lifecycle = str(raw["lifecycle"])
        docs = tuple(raw.get("docs", ()))

        pads = self._parse_pads(raw["pads"])
        pad_lookup = {id(value): name for name, value in raw["pads"].items()}
        variants = self._parse_variants(raw["variants"], pad_lookup)

        return ChipSeries(
            model_id=model_id,
            lifecycle=lifecycle,
            docs=docs,
            pads=pads,
            variants=variants,
            schema_version=schema_version,
            source_path=path,
        )

    def _parse_pads(self, pads: Mapping[str, Mapping[str, Any]]) -> Mapping[str, ChipPad]:
        parsed: dict[str, ChipPad] = {}
        for name, payload in pads.items():
            pinmux_entries = tuple(
                PinmuxEntry(
                    function=item["function"],
                    select=int(item["select"]),
                    description=item.get("description"),
                    notes=item.get("notes"),
                )
                for item in payload.get("pinmux", []) or []
            )
            parsed[name] = ChipPad(
                name=name,
                type=payload["type"],
                description=payload.get("description"),
                notes=payload.get("notes"),
                pinmux=pinmux_entries,
            )
        return parsed

    def _parse_variants(
        self,
        variants: Iterable[Mapping[str, Any]],
        pad_lookup: Mapping[int, str],
    ) -> Tuple[ChipVariant, ...]:
        parsed_variants: list[ChipVariant] = []
        for variant in variants:
            pins_raw = variant["pins"]
            pins_data = tuple(self._parse_variant_pin(pin, pad_lookup) for pin in pins_raw)
            parsed_variants.append(
                ChipVariant(
                    part_number=str(variant["part_number"]),
                    package=str(variant["package"]),
                    description=variant.get("description"),
                    pins=pins_data,
                    pin_group_id=id(pins_raw),
                )
            )
        return tuple(parsed_variants)

    def _parse_variant_pin(
        self,
        pin: Mapping[str, Any],
        pad_lookup: Mapping[int, str],
    ) -> ChipVariantPin:
        pads = self._normalize_pad_refs(pin["pad"], pad_lookup)
        return ChipVariantPin(
            number=str(pin["number"]),
            pads=pads,
            description=pin.get("description"),
            notes=pin.get("notes"),
        )

    def _normalize_pad_refs(
        self,
        pad_value: Any,
        pad_lookup: Mapping[int, str],
    ) -> Tuple[str, ...]:
        if isinstance(pad_value, str):
            return (pad_value,)

        if isinstance(pad_value, list):
            pads: list[str] = []
            for entry in pad_value:
                pads.extend(self._normalize_pad_refs(entry, pad_lookup))
            return tuple(pads)

        if isinstance(pad_value, dict):
            pad_name = pad_lookup.get(id(pad_value))
            if not pad_name:
                msg = (
                    "Found inline pad definition that does not map to a named pad. "
                    "Please reference pads by name."
                )
                raise ValueError(msg)
            return (pad_name,)

        msg = f"Unsupported pad reference type: {type(pad_value)!r}"
        raise TypeError(msg)
