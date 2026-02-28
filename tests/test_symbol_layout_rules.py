import sys
import tempfile
import unittest
from pathlib import Path


# Allow `import kicad_generator` without installing the project.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


from kicad_generator.footprints import FootprintArtifact, FootprintGenerationResult  # noqa: E402
from kicad_generator.schema_loader import (  # noqa: E402
    ChipPad,
    ChipSeries,
    ChipVariant,
    ChipVariantPin,
    PinmuxEntry,
)
from kicad_generator.symbols import SymbolGenerator, SymbolPinSpec  # noqa: E402
import kicad_generator.symbols as symbols  # noqa: E402


def dummy_footprints(namespace: str, package: str, output_dir: Path) -> FootprintGenerationResult:
    artifact = FootprintArtifact(
        name="DUMMY",
        namespace=namespace,
        library="DUMMY",
        path=output_dir / "dummy.kicad_mod",
        package=package,
    )
    return FootprintGenerationResult(
        namespace=namespace,
        artifacts={"DUMMY": artifact},
        missing=(),
        manifest_path=None,
        package_map={package: "DUMMY"},
    )


class TestSymbolLayoutRules(unittest.TestCase):
    def test_symbol_has_ki_locked_property(self) -> None:
        namespace = "PCM_SiFli_MOD"
        package = "DUMMY_PKG"
        pads = {
            "PA00": ChipPad(
                name="PA00",
                type="bidirectional",
                subsystem=None,
                description=None,
                notes=None,
                pinmux=(),
            ),
            "PB00": ChipPad(
                name="PB00",
                type="bidirectional",
                subsystem=None,
                description=None,
                notes=None,
                pinmux=(),
            ),
        }
        variant = ChipVariant(
            part_number="PN1",
            package=package,
            description=None,
            pins=(
                ChipVariantPin(number="1", pads=("PA00",)),
                ChipVariantPin(number="2", pads=("PB00",)),
            ),
            pin_group_id=None,
        )
        series = ChipSeries(
            model_id="TEST",
            lifecycle="production",
            docs=(),
            pads=pads,
            variants=(variant,),
            schema_version="0",
            source_path=Path("series.yaml"),
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            gen = SymbolGenerator(
                output_dir=output_dir,
                footprint_namespace=namespace,
                library_utils_root=ROOT / "kicad-library-utils",
            )
            footprints = dummy_footprints(namespace, package, output_dir)
            gen.generate(series=(series,), footprints=footprints)

            library_path = output_dir / "symbols" / "libs" / f"{gen.library_name}.kicad_sym"
            library = gen.KicadLibrary.from_file(str(library_path))
            symbol = next(sym for sym in library.symbols if sym.name == "PN1")
            self.assertIsNotNone(symbol.get_property("ki_locked"))

    def test_pin_length_scales_with_pin_number_length(self) -> None:
        namespace = "PCM_SiFli_MOD"

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            gen = SymbolGenerator(
                output_dir=output_dir,
                footprint_namespace=namespace,
                library_utils_root=ROOT / "kicad-library-utils",
            )

            def make_pin(number: str) -> SymbolPinSpec:
                return SymbolPinSpec(
                    number=number,
                    name=f"PIN{number}",
                    pad_type="input",
                    electrical_type="input",
                    pinmux=(),
                    pad_name=f"X{number}",
                    subsystem=None,
                )

            sym = gen.KicadSymbol.new("TEST", gen.library_name)
            gen._place_pins(sym, unit=1, pins=[make_pin("1"), make_pin("100")], pair_mode=False)
            self.assertTrue(sym.pins)
            for pin in sym.pins:
                self.assertAlmostEqual(pin.length, 3 * symbols.GRID, places=6)

            sym2 = gen.KicadSymbol.new("TEST2", gen.library_name)
            gen._place_pins(sym2, unit=1, pins=[make_pin("ABCDEFG")], pair_mode=False)
            self.assertTrue(sym2.pins)
            for pin in sym2.pins:
                self.assertAlmostEqual(pin.length, 6 * symbols.GRID, places=6)

    def test_port_unit_width_accounts_for_alt_functions_both_sides(self) -> None:
        namespace = "PCM_SiFli_MOD"

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            gen = SymbolGenerator(
                output_dir=output_dir,
                footprint_namespace=namespace,
                library_utils_root=ROOT / "kicad-library-utils",
            )

            def make_port_pin(number: str, pad_name: str, alt: str) -> SymbolPinSpec:
                return SymbolPinSpec(
                    number=number,
                    name=pad_name,
                    pad_type="bidirectional",
                    electrical_type="bidirectional",
                    pinmux=(PinmuxEntry(function=alt),),
                    pad_name=pad_name,
                    subsystem=None,
                )

            pins = [
                make_port_pin("1", "PA00", "RIGHT_SIDE_SUPER_LONG_ALT"),
                make_port_pin("2", "PA01", "ALT"),
                make_port_pin("3", "PA02", "ALT"),
                make_port_pin("4", "PA03", "LEFT_SIDE_LONGER_ALT_FUNC"),
            ]
            sym = gen.KicadSymbol.new("TEST", gen.library_name)
            _, body_half = gen._place_pins(sym, unit=1, pins=pins, pair_mode=True)

            char_width = 0.75
            label_margin = 1.5
            base_half = max(symbols.BODY_HALF_WIDTH, symbols.PIN_PITCH)
            left_label_len = len("LEFT_SIDE_LONGER_ALT_FUNC")
            right_label_len = len("RIGHT_SIDE_SUPER_LONG_ALT")
            left_extra = left_label_len * char_width + label_margin
            right_extra = right_label_len * char_width + label_margin
            expected = gen._snap(max(base_half, base_half + (left_extra + right_extra) / 2))
            self.assertAlmostEqual(body_half, expected, places=6)


if __name__ == "__main__":
    unittest.main()
