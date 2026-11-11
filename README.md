# HARP-PCB Placement (prototype)

Auto-place **unrouted/unplaced** KiCad `.kicad_pcb` boards whose footprints initially overlap, and generate a new rectangular `Edge.Cuts` outline. Geometric validation is performed before a result is written.

> **Scope:** This is a prototype placement and board-sizing engine; **not** a router or formal DRC checker. No claim of production-safe or electrically optimal layout is made. For boards with mechanical, thermal, high-voltage, RF or assembly constraints, have an engineer review placement and run KiCad DRC and routing. The engine conservatively uses rectangular bounds, even for non-rectangular courtyards.

## Google Colab

Open [`notebooks/HARP_PCB_Colab.ipynb`](notebooks/HARP_PCB_Colab.ipynb) in Colab, run cells in order, upload your **unplaced** KiCad PCB, and download a ZIP containing:

- `placed.kicad_pcb` — moved footprints and new rectangular board outline
- `placement_report.json` — basic geometry/connectivity checks
- `placement_preview.png` — simple geometry preview

**No API key required for placement.** The first Colab cell includes an optional masked IntelCAD API key and a model choice (`cc/claude-haiku-5-5` / `cc/claude-opus-5-5`), strictly for a report-only AI review. The selected AI model does **not** control placement or validate physical design. A report sent to an external AI service should not contain confidential information.

## Local CLI

```bash
python -m pip install -e '.[preview]'
harp-pcb unplaced.kicad_pcb -o placed.kicad_pcb \
  --preview placed.png --report placement_report.json \
  --iterations 3000 --seed 42 --clearance 0.65 --edge-margin 2.0
harp-pcb placed.kicad_pcb --verify
```

- Input must have footprints with actual pads, no routed segments/vias/zones; existing outline may be replaced.
- `--clearance` and `--edge-margin` are simplified geometric proxies, **not** substitutes for correct KiCad design rules.
- An overlaid footprint at `(0,0)` is a valid input; output preserves pad local positions, net table, footprint internals and UUIDs while changing footprint positions and text property positions.
- Some exotic KiCad footprints/courtyard shapes are conservatively enclosed by a bounding rectangle; missing courtyards receive a pad-based fallback envelope.
- **Do not commit private customer board files or API keys to this public repository.** `.gitignore` excludes KiCad design and generated output files.

## Quality gates and release automation

CI on every push to `main` and every pull request:
- Python 3.10 / 3.12 / 3.13 test matrix
- syntax and Ruff correctness checks
- tests against synthetic boards (no real customer data)
- installed CLI smoke test
- distribution build and downloadable Actions artifact

CD on a new `v*` Git tag validates tests, builds wheel/sdist and publishes a **GitHub Release** with those files. Nothing is automatically published to PyPI or deployed to a hosted service. GitHub Actions secrets are not needed for ordinary CI or releases.

Developer commands:

```bash
python -m pip install -e '.[dev,preview]'
python -m ruff check harp_pcb.py tests
python -m pytest -q
python -m build
```

## Validation boundaries

A passing report checks that footprints/pads/net definitions remain present and checks approximate bounding-box clearances and a valid rectangular outline. It **does not** certify EDA correctness (DRC-clean, routability, manufacturability, 3D collision, thermal constraints, impedance or connectivity after routing).

To run formal KiCad DRC on a machine with KiCad installed:

```bash
kicad-cli pcb drc -o drc_report.txt placed.kicad_pcb
```

Unrouted nets are expected because this package performs placement only. Open the result in KiCad before any downstream use.

## Repository status

Initial implementation is based on an experimentally tested Python placer; future improvements may add routability estimation and local placement repair. LLM-based coordinate placement was deliberately excluded for determinism and safety. Repository does not bundle third-party proprietary PCB source code.
