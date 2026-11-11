# HARP-PCB — KiCad auto-placement prototype (v0.4)

> **Geometric placement research tool, not a production board designer.**
> The engine does **not** route traces, perform formal KiCad DRC, determine genuine footprints from part numbers, or guarantee that arbitrary mechanical/electrical requirements are met.

HARP-PCB reads an **unrouted** KiCad `.kicad_pcb` containing pads/net assignments; footprints may all overlap at `(0,0)` **or already have arbitrary, even wrong, positions and rotations**. It discards the existing placement, builds a new **rectangular** board outline, legalizes bounding boxes, then runs a pin-weighted local optimization. Original pad geometry, footprint content, UUIDs, and board nets are verified unchanged.

**Supports:** rectilinear board auto-sizing; existing overlapping or nonzero starting coordinates; explicit connector/outward-face constraints and a limited `PCB Edge` marker heuristic; stage-by-stage KiCad and PNG snapshots; non-blocking synthetic footprint and ambiguous opening-direction warnings, optional AI preflight advice, and geometry validation after every saved snapshot.

**Does not support generically:** non-rectangular/cutout-aware placement, full manufacturer-specific RF/copper keepouts, 3D mechanical access verification, locked mounting holes, previously routed tracks/vias, or copper zones. Some footprint shapes and both PCB sides may need a KiCad-level mechanical audit; bounding boxes are approximations. A complex outline is rejected unless explicitly replaced. Existing routes, vias or zones are rejected to avoid corrupting an existing design.

## Google Colab

