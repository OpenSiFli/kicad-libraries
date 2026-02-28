import sys
import tempfile
import unittest
from pathlib import Path
from types import MethodType


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
from kicad_generator.symbols import (  # noqa: E402
    SymbolGenerator,
    SymbolPinSpec,
    SysTemplate,
    SysTemplateUnit,
)


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


class TestSysTemplateExport(unittest.TestCase):
    def test_missing_sys_template_exports_suggestion(self) -> None:
        namespace = "PCM_SiFli_MOD"
        package = "DUMMY_PKG"

        pads = {
            "VSS": ChipPad(
                name="VSS",
                type="power_input",
                subsystem="power",
                description=None,
                notes=None,
                pinmux=(),
            ),
        }
        variant = ChipVariant(
            part_number="PN1",
            package=package,
            description=None,
            pins=(ChipVariantPin(number="1", pads=("VSS",)),),
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

            empty_template_dir = output_dir / "empty_sys_templates"
            empty_template_dir.mkdir(parents=True, exist_ok=True)
            gen.sys_template_dir = empty_template_dir

            footprints = dummy_footprints(namespace, package, output_dir)

            with self.assertLogs("kicad_generator.symbols", level="WARNING") as logs:
                gen.generate(series=(series,), footprints=footprints)

            suggestion = output_dir / "template" / "TEST__PN1.kicad_sym"
            self.assertTrue(suggestion.is_file())
            self.assertFalse((empty_template_dir / "TEST.kicad_sym").exists())
            self.assertTrue(
                any("Wrote SYS template suggestion" in message for message in logs.output),
                logs.output,
            )

    def test_multiple_pin_groups_skip_series_template_fallback(self) -> None:
        namespace = "PCM_SiFli_MOD"
        package = "DUMMY_PKG"

        pads = {
            "VSS": ChipPad(
                name="VSS",
                type="power_input",
                subsystem="power",
                description=None,
                notes=None,
                pinmux=(),
            ),
        }
        variant1 = ChipVariant(
            part_number="PN1",
            package=package,
            description=None,
            pins=(ChipVariantPin(number="1", pads=("VSS",)),),
            pin_group_id=1,
        )
        variant2 = ChipVariant(
            part_number="PN2",
            package=package,
            description=None,
            pins=(ChipVariantPin(number="1", pads=("VSS",)),),
            pin_group_id=2,
        )
        series = ChipSeries(
            model_id="TEST",
            lifecycle="production",
            docs=(),
            pads=pads,
            variants=(variant1, variant2),
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

            calls: list[str] = []

            def spy_load_sys_template(self, template_id: str, symbol_name_hint: str | None = None):
                calls.append(template_id)
                return None

            gen._load_sys_template = MethodType(spy_load_sys_template, gen)
            footprints = dummy_footprints(namespace, package, output_dir)
            with self.assertLogs("kicad_generator.symbols", level="WARNING"):
                gen.generate(series=(series,), footprints=footprints)

            self.assertNotIn("TEST", calls)
            self.assertTrue((output_dir / "template" / "TEST__PN1.kicad_sym").is_file())
            self.assertTrue((output_dir / "template" / "TEST__PN2.kicad_sym").is_file())

    def test_sys_template_power_pin_does_not_emit_alt_functions(self) -> None:
        namespace = "PCM_SiFli_MOD"

        with tempfile.TemporaryDirectory() as tmpdir:
            output_dir = Path(tmpdir)
            gen = SymbolGenerator(
                output_dir=output_dir,
                footprint_namespace=namespace,
                library_utils_root=ROOT / "kicad-library-utils",
            )

            power_pin = SymbolPinSpec(
                number="1",
                name="VDD",
                pad_type="power_input",
                electrical_type="power_in",
                pinmux=(
                    PinmuxEntry(function="VDD"),
                    PinmuxEntry(function="VDD_ALT"),
                ),
                pad_name="VDD",
                subsystem="power",
            )

            unit = SymbolGenerator.Unit(name="SYS", pins=[power_pin], pair_mode=False)
            template_pin = gen.Pin(
                name="VDD",
                number="1",
                etype="power_in",
                posx=0,
                posy=0,
                rotation=0,
                length=2.54,
                unit=1,
            )
            template_unit = SysTemplateUnit(
                name="SYS",
                pins=(template_pin,),
                rectangles=(),
                circles=(),
                arcs=(),
                polylines=(),
                beziers=(),
                texts=(),
            )
            template = SysTemplate(template_id="TEST", path=output_dir / "TEST.kicad_sym", units={"SYS": template_unit})

            symbol = gen.KicadSymbol.new("TEST", gen.library_name)
            applied, mismatch = gen._apply_sys_template_unit(
                symbol=symbol,
                unit_index=1,
                unit=unit,
                template=template,
            )

            self.assertTrue(applied)
            self.assertFalse(mismatch)
            self.assertEqual(len(symbol.pins), 1)
            self.assertEqual(len(symbol.pins[0].altfuncs), 0)


if __name__ == "__main__":
    unittest.main()
