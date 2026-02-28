from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import yaml

from .module_loader import ModuleDefinition, ModuleVariantDefinition
from .upstream import ensure_footprint_repo_on_sys_path

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ModuleFootprintArtifact:
    """Result of generating a module footprint."""

    name: str
    path: Path


def _as_xy(value: Any, *, source: Path, field: str) -> tuple[float, float]:
    if not isinstance(value, Sequence) or len(value) != 2:
        raise ValueError(f"Expected {field} to be a 2-item sequence in {source}")
    return float(value[0]), float(value[1])


def _as_size(value: Any, *, source: Path, field: str) -> tuple[float, float]:
    return _as_xy(value, source=source, field=field)


def _flatten_numbers_from_groups(groups: Sequence[Mapping[str, Any]]) -> set[str]:
    numbers: set[str] = set()
    for group in groups:
        kind = str(group.get("kind", "")).lower()
        if kind == "row":
            for item in group.get("numbers") or []:
                numbers.add(str(item))
        elif kind == "grid":
            for row in group.get("numbers_matrix") or []:
                for item in row or []:
                    numbers.add(str(item))
        elif kind == "single":
            number = group.get("number")
            if number is not None:
                numbers.add(str(number))
    return numbers


class ModuleFootprintGenerator:
    """Generates module footprints using KicadModTree primitives."""

    def __init__(self, footprint_repo: Path) -> None:
        self.footprint_repo = footprint_repo
        ensure_footprint_repo_on_sys_path(footprint_repo)
        # Import lazily after sys.path manipulation.
        from KicadModTree import (  # type: ignore
            Circle,
            Footprint,
            FootprintType,
            Hatch,
            Keepouts,
            KicadFileHandler,
            Pad,
            Property,
            Rectangle,
            Zone,
        )

        self.Circle = Circle
        self.Footprint = Footprint
        self.FootprintType = FootprintType
        self.Hatch = Hatch
        self.Keepouts = Keepouts
        self.KicadFileHandler = KicadFileHandler
        self.Pad = Pad
        self.Property = Property
        self.Rectangle = Rectangle
        self.Zone = Zone

        self._spec_cache: dict[Path, Mapping[str, Any]] = {}

    def generate(
        self,
        *,
        output_dir: Path,
        namespace: str,
        module: ModuleDefinition,
        variant: ModuleVariantDefinition,
    ) -> ModuleFootprintArtifact:
        """Generate a module footprint and return the written artifact.

        Args:
            output_dir: Generator output directory (root).
            namespace: Footprint namespace/library nickname.
            module: Module definition that provides pins/paths.
            variant: Module variant which provides the package name.

        Returns:
            The generated footprint artifact (name + .kicad_mod path).
        """

        spec = self._load_spec(module.footprint_file)
        footprint_payload = spec.get("footprint") or {}
        if not isinstance(footprint_payload, Mapping):
            raise ValueError(f"Module footprint spec must contain a mapping footprint in {module.footprint_file}")

        footprint_name = str(footprint_payload.get("name") or variant.package)

        pins = module.pins_by_variant.get(variant.part_number) or ()
        expected_numbers = {pin.number for pin in pins}
        groups = footprint_payload.get("pad_groups") or []
        if not isinstance(groups, list):
            raise ValueError(f"footprint.pad_groups must be a list in {module.footprint_file}")
        spec_numbers = _flatten_numbers_from_groups(groups)
        if expected_numbers != spec_numbers:
            missing = sorted(expected_numbers - spec_numbers)
            extra = sorted(spec_numbers - expected_numbers)
            raise ValueError(
                "Module footprint pad numbers do not match pins.yml for "
                f"{module.module_id}/{variant.part_number}. Missing={missing} Extra={extra}"
            )

        footprints_root = output_dir / "footprints"
        library_dir = footprints_root / f"{namespace}.pretty"
        library_dir.mkdir(parents=True, exist_ok=True)
        path = library_dir / f"{footprint_name}.kicad_mod"

        kicad_mod = self.Footprint(footprint_name, self.FootprintType.SMD)
        kicad_mod.setDescription(str(footprint_payload.get("description") or ""))
        tags = footprint_payload.get("tags")
        if isinstance(tags, str):
            kicad_mod.setTags(tags)

        self._append_properties(kicad_mod, footprint_name, footprint_payload)
        self._append_body(kicad_mod, footprint_payload, module.footprint_file)
        self._append_pin1_marker(kicad_mod, footprint_payload, module.footprint_file)
        self._append_keepouts(kicad_mod, footprint_payload, module.footprint_file)
        self._append_pads(kicad_mod, groups, module.footprint_file)

        handler = self.KicadFileHandler(kicad_mod)
        handler.writeFile(path)
        return ModuleFootprintArtifact(name=footprint_name, path=path)

    def _load_spec(self, path: Path) -> Mapping[str, Any]:
        cached = self._spec_cache.get(path)
        if cached is not None:
            return cached
        if not path.is_file():
            raise FileNotFoundError(f"Module footprint file not found: {path}")
        with path.open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle) or {}
        if not isinstance(raw, Mapping):
            raise ValueError(f"Module footprint YAML must be a mapping: {path}")
        self._spec_cache[path] = raw
        return raw

    def _append_properties(self, kicad_mod, footprint_name: str, payload: Mapping[str, Any]) -> None:
        body = payload.get("body") if isinstance(payload.get("body"), Mapping) else {}
        width = float(body.get("width", 0.0) or 0.0)
        height = float(body.get("height", 0.0) or 0.0)
        y = -(height / 2.0 + 1.5) if height else -3.0

        ref = self.Property(
            name=self.Property.REFERENCE,
            text="REF**",
            layer="F.SilkS",
            at=[0, y],
            hide=True,
        )
        val = self.Property(
            name=self.Property.VALUE,
            text=footprint_name,
            layer="F.Fab",
            at=[0, -y],
            hide=True,
        )
        kicad_mod.append(ref)
        kicad_mod.append(val)

    def _append_body(self, kicad_mod, payload: Mapping[str, Any], source: Path) -> None:
        body = payload.get("body")
        if body is None:
            return
        if not isinstance(body, Mapping):
            raise ValueError(f"footprint.body must be a mapping in {source}")

        width = float(body.get("width", 0.0))
        height = float(body.get("height", 0.0))
        if width <= 0 or height <= 0:
            raise ValueError(f"footprint.body.width/height must be positive in {source}")

        fab_w = float(body.get("fab_line_width", 0.1))
        silk_w = float(body.get("silk_line_width", 0.12))
        crt_w = float(body.get("courtyard_line_width", 0.05))
        courtyard_margin = float(payload.get("courtyard_margin", 0.25))

        kicad_mod.append(
            self.Rectangle(
                layer="F.Fab",
                width=fab_w,
                center=[0, 0],
                size=[width, height],
            )
        )
        kicad_mod.append(
            self.Rectangle(
                layer="F.SilkS",
                width=silk_w,
                center=[0, 0],
                size=[width, height],
            )
        )
        kicad_mod.append(
            self.Rectangle(
                layer="F.CrtYd",
                width=crt_w,
                center=[0, 0],
                size=[width + 2 * courtyard_margin, height + 2 * courtyard_margin],
            )
        )

    def _append_pin1_marker(self, kicad_mod, payload: Mapping[str, Any], source: Path) -> None:
        marker = payload.get("pin1_marker")
        if marker is None:
            return
        if not isinstance(marker, Mapping):
            raise ValueError(f"footprint.pin1_marker must be a mapping in {source}")
        kind = str(marker.get("kind", "circle")).lower()
        if kind != "circle":
            raise ValueError(f"Unsupported pin1_marker kind {kind!r} in {source}")
        center = _as_xy(marker.get("at"), source=source, field="pin1_marker.at")
        diameter = float(marker.get("diameter", 1.0))
        line_width = float(marker.get("line_width", 0.12))
        layer = str(marker.get("layer", "F.SilkS"))
        kicad_mod.append(
            self.Circle(
                layer=layer,
                width=line_width,
                center=[center[0], center[1]],
                radius=diameter / 2.0,
            )
        )

    def _append_keepouts(self, kicad_mod, payload: Mapping[str, Any], source: Path) -> None:
        keepouts = payload.get("keepouts")
        if not keepouts:
            return
        if not isinstance(keepouts, list):
            raise ValueError(f"footprint.keepouts must be a list in {source}")

        for item in keepouts:
            if not isinstance(item, Mapping):
                raise ValueError(f"keepout entry must be a mapping in {source}")
            shape = str(item.get("shape", "rect")).lower()
            if shape != "rect":
                raise ValueError(f"Unsupported keepout shape {shape!r} in {source}")
            start = _as_xy(item.get("start"), source=source, field="keepout.start")
            end = _as_xy(item.get("end"), source=source, field="keepout.end")
            layers_raw = item.get("layers") or ["*.Cu"]
            if isinstance(layers_raw, str):
                layers = [layers_raw]
            elif isinstance(layers_raw, list) and all(isinstance(v, str) for v in layers_raw):
                layers = list(layers_raw)
            else:
                raise ValueError(f"keepout.layers must be a string or list of strings in {source}")

            rules = item.get("rules") or {}
            if rules and not isinstance(rules, Mapping):
                raise ValueError(f"keepout.rules must be a mapping in {source}")

            def rule(name: str) -> bool:
                value = str(rules.get(name, "deny")).lower()
                if value in {"deny", "not_allowed"}:
                    return self.Keepouts.DENY
                if value in {"allow", "allowed"}:
                    return self.Keepouts.ALLOW
                raise ValueError(f"Unsupported keepout rule value {value!r} for {name} in {source}")

            ko = self.Keepouts(
                tracks=rule("tracks"),
                vias=rule("vias"),
                copperpour=rule("copperpour"),
                pads=rule("pads"),
                footprints=rule("footprints"),
            )

            x1, y1 = start
            x2, y2 = end
            points = [[x1, y1], [x2, y1], [x2, y2], [x1, y2]]
            zone = self.Zone(
                shape=points,
                layers=layers,
                hatch=self.Hatch(self.Hatch.EDGE, 0.5),
                net=0,
                net_name="",
                name=str(item.get("name") or ""),
                keepouts=ko,
            )
            kicad_mod.append(zone)

    def _append_pads(self, kicad_mod, groups: Sequence[Mapping[str, Any]], source: Path) -> None:
        pads: list[tuple[str, float, float, Mapping[str, Any]]] = []
        seen_numbers: set[str] = set()
        for group in groups:
            if not isinstance(group, Mapping):
                raise ValueError(f"pad_groups entry must be a mapping in {source}")
            kind = str(group.get("kind", "")).lower()
            pad_style = group.get("pad") or {}
            if not isinstance(pad_style, Mapping):
                raise ValueError(f"pad_groups.pad must be a mapping in {source}")

            if kind == "row":
                numbers = group.get("numbers") or []
                start = _as_xy(group.get("start"), source=source, field="pad_groups.row.start")
                pitch = _as_xy(group.get("pitch"), source=source, field="pad_groups.row.pitch")
                for idx, number in enumerate(numbers):
                    num = str(number)
                    if num in seen_numbers:
                        raise ValueError(f"Duplicate pad number {num} in {source}")
                    seen_numbers.add(num)
                    x = start[0] + idx * pitch[0]
                    y = start[1] + idx * pitch[1]
                    pads.append((num, x, y, pad_style))
                continue

            if kind == "grid":
                matrix = group.get("numbers_matrix") or []
                origin = _as_xy(group.get("origin"), source=source, field="pad_groups.grid.origin")
                pitch = _as_xy(group.get("pitch"), source=source, field="pad_groups.grid.pitch")
                for row_idx, row in enumerate(matrix):
                    if not isinstance(row, list):
                        raise ValueError(f"numbers_matrix rows must be lists in {source}")
                    for col_idx, number in enumerate(row):
                        num = str(number)
                        if num in seen_numbers:
                            raise ValueError(f"Duplicate pad number {num} in {source}")
                        seen_numbers.add(num)
                        x = origin[0] + col_idx * pitch[0]
                        y = origin[1] + row_idx * pitch[1]
                        pads.append((num, x, y, pad_style))
                continue

            if kind == "single":
                number = group.get("number")
                at = _as_xy(group.get("at"), source=source, field="pad_groups.single.at")
                num = str(number)
                if num in seen_numbers:
                    raise ValueError(f"Duplicate pad number {num} in {source}")
                seen_numbers.add(num)
                pads.append((num, at[0], at[1], pad_style))
                continue

            raise ValueError(f"Unsupported pad group kind {kind!r} in {source}")

        for number, x, y, style in pads:
            size = _as_size(style.get("size"), source=source, field="pad.size")
            rotation = float(style.get("rotation", 0.0))
            shape = str(style.get("shape", self.Pad.SHAPE_RECT))
            layers = style.get("layers")
            if layers is None:
                layer_list = self.Pad.LAYERS_SMT
            elif isinstance(layers, list) and all(isinstance(v, str) for v in layers):
                layer_list = list(layers)
            else:
                raise ValueError(f"pad.layers must be a list of strings in {source}")

            kicad_mod.append(
                self.Pad(
                    number=number,
                    type=self.Pad.TYPE_SMT,
                    shape=shape,
                    at=[x, y],
                    size=[size[0], size[1]],
                    rotation=rotation,
                    layers=layer_list,
                )
            )


def iter_module_pad_numbers(
    module: ModuleDefinition, variant: ModuleVariantDefinition
) -> Iterable[str]:
    """Iterate over pad numbers declared in the module pins list."""

    pins = module.pins_by_variant.get(variant.part_number) or ()
    for pin in pins:
        yield pin.number
