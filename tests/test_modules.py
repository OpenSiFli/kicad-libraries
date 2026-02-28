import sys
import tempfile
import unittest
from pathlib import Path


# Allow `import kicad_generator` without installing the project.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


from kicad_generator.module_footprints import ModuleFootprintGenerator  # noqa: E402
from kicad_generator.module_loader import ModuleLibrary  # noqa: E402
from kicad_generator.schema_loader import SiliconSchemaRepository  # noqa: E402


class TestModuleLibrary(unittest.TestCase):
    def _write_yaml(self, path: Path, payload: object) -> None:
        import yaml

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8")

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
                    "footprint_file": "footprint.yml",
                },
            )
            self._write_yaml(
                module_dir / "pins.yml",
                {
                    "pads": {
                        "VBATS": {"type": "power_input", "functions": ["VBATS"]},
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

            variant = module_series.variants[0]
            self.assertEqual(variant.package, "SF32LB52-MOD-1")
            pin2 = next(pin for pin in variant.pins if pin.number == "2")
            self.assertEqual(pin2.pads, ("PA44",))
            self.assertEqual(pin2.description, "PA44_DPI_R0")

    def test_module_footprint_generates_keepout_zone(self) -> None:
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
                    "footprint_file": "footprint.yml",
                },
            )
            self._write_yaml(
                module_dir / "pins.yml",
                {
                    "pads": {"GND": {"type": "power_input", "functions": ["GND"]}},
                    "pins": [{"number": "1", "pad": "GND"}],
                },
            )
            self._write_yaml(
                module_dir / "footprint.yml",
                {
                    "footprint": {
                        "name": "TEST-MOD",
                        "courtyard_margin": 0.25,
                        "body": {"width": 10.0, "height": 10.0},
                        "keepouts": [
                            {
                                "name": "Antenna",
                                "layers": ["*.Cu"],
                                "shape": "rect",
                                "start": [-2.0, 2.0],
                                "end": [2.0, 4.0],
                                "rules": {
                                    "tracks": "deny",
                                    "vias": "deny",
                                    "pads": "deny",
                                    "copperpour": "deny",
                                    "footprints": "deny",
                                },
                            }
                        ],
                        "pad_groups": [
                            {
                                "kind": "single",
                                "number": "1",
                                "at": [0.0, 0.0],
                                "pad": {"size": [1.0, 1.0], "shape": "rect"},
                            }
                        ],
                    }
                },
            )

            lib = ModuleLibrary.from_directory(modules_root)
            entry = lib.package_entry("TEST-MOD")
            self.assertIsNotNone(entry)
            assert entry is not None
            module, variant = entry

            generator = ModuleFootprintGenerator(ROOT / "kicad-footprint-generator")
            artifact = generator.generate(
                output_dir=tmp_path / "out",
                namespace="PCM_SiFli_MOD",
                module=module,
                variant=variant,
            )
            self.assertTrue(artifact.path.is_file())
            content = artifact.path.read_text(encoding="utf-8")
            self.assertIn("(zone", content)
            self.assertIn("(keepout", content)
            self.assertIn('layers "*.Cu"', content)
            self.assertIn("(tracks not_allowed)", content)
            self.assertIn("(vias not_allowed)", content)

    def test_repository_module_specs_generate(self) -> None:
        modules_root = ROOT / "modules"
        if not modules_root.is_dir():
            self.skipTest("Repository does not ship module YAML definitions.")

        lib = ModuleLibrary.from_directory(modules_root)
        generator = ModuleFootprintGenerator(ROOT / "kicad-footprint-generator")

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            found_any = False
            for module in lib.modules():
                for variant in module.variants:
                    found_any = True
                    artifact = generator.generate(
                        output_dir=tmp_path / "out",
                        namespace="PCM_SiFli_MOD",
                        module=module,
                        variant=variant,
                    )
                    self.assertTrue(artifact.path.is_file())
                    content = artifact.path.read_text(encoding="utf-8")
                    self.assertIn("(footprint", content)

            if not found_any:
                self.skipTest("No module variants found in repository modules directory.")


if __name__ == "__main__":
    unittest.main()
