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
)
from kicad_generator.symbols import SymbolGenerator  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()

