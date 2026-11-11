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


def change_positions(data: str, num_parts: int) -> str:
    """Scatter already-positioned footprints to test genuine full re-placement."""
    for i in range(num_parts):
        name = f'Test:Sample_{i}'
        prefix = f'(footprint "{name}" (layer "F.Cu") (at 0 0 0)'
        replacement = f'(footprint "{name}" (layer "F.Cu") (at {110+i*7} {60+i*3} 180)'
        assert prefix in data
        data = data.replace(prefix, replacement)
        # Board-level fields in the sample fixture are absolute.
        # They are intentionally left untouched here; relocation should not break
        # the relative transform verification.
    return data


def access_board(marker=False):
    src = board(num_parts=6)
    src = src.replace('Test:Sample_0', 'Connector:USB_C_Receptacle', 1)
    if marker:
        target = '(property "Value" "TEST" (at 0 3 0))'
        src = src.replace(target, target+'\n    (fp_text user "PCB Edge" (at 5 0) (layer "Dwgs.User"))',1)
    return src


def test_placed_input_is_fully_replaced(tmp_path):
    src=change_positions(board(8),8)
    assert all(float(h.child(p.node,'at')[1])>100 for p in h.decode_parts(h.parse(src)))
    path=tmp_path/'preplaced.kicad_pcb';out=tmp_path/'new.kicad_pcb'
    path.write_text(src)
    proc=subprocess.run([sys.executable,'-m','harp_pcb',str(path),'-o',str(out),'--iterations','60'],
                        capture_output=True,text=True,timeout=100)
    assert proc.returncode==0,proc.stderr
    a=h.verify(src,out.read_text());assert a['unique_placements']==8
    assert any(abs(float(h.child(p.node,'at')[1])-110)>3 for p in h.decode_parts(h.parse(out.read_text())))


def test_orientation_missing_is_blocked(tmp_path):
    path=tmp_path/'access.kicad_pcb';out=tmp_path/'out.kicad_pcb'
    path.write_text(access_board())
    proc=subprocess.run([sys.executable,'-m','harp_pcb',str(path),'-o',str(out),'--iterations','50'],
                        capture_output=True,text=True,timeout=90)
    assert proc.returncode!=0 and 'Ambiguous access direction' in proc.stderr
    assert not out.exists()


def test_marked_usb_faces_out_and_staged_snapshots(tmp_path):
    src=access_board(marker=True)
    path=tmp_path/'marked.kicad_pcb';out=tmp_path/'out.kicad_pcb'
    path.write_text(src)
    stage=tmp_path/'stages'
    proc=subprocess.run([sys.executable,'-m','harp_pcb',str(path),'-o',str(out),
                         '--iterations','105','--snapshot-every','50','--stages-dir',str(stage),
                         '--report',str(tmp_path/'report.json')],capture_output=True,text=True,timeout=120)
    assert proc.returncode==0,proc.stderr
    audit=json.loads((tmp_path/'report.json').read_text())
    assert audit['footprint_audit']['edge_rules']['U1']['outward_local']=='right'
    assert audit['footprint_audit']['edge_rules']['U1']['evidence']=='footprint-PCB-Edge-marker'
    assert len(audit['stages'])==5
    for st in audit['stages']:
        assert (stage/st['pcb']).exists() and (stage/st['preview']).exists()
    parts=h.decode_parts(h.parse(out.read_text()))
    usb=next(p for p in parts if p.name=='U1')
    assert usb.angle in (0,90,180,270)
    rules=h.apply_direction_rules(parts)['edge_rules']
    assert rules['U1']['outward_local']=='right'
    assert h.verify(src,out.read_text(),direction_rules={'U1':usb.edge_rule})['validated_outward_facing']==['U1']


@pytest.mark.parametrize('side,local,angle',[
    ('right','right',0),('top','right',90),('left','right',180),('bottom','right',270)])
