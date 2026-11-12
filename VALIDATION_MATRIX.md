# Cross-board validation matrix — HARP-PCB v0.5.1

The cross-board suite is synthetic and deliberately unrelated to the one PCB supplied by the user.
It is **not proof of universal KiCad compatibility** and does not include formal KiCad DRC.

| Input family | Result |
| --- | --- |
| 1 footprint | Proper strict JSON and valid placement (no pairwise gap available) |
| 2–48 footprints, eight distinct seeds | Passed; no protected bounding-box collisions; pad and net structure retained |
| Overlapping and already positioned initial inputs | Re-placed from scratch |
| Big/small, high-aspect-ratio, through-hole, front/back parts | Passed using conservative 2D envelopes |
| Missing courtyards | Pads used as a fallback geometric envelope |
| Graphic-only footprint with measurable geometry | Preserved and placed |
| KiCad 6/7 style fp_text reference and value | Recognized and preserved in synthetic fixture; NOT validated in a real KiCad installation |
| KiCad 8/9 style property-based references | Existing regression coverage |
| KiCad 4/5 (module ...) | Explicitly rejected instead of silently omitted |
| Locked footprints | Explicitly rejected instead of moving mechanically anchored parts |
| Text-only, no-pad footprint without measurable geometry | Explicitly rejected |
| Routed boards, copper zones, complex outlines | Previously tested rejection; NOT automatically supported |
| Directional I/O, explicit RF keepouts, synthetic footprints | Covered by existing regression tests; production accuracy not certified |

Tests run under pytest on Python 3.10, 3.12, 3.13 in the GitHub Actions CI matrix.

**Known limits:** no official KiCad CLI validation here, no routing, no manufacturer's package/pinout
certification, no 3D access verification, no copper antenna keepouts, no exact custom-pad geometry,
no custom nonrectangular board placement, no locked mechanical anchor handling, and no
guaranteed runtime/quality for very large hundreds-of-parts designs.
A board may be geometrically legalized and still be electrically or mechanically invalid.
