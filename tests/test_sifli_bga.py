import sys
import unittest
from pathlib import Path


# Allow `import kicad_generator` without installing the project.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


from kicad_generator.footprints import (  # noqa: E402
    bga_row_names,
    infer_sifli_bga_pad_skips,
    infer_sifli_bga_present_balls,
    parse_sifli_bga_package_name,
)
from kicad_generator.schema_loader import (  # noqa: E402
    ChipPad,
    ChipSeries,
    ChipVariant,
    ChipVariantPin,
    PinmuxEntry,
)


class TestSifliBgaParsing(unittest.TestCase):
    def test_parse_package_name(self) -> None:
        pkg = parse_sifli_bga_package_name(
            "SiFli_BGA-175_6.5x6.1mm_Layout16x15_P0.4mm"
        )
        self.assertEqual(pkg.ball_count, 175)
        self.assertEqual(pkg.body_size_x, 6.5)
        self.assertEqual(pkg.body_size_y, 6.1)
        self.assertEqual(pkg.layout_x, 16)
        self.assertEqual(pkg.layout_y, 15)
        self.assertEqual(pkg.pitch, 0.4)


class TestBgaRows(unittest.TestCase):
    def test_row_names_skip_letters(self) -> None:
        rows = bga_row_names(15)
        # A..H then J (I skipped)
        self.assertEqual(rows[0], "A")
        self.assertEqual(rows[7], "H")
        self.assertEqual(rows[8], "J")

    def test_row_names_multi_letter(self) -> None:
        rows = bga_row_names(21)
        self.assertEqual(rows[19], "Y")
        self.assertEqual(rows[20], "AA")


class TestBgaPadSkips(unittest.TestCase):
    def test_infer_pad_skips(self) -> None:
        pkg = parse_sifli_bga_package_name("SiFli_BGA-12_4x4mm_Layout4x4_P0.4mm")
        present = {
            "A1",
            "A2",
            "A3",
            "A4",
            "B1",
            "B4",
            "C1",
            "C4",
            "D1",
            "D2",
            "D3",
            "D4",
        }
        skips = infer_sifli_bga_pad_skips(pkg, present)
        self.assertEqual(skips, ["B2", "B3", "C2", "C3"])


class TestBgaVariantConsistency(unittest.TestCase):
    def test_present_balls_mismatch_raises(self) -> None:
        pads = {
            "VSS": ChipPad(
                name="VSS",
                type="power_input",
                subsystem=None,
                description=None,
                notes=None,
                pinmux=(),
            ),
        }
        variant1 = ChipVariant(
            part_number="PN1",
            package="SiFli_BGA-2_1x1mm_Layout2x1_P0.4mm",
            description=None,
            pins=(
                ChipVariantPin(number="A1", pads=("VSS",)),
                ChipVariantPin(number="A2", pads=("VSS",)),
            ),
            pin_group_id=None,
        )
        variant2 = ChipVariant(
            part_number="PN2",
            package="SiFli_BGA-2_1x1mm_Layout2x1_P0.4mm",
            description=None,
            pins=(
                ChipVariantPin(number="A1", pads=("VSS",)),
                ChipVariantPin(number="B2", pads=("VSS",)),
            ),
            pin_group_id=None,
        )
        series = (
            ChipSeries(
                model_id="TEST",
                lifecycle="production",
                docs=(),
                pads=pads,
                variants=(variant1, variant2),
                schema_version="0",
                source_path=Path("series.yaml"),
            ),
        )
        with self.assertRaises(ValueError):
            infer_sifli_bga_present_balls(series, "SiFli_BGA-2_1x1mm_Layout2x1_P0.4mm")


if __name__ == "__main__":
    unittest.main()
