"""Cross-board regression tests using only synthetic generated boards."""
from __future__ import annotations

import json
import random
import subprocess
import sys

import pytest
from shapely.geometry import box

import harp_pcb as h
from test_harp_pcb import board


def varied_board(n, seed=0, large=False, positions='overlap', back=False, no_courtyard=False):
    rng = random.Random(seed)
    footprints = []
    nets = [f'(net {k} "N{k}")' for k in range(1, n + 4)]
    nets += ['(net 0 "")', '(net 200 "GND")', '(net 201 "VDD")']
    for i in range(n):
        kind = 'C' if i % 7 < 4 else 'U' if i % 7 == 4 else 'J' if i % 7 == 5 else 'R'
        ref = f'{kind}{i + 1}'
        w = rng.uniform(0.6, 2) if kind in ('C', 'R') else rng.uniform(3, 8)
        height = rng.uniform(0.8, 2) if kind in ('C', 'R') else rng.uniform(3, 8)
        if large and i == 0:
            w, height = rng.uniform(15, 26), rng.uniform(12, 23)
        x, y = (0, 0) if positions == 'overlap' else (rng.uniform(-50, 180), rng.uniform(-30, 160))
        angle = rng.choice([0, 90, 180, 270])
        layer = 'B.Cu' if back and i % 5 == 0 else 'F.Cu'
        props = (f'(property "Reference" "{ref}" (at {x} {y - 3} 0))'
                 f'(property "Value" "VAL{i}" (at {x} {y + 3} 0))')
        courtyard = '' if no_courtyard or i % 4 == 0 else (
            f'(fp_rect (start {-w / 2:.4f} {-height / 2:.4f})'
            f' (end {w / 2:.4f} {height / 2:.4f}) (layer "F.CrtYd")'
            ' (stroke (width 0.05) (type default)))')
        pads = []
        for k in range(1, 5 if kind == 'U' else 3):
            px = -w / 2 + .3 if k % 2 else w / 2 - .3
            py = -height / 2 + .3 if k < 3 else height / 2 - .3
            net = 200 if k == 2 else 201 if k == 3 else i % 6 + 1
            padtype = 'thru_hole circle' if kind == 'J' else 'smd rect'
            layers = '"*.Cu" "*.Mask"' if kind == 'J' else f'"{layer}"'
            drill = '(drill 0.3)' if kind == 'J' else ''
            label = 'GND' if net == 200 else 'VDD' if net == 201 else f'N{net}'
            pads.append(f'(pad "{k}" {padtype} (at {px:.3f} {py:.3f})'
                        f' (size .4 .55) {drill} (layers {layers}) (net {net} "{label}"))')
        footprints.append(f'(footprint "Test:{kind}_{i}" (layer "{layer}")'
                          f' (at {x:.4f} {y:.4f} {angle}) {props} {courtyard} {" ".join(pads)})')
    return ('(kicad_pcb (version 20241229) (generator "cross-board-test") '
            + ' '.join(nets) + ' ' + ' '.join(footprints) + ')')


@pytest.mark.parametrize('count,seed,large,positions,back,no_courtyard', [
    (2, 0, False, 'overlap', False, False),
    (4, 2, False, 'random', False, True),
    (8, 3, False, 'random', True, False),
    (12, 13, True, 'overlap', True, False),
    (16, 17, True, 'random', False, True),
    (24, 23, True, 'overlap', True, True),
    (32, 31, True, 'random', True, False),
    (48, 37, True, 'random', True, True),
])
def test_diverse_layouts(count, seed, large, positions, back, no_courtyard):
    original = varied_board(count, seed, large, positions, back, no_courtyard)
    parts = h.decode_parts(h.parse(original))
    placed = h.place(parts, h.make_edges(parts), iterations=90, seed=seed)
    output = h.edit_board(original, parts, placed['width'], placed['height'],
                          placed['origin_x'], placed['origin_y'])
    report = h.verify(original, output)
    assert report['footprints'] == count
    assert report['unique_placements'] == count
    assert report['pads_and_nets_unchanged']
    assert report['perimeter_and_clearance_pass']
    rectangles = []
    for part in h.decode_parts(h.parse(output)):
        at = h.child(part.node, 'at')
        part.x, part.y, part.angle = float(at[1]), float(at[2]), int(float(at[3]))
        rectangles.append(box(*part.bbox()))
    assert all(a.distance(b) >= .649 for i, a in enumerate(rectangles) for b in rectangles[i + 1:])


