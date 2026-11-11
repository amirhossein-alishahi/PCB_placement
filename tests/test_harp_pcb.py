"""Synthetic public fixtures: no private/user PCB is committed to this repo."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest
from shapely.geometry import box

import harp_pcb as h


def board(num_parts: int = 8, with_tracks: bool = False, existing_outline: bool = False) -> str:
    assert num_parts >= 2
    footprints = []
    for i in range(num_parts):
        ref = f"U{i+1}" if i % 3 == 0 else f"R{i+1}"
        # Source footprints deliberately overlap at (0,0).
        w = 3.4 if i % 3 == 0 else 2.0
        lines = [
            f'  (footprint "Test:Sample_{i}" (layer "F.Cu") (at 0 0 0)',
            f'    (property "Reference" "{ref}" (at 0 -3 0))',
            '    (property "Value" "TEST" (at 0 3 0))',
        ]
        if i != num_parts - 1:
            lines.append(f'    (fp_rect (start {-w/2} -1) (end {w/2} 1) (layer "F.CrtYd") (stroke (width 0.05) (type default)))')
        lines.extend([
            '    (pad "1" smd rect (at -0.8 0 0) (size 0.7 0.7) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "DATA"))',
            '    (pad "2" smd rect (at 0.8 0 0) (size 0.7 0.7) (layers "F.Cu" "F.Paste" "F.Mask") (net 2 "GND"))',
            '  )',
        ])
        footprints.append('\n'.join(lines))
    edges = ('  (gr_line (start 0 0) (end 20 0) (stroke (width 0.1) (type default)) (layer "Edge.Cuts"))\n' if existing_outline else '')
    tracks = ('  (segment (start 0 0) (end 2 2) (width 0.2) (layer "F.Cu") (net 1))\n' if with_tracks else '')
    return ('(kicad_pcb (version 20241229) (generator "harp-test")\n'
            '  (net 0 "") (net 1 "DATA") (net 2 "GND")\n'
            + '\n'.join(footprints) + '\n' + edges + tracks + ')\n')


def test_parser_and_missing_courtyard():
    parts = h.decode_parts(h.parse(board()))
    assert len(parts) == 8
    assert sum(map(lambda p: len(p.pads), parts)) == 16
    assert parts[-1].has_courtyard is False
    assert parts[0].has_courtyard is True


def test_end_to_end_cli(tmp_path):
    path = tmp_path / 'unplaced.kicad_pcb'
    out = tmp_path / 'placed.kicad_pcb'
    report = tmp_path / 'report.json'
    path.write_text(board(), encoding='utf-8')
    proc = subprocess.run([sys.executable, '-m', 'harp_pcb', str(path), '-o', str(out),
                           '--iterations', '200', '--report', str(report)],
                          capture_output=True, text=True, timeout=90)
    assert proc.returncode == 0, proc.stderr
    data = json.loads(report.read_text())
    assert data['footprints'] == 8
    assert data['pads'] == 16
    assert data['nets'] == 3
    assert data['unique_placements'] == 8
    assert data['pads_and_nets_unchanged']
    assert h.verify(path.read_text(), out.read_text())['perimeter_and_clearance_pass']


@pytest.mark.parametrize('seed', [1, 42, 171])
def test_repeated_seeds_valid(seed):
    original = board(7)
    parts = h.decode_parts(h.parse(original))
    result = h.place(parts, h.make_edges(parts), iterations=80, seed=seed)
    output = h.edit_board(original, parts, result['width'], result['height'],
                          result['origin_x'], result['origin_y'])
    report = h.verify(original, output)
    assert report['footprints'] == 7
    ps = h.decode_parts(h.parse(output))
    for p in ps:
        pos = h.child(p.node, 'at')
        p.x, p.y, p.angle = float(pos[1]), float(pos[2]), int(pos[3])
    polys = [box(*p.bbox()) for p in ps]
    for i, a in enumerate(polys):
        for b in polys[i+1:]:
            assert a.distance(b) >= 0.649


def test_existing_outline_can_be_resized():
    original = board(existing_outline=True)
    parts = h.decode_parts(h.parse(original))
    result = h.place(parts, h.make_edges(parts), iterations=20)
    output = h.edit_board(original, parts, result['width'], result['height'],
                          result['origin_x'], result['origin_y'])
    assert h.outline_rect(h.parse(output)) is not None
    assert h.verify(original, output)['footprints'] == 8


def test_rejects_routed_board(tmp_path):
    input_path = tmp_path / 'routed.kicad_pcb'
    output_path = tmp_path / 'should-not-exist.kicad_pcb'
    input_path.write_text(board(with_tracks=True))
    proc = subprocess.run([sys.executable, '-m', 'harp_pcb', str(input_path),
                           '-o', str(output_path)], capture_output=True, text=True, timeout=20)
    assert proc.returncode != 0
    assert 'segment' in proc.stderr
    assert not output_path.exists()


def test_parser_rejects_malformed():
    for broken in ['(kicad_pcb', '(kicad_pcb))', '(kicad_pcb (net 0 "broken))']:
        with pytest.raises(ValueError):
            h.parse(broken)


def test_output_file_cannot_overwrite_input(tmp_path):
    input_path = tmp_path / 'input.kicad_pcb'
    input_path.write_text(board())
    proc = subprocess.run([sys.executable, '-m', 'harp_pcb', str(input_path),
                           '-o', str(input_path)], capture_output=True, text=True, timeout=20)
    assert proc.returncode != 0
    assert input_path.read_text() == board()


def test_property_transform_preserves_absolute_position():
    original = board()
    parts = h.decode_parts(h.parse(original))
    result = h.place(parts, h.make_edges(parts), iterations=20)
    output = h.edit_board(original, parts, result['width'], result['height'],
                          result['origin_x'], result['origin_y'])
    assert h.verify(original, output)['pads_and_nets_unchanged']
