import sys
import unittest
from pathlib import Path


# Allow `import kicad_generator` without installing the project.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


from kicad_generator.symbols import SymbolGenerator, SymbolPinSpec  # noqa: E402
import kicad_generator.symbols as symbols  # noqa: E402


def make_misc_pin(number: int, subsystem: str | None) -> SymbolPinSpec:
    return SymbolPinSpec(
        number=str(number),
        name=f"PIN{number}",
        pad_type="passive",
        electrical_type="passive",
        pinmux=(),
        pad_name=f"X{number}",
        subsystem=subsystem,
    )


def make_port_pin(number: int, pad_name: str) -> SymbolPinSpec:
    return SymbolPinSpec(
        number=str(number),
        name=pad_name,
        pad_type="bidirectional",
        electrical_type="bidirectional",
        pinmux=(),
        pad_name=pad_name,
        subsystem=None,
    )


class TestSysSubsystemPacking(unittest.TestCase):
    def test_large_sys_packs_by_subsystem(self) -> None:
        gen = SymbolGenerator.__new__(SymbolGenerator)

        misc: list[SymbolPinSpec] = []
        counter = 1
        for subsystem, count in [
            ("power", 46),
            ("analog", 12),
            ("rf", 9),
            ("crystal", 4),
            ("audio", 9),
            ("mipi", 10),
            ("usb", 4),
            ("strapping", 1),
            (None, 1),
        ]:
            for _ in range(count):
                misc.append(make_misc_pin(counter, subsystem))
                counter += 1

        units = gen._group_sys_units(misc)
        self.assertEqual(
            [unit.name for unit in units],
            ["SYS1", "SYS2", "SYS3", "SYS4", "SYS5"],
        )
        self.assertEqual([len(unit.pins) for unit in units], [46, 12, 14, 23, 1])
        self.assertEqual({spec.subsystem for spec in units[0].pins}, {"power"})

        subsystem_seen: dict[str | None, str] = {}
        for unit in units:
            for subsystem in {spec.subsystem for spec in unit.pins}:
                if subsystem in subsystem_seen:
                    self.fail(f"subsystem {subsystem!r} appears in both {subsystem_seen[subsystem]} and {unit.name}")
                subsystem_seen[subsystem] = unit.name

    def test_sys_packing_respects_priority_levels(self) -> None:
        gen = SymbolGenerator.__new__(SymbolGenerator)
        original = symbols.SYS_SUBSYSTEM_PRIORITY_LEVELS
        symbols.SYS_SUBSYSTEM_PRIORITY_LEVELS = (
            ("power",),
            ("analog", "rf"),
            ("crystal",),
        )
        try:
            misc: list[SymbolPinSpec] = []
            counter = 1
            for subsystem, count in [
                ("power", 10),
                ("analog", 15),
                ("rf", 10),
                ("crystal", 6),
            ]:
                for _ in range(count):
                    misc.append(make_misc_pin(counter, subsystem))
                    counter += 1

            units = gen._group_sys_units(misc)
            self.assertEqual([unit.name for unit in units], ["SYS1", "SYS2", "SYS3"])
            self.assertEqual({spec.subsystem for spec in units[0].pins}, {"power"})
            self.assertEqual({spec.subsystem for spec in units[1].pins}, {"analog", "rf"})
            self.assertEqual({spec.subsystem for spec in units[2].pins}, {"crystal"})
            self.assertEqual(len(units[1].pins), 25)
            self.assertEqual(len(units[2].pins), 6)
        finally:
            symbols.SYS_SUBSYSTEM_PRIORITY_LEVELS = original

    def test_sys_packing_over_marker_forces_part_break(self) -> None:
        gen = SymbolGenerator.__new__(SymbolGenerator)
        original = symbols.SYS_SUBSYSTEM_PRIORITY_LEVELS
        symbols.SYS_SUBSYSTEM_PRIORITY_LEVELS = (
            ("analog", "rf", "over"),
            ("crystal",),
        )
        try:
            misc: list[SymbolPinSpec] = []
            counter = 1
            for subsystem, count in [
                ("analog", 15),
                ("rf", 15),
                ("crystal", 20),
            ]:
                for _ in range(count):
                    misc.append(make_misc_pin(counter, subsystem))
                    counter += 1

            units = gen._group_sys_units(misc)
            self.assertEqual([unit.name for unit in units], ["SYS1", "SYS2", "SYS3"])
            self.assertEqual({spec.subsystem for spec in units[0].pins}, {"analog"})
            self.assertEqual({spec.subsystem for spec in units[1].pins}, {"rf"})
            self.assertEqual({spec.subsystem for spec in units[2].pins}, {"crystal"})
            self.assertEqual([len(unit.pins) for unit in units], [15, 15, 20])
        finally:
            symbols.SYS_SUBSYSTEM_PRIORITY_LEVELS = original

    def test_small_sys_keeps_single_part(self) -> None:
        gen = SymbolGenerator.__new__(SymbolGenerator)
        misc = [make_misc_pin(i, "power" if i <= 20 else "analog") for i in range(1, 41)]
        units = gen._group_sys_units(misc)
        self.assertEqual(len(units), 1)
        self.assertEqual(units[0].name, "SYS")
        self.assertEqual(len(units[0].pins), 40)

    def test_sys_units_are_emitted_before_ports(self) -> None:
        gen = SymbolGenerator.__new__(SymbolGenerator)
        pins = [
            make_port_pin(1, "PA00"),
            make_misc_pin(2, "power"),
        ]
        units = gen._group_units(pins)
        self.assertEqual(units[0].name, "SYS")
        self.assertEqual(units[1].name, "PA")


if __name__ == "__main__":
    unittest.main()