def test_explicit_facing_on_four_edges(tmp_path,side,local,angle):
    src=access_board()
    path=tmp_path/'in.kicad_pcb';out=tmp_path/'out.kicad_pcb'
    cfg=tmp_path/'directions.json';path.write_text(src)
    cfg.write_text(json.dumps({'components':{'U1':{'side':side,'outward_local':local,'edge_offset_mm':0.5}}}))
    proc=subprocess.run([sys.executable,'-m','harp_pcb',str(path),'-o',str(out),
                         '--direction-rules',str(cfg),'--iterations','50'],
                        capture_output=True,text=True,timeout=120)
    assert proc.returncode==0,proc.stderr
    p=next(p for p in h.decode_parts(h.parse(out.read_text())) if p.name=='U1')
    assert int(float(h.child(p.node,'at')[3]))==angle
    checks=h.verify(src,out.read_text(),direction_rules={'U1':h.EdgeRule(side,local,0.5)})
    assert checks['validated_outward_facing']==['U1']


def test_synthetic_footprint_halts_and_preflight_can_report(tmp_path):
    src=board().replace('Test:Sample_0','SYNTH_FAKE_LAND_PATTERN',1)
    path=tmp_path/'in.kicad_pcb';out=tmp_path/'out.kicad_pcb';path.write_text(src)
    proc=subprocess.run([sys.executable,'-m','harp_pcb',str(path),'-o',str(out)],
                        capture_output=True,text=True,timeout=90)
    assert proc.returncode!=0 and 'Synthetic/placeholder' in proc.stderr
    assert not out.exists()
    report=tmp_path/'preflight.json'
    pre=subprocess.run([sys.executable,'-m','harp_pcb',str(path),'--preflight-only','--report',str(report)],
                       capture_output=True,text=True,timeout=90)
    assert pre.returncode==0
    assert json.loads(report.read_text())['synthetic_footprints'][0]['ref']=='U1'


def test_rule_file_rejects_unknown_ref_or_invalid_vector(tmp_path):
    parts=h.decode_parts(h.parse(board()))
    with pytest.raises(ValueError,match='Unknown component'):
        h.apply_direction_rules(parts,{'components':{'NOT_REAL':{'side':'right','outward_local':'right'}}})
    with pytest.raises(ValueError,match='cardinal'):
        h.apply_direction_rules(parts,{'components':{'U1':{'side':'east','outward_local':'northwest'}}})


def test_nonrectangular_outline_rejected_without_opt_in(tmp_path):
    src=board()+''
    src=src[:src.rfind(')')]+'''(gr_circle (center 20 20) (end 40 20)
          (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts"))\n)'''
    p=tmp_path/'circle.kicad_pcb';out=tmp_path/'out.kicad_pcb';p.write_text(src)
    run=subprocess.run([sys.executable,'-m','harp_pcb',str(p),'-o',str(out)],
                       text=True,capture_output=True,timeout=90)
    assert run.returncode!=0 and 'nonrectangular' in run.stderr
    assert not out.exists()
    okay=subprocess.run([sys.executable,'-m','harp_pcb',str(p),'-o',str(out),
                         '--replace-nonrectangular-outline','--iterations','50'],
                        text=True,capture_output=True,timeout=90)
    assert okay.returncode==0,okay.stderr
    assert h.outline_rect(h.parse(out.read_text())) is not None


def test_notebook_code_is_syntax_valid_and_has_no_secrets():
    import ast
    nbpath=Path(__file__).resolve().parents[1]/'notebooks/HARP_PCB_Colab.ipynb'
    notebook=json.loads(nbpath.read_text())
    assert notebook['nbformat']==4
    script='\n'.join(''.join(cell['source']) for cell in notebook['cells'] if cell['cell_type']=='code')
    ast.parse(script)
    assert 'files.upload()' in script and 'files.download(' in script
    assert 'getpass(' in script
    assert 'USE_LLM_REPORT = False' in script
    assert 'ALLOW_UNVERIFIED_FOOTPRINTS = False' in script