def test_kicad_6_7_reference_fields(tmp_path):
    original = board(6)
    original = original.replace('(property "Reference" "U1" (at 0 -3 0))',
                                '(fp_text reference "U1" (at 0 -3 0) (layer "F.SilkS"))', 1)
    original = original.replace('(property "Value" "TEST" (at 0 3 0))',
                                '(fp_text value "TEST" (at 0 3 0) (layer "F.Fab"))', 1)
    original = original.replace('20241229', '20211014')
    assert h.footprint_value(h.decode_parts(h.parse(original))[0].node) == 'TEST'
    source = tmp_path / 'old.kicad_pcb'
    target = tmp_path / 'placed.kicad_pcb'
    source.write_text(original)
    run = subprocess.run([sys.executable, '-m', 'harp_pcb', str(source), '-o', str(target),
                          '--iterations', '40'], capture_output=True, text=True, timeout=90)
    assert run.returncode == 0, run.stderr
    assert h.verify(original, target.read_text())['footprints'] == 6
    assert '(fp_text reference "U1" (at 0 -3 0)' in target.read_text()


def test_padless_graphical_footprints():
    original = board(5)
    original = original.replace('(pad "1" smd rect (at -0.8 0 0) (size 0.7 0.7) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "DATA"))', '', 1)
    original = original.replace('(pad "2" smd rect (at 0.8 0 0) (size 0.7 0.7) (layers "F.Cu" "F.Paste" "F.Mask") (net 2 "GND"))', '', 1)
    parts = h.decode_parts(h.parse(original))
    assert len(parts[0].pads) == 0
    placement = h.place(parts, h.make_edges(parts), iterations=30)
    output = h.edit_board(original, parts, placement['width'], placement['height'],
                          placement['origin_x'], placement['origin_y'])
    assert h.verify(original, output)['pads'] == 8


def test_single_footprint_serializes_valid_json():
    original = board(2)
    for tag, a, b in h.span_children(original):
        if tag == 'footprint' and '"R2"' in original[a:b]:
            original = original[:a] + original[b:]
            break
    parts = h.decode_parts(h.parse(original))
    result = h.place(parts, h.make_edges(parts), iterations=0)
    output = h.edit_board(original, parts, result['width'], result['height'],
                          result['origin_x'], result['origin_y'])
    report = h.verify(original, output)
    assert report['footprints'] == 1
    assert report['min_bounding_box_gap_mm'] is None
    json.dumps(report, allow_nan=False)


def test_legacy_module_rejected_not_dropped():
    source = board(5).replace('(footprint "Test:Sample_0"', '(module "Test:Sample_0"', 1)
    with pytest.raises(ValueError, match='KiCad 4/5'):
        h.decode_parts(h.parse(source))


def test_padless_no_geometry_rejected():
    source = board(5)
    for token in [
        '(pad "1" smd rect (at -0.8 0 0) (size 0.7 0.7) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "DATA"))',
        '(pad "2" smd rect (at 0.8 0 0) (size 0.7 0.7) (layers "F.Cu" "F.Paste" "F.Mask") (net 2 "GND"))',
        '(fp_rect (start -1.7 -1) (end 1.7 1) (layer "F.CrtYd") (stroke (width 0.05) (type default)))',
    ]:
        source = source.replace(token, '', 1)
    with pytest.raises(ValueError, match='no pads or measurable graphics'):
        h.decode_parts(h.parse(source))


def test_locked_footprint_rejected():
    source = board(5).replace('(footprint "Test:Sample_0" (layer "F.Cu")',
                              '(footprint "Test:Sample_0" locked (layer "F.Cu")', 1)
    with pytest.raises(ValueError, match='locked mechanical placement'):
        h.decode_parts(h.parse(source))


def test_nonfinite_clearance_rejected(tmp_path):
    source = tmp_path / 'a.kicad_pcb'
    target = tmp_path / 'b.kicad_pcb'
    source.write_text(board(5))
    run = subprocess.run([sys.executable, '-m', 'harp_pcb', str(source), '-o', str(target),
                          '--clearance', 'nan'], capture_output=True, text=True, timeout=20)
    assert run.returncode != 0
    assert 'Invalid clearance' in run.stderr
    assert not target.exists()
