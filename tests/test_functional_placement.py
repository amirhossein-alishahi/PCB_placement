"""Functional role and RF keepout regression tests; all boards are synthetic."""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

import harp_pcb as h
from test_harp_pcb import board


def role_test_board():
    # Uses public fixture, never an actual customer's design.
    source = board(9)
    source = source.replace('"U1" (at 0 -3 0)', '"ANT1" (at 0 -3 0)', 1)
    source = source.replace('"U4" (at 0 -3 0)', '"MCU1" (at 0 -3 0)', 1)
    source = source.replace('"U7" (at 0 -3 0)', '"SW1" (at 0 -3 0)', 1)
    source = source.replace('"R2" (at 0 -3 0)', '"J2" (at 0 -3 0)', 1)
    return source


def settings(distance=7.5):
    return {
        'components': {'ANT1': {'role': 'antenna'},
                       'MCU1': {'role': 'processor'},
                       'SW1': {'role': 'noisy'},
                       'J2': {'role': 'io'}},
        'antenna_keepouts': {'ANT1': {'distance_mm': distance,
                                      'against': 'noisy',
                                      'references': ['SW1']}}
    }


def test_role_inference_and_overrides():
    ps=h.decode_parts(h.parse(board()))
    ps[0].node[1] = ps[0].node[1]  # no mutation of netlist is necessary
    # Sensor/processor should require recognizable Value; an arbitrary U is NOT a CPU.
    assert h.infer_role(ps[0])[0] == 'general'
    known=board().replace('"TEST" (at 0 3 0)', '"STM32F410" (at 0 3 0)', 1)
    ps=h.decode_parts(h.parse(known))
    assert h.infer_role(ps[0])[0] == 'processor'
    cfg=settings()
    ps=h.decode_parts(h.parse(role_test_board()))
    meta=h.apply_placement_guidance(ps,cfg)
    assert meta['roles']['ANT1']['role']=='antenna'
    assert meta['roles']['MCU1']['role']=='processor'
    assert meta['roles']['J2']['role']=='io'
    assert meta['hard_antenna_keepouts']['ANT1']['distance_mm']==7.5
    assert h.required_rf_clearance(ps[0],next(p for p in ps if p.name=='SW1'))==7.5


def test_role_priors_prefer_edge_for_io_antenna_and_center_for_cpu():
    ps=h.decode_parts(h.parse(role_test_board()))
    h.apply_placement_guidance(ps,settings())
    io=next(p for p in ps if p.name=='J2')
    antenna=next(p for p in ps if p.name=='ANT1')
    cpu=next(p for p in ps if p.name=='MCU1')
    for p in (io,antenna):
        assert h.role_prior(p,3,15,0,70,40) < h.role_prior(p,35,20,0,70,40)
    assert h.role_prior(cpu,35,20,0,70,40) < h.role_prior(cpu,5,5,0,70,40)


def test_no_hard_clearance_is_invented_by_auto_role_inference():
    ps=h.decode_parts(h.parse(role_test_board()))
    data=h.apply_placement_guidance(ps, {'components':{'ANT1':{'role':'antenna'},
                                                       'MCU1':{'role':'processor'},
                                                       'SW1':{'role':'noisy'}}})
    assert data['rf_review_needed']==['ANT1']
    assert not data['hard_antenna_keepouts']
    assert h.required_rf_clearance(ps[0],next(p for p in ps if p.name=='SW1'))==0


@pytest.mark.parametrize('bad', [
    {'components':{'NONE':{'role':'io'}}},
    {'components':{'ANT1':{'role':'spaceduck'}}},
    {'antenna_keepouts':{'ANT1':{'distance_mm':-5}}},
    {'antenna_keepouts':{'ANT1':{'distance_mm': float('nan')}}},
    {'antenna_keepouts':{'ANT1':{'distance_mm':7,'against':'listed','references':['MISSING']}}},
    {'antenna_keepouts':{'ANT1':{'distance_mm':7,'against':'listed','references':[]}}},
    {'antenna_keepouts':{'MCU1':{'distance_mm':7}}},
])
def test_reject_invalid_role_rules(bad):
    ps=h.decode_parts(h.parse(role_test_board()))
    with pytest.raises(ValueError):
        h.apply_placement_guidance(ps,bad)


def test_hard_keepouts_are_enforced_in_solver_and_verified(tmp_path):
    original=role_test_board()
    ip=tmp_path/'input.kicad_pcb'
    op=tmp_path/'placed.kicad_pcb'
    rules=tmp_path/'roles.json'
    report=tmp_path/'report.json'
    stage=tmp_path/'stages'
    ip.write_text(original)
    rules.write_text(json.dumps(settings(6.5)))
    proc=subprocess.run([sys.executable,'-m','harp_pcb',str(ip),'-o',str(op),
                          '--placement-rules',str(rules),'--iterations','100',
                          '--snapshot-every','50','--stages-dir',str(stage),
                          '--report',str(report)], capture_output=True,text=True,timeout=130)
    assert proc.returncode==0,proc.stderr
    info=json.loads(report.read_text())
    assert info['validated_rf_keepouts']==['ANT1']
    assert info['functional_placement']['hard_antenna_keepouts']['ANT1']['distance_mm']==6.5
    assert info['functional_placement']['roles']['MCU1']['role']=='processor'
    parts=h.decode_parts(h.parse(op.read_text()))
    for p in parts:
        a=h.child(p.node,'at');p.x=float(a[1]);p.y=float(a[2]);p.angle=int(float(a[3]))
    ant=next(p for p in parts if p.name=='ANT1')
    for ref in ('SW1','MCU1'):
        other=next(p for p in parts if p.name==ref)
        assert h.bbox_gap(ant.bbox(),other.bbox())>=6.499
    for snapshot in info['stages']:
        h.verify(original,(stage/snapshot['pcb']).read_text(),placement_rules=settings(6.5))


def test_postwrite_verification_rejects_broken_rf_gap():
    source=role_test_board()
    cfg=settings(8)
    parts=h.decode_parts(h.parse(source))
    h.apply_placement_guidance(parts,cfg)
    placed=h.place(parts,h.make_edges(parts),iterations=10)
    correct=h.edit_board(source,parts,placed['width'],placed['height'],placed['origin_x'],placed['origin_y'])
    assert h.verify(source,correct,placement_rules=cfg)['validated_rf_keepouts']==['ANT1']
    # Validation must fail if the *rule* is stronger than the clearance that was placed.
    stronger=settings(99)
    with pytest.raises(AssertionError,match='RF keepout'):
        h.verify(source,correct,placement_rules=stronger)
