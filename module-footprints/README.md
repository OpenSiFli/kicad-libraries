# Manual Module Footprints

This directory stores manually maintained KiCad module footprints.

Expected layout:

```text
module-footprints/
└── <library>.pretty/
    └── <package>.kicad_mod
```

The generator no longer builds module footprints from YAML. Instead, it copies
matching `.kicad_mod` files from this directory into the output footprint tree
and records them in the footprint manifest for symbol generation and release.
