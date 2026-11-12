"""Pin-aware, conservative functional placement hints.

Only matches actual shared nets and numbered pads. No fabricated nets or DRC
clearances. Crystal/MCU links and decoupling links remain SOFT objectives;
validation requires real routing, power-domain checks and datasheets.
"""
from __future__ import annotations

from collections import defaultdict
import math
import re

POWER_RE = re.compile(r'^(?:GND|AGND|DGND|PGND|VSS.*|VDD.*|VCC.*|VBAT.*|\+?[0-9.]+V.*)$', re.I)
GROUND_RE = re.compile(r'^(?:GND|AGND|DGND|PGND|VSS.*)$', re.I)
OSC_RE = re.compile(r'OSC|XTAL|CRYSTAL|HSE|LSE',re.I)


def _pad_index(part, number):
    return next((i for i,p in enumerate(part.pads) if p.name==str(number)),None)


def infer_pin_guidance(parts, *, edge_class, config=None):
    """Returns (weighted pin edges, report). Never edits pads/net connectivity.

    Optional explicit user links format:
      {"pin_links": [{"from_ref":"C2", "from_pad":"1",
                      "to_ref":"U3", "to_pad":"24", "weight":6.0}]}
    Every link must connect two pads on the same named net. If invalid, the
    entire mapping is rejected before solving rather than inventing a net.
    """
    config=config or {}
    if not isinstance(config,dict) or set(config)-{'pin_links'} or not isinstance(config.get('pin_links',[]),list):
        raise ValueError('Electrical guidance expects {"pin_links": [...]}')
    by_ref={p.name:(i,p) for i,p in enumerate(parts)}
    by_net=defaultdict(list)
    for i,p in enumerate(parts):
        for k,pad in enumerate(p.pads):
            if pad.net:
                by_net[pad.net].append((i,k))
    edges=[]; diagnostics=[]; ambiguous=[]
    keys=set()

    def append(ref_a,pad_a,ref_b,pad_b,weight,origin):
        if ref_a not in by_ref or ref_b not in by_ref:
            raise ValueError(f'Unknown pin-link reference {ref_a} or {ref_b}')
        ia,a=by_ref[ref_a];ib,b=by_ref[ref_b]
        ka=_pad_index(a,pad_a);kb=_pad_index(b,pad_b)
        if ia==ib or ka is None or kb is None:
            raise ValueError(f'Invalid pad link {ref_a}:{pad_a} -> {ref_b}:{pad_b}')
        net_a=a.pads[ka].net
        if not net_a or net_a!=b.pads[kb].net:
            raise ValueError(f'Pin link crosses nets: {ref_a}:{pad_a} to {ref_b}:{pad_b}')
        if isinstance(weight,bool) or not isinstance(weight,(int,float)) or not math.isfinite(weight) or not (0.1<=weight<=30):
            raise ValueError('Pin-link weight must be finite in [0.1,30]')
        key=(min((ia,ka),(ib,kb)),max((ia,ka),(ib,kb)))
        if key in keys:return
        keys.add(key)
        edges.append(edge_class(ia,ib,ka,kb,float(weight)))
        diagnostics.append({'from':f'{ref_a}:{pad_a}','to':f'{ref_b}:{pad_b}',
                            'net':net_a,'weight':float(weight),'reason':origin})

    for link in config.get('pin_links',[]):
        if not isinstance(link,dict) or set(link)-{'from_ref','from_pad','to_ref','to_pad','weight'}:
            raise ValueError('Invalid explicit pin-link object')
        append(link['from_ref'],link['from_pad'],link['to_ref'],link['to_pad'],link.get('weight',6),'explicit')

    # A decoupling capacitor connected between a local supply and return net
    # is attached only when its host can be identified uniquely by a real net.
    # Shared VDD/GND among several ICs is ambiguous: don't invent the parent.
    for i,p in enumerate(parts):
        if not re.match(r'^C[A-Z_]*\d+$',p.name,re.I) and not re.match(r'^C(?:IN|OUT)$',p.name,re.I):
            continue
        if len(p.pads)!=2:continue
        if not (GROUND_RE.search(p.pads[0].net or '') or GROUND_RE.search(p.pads[1].net or '')):
            continue
        for k,pad in enumerate(p.pads):
            if not pad.net or GROUND_RE.search(pad.net):continue
            candidates=[]
            for j,q in enumerate(parts):
                if i==j or not q.name.upper().startswith('U') or len(q.pads)<4:continue
                q_matches=[m for m,other in enumerate(q.pads) if other.net==pad.net]
                if q_matches:
                    candidates.append((j,q,q_matches))
            if not candidates:continue
            def name_mentions(q):
                return bool(re.search(r'(?<![A-Z0-9])'+re.escape(q.name)+r'(?![A-Z0-9])',pad.net,re.I))
            strong=[c for c in candidates if name_mentions(c[1])]
            if strong:candidates=strong
            if len(candidates)!=1:
                ambiguous.append({'ref':p.name,'net':pad.net,
                                  'candidate_hosts':sorted(q.name for _,q,_ in candidates)})
                continue
            j,q,matching=candidates[0]
            if len(matching)!=1:
                ambiguous.append({'ref':p.name,'net':pad.net,
                                  'candidate_hosts':[q.name],
                                  'reason':'More than one host pad shares supply net; specify target pin'})
                continue
            try:
                append(p.name,pad.name,q.name,q.pads[matching[0]].name,5.5,'unique-net-decoupling')
            except ValueError:pass

    # Oscillator/quartz: require two distinct oscillator-labelled nets linked to
    # the SAME IC; never infer from a generic clock value alone.
    for i,p in enumerate(parts):
        if len(p.pads)<2 or len(p.pads)>6:continue
        oscillator=[(k,pad) for k,pad in enumerate(p.pads) if pad.net and OSC_RE.search(pad.net)]
        if len(set(pad.net for _,pad in oscillator))<2:continue
        hosts=[]
        for j,q in enumerate(parts):
            if j==i or not q.name.upper().startswith('U') or len(q.pads)<10:continue
            pairs=[]
            for k,pad in oscillator:
                options=[m for m,other in enumerate(q.pads) if other.net==pad.net]
                if len(options)==1:
                    pairs.append((k,options[0],pad.net))
            if len({n for _,_,n in pairs})>=2:
                hosts.append((q,pairs))
        if len(hosts)!=1:continue
        q,pairs=hosts[0]
        for ka,kb,_ in pairs:
            try:append(p.name,p.pads[ka].name,q.name,q.pads[kb].name,8.0,'oscillator-pin-pair')
            except ValueError:pass

    return edges,{
        'pin_aware_links':diagnostics,
        'ambiguous_capacitors':ambiguous,
        'pin_guidance_is_soft':True,
        'note':'These are placement affinities, not electrical safety/routing constraints.'
    }
