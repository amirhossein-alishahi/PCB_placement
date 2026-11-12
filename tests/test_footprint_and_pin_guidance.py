"""Guarded footprint and electrical intent tests use fabricated fixtures only."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

import harp_pcb as h
from harp_electrical import infer_pin_guidance
from harp_footprints import apply_replacements, substitute_footprint
from test_harp_pcb import board


def _sample_two_pad_mod():
    return '''(footprint "TEST_2PAD" (layer "F.Cu") (attr smd)
      (fp_rect (start -1.5 -1) (end 1.5 1) (stroke (width 0.05)) (layer "F.CrtYd"))
      (fp_text reference "REF**" (at 0 -2) (layer "F.SilkS"))
      (pad "1" smd rect (at -1 0 0) (size 0.7 0.7) (layers "F.Cu" "F.Paste" "F.Mask"))
      (pad "2" smd rect (at 1 0 0) (size 0.7 0.7) (layers "F.Cu" "F.Paste" "F.Mask")))'''


def _synthetic_two_pad():
    return '''(footprint "SYNTH_TEST" (layer "F.Cu") (at 0 0 0)
       (property "Reference" "U1" (at 0 -3 0))
       (property "Value" "THE_PART" (at 0 3 0))
       (fp_text user "SYNTHETIC - NOT A REAL LAND PATTERN" (at 0 0) (layer "F.SilkS"))
       (pad "1" smd rect (at -2.54 0) (size 1.6 1.6) (layers "F.Cu") (net 1 "DATA"))
       (pad "2" smd rect (at 2.54 0) (size 1.6 1.6) (layers "F.Cu") (net 2 "GND")))'''


def test_guarded_kicad_module_substitution_preserves_pad_nets():
    converted=substitute_footprint(_synthetic_two_pad(),_sample_two_pad_mod(),
                                   'TestLib:TEST_2PAD',h.parse,h.span_children)
    p=h.parse(converted)
    assert p[1]=='TestLib:TEST_2PAD'
    assert len(h.children(p,'pad'))==2
    assert h.child(h.children(p,'pad')[0],'net') == ['net','1','DATA']
    assert h.child(h.children(p,'pad')[1],'net') == ['net','2','GND']
    assert h.get(h.children(p,'property')[0],'at') is None or len(h.children(p,'property'))>=2
    assert 'NOT A REAL LAND PATTERN' not in converted
    assert len(h.children(p,'fp_rect'))==1


def test_guarded_substitution_rejects_wrong_pad_set():
    wrong = _sample_two_pad_mod().replace('(pad "2"','(pad "3"')
    with pytest.raises(ValueError,match='Pad-number multiplicity mismatch'):
        substitute_footprint(_synthetic_two_pad(),wrong,'Lib:Bad',h.parse,h.span_children)


def test_synth_footprint_repair_is_best_effort_if_hash_wrong(tmp_path):
    src=board(3).replace('Test:Sample_0','SYNTH_TEST',1)
    inp=tmp_path/'board.kicad_pcb';inp.write_text(src)
    mod=tmp_path/'TEST_2PAD.kicad_mod';mod.write_text(_sample_two_pad_mod())
    conf={'components':{'U1':{'file':mod.name,'library_id':'TestLib:TEST_2PAD','sha256':'0'*64}}}
    output,report=apply_replacements(src,conf,base_dir=tmp_path,auto_reuse=False,
                                      parse=h.parse,span_children=h.span_children,
                                      footprint_audit=h.footprint_audit)
    assert output==src
    assert not report['applied']
    assert 'SHA256 mismatch' in report['skipped'][0]['reason']


def test_synth_footprint_repair_end_to_end_cli(tmp_path):
    src=board(5).replace('Test:Sample_0','SYNTH_TEST',1)
    inp=tmp_path/'board.kicad_pcb';inp.write_text(src)
    mod=tmp_path/'TEST_2PAD.kicad_mod';mod.write_text(_sample_two_pad_mod())
    replacement=tmp_path/'replacement.json'
    replacement.write_text(json.dumps({'components':{'U1':{'file':mod.name,'library_id':'TestLib:TEST_2PAD'}}}))
    output=tmp_path/'result.kicad_pcb';report=tmp_path/'report.json'
    result=subprocess.run([sys.executable,'-m','harp_pcb',str(inp),'-o',str(output),
                           '--footprint-replacements',str(replacement),
                           '--report',str(report),'--iterations','80'],
                           text=True,capture_output=True,timeout=90)
    assert result.returncode==0,result.stderr
    data=json.loads(report.read_text())
    assert len(data['footprint_resolution']['applied'])==1
    assert data['pads_and_nets_unchanged']
    assert data['manufacturing_ready'] is False
    assert data['output_status']=='GEOMETRY_ONLY_REVIEW_REQUIRED'
    assert h.decode_parts(h.parse(output.read_text()))[0].footprint=='TestLib:TEST_2PAD'
    # Board input vs output must preserve numbered electrical pad connectivity.
    a=h.decode_parts(h.parse(src));b=h.decode_parts(h.parse(output.read_text()))
    for old,new in zip(a,b):
        assert sorted((p.name,p.net) for p in old.pads)==sorted((p.name,p.net) for p in new.pads)


def test_same_value_real_donor_reuse_in_actual_user_board():
    path=Path('/mnt/data/unplaced.kicad_pcb')
    if not path.exists():pytest.skip('Private integration fixture not mounted')
    src=path.read_text()
    updated,rep=apply_replacements(src,{},base_dir=path.parent,auto_reuse=True,
                                   parse=h.parse,span_children=h.span_children,
                                   footprint_audit=h.footprint_audit)
    assert {item['ref'] for item in rep['applied']}=={'U6'}
    parts={p.name:p for p in h.decode_parts(h.parse(updated))}
    assert parts['U6'].footprint.startswith('BoardReuse:IDC-Header_2x05')
    assert abs(parts['U6'].outline[3]-parts['U6'].outline[1] - 21.66)<0.03
    assert parts['U3'].footprint.startswith('SYNTH_')
    assert h.infer_role(parts['U6'])[0]=='io'


def _part(name,net_names,role='general'):
    pads=[h.Pad(str(i+1),float(i),0,n,0.5,0.5,0) for i,n in enumerate(net_names)]
    return h.Part(name,'Fixture:Test','F.Cu',pads,(-2,-2,2,2),True,[],role=role)


def test_pin_aware_capacitor_prefers_unique_power_host_and_crystal_pair():
    c=_part('C1',['MCU_VCAP','GND'])
    u=_part('U3',['MCU_VCAP','OSC_IN','OSC_OUT']+[f'IO_{i}' for i in range(10)],'processor')
    crystal=_part('X1',['OSC_IN','OSC_OUT'])
    links,rep=infer_pin_guidance([c,u,crystal],edge_class=h.Edge)
    assert any(x['reason']=='unique-net-decoupling' for x in rep['pin_aware_links'])
    assert len([x for x in rep['pin_aware_links'] if x['reason']=='oscillator-pin-pair'])==2
    assert len(links)==3
    assert all(e.weight>=5.5 for e in links)


def test_pin_aware_rejects_wrong_net_and_does_not_invent_cap_parent():
    c=_part('C1',['VDD','GND'])
    u=_part('U3',['VDD']+[f'A{i}' for i in range(12)],'processor')
    u2=_part('U2',['VDD']+[f'B{i}' for i in range(12)],'core')
    links,rep=infer_pin_guidance([c,u,u2],edge_class=h.Edge)
    assert not links
    assert rep['ambiguous_capacitors'][0]['candidate_hosts']==['U2','U3']
    with pytest.raises(ValueError,match='crosses nets'):
        infer_pin_guidance([c,u,u2],edge_class=h.Edge,config={'pin_links':[
            {'from_ref':'C1','from_pad':'2','to_ref':'U3','to_pad':'1'}]})


def test_value_based_io_detection_for_synthetic_header():
    p=_part('U6',['VDD','GND'])
    p.footprint='SYNTH_10PAD_SBH11_PBPC_D05_ST_BK'
    p.node=[['property','Value','SBH11-PBPC-D05-ST-BK']]
    assert h.infer_role(p)[0]=='io'


def test_exact_st_mcu_labels_checked_without_claiming_full_pinout():
    from harp_pinout import check_known_pinout
    p=_part('U3',['VDD']*48)
    p.pads[4].net='U3_PH0_OSC_IN'
    p.pads[5].net='U3_PH1_OSC_OUT'
    p.pads[21].net='Net-(U3-VCAP_1)'
    p.node=[['property','Value','STM32F410CBT6']]
    report=check_known_pinout([p],h.part_value)[0]
    assert not report['conflicting_labels']
    assert len(report['matched_labels'])==3
    assert report['unverified_pin_count']==45
    p.pads[21].net='Net-(U3-PA13)'
    bad=check_known_pinout([p],h.part_value)[0]
    assert bad['status']=='conflicting_pin_labels'
    assert bad['conflicting_labels'][0]['expected_function']=='VCAP_1'