[Open the notebook](https://colab.research.google.com/github/amirhossein-alishahi/PCB_placement/blob/main/notebooks/HARP_PCB_Colab.ipynb)

Run the notebook in order. It installs this repository, accepts one `.kicad_pcb`, shows a **preflight audit**, permits reference-indexed edge rules, then generates `placed.kicad_pcb`, a preview image, JSON report, and optionally a `stages/` folder with inspected KiCad checkpoint boards and previews. A drop-down allows viewing the stages.

**No API key is required.** With `USE_LLM_PREFLIGHT = True` and a key entered in the masked prompt, the optional `https://ai-router.intelcad.ai/v1` request asks `cc/claude-haiku-5-5` or `cc/claude-opus-5-5` to diagnose flagged issues and suggest **unverified** library footprint candidates. Only flagged reference/value/footprint identifiers and pad counts are shared; no raw PCB file, routing, nets, or pad positions. Failed requests fall back locally, without blocking placement. AI recommendations are **never applied automatically** because changing land patterns safely requires datasheet and pad-map verification. Aggregate report explanation is also optional.


## New: functional placement roles and antenna footprint spacing (v0.4)

HARP-PCB now infers **soft placement preferences** from footprint identifiers,
component references and value names (and allows overrides). They are priors,
not guaranteed placements:

| Inferred role | Default geometric preference | Important limits |
| --- | --- | --- |
| `io` | Prefer board perimeter (connectors, headers, I/O) | A direction-dependent opening still requires `--direction-rules` / a reliable `PCB Edge` marker; do not guess local opening direction |
| `processor` | Prefer the central area (e.g. identifiable STM32/ESP/RP2040 part values) | Connected decoupling/peripherals are placed by pin-weighted connectivity; not forced to center |
| `core` | Weak central preference for large ICs | Does not assume every `U*` is a CPU |
| `antenna` | Prefer board edge, away from identifiable noisy/processor parts | Soft heuristic only; cannot validate an antenna's radiation pattern or embedded module's antenna keepout |
| `noisy` | Soft separation from an antenna (e.g. switching regulators/oscillators) | Name-based inference is incomplete; list known switching sources explicitly |
| `general` | Electrical/geometric placement | No special prior |

To control roles and enforce a **specific, datasheet-backed, hard footprint-to-footprint clearance**, create `placement_rules.json`:

```json
{
  "components": {
    "J1": {"role": "io"},
    "U1": {"role": "processor"},
    "ANT1": {"role": "antenna"},
    "U2": {"role": "noisy"}
  },
  "antenna_keepouts": {
    "ANT1": {
      "distance_mm": 8.0,
      "against": "noisy",
      "references": ["U2"]
    }
  }
}
```

Change references and the **8.0 mm example distance** to match YOUR design and
component/antenna manufacturer requirements. `against` can be `noisy`
(processors and noisy components plus explicit references), `all` (all other
component footprints), or `listed` (only `references`). This is a minimum
spacing between **conservative footprint bounding boxes**; it does **not**
create a copper keepout/ground-plane void, protect RF matching traces,
account for the casing, or validate RF performance. No hard RF distance is
automatically invented. If a recognized antenna has no hard rule, the report
flags it for review and the placer still completes with an RF advisory warning.

```bash
harp-pcb board.kicad_pcb -o board_placed.kicad_pcb \
  --placement-rules placement_rules.json --report report.json \
  --stages-dir stages --snapshot-every 1000
```

Auto role inference and strict user-specified keepouts are included in
`functional_placement`, `role_placement`, and `validated_rf_keepouts` report
fields. Snapshots are validated against the same RF constraints. The solver
retries a larger rectangular board if hard keepouts cannot be legalized;
physically impossible demands produce a clear error instead of a fabricated
valid result. Existing copper zones, cutouts and routed designs remain outside
the safe automation scope.

**RF caution:** Antenna matching networks are frequently required *close* to
the antenna feed and must not automatically be classified as noise sources.
The reference design determines which layers require copper/trace clearance;
a generic footprint clearance is insufficient. See the manufacturer's antenna
layout guidelines and validate on actual hardware.

## Local installation and quick start

```bash
python -m pip install -e '.[preview]'
python -m harp_pcb your_board.kicad_pcb --preflight-only --report preflight.json
python -m harp_pcb your_board.kicad_pcb -o placed.kicad_pcb \
  --preview placed.png --report report.json --iterations 4000 --seed 42 \
  --stages-dir stages --snapshot-every 1000
```

**Best-effort default:** Fabricated `SYNTH_...` land patterns, embedded `SYNTHETIC - NOT A REAL LAND PATTERN` text and ambiguous USB/SD/switch opening directions produce **review warnings** but do not block a geometry-only PCB output. They are **not fixed**; the source footprint stays intact. `--strict-preflight` restores rejection; legacy `--allow-unverified-footprints`/`--allow-unresolved-orientation` override the respective check in strict mode. Crucial: unresolved openings are not asserted to face the board edge.

To request optional AI diagnosis and get a warning-preserving report:

```bash
# set the secret ONLY in an environment variable (never put it in Git)
export INTELCAD_API_KEY='your-private-key'
python -m harp_pcb board.kicad_pcb -o placed.kicad_pcb \
  --ai-preflight --ai-model cc/claude-haiku-5-5 --report report.json
```

The model's recommendations are **advisory and unverified**; do not copy a guessed footprint without checking datasheets, pad mapping, pitch and mechanical clearances. Without a key or if the API fails, local fallback continues and the JSON records `no_api_key_fallback` or `api_unavailable_fallback`. The API host is restricted to `https://ai-router.intelcad.ai`. API errors are deliberately sanitized so the token is not printed. The PCB output always reports `manufacturing_ready: false`.

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

`pytest` exercises arbitrary pre-placed coordinates, overlap legalization, cardinal direction constraints, marker inference, best-effort synthetic/ambiguous continuation, strict-mode rejection, mocked AI success/timeout/invalid response, secret redaction, unsupported outline safety, source integrity, stages, and Colab notebook syntax. GitHub Actions runs Python 3.10, 3.12, 3.13, Ruff, pytest and package build; a tagged `v*` push triggers a GitHub Release workflow. GitHub Actions success and branch policies must be separately confirmed in GitHub.

```bash
python -m pip install -e '.[dev,preview]'
python -m pytest -q
python -m ruff check harp_pcb.py tests
python -m build
```

## Final checks before using the output

Open `placed.kicad_pcb` in **KiCad**, replace every unverified footprint, inspect connector side and mating envelope, run `kicad-cli pcb drc`, and route all nets before manufacturing. The program currently validates only geometry approximations and textual structural preservation; no claim of DRC-clean board, complete routing, shortest electrical path, or manufacturing quality is implied.

**Privacy:** This repository is public. The Colab workflow uploads your PCB to your Colab runtime; it is not committed to GitHub. Your private PCB and any API key must not be placed in public issues, commits, examples, or AI-report requests.
