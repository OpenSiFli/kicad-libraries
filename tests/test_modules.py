import sys
import tempfile
import unittest
from pathlib import Path


# Allow `import kicad_generator` without installing the project.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


from kicad_generator.footprint_loader import FootprintLibrary  # noqa: E402
from kicad_generator.footprints import FootprintGenerator  # noqa: E402
from kicad_generator.module_loader import ModuleLibrary  # noqa: E402
from kicad_generator.schema_loader import SiliconSchemaRepository  # noqa: E402


class TestModuleLibrary(unittest.TestCase):
    def _write_yaml(self, path: Path, payload: object) -> None:
        import yaml

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )

    def test_module_to_chip_series_merges_include_and_local(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            silicon_root = tmp_path / "SiliconSchema"
            (silicon_root / "chips").mkdir(parents=True, exist_ok=True)
            series_dir = silicon_root / "out" / "SF32LB52x"
            series_yaml = {
                "schema_version": "1",
                "lifecycle": "production",
                "docs": [],
                "pads": {
                    "PA44": {
                        "type": "bidirectional",
                        "description": None,
                        "notes": None,
                        "functions": ["GPIO_A44"],
                    }
                },
                "variants": [],
            }
            self._write_yaml(series_dir / "series.yaml", series_yaml)

            modules_root = tmp_path / "modules"
            module_dir = modules_root / "SF32LB52-MOD-1"
            self._write_yaml(
                module_dir / "module.yml",
                {
                    "schema_version": 1,
                    "module_id": "SF32LB52-MOD-1",
                    "docs": {"datasheet": {"en": "var/DS5203.pdf"}},
                    "includes": {"soc": {"kind": "silicon_schema", "series": "SF32LB52x"}},
                    "variants": [
                        {
                            "part_number": "SF32LB52-MOD-1",
                            "package": "SF32LB52-MOD-1",
                            "pins_file": "pins.yml",
                        }
                    ],
                },
            )
            self._write_yaml(
                module_dir / "pins.yml",
                {
                    "pads": {
                        "VBATS": {
                            "type": "power_input",
                            "subsystem": "power",
                            "functions": ["VBATS"],
                        },
                    },
                    "pins": [
                        {
                            "number": "2",
                            "pad": {"include": "soc", "name": "PA44"},
                            "name": "PA44_DPI_R0",
                        },
                        {"number": "61", "pad": "VBATS"},
                    ],
                },
            )

            lib = ModuleLibrary.from_directory(modules_root)
            repo = SiliconSchemaRepository(silicon_root)
            series = lib.to_chip_series(repo)
            self.assertEqual(len(series), 1)
            module_series = series[0]
            self.assertEqual(module_series.model_id, "SF32LB52-MOD-1")
            self.assertIn("PA44", module_series.pads)
            self.assertIn("VBATS", module_series.pads)
            self.assertEqual(module_series.pads["PA44"].pinmux[0].function, "GPIO_A44")
            self.assertEqual(module_series.pads["VBATS"].pinmux[0].function, "VBATS")
            self.assertEqual(module_series.pads["VBATS"].subsystem, "power")

            variant = module_series.variants[0]
            self.assertEqual(variant.package, "SF32LB52-MOD-1")
            pin2 = next(pin for pin in variant.pins if pin.number == "2")
            self.assertEqual(pin2.pads, ("PA44",))
            self.assertEqual(pin2.description, "PA44_DPI_R0")

    def test_manual_module_footprint_is_copied_into_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)

            silicon_root = tmp_path / "SiliconSchema"
            (silicon_root / "chips").mkdir(parents=True, exist_ok=True)

            modules_root = tmp_path / "modules"
            module_dir = modules_root / "TEST-MOD"
            self._write_yaml(
                module_dir / "module.yml",
                {
                    "schema_version": 1,
                    "module_id": "TEST-MOD",
                    "variants": [
                        {
                            "part_number": "TEST-MOD",
                            "package": "TEST-MOD",
                            "pins_file": "pins.yml",
                        }
                    ],
                },
            )
            self._write_yaml(
                module_dir / "pins.yml",
                {
                    "pads": {"GND": {"type": "power_input", "functions": ["GND"]}},
                    "pins": [{"number": "1", "pad": "GND"}],
                },
            )

            manual_root = tmp_path / "module-footprints" / "SiFli_MOD.pretty"
            manual_root.mkdir(parents=True, exist_ok=True)
            manual_file = manual_root / "TEST-MOD.kicad_mod"
            manual_content = '(footprint "TEST-MOD"\n\t(version 20241229)\n\t(layer "F.Cu")\n)\n'
            manual_file.write_text(manual_content, encoding="utf-8")

            lib = ModuleLibrary.from_directory(modules_root)
            repo = SiliconSchemaRepository(silicon_root)
            series = lib.to_chip_series(repo)

            generator = FootprintGenerator(
                output_dir=tmp_path / "out",
                namespace="SiFli_MOD",
                footprint_repo=ROOT / "kicad-footprint-generator",
                module_footprint_dir=tmp_path / "module-footprints",
            )
            result = generator.generate(
                series=series,
                library=FootprintLibrary({}),
                module_library=lib,
            )

            artifact = result.footprint_for_package("TEST-MOD")
            self.assertIsNotNone(artifact)
            assert artifact is not None
            self.assertTrue(artifact.path.is_file())
            self.assertEqual(artifact.path.read_text(encoding="utf-8"), manual_content)
            self.assertEqual(artifact.library, "SiFli_MOD")
            self.assertEqual(artifact.qualified_name, "PCM_SiFli_MOD:TEST-MOD")
            self.assertEqual(result.missing, [])

    def test_repository_manual_module_footprints_exist_for_all_modules(self) -> None:
        modules_root = ROOT / "modules"
        if not modules_root.is_dir():
            self.skipTest("Repository does not ship module YAML definitions.")

        manual_root = ROOT / "module-footprints"
        if not manual_root.is_dir():
            self.skipTest("Repository does not ship manual module footprints.")

        lib = ModuleLibrary.from_directory(modules_root)
        missing: list[str] = []
        for module in lib.modules():
            for variant in module.variants:
                expected = manual_root / "SiFli_MOD.pretty" / f"{variant.package}.kicad_mod"
                if not expected.is_file():
                    missing.append(str(expected))

        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
