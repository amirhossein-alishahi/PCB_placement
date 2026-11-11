# HARP-PCB — KiCad auto-placement prototype (v0.2)

> **Geometric placement research tool, not a production board designer.**
> The engine does **not** route traces, perform formal KiCad DRC, determine genuine footprints from part numbers, or guarantee that arbitrary mechanical/electrical requirements are met.

HARP-PCB reads an **unrouted** KiCad `.kicad_pcb` containing pads/net assignments; footprints may all overlap at `(0,0)` **or already have arbitrary, even wrong, positions and rotations**. It discards the existing placement, builds a new **rectangular** board outline, legalizes bounding boxes, then runs a pin-weighted local optimization. Original pad geometry, footprint content, UUIDs, and board nets are verified unchanged.

**Supports:** rectilinear board auto-sizing; existing overlapping or nonzero starting coordinates; explicit connector/outward-face constraints and a limited `PCB Edge` marker heuristic; stage-by-stage KiCad and PNG snapshots; synthetic footprint warnings and safety gate; strict validation after every saved snapshot.

**Does not support generically:** non-rectangular/cutout-aware placement, arbitrary part-specific placement rules, 3D mechanical access verification, locked mounting holes, previously routed tracks/vias, or copper zones. Some footprint shapes and both PCB sides may need a KiCad-level mechanical audit; bounding boxes are approximations. A complex outline is rejected unless explicitly replaced. Existing routes, vias or zones are rejected to avoid corrupting an existing design.

## Google Colab

[Open the notebook](https://colab.research.google.com/github/amirhossein-alishahi/PCB_placement/blob/main/notebooks/HARP_PCB_Colab.ipynb)

Run the notebook in order. It installs this repository, accepts one `.kicad_pcb`, shows a **preflight audit**, permits reference-indexed edge rules, then generates `placed.kicad_pcb`, a preview image, JSON report, and optionally a `stages/` folder with inspected KiCad checkpoint boards and previews. A drop-down allows viewing the stages.

**No API key is required.** The optional `https://ai-router.intelcad.ai/v1` call using `cc/claude-haiku-5-5` or `cc/claude-opus-5-5` only summarizes aggregate report statistics, never edits or validates geometry. It is off by default, and key entry is masked.

## Local installation and quick start

```bash
python -m pip install -e '.[preview]'
python -m harp_pcb your_board.kicad_pcb --preflight-only --report preflight.json
python -m harp_pcb your_board.kicad_pcb -o placed.kicad_pcb \
  --preview placed.png --report report.json --iterations 4000 --seed 42 \
  --stages-dir stages --snapshot-every 1000
```

If the input contains fabricated `SYNTH_...` land patterns or embedded `SYNTHETIC - NOT A REAL LAND PATTERN` text, the software **blocks normal placement**. You must replace the placeholder with an actual, verified land pattern. To intentionally run a **non-manufacturing geometry experiment**, use `--allow-unverified-footprints`. This **does not fix** fake pad locations or remove the warning text.

The tool also blocks **ambiguous access-facing** components (USB/SD-side access/slide switches) unless given a direction rule. `--allow-unresolved-orientation` explicitly disables this safety gate for experiments, but cannot guarantee functionality.

### Mechanical direction constraints

Create `edge_rules.json`:

```json
{
  "components": {
    "J1": {"side": "right", "outward_local": "right", "edge_offset_mm": 0.5},
    "J2": {"side": "bottom", "outward_local": "right", "edge_offset_mm": 0.5},
    "SW1": {"side": "any", "outward_local": "top", "edge_offset_mm": 0.5}
  }
}
```

`side` is the desired external board side (`left`, `right`, `top`, `bottom`, or `any`). `outward_local` is the direction the **opening faces in the unrotated footprint's own board coordinates**. At 0°: `right` is local +X, `top` is local -Y. If the connector opens toward local +X and is placed on the top edge, the footprint must be rotated 90° counterclockwise (KiCad convention). This direction must be confirmed from footprint drawings or the datasheet. A `PCB Edge` text marker on a documentation/fabrication layer can be used to infer an opening direction when unambiguous; other parts require user guidance. Geometry checks ensure a correctly rotated, edge-adjacent bbox (not genuine mechanical fit/connector mating).

```bash
python -m harp_pcb your_board.kicad_pcb -o placed.kicad_pcb \
  --direction-rules edge_rules.json --stages-dir stages \
  --iterations 4000 --snapshot-every 1000
```

**Notes:** Edge-adjacent parts are kept at a specified offset from their bbox to the outline; this is not a plated edge connector design rule. The placer will not automatically route connector pins or guarantee user access. For rigid fixed mechanical coordinates you must implement and verify more constraints before manufacturing.

### Intermediate results

With `--stages-dir stages`, output contains:

- `01_greedy.kicad_pcb` and PNG: complete legal placement before optimization;
- optionally `anneal_XXXXXX` checkpoints taken at `--snapshot-every N` iterations;
- optimized complete placement before board shrink;
- final auto-sized KiCad board;
- `manifest.json` specifying stage order and preview paths.

Each snapshot is independently checked for net/pad integrity, bounding-box separation, side orientation, and placement within the corresponding outline. The starting input can have all parts overlapped; it is intentionally **not** emitted as a valid placement snapshot.

### Nonrectangular outlines and routed designs

The engine defaults to replacing a simple rectangle. If Edge.Cuts represents circles, arcs, cutouts, polygons, or unsupported geometry, the placer **refuses** to destroy those design constraints. You can explicitly force conversion with `--replace-nonrectangular-outline`, but only if replacing the mechanical geometry is truly acceptable. No arbitrary board shapes/cutouts are legalized currently.

Input with `segment`, `arc`, `via`, or `zone` is refused because moving footprints would invalidate copper geometry; **do not** remove these objects blindly.

## Quality gates

`pytest` exercises arbitrary pre-placed footprint coordinates, no-overlap legalization, cardinal side + rotation for all four external faces, marker inference, synthetic refusal, ambiguous direction refusal, nonrectangular safety, source integrity, intermediate stages, and Colab notebook syntax. GitHub Actions runs Python 3.10, 3.12, 3.13, Ruff, pytest and package build; a tagged `v*` push triggers a GitHub Release workflow. GitHub Actions success and branch policies must be separately confirmed in GitHub.

```bash
python -m pip install -e '.[dev,preview]'
python -m pytest -q
python -m ruff check harp_pcb.py tests
python -m build
```

## Final checks before using the output

Open `placed.kicad_pcb` in **KiCad**, replace every unverified footprint, inspect connector side and mating envelope, run `kicad-cli pcb drc`, and route all nets before manufacturing. The program currently validates only geometry approximations and textual structural preservation; no claim of DRC-clean board, complete routing, shortest electrical path, or manufacturing quality is implied.

**Privacy:** This repository is public. The Colab workflow uploads your PCB to your Colab runtime; it is not committed to GitHub. Your private PCB and any API key must not be placed in public issues, commits, examples, or AI-report requests.
