#!/usr/bin/env python3
"""HARP-PCB Lite: deterministic, un-routed KiCad PCB automatic placement.

Standalone (no pcbnew / KiCad CLI required); Python >=3.10, numpy,
matplotlib only for --preview. Shapely is used by the optional test suite.  Deliberately does NOT perform routing or DRC.

Safety: no edit to nets, pads, footprint internals or UUIDs. Only board-level
Edge.Cuts drawing expressions, footprint origins, and absolute Reference/Value
property positions change. Pad local coordinates and all connectivity are untouched.
A post-write structural integrity and geometry validator runs before success.

Usage:
  python harp_pcb.py input.kicad_pcb -o placed.kicad_pcb --preview board.png
  python harp_pcb.py --verify placed.kicad_pcb
"""
from __future__ import annotations

import argparse
import collections
from dataclasses import dataclass, field
import json
import math
from pathlib import Path
import random
import re
import sys
import time
from typing import Any

from harp_ai import DEFAULT_BASE_URL, DEFAULT_MODEL, review_preflight
from harp_electrical import infer_pin_guidance
from harp_footprints import apply_replacements
from harp_pinout import check_known_pinout

try:
    import numpy as np
except ImportError:
    np = None

_TOK = re.compile(r'\(|\)|"(?:[^"\\]|\\.)*"|[^\s()"]+')

class SString(str):
    pass


def parse(text: str):
    """Parse KiCad S-expressions with balanced-parenthesis validation.

    Strings are differentiated from unquoted atoms, and input text is preserved
    verbatim during output editing (this parser is read-only).
    """
    stack = [[]]
    pos = 0
    for tok in _TOK.finditer(text):
        if text[pos:tok.start()].strip():
            raise ValueError(f'Unexpected source content at offset {pos}')
        t = tok.group()
        if t == '(':
            stack.append([])
        elif t == ')':
            if len(stack) <= 1:
                raise ValueError('Unbalanced closing parenthesis')
            node = stack.pop()
            if not node:
                raise ValueError('Empty S-expression')
            stack[-1].append(node)
        elif t.startswith('"'):
            stack[-1].append(SString(t[1:-1].replace('\\"', '"').replace('\\\\', '\\')))
        else:
            stack[-1].append(t)
        pos = tok.end()
    if text[pos:].strip() or len(stack) != 1 or len(stack[0]) != 1:
        raise ValueError('Invalid or unbalanced KiCad S-expression')
    return stack[0][0]


def children(node, name):
    return [c for c in node if isinstance(c, list) and c and c[0] == name]


def child(node, name):
    return next((c for c in node if isinstance(c, list) and c and c[0] == name), None)


def get(node, name, default=None):
    c = child(node, name)
    return c[1] if c and len(c) > 1 else default


def footprint_reference(fp):
    """Support KiCad 8/9 property and KiCad 6/7 fp_text references."""
    for item in children(fp, 'property'):
        if len(item) > 2 and item[1] == 'Reference':
            return str(item[2])
    for item in children(fp, 'fp_text'):
        if len(item) > 2 and str(item[1]).lower() == 'reference':
            return str(item[2])
    return None


def footprint_value(fp):
    for item in children(fp, 'property'):
        if len(item) > 2 and item[1] == 'Value':
            return str(item[2])
    for item in children(fp, 'fp_text'):
        if len(item) > 2 and str(item[1]).lower() == 'value':
            return str(item[2])
    return ''


def fnum(value, default=0.0):
    try:
        return float(value)
    except (ValueError, TypeError):
        return float(default)


def xyz(node, tag, d=(0.0, 0.0)):
    c = child(node, tag)
    return (fnum(c[1]), fnum(c[2])) if c and len(c) >= 3 else d


def span_children(source: str, start: int = 0, end: int | None = None):
    """Return (tag,start,end) spans of direct children of one S-expression.

    This tokenizer is quote-aware, and knows nested expression depth. Thus
    replacing footprint-level `at` cannot accidentally modify pad/text `at`.
    """
    if end is None:
        end = len(source)
    depth = 0
    begin = None
    spans = []
    for t in _TOK.finditer(source, start, end):
        if t.group() == '(':
            depth += 1
            if depth == 2:
                begin = t.start()
        elif t.group() == ')':
            if depth == 2 and begin is not None:
                last = t.end()
                head = re.match(r'\(\s*([^\s()]+)', source[begin:last])
                if head:
                    spans.append((head.group(1), begin, last))
                begin = None
            depth -= 1
            if depth < 0:
                raise ValueError('Invalid span structure')
    if depth != 0:
        raise ValueError('Missing close in span lookup')
    return spans


def rot_xy(x: float, y: float, angle: int):
    """KiCad uses positive CCW in a Y-down board coordinate system."""
    a = angle % 360
    if a == 0: return x, y
    if a == 90: return y, -x
    if a == 180: return -x, -y
    if a == 270: return -y, x
    rad = math.radians(a)
    return x*math.cos(rad)+y*math.sin(rad), -x*math.sin(rad)+y*math.cos(rad)


@dataclass(frozen=True)
class EdgeRule:
    """A hard mechanical-facing rule. `outward_local` is the footprint-local vector
    pointing through an opening. It MUST be supported by a marker or entered by
    the designer, never guessed solely from an SD/USB/switch name.
    """
    side: str  # left | right | top | bottom | any
    outward_local: str  # left | right | top | bottom
    edge_offset_mm: float = 0.5
    evidence: str = "user"


DIRECTIONS = {"left": (-1, 0), "right": (1, 0),
              "top": (0, -1), "bottom": (0, 1)}
ACCESS_RE = re.compile(
    r"usb|microusb|usb_c|rj45|ethernet_jack|micro.?sd|sdcard|sd.?socket|"
    r"hdmi|displayport|audiojack|audio_jack|barreljack|barrel_jack|"
    r"switch.?slide|slide.?switch|sideswitch|side.?actuat", re.I)
SYNTH_RE = re.compile(r"synthetic\s*[-:]\s*not\s+a\s+real\s+land\s+pattern|"
                      r"not\s+a\s+real\s+land\s+pattern", re.I)


def marker_facing(part):
    """Find explicit PCB Edge mark INSIDE the footprint, never infer facing from name.

    Markers must be in a user/documentation layer with a local (at x y). The
    vector from the pad centroid to marker determines the nearest cardinal face.
    """
    candidates = []
    for node in part.node:
        if not isinstance(node, list) or not node or node[0] not in ('fp_text', 'property'):
            continue
        if not any('pcb edge' in str(v).lower() for v in node[1:3]):
            continue
        layer = str(get(node, 'layer', ''))
        if layer not in ('Dwgs.User', 'Cmts.User', 'F.Fab', 'B.Fab'):
            continue
        at = child(node, 'at')
        if not at or len(at) < 3:
            continue
        candidates.append((fnum(at[1]), fnum(at[2])))
    if not candidates or not part.pads:
        return None
    px = sum(p.x for p in part.pads) / len(part.pads)
    py = sum(p.y for p in part.pads) / len(part.pads)
    vx = sum(x for x, _ in candidates) / len(candidates) - px
    vy = sum(y for _, y in candidates) / len(candidates) - py
    if max(abs(vx), abs(vy)) < 0.75:
        return None
    if abs(vx) > 1.25 * abs(vy):
        return 'right' if vx > 0 else 'left'
    if abs(vy) > 1.25 * abs(vx):
        return 'bottom' if vy > 0 else 'top'
    return None


def footprint_audit(parts):
    flagged = []
    directional = []
    for p in parts:
        suspicious = p.footprint.upper().startswith('SYNTH_')
        suspicious |= any(isinstance(n, list) and n and n[0] in ('fp_text', 'property')
                          and any(SYNTH_RE.search(str(x)) for x in n[1:3])
                          for n in p.node)
        if suspicious:
            flagged.append({'ref': p.name, 'footprint': p.footprint,
                            'reason': 'Placeholder/synthetic land pattern; check against datasheet'})
        if ACCESS_RE.search(p.footprint) or ACCESS_RE.search(p.name):
            directional.append({'ref': p.name, 'footprint': p.footprint,
                                'marker_facing': marker_facing(p),
                                'resolved': False})
    return flagged, directional


def apply_direction_rules(parts, config=None):
    """Attach mechanically enforced edge constraints; return audit information."""
    config = config or {}
    if not isinstance(config, dict):
        raise ValueError('Direction rules must be a JSON object')
    unknown = set(config) - {'components'}
    if unknown:
        raise ValueError(f'Unknown direction rules keys: {sorted(unknown)}')
    rules = config.get('components', {})
    if not isinstance(rules, dict):
        raise ValueError('"components" must be an object indexed by reference')
    known = {p.name for p in parts}
    if set(rules) - known:
        raise ValueError(f'Unknown component references: {sorted(set(rules)-known)}')
    synthetic, directional = footprint_audit(parts)
    for p in parts:
        cfg = rules.get(p.name)
        if cfg is not None:
            if not isinstance(cfg, dict):
                raise ValueError(f'{p.name}: direction rule must be an object')
            side = str(cfg.get('side', 'any')).lower()
            outward = str(cfg.get('outward_local', '')).lower()
            gap = float(cfg.get('edge_offset_mm', 0.5))
            if side not in (*DIRECTIONS, 'any') or outward not in DIRECTIONS:
                raise ValueError(f'{p.name}: side and outward_local must be cardinal directions')
            if not 0 <= gap <= 5 or not math.isfinite(gap):
                raise ValueError(f'{p.name}: edge_offset_mm must be between 0 and 5')
            p.edge_rule = EdgeRule(side, outward, gap, 'explicit-user-rule')
        else:
            facing = marker_facing(p)
            if facing:
                p.edge_rule = EdgeRule('any', facing, 0.5, 'footprint-PCB-Edge-marker')
    for item in directional:
        p = next(p for p in parts if p.name == item['ref'])
        item['resolved'] = bool(p.edge_rule)
        item['resolution'] = p.edge_rule.evidence if p.edge_rule else 'unknown-facing-needs-review'
    return {'synthetic_footprints': synthetic,
            'directional_candidates': directional,
            'edge_rules': {p.name: vars(p.edge_rule) for p in parts if p.edge_rule},
            'unresolved_directions': [r['ref'] for r in directional if not r['resolved']]}


def outward_side(part, angle):
    if not part.edge_rule:
        return None
    v = DIRECTIONS[part.edge_rule.outward_local]
    x, y = rot_xy(*v, angle)
    return max(DIRECTIONS, key=lambda direction: x*DIRECTIONS[direction][0]
               + y*DIRECTIONS[direction][1])


def allowed_angles(part):
    if not part.edge_rule or part.edge_rule.side == 'any':
        return (0, 90, 180, 270)
    return tuple(a for a in (0, 90, 180, 270)
                 if outward_side(part, a) == part.edge_rule.side)


def edge_gap(b, side, W, H):
    return {'left': b[0], 'right': W-b[2], 'top': b[1], 'bottom': H-b[3]}[side]


def valid_envelope(part, x, y, angle, W, H, margin):
    b = part.bbox(x, y, angle)
    if not part.edge_rule:
        return within(b, W, H, margin)
    side = outward_side(part, angle)
    if part.edge_rule.side != 'any' and side != part.edge_rule.side:
        return False
    desired = part.edge_rule.edge_offset_mm
    if abs(edge_gap(b, side, W, H) - desired) > 0.16:
        return False
    if min(edge_gap(b, d, W, H) for d in DIRECTIONS if d != side) < margin - 1e-5:
        return False
    return True


def snap_to_edge(part, x, y, angle, W, H):
    """Project candidate onto the correct edge while retaining tangential freedom."""
    side = outward_side(part, angle)
    if side is None:
        return x, y
    b = part.bbox(x, y, angle)
    delta = edge_gap(b, side, W, H) - part.edge_rule.edge_offset_mm
    if side == 'left': x -= delta
    elif side == 'right': x += delta
    elif side == 'top': y -= delta
    else: y += delta
    return x, y


def edge_candidates(part, W, H, margin, rng):
    """Generate legal edge anchors for explicit/marker-derived rules."""
    candidates = []
    for a in allowed_angles(part):
        side = outward_side(part, a)
        o = part.offsets(a)
        if side in ('left', 'right'):
            low, high = margin-o[1], H-margin-o[3]
            if low > high:
                continue
            count = max(2, int((high-low)/2.5)+1)
            samples = [low+(high-low)*j/(count-1) for j in range(count)]
            samples.sort(key=lambda y: abs(y+(o[1]+o[3])/2-H/2))
            for y in samples:
                x = part.edge_rule.edge_offset_mm-o[0] if side == 'left' else W-part.edge_rule.edge_offset_mm-o[2]
                candidates.append((x, y, a))
        else:
            low, high = margin-o[0], W-margin-o[2]
            if low > high:
                continue
            count = max(2, int((high-low)/2.5)+1)
            samples = [low+(high-low)*j/(count-1) for j in range(count)]
            samples.sort(key=lambda x: abs(x+(o[0]+o[2])/2-W/2))
            for x in samples:
                y = part.edge_rule.edge_offset_mm-o[1] if side == 'top' else H-part.edge_rule.edge_offset_mm-o[3]
                candidates.append((x, y, a))
    return candidates


@dataclass
class Pad:
    name: str
    x: float
    y: float
    net: str | None
    w: float
    h: float
    angle: float


@dataclass
class Part:
    name: str
    footprint: str
    layer: str
    pads: list[Pad]
    outline: tuple[float, float, float, float]
    has_courtyard: bool
    node: Any = field(repr=False)
    x: float = 0.0
    y: float = 0.0
    angle: int = 0
    edge_rule: EdgeRule | None = None
    # Functional hints are soft priors, not mechanical or electrical proof.
    role: str = 'general'
    role_evidence: str = 'default'
    rf_keepout_mm: float = 0.0
    rf_against: str = 'noisy'
    rf_references: tuple[str, ...] = ()

    @property
    def area(self):
        x0, y0, x1, y1 = self.outline
        return (x1-x0)*(y1-y0)

    @property
    def is_connector(self):
        return bool(re.search('connector|pinheader|header|terminal|usb|jack|socket|idc',
                              self.footprint, re.I)) or self.name.startswith('J')

    def offsets(self, a=None):
        if a is None: a = self.angle
        x0,y0,x1,y1 = self.outline
        pts = [rot_xy(x,y,a) for x,y in [(x0,y0),(x0,y1),(x1,y0),(x1,y1)]]
        return min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts)

    def bbox(self, x=None, y=None, a=None):
        if x is None: x=self.x
        if y is None: y=self.y
        o=self.offsets(a)
        return (x+o[0], y+o[1], x+o[2], y+o[3])

    def pin_pos(self, pad: Pad, x=None, y=None, a=None):
        if x is None: x=self.x
        if y is None: y=self.y
        if a is None: a=self.angle
        px,py=rot_xy(pad.x,pad.y,a)
        return x+px,y+py


# Soft functional priors are intentionally distinct from the explicit hard
# antenna spacing rules below. No fixed 3/5/15 mm is assumed to be safe.
ROLES = {'io', 'processor', 'core', 'antenna', 'noisy', 'general'}
PROCESSOR_RE = re.compile(
    r'stm32|stm8|atmega|attiny|esp32|esp8266|rp2040|rp2350|nrf(?:5[0-9]|91)|'
    r'samd|same5|samd5|pic(?:16|18|24|32)|mcu|microcontroller|cortex.?m|'
    r'imx|raspberry.?pi.?pico', re.I)
ANTENNA_RE = re.compile(r'antenna|chip.?ant|pcb.?ant', re.I)
NOISE_RE = re.compile(r'dc.?dc|dcdc|buck(?:.?converter)?|boost(?:.?converter)?|'
                      r'switching.?regulator|switch.?mode|smps|oscillator', re.I)


def part_value(part: Part) -> str:
    return footprint_value(part.node)


def infer_role(part: Part) -> tuple[str, str]:
    value = part_value(part)
    footprint = part.footprint
    if ANTENNA_RE.search(footprint) or ANTENNA_RE.search(value) or re.match(r'^(ANT|AE)\d+$',part.name,re.I):
        return 'antenna', 'footprint/value/reference-name heuristic'
    if part.is_connector or re.search(r'SBH11|IDC[-_ ]?Header|B2B[-_ ]?XH|Pin.?Header|Box.?Header', value, re.I):
        return 'io', 'connector/reference/value heuristic'
    if PROCESSOR_RE.search(value):
        return 'processor', 'recognizable processor part number/value'
    if NOISE_RE.search(value):
        return 'noisy', 'switching/oscillator part number/value heuristic'
    if part.name.upper().startswith('U') and len(part.pads)>=12:
        return 'core', 'multi-pin integrated circuit heuristic'
    return 'general', 'no applicable signal'


def apply_placement_guidance(parts: list[Part], config=None) -> dict:
    """Attach *hints* and user-authored hard RF spacings to placement parts.

    The role inference does not assert datasheet correctness. A hard antenna
    keepout exists only if the user supplies distance_mm in this config.
    """
    config = config or {}
    if not isinstance(config,dict) or set(config)-{'components','antenna_keepouts'}:
        raise ValueError('Placement rules need an object with components and/or antenna_keepouts')
    overrides = config.get('components', {})
    rf_rules = config.get('antenna_keepouts', {})
    if not isinstance(overrides,dict) or not isinstance(rf_rules,dict):
        raise ValueError('components and antenna_keepouts must both be objects')
    known = {p.name for p in parts}
    unknown = (set(overrides)|set(rf_rules))-known
    if unknown:
        raise ValueError(f'Unknown component references in placement rules: {sorted(unknown)}')
    for p in parts:
        role,evidence = infer_role(p)
        rule = overrides.get(p.name, {})
        if not isinstance(rule,dict) or set(rule)-{'role'}:
            raise ValueError(f'{p.name}: component placement rule must contain only role')
        if rule:
            role = str(rule['role']).lower()
            evidence = 'explicit user role'
        if role not in ROLES:
            raise ValueError(f'{p.name}: invalid role {role!r}; expected {sorted(ROLES)}')
        p.role,p.role_evidence = role,evidence
        p.rf_keepout_mm=0.0;p.rf_against='noisy';p.rf_references=()
    for ref,rule in rf_rules.items():
        p=next(p for p in parts if p.name==ref)
        if p.role!='antenna':
            raise ValueError(f'{ref}: hard antenna keepout requires role=antenna')
        if not isinstance(rule,dict) or set(rule)-{'distance_mm','against','references'}:
            raise ValueError(f'{ref}: antenna keepout has unsupported fields')
        distance = rule.get('distance_mm')
        if isinstance(distance,bool) or not isinstance(distance,(int,float)) or not math.isfinite(distance) or distance<0 or distance>100:
            raise ValueError(f'{ref}: distance_mm must be a finite number between 0 and 100')
        against = str(rule.get('against','noisy')).lower()
        refs = rule.get('references',[])
        if against not in ('noisy','all','listed') or not isinstance(refs,list) or any(not isinstance(x,str) for x in refs):
            raise ValueError(f'{ref}: against must be noisy/all/listed and references a list')
        if set(refs)-known or ref in refs:
            raise ValueError(f'{ref}: references must name other existing footprints')
        if against=='listed' and not refs and distance>0:
            raise ValueError(f'{ref}: listed antenna keepout needs references')
        p.rf_keepout_mm=float(distance)
        p.rf_against=against
        p.rf_references=tuple(dict.fromkeys(refs))
    return {'roles': {p.name:{'role':p.role,'evidence':p.role_evidence} for p in parts},
            'hard_antenna_keepouts': {p.name:{'distance_mm':p.rf_keepout_mm,
                                            'against':p.rf_against,'references':list(p.rf_references)}
                                       for p in parts if p.rf_keepout_mm>0},
            'rf_review_needed': [p.name for p in parts if p.role=='antenna' and not p.rf_keepout_mm],
            'automatic_roles_are_soft_only': True,
            'antenna_note': 'Automatically inferred RF hints are NOT copper/trace keepouts or datasheet guarantees.'}


def bbox_gap(a,b):
    dx=max(a[0]-b[2],b[0]-a[2],0.0)
    dy=max(a[1]-b[3],b[1]-a[3],0.0)
    return math.hypot(dx,dy)


def required_rf_clearance(a: Part,b: Part) -> float:
    """Minimum *footprint envelope* distance from verified user input only."""
    def one_way(antenna, other):
        if antenna.role!='antenna' or antenna.rf_keepout_mm<=0:
            return 0.0
        match = (antenna.rf_against=='all' or
                 (antenna.rf_against=='noisy' and other.role in ('noisy','processor')) or
                 (antenna.rf_against=='listed' and other.name in antenna.rf_references) or
                 other.name in antenna.rf_references)
        return antenna.rf_keepout_mm if match else 0.0
    return max(one_way(a,b),one_way(b,a))


def role_prior(part: Part,x,y,angle,W,H) -> float:
    b=part.bbox(x,y,angle)
    edge_distance=max(0.0,min(b[0],b[1],W-b[2],H-b[3]))
    central_distance=math.hypot((b[0]+b[2])/2-W/2,(b[1]+b[3])/2-H/2)
    return {'io':0.42*edge_distance,'processor':0.72*central_distance,
            'core':0.13*central_distance,'antenna':0.58*edge_distance,
            'noisy':0.0,'general':0.0}[part.role]


def soft_antenna_pair(a: Part,box_a,b: Part,box_b) -> float:
    # Heuristic spacing for antenna vs processors / switching noise sources.
    # This is NOT a validated minimum RF spacing and does not affect legality.
    if (a.role=='antenna' and b.role in ('processor','noisy')) or \
       (b.role=='antenna' and a.role in ('processor','noisy')):
        return 2.4*max(0.0,5.0-bbox_gap(box_a,box_b))
    return 0.0


def role_candidate_cost(parts,idx,x,y,angle,W,H,active=None):
    p=parts[idx];b=p.bbox(x,y,angle)
    cost=role_prior(p,x,y,angle,W,H)
    if p.role in ('antenna','noisy','processor'):
        active=range(len(parts)) if active is None else active
        for j in active:
            if j!=idx:
                q=parts[j]
                if q.role in ('antenna','noisy','processor'):
                    cost+=soft_antenna_pair(p,b,q,q.bbox())
    return cost


def total_role_cost(parts,W,H,origin_x=0.0,origin_y=0.0):
    cost=sum(role_prior(p,p.x-origin_x,p.y-origin_y,p.angle,W,H) for p in parts)
    for i,p in enumerate(parts):
        if p.role!='antenna':continue
        for j,q in enumerate(parts):
            if i!=j and q.role in ('noisy','processor'):
                cost+=soft_antenna_pair(p,p.bbox(),q,q.bbox())
    return cost


def shape_bounds(node):
    """Bounding envelope of courtyard primitives; pad geometry is also included."""
    kind=node[0]
    if kind == 'fp_rect':
        a,b=xyz(node,'start'),xyz(node,'end')
        return (min(a[0],b[0]),min(a[1],b[1]),max(a[0],b[0]),max(a[1],b[1]))
    if kind == 'fp_line':
        a,b=xyz(node,'start'),xyz(node,'end')
        return (min(a[0],b[0]),min(a[1],b[1]),max(a[0],b[0]),max(a[1],b[1]))
    if kind == 'fp_circle':
        a,b=xyz(node,'center'),xyz(node,'end')
        r=math.hypot(a[0]-b[0],a[1]-b[1])
        return (a[0]-r,a[1]-r,a[0]+r,a[1]+r)
    if kind == 'fp_poly':
        pt=child(node,'pts')
        points = [(fnum(t[1]),fnum(t[2])) for t in pt[1:] if isinstance(t,list) and t[0]=='xy'] if pt else []
        if points:
            return (min(x for x,y in points),min(y for x,y in points),
                    max(x for x,y in points),max(y for x,y in points))
    if kind == 'fp_arc':
        # Conservative circle envelope through start/mid/end, rather than missing arc bulges.
        pts=[xyz(node,t) for t in ('start','mid','end')]
        (a,b),(c,d),(e,f)=pts
        D=2*(a*(d-f)+c*(f-b)+e*(b-d))
        if abs(D)>1e-9:
            ux=((a*a+b*b)*(d-f)+(c*c+d*d)*(f-b)+(e*e+f*f)*(b-d))/D
            uy=((a*a+b*b)*(e-c)+(c*c+d*d)*(a-e)+(e*e+f*f)*(c-a))/D
            r=math.hypot(a-ux,b-uy)
            return (ux-r,uy-r,ux+r,uy+r)
        return (min(a,c,e),min(b,d,f),max(a,c,e),max(b,d,f))
    return None


def decode_parts(tree):
    if children(tree, 'module'):
        raise ValueError('KiCad 4/5 (module ...) footprints are unsupported. Resave the board with KiCad 9 before placement; no parts were moved.')
    parts=[]
    for fp in children(tree,'footprint'):
        ref=footprint_reference(fp)
        if not ref:
            raise ValueError('Footprint has no Reference (property or fp_text reference)')
        if 'locked' in fp or child(fp, 'locked') is not None:
            raise ValueError(f'{ref}: locked mechanical placement is not supported; refusing to move a locked footprint')
        pads=[]
        bounds=[]
        for p in children(fp,'pad'):
            at=child(p,'at')
            size=child(p,'size')
            if not at or len(at)<3 or not size or len(size)<3:
                raise ValueError(f'Unsupported pad geometry in {ref}')
            px,py=fnum(at[1]),fnum(at[2])
            w,h=fnum(size[1]),fnum(size[2]); ang=fnum(at[3]) if len(at)>3 else 0.0
            nt=child(p,'net')
            net=str(nt[2]) if nt and len(nt)>2 and str(nt[2]) else None
            pads.append(Pad(str(p[1]),px,py,net,w,h,ang))
            # Pad local angle is independent of footprint rotation.
            cs=[rot_xy(dx,dy,ang) for dx in (-w/2,w/2) for dy in (-h/2,h/2)]
            bounds.extend([(px+dx,py+dy) for dx,dy in cs])
        courtyard=[]
        for n in fp:
            if not isinstance(n,list) or not n or not n[0].startswith('fp_'):continue
            if get(n,'layer') in ('F.CrtYd','B.CrtYd'):
                b=shape_bounds(n)
                if b: courtyard.append(b)
        for x0,y0,x1,y1 in courtyard:
            bounds.extend([(x0,y0),(x1,y1)])
        if not pads and not bounds:
            # Padless logos and mechanical graphics are included only when their
            # measurable Fab/Silk geometry provides a conservative envelope.
            for primitive in fp:
                if not isinstance(primitive,list) or not primitive or not primitive[0].startswith('fp_'):
                    continue
                if get(primitive, 'layer') in ('F.Fab','B.Fab','F.SilkS','B.SilkS'):
                    bounds_rect=shape_bounds(primitive)
                    if bounds_rect:
                        x0,y0,x1,y1=bounds_rect
                        bounds.extend([(x0,y0),(x1,y1)])
            if not bounds:
                raise ValueError(f'Footprint {ref} has no pads or measurable graphics/courtyard')
        # When courtyard missing, protect the pads plus a manufacturing proxy margin.
        m=0.15 if courtyard else 0.6
        minx,miny=min(p[0] for p in bounds)-m,min(p[1] for p in bounds)-m
        maxx,maxy=max(p[0] for p in bounds)+m,max(p[1] for p in bounds)+m
        parts.append(Part(ref,str(fp[1]),str(get(fp,'layer','F.Cu')),pads,
                          (minx,miny,maxx,maxy),bool(courtyard),fp))
    names=[p.name for p in parts]
    if len(names)!=len(set(names)):
        raise ValueError('Duplicate footprint references cannot be safely matched')
    if not parts:
        raise ValueError('No footprints in KiCad board')
    return parts


@dataclass
class Edge:
    i:int
    j:int
    pi:int
    pj:int
    weight:float


def is_power(net):
    return bool(re.search(r'(^GND$|^VSS|^GROUND|^AGND|^DGND|^PGND|^VCC|^VDD|^VBAT|^\+?[0-9.]+V)',net,re.I))


def make_edges(parts):
    bynet=collections.defaultdict(list)
    for i,p in enumerate(parts):
        for k,pad in enumerate(p.pads):
            if pad.net:
                bynet[pad.net].append((i,k))
    edges=[]
    for net, points in bynet.items():
        if len(points)<2: continue
        pairs=[]
        for i in range(len(points)):
            for j in range(i+1,len(points)):
                a,b=points[i],points[j]
                if a[0]!=b[0]: pairs.append((a,b))
        if not pairs:continue
        # Large power nets are NOT all-pairs gravitational springs.
        if len(points)>10 or is_power(net):
            wt=0.04/max(1,len(points)-1)
        elif len(points)>4:
            wt=0.45/max(1,len(points)-1)
        else:
            wt=1.0/max(1,len(points)-1)
        for ((i,pi),(j,pj)) in pairs:
            edges.append(Edge(i,j,pi,pj,wt))
    return edges


def rects_intersect(a,b,clearance=0.0):
    return not (a[2]+clearance <= b[0]+1e-8 or b[2]+clearance <= a[0]+1e-8 or
                a[3]+clearance <= b[1]+1e-8 or b[3]+clearance <= a[1]+1e-8)


def within(b,W,H,m=2.0):
    return b[0]>=m-1e-7 and b[1]>=m-1e-7 and b[2]<=W-m+1e-7 and b[3]<=H-m+1e-7


def board_area_from_parts(parts):
    return sum(p.area for p in parts)


def target_dims(parts,density=0.42,margin=2.0):
    a=board_area_from_parts(parts)
    big=max(parts,key=lambda p:max(p.offsets(90)[2]-p.offsets(90)[0],p.offsets(90)[3]-p.offsets(90)[1]))
    # Board shape is inferred: prefer placing the longest unusual footprint horizontally.
    span=max((max(p.outline[2]-p.outline[0],p.outline[3]-p.outline[1]) for p in parts))
    W=max(math.sqrt(a/density*2.15),span+2*margin+8)
    H=max(a/density/W,26.0)
    W=math.ceil(W/2)*2; H=math.ceil(H/2)*2
    return W,H


def grouped_edges(edges,n):
    touching=[[] for _ in range(n)]
    for e in edges:
        touching[e.i].append(e);touching[e.j].append(e)
    return touching


def wire_between(e,parts,changes=None):
    p,q=parts[e.i],parts[e.j]
    cfgp=changes.get(e.i,(p.x,p.y,p.angle)) if changes else (p.x,p.y,p.angle)
    cfgq=changes.get(e.j,(q.x,q.y,q.angle)) if changes else (q.x,q.y,q.angle)
    a=p.pin_pos(p.pads[e.pi],*cfgp);b=q.pin_pos(q.pads[e.pj],*cfgq)
    return (abs(a[0]-b[0])+abs(a[1]-b[1]))*e.weight


def wire_sum(edges,parts,changes=None):
    return sum(wire_between(e,parts,changes) for e in edges)


def legal_candidate(parts,idx,x,y,angle,placed,W,H,clearance,margin):
    b=parts[idx].bbox(x,y,angle)
    if not valid_envelope(parts[idx],x,y,angle,W,H,margin):return False
    for j in placed:
        if j==idx: continue
        q=parts[j];other=q.bbox()
        if rects_intersect(b,other,clearance): return False
        need=required_rf_clearance(parts[idx],q)
        if need and bbox_gap(b,other)<need-1e-7: return False
    return True


def greedy_place(parts,edges,W,H,clearance,margin,rng):
    n=len(parts)
    touch=grouped_edges(edges,n)
    # Sort biggest first; connected medium parts placed early for useful anchors.
    # Place hard mechanical anchors first. Never move an external-facing opening
    # into the middle of the board just to reduce electrical HPWL.
    placed = set()
    pinned = sorted((i for i,p in enumerate(parts) if p.edge_rule),
                    key=lambda i: -parts[i].area)
    for i in pinned:
        p=parts[i]
        best=None
        for x,y,a in edge_candidates(p,W,H,margin,rng):
            if not legal_candidate(parts,i,x,y,a,placed,W,H,clearance,margin):
                continue
            score=sum(wire_between(e,parts,{i:(x,y,a)}) for e in touch[i]
                      if (e.i if e.j==i else e.j) in placed)
            score += role_candidate_cost(parts,i,x,y,a,W,H,placed)
            score += 0.002*math.hypot(x-W/2,y-H/2)
            if best is None or score < best[0]:best=(score,x,y,a)
        if best is None:
            return False
        _,p.x,p.y,p.angle=best
        placed.add(i)
    free=[i for i in range(n) if i not in placed]
    if free:
        root=max(free,key=lambda i:(int(parts[i].role=='processor'), int(parts[i].role=='core'),parts[i].area,len(parts[i].pads)))
        p=parts[root]
        p.angle=90 if (p.outline[3]-p.outline[1]) > 1.25*(p.outline[2]-p.outline[0]) else 0
        o=p.offsets();p.x=W/2-(o[0]+o[2])/2;p.y=H/2-(o[1]+o[3])/2
        if not legal_candidate(parts,root,p.x,p.y,p.angle,placed,W,H,clearance,margin):
            # Root may overlap an edge anchor; let the general candidate search handle it.
            free.remove(root)
            free.insert(0,root)
        else:
            placed.add(root)
    rem=set(range(n))-placed
    while rem:
        def priority(i):
            conn=sum(e.weight for e in touch[i] if (e.i if e.j==i else e.j) in placed)
            return (conn*8+math.log1p(parts[i].area)*0.1+0.005*len(parts[i].pads))
        i=max(rem,key=lambda k:priority(k)+rng.random()*0.001)
        p=parts[i]
        best=None
        for angle in ((0,90,180,270) if p.area>4 or p.is_connector else (0,90)):
            o=p.offsets(angle);ox=(o[0]+o[2])/2;oy=(o[1]+o[3])/2
            # Pin-aligned target plus uniform fallback raster.
            target=[]
            ww=0
            for e in touch[i]:
                j=e.j if e.i==i else e.i
                if j not in placed:continue
                q=parts[j];pj=e.pj if e.i==i else e.pi
                pi=e.pi if e.i==i else e.pj
                qx,qy=q.pin_pos(q.pads[pj])
                dx,dy=rot_xy(p.pads[pi].x,p.pads[pi].y,angle)
                w=e.weight
                if w>0.001:
                    target.append((qx-dx,qy-dy,w));ww+=w
            if target and ww>0:
                tx=sum(x*w for x,y,w in target)/ww+ox
                ty=sum(y*w for x,y,w in target)/ww+oy
            else:tx,ty=W/2,H/2
            # Candidate center positions; seeded search, geometry is checked for each.
            step=2.8 if p.area<80 else 3.7
            coords=[]
            for r in [0,2.5,5,7.5,10,14,19,26,36,50]:
                num=1 if r==0 else max(8,int(2*math.pi*r/step))
                phase=rng.uniform(0,2*math.pi)
                for k in range(num):
                    phi=phase+2*math.pi*k/num
                    coords.append((tx+r*math.cos(phi),ty+r*math.sin(phi)))
            # Raster makes algorithm complete for most possible rectangles.
            st=max(1.5,min(3.0,math.sqrt(p.area)*0.32))
            xx=np.arange(margin+(o[2]-o[0])/2,W-margin-(o[2]-o[0])/2+0.01,st) if np is not None else []
            yy=np.arange(margin+(o[3]-o[1])/2,H-margin-(o[3]-o[1])/2+0.01,st) if np is not None else []
            if len(xx)*len(yy)<5000:
                coords.extend((float(x),float(y)) for x in xx for y in yy)
            for cx,cy in coords:
                x,y=cx-ox,cy-oy
                if not legal_candidate(parts,i,x,y,angle,placed,W,H,clearance,margin):continue
                cost=sum(wire_between(e,parts,{i:(x,y,angle)}) for e in touch[i]
                         if (e.i if e.j==i else e.j) in placed)
                # Functional role priors: I/O near an edge, processors near
                # center, antennas away from known noise candidates. Still soft.
                cost+=role_candidate_cost(parts,i,x,y,angle,W,H,placed)
                cost+=0.001*math.hypot(cx-W/2,cy-H/2)
                # Tight designs: prefer locations adjacent to existing bbox union.
                if best is None or cost<best[0]:best=(cost,x,y,angle)
        if best is None:return False
        _,p.x,p.y,p.angle=best
        placed.add(i);rem.remove(i)
    return True


def optimize_local(parts,edges,W,H,clearance,margin,iterations,seed,checkpoint=None,interval=0):
    rng=random.Random(seed)
    n=len(parts); touch=grouped_edges(edges,n)
    areas=[p.area for p in parts]
    big=max(range(n),key=lambda i:areas[i])
    objective=wire_sum(edges,parts)
    accepted=0
    for step in range(iterations):
        idx=rng.randrange(n)
        p=parts[idx]
        # Keep a weak anchor on the biggest footprint to avoid drifting entire board.
        x,y,a=p.x,p.y,p.angle
        v=rng.random()
        if v<0.14:
            an=(a+rng.choice([90,180,270]))%360
            # Maintain bounding center when rotating an asymmetric part.
            old=p.offsets(a);new=p.offsets(an)
            nx=x+(old[0]+old[2]-new[0]-new[2])/2
            ny=y+(old[1]+old[3]-new[1]-new[3])/2
            na=an
        elif v<0.32:
            # Target nearby connected pin from another part.
            near=[e for e in touch[idx] if e.weight>0.001]
            if near:
                e=rng.choice(near);j=e.j if e.i==idx else e.i;q=parts[j]
                pp=p.pads[e.pi if e.i==idx else e.pj]
                pq=q.pads[e.pj if e.i==idx else e.pi]
                tx,ty=q.pin_pos(pq)
                px,py=rot_xy(pp.x,pp.y,a)
                nx=tx-px+rng.gauss(0,3);ny=ty-py+rng.gauss(0,3);na=a
            else:nx,ny,na=x+rng.gauss(0,3),y+rng.gauss(0,3),a
        else:
            sigma=2.0 if (step/iterations)>0.35 else 0.8
            nx,ny,na=x+rng.gauss(0,sigma),y+rng.gauss(0,sigma),a
        if p.edge_rule:
            if na not in allowed_angles(p):
                continue
            nx,ny=snap_to_edge(p,nx,ny,na,W,H)
        if not legal_candidate(parts,idx,nx,ny,na,(k for k in range(n) if k!=idx),W,H,clearance,margin):continue
        current=sum(wire_between(e,parts) for e in touch[idx]);
        new=sum(wire_between(e,parts,{idx:(nx,ny,na)}) for e in touch[idx]);
        # Include pairwise RF soft separation for *either* endpoint moving.
        current+=role_candidate_cost(parts,idx,x,y,a,W,H)
        new+=role_candidate_cost(parts,idx,nx,ny,na,W,H)
        if idx==big: new+=0.02*math.hypot(nx-x,ny-y)
        temp=1.0*(1-step/iterations)**1.3+0.007
        if new<=current or rng.random()<math.exp(min(0.0,(current-new)/temp)):
            p.x,p.y,p.angle=nx,ny,na
            accepted+=1
        if checkpoint and interval and (step + 1) % interval == 0:
            checkpoint(f'anneal_{step+1:06}', parts, W, H)
    return accepted


def compact(parts,edge_margin,clearance):
    """Shrink exact board rectangle around legal footprints (without moving any part)."""
    rects=[p.bbox() for p in parts]
    x0=min(b[0] for b in rects)-edge_margin
    y0=min(b[1] for b in rects)-edge_margin
    x1=max(b[2] for b in rects)+edge_margin
    y1=max(b[3] for b in rects)+edge_margin
    return x0,y0,x1-x0,y1-y0


def place(parts,edges,clearance=0.65,margin=2.0,seed=42,iterations=8000,
          checkpoint=None, checkpoint_interval=0):
    if np is None:
        raise RuntimeError('numpy is required for deterministic candidate generation')
    W,H=target_dims(parts,margin=margin)
    for growth in range(11):
        # Two independent greedy orders per area setting, scoring final wire cost.
        solutions=[]
        for trial in range(2):
            rr=random.Random(seed+growth*1009+trial*8191)
            if greedy_place(parts,edges,W,H,clearance,margin,rr):
                solutions.append((wire_sum(edges,parts)+total_role_cost(parts,W,H),
                                  [(p.x,p.y,p.angle) for p in parts]))
        if solutions:
            _,locations=min(solutions,key=lambda s:s[0])
            for p,(x,y,a) in zip(parts,locations):p.x,p.y,p.angle=x,y,a
            if checkpoint: checkpoint('greedy', parts, W, H)
            accepted=optimize_local(parts,edges,W,H,clearance,margin,iterations,seed+1000003,
                                    checkpoint=checkpoint, interval=checkpoint_interval)
            if checkpoint: checkpoint('optimized', parts, W, H)
            # Source file has no shape constraints. Bounding rectangle is minimized
            # after final placement. No rewrite if legalization would be invalid.
            if any(p.edge_rule for p in parts):
                # The board edge itself is part of a mechanical facing constraint.
                # Tight compaction would invalidate the specified opening offset.
                x0,y0,bw,bh=0.0,0.0,W,H
            else:
                x0,y0,bw,bh=compact(parts,margin,clearance)
            ORIGIN=(100.0,80.0)
            for p in parts:
                p.x+=ORIGIN[0]-x0;p.y+=ORIGIN[1]-y0
            return {'width':bw,'height':bh,'origin_x':ORIGIN[0],'origin_y':ORIGIN[1],
                    'initial_width':W,'initial_height':H,'growth_steps':growth,
                    'wire_proxy':wire_sum(edges,parts),'accepted_moves':accepted}
        # Grow both dimensions; never permit massive silent size explosion.
        W=round(W*1.13+1.5,1);H=round(H*1.13+1.0,1)
    raise RuntimeError('Failed to legalize all components after 11 board expansion attempts')


def fmt(v):
    if abs(v)<5e-6:return '0'
    return f'{v:.5f}'.rstrip('0').rstrip('.')


def edit_board(original,parts,width,height,origin_x,origin_y):
    spans=span_children(original)
    by_ref={p.name:p for p in parts}
    edits=[]
    seen=set()
    for tag,a,b in spans:
        if tag=='footprint':
            childsp=span_children(original,a,b)
            # Reference property extracted from a parsed copy, avoiding any string matching.
            fp_node=parse(original[a:b])
            ref=footprint_reference(fp_node)
            if ref not in by_ref or ref in seen: raise ValueError('Footprint reference changed or duplicate')
            seen.add(ref)
            ats=[(aa,bb) for tg,aa,bb in childsp if tg=='at']
            if len(ats)!=1:raise ValueError(f'{ref}: expected one direct footprint (at)')
            aa,bb=ats[0];p=by_ref[ref]
            old_at=parse(original[aa:bb])
            x_old,y_old=fnum(old_at[1]),fnum(old_at[2])
            a_old=fnum(old_at[3]) if len(old_at)>3 else 0.0
            rotation=p.angle-a_old
            edits.append((aa,bb,f'(at {fmt(p.x)} {fmt(p.y)} {p.angle})'))
            # KiCad board file property positions are ABSOLUTE, unlike pad-at.
            # Leaving the original coordinates would strand all designators
            # around the old, unplaced (0,0) location.
            for typ,sa,sb in childsp:
                if typ!='property':continue
                prop_at_spans=[(pa,pb) for tag,pa,pb in span_children(original,sa,sb) if tag=='at']
                for pa,pb in prop_at_spans:
                    oldp=parse(original[pa:pb])
                    dx=fnum(oldp[1])-x_old;dy=fnum(oldp[2])-y_old
                    rx,ry=rot_xy(dx,dy,rotation)
                    old_angle=fnum(oldp[3]) if len(oldp)>3 else 0.0
                    new_angle=(old_angle+rotation)%360
                    # Avoid touching an optional remaining property at field.
                    edits.append((pa,pb,f'(at {fmt(p.x+rx)} {fmt(p.y+ry)} {fmt(new_angle)})'))
        elif tag.startswith('gr_') or tag in ('dimension',):
            # Existing outline and dimensions only: preserve unrelated graphics.
            group=parse(original[a:b])
            if get(group,'layer') == 'Edge.Cuts':edits.append((a,b,''))
    if seen!=set(by_ref):raise ValueError('Not all footprints found in target source')
    ox,oy=origin_x,origin_y
    x2=ox+width;y2=oy+height
    # Four segments use UUIDs so KiCad 9 can inspect and modify them normally.
    import uuid
    lines=[]
    for (x1,y1),(x3,y3) in [((ox,oy),(x2,oy)),((x2,oy),(x2,y2)),
                             ((x2,y2),(ox,y2)),((ox,y2),(ox,oy))]:
        lines.append(f'\n\t(gr_line (start {fmt(x1)} {fmt(y1)}) (end {fmt(x3)} {fmt(y3)}) '
                     f'(stroke (width 0.1) (type default)) (layer "Edge.Cuts") '
                     f'(uuid "{uuid.uuid4()}") )')
    # Insert before final root closing paren.
    last=original.rfind(')')
    if last<0:raise ValueError('Malformed source')
    edits.append((last,last,''.join(lines)+'\n'))
    data=original
    for a,b,rep in sorted(edits,reverse=True):data=data[:a]+rep+data[b:]
    return data


def pad_signatures(tree):
    signatures={}
    for p in decode_parts(tree):
        pp=tuple((pad.name,pad.x,pad.y,pad.net,pad.w,pad.h,pad.angle) for pad in p.pads)
        # Footprint source excluding only its direct `(at ...)` ensures everything
        # else, including metadata, text, pad dimensions, net labels, is identical.
        from copy import deepcopy
        fp=deepcopy(p.node)
        fp[:]=[x for x in fp if not (isinstance(x,list) and x and x[0]=='at')]
        for node in fp:
            if isinstance(node,list) and node and node[0]=='property':
                node[:]=[x for x in node if not (isinstance(x,list) and x and x[0]=='at')]
        signatures[p.name]=(p.footprint,p.layer,pp,fp)
    return signatures


def outline_rect(tree):
    lines=[]
    for g in children(tree,'gr_line'):
        if get(g,'layer')=='Edge.Cuts':
            lines.append((xyz(g,'start'),xyz(g,'end')))
    if len(lines)!=4:return None
    xs=[x for seg in lines for pt in seg for x in (pt[0],)]
    ys=[y for seg in lines for pt in seg for y in (pt[1],)]
    bb=(min(xs),min(ys),max(xs),max(ys))
    expected={frozenset((bb[:2],(bb[2],bb[1]))),frozenset(((bb[2],bb[1]),bb[2:])),
              frozenset((bb[2:],(bb[0],bb[3]))),frozenset(((bb[0],bb[3]),bb[:2]))}
    actual={frozenset((a,b)) for a,b in lines}
    return bb if actual==expected else None


def verify(source_text, output_text,clearance=0.65,margin=2.0,direction_rules=None,
           placement_rules=None):
    old=parse(source_text);new=parse(output_text)
    if old[0]!='kicad_pcb' or new[0]!='kicad_pcb':raise AssertionError('Not KiCad PCB')
    if pad_signatures(old)!=pad_signatures(new):
        raise AssertionError('Footprint contents, net names or pad data were modified')
    original_net=[tuple(x) for x in children(old,'net')]
    final_net=[tuple(x) for x in children(new,'net')]
    if original_net!=final_net:raise AssertionError('Board net table was modified')
    oldparts={p.name:p for p in decode_parts(old)}
    newparts={p.name:p for p in decode_parts(new)}
    for ref,p0 in oldparts.items():
        p1=newparts[ref]
        a0=child(p0.node,'at');a1=child(p1.node,'at')
        ox0,oy0=fnum(a0[1]),fnum(a0[2])
        ox1,oy1=fnum(a1[1]),fnum(a1[2])
        theta0=fnum(a0[3]) if len(a0)>3 else 0.0
        theta1=fnum(a1[3]) if len(a1)>3 else 0.0
        delta=theta1-theta0
        op=children(p0.node,'property');np=children(p1.node,'property')
        for oldprop,newprop in zip(op,np):
            c0=child(oldprop,'at');c1=child(newprop,'at')
            if c0 is None or c1 is None:
                if (c0 is None)!=(c1 is None):raise AssertionError('Property position removed')
                continue
            relx,rely=fnum(c0[1])-ox0,fnum(c0[2])-oy0
            rx,ry=rot_xy(relx,rely,delta)
            if math.hypot(ox1+rx-fnum(c1[1]),oy1+ry-fnum(c1[2]))>0.0002:
                raise AssertionError(f'Property {oldprop[1]} of {ref} was not moved correctly')
            olda=fnum(c0[3]) if len(c0)>3 else 0.0
            newa=fnum(c1[3]) if len(c1)>3 else 0.0
            if abs(((olda+delta-newa+180)%360)-180)>1e-5:
                raise AssertionError(f'Property {oldprop[1]} of {ref} rotated incorrectly')
    ps=decode_parts(new)
    guidance=apply_placement_guidance(ps,placement_rules)
    for p in ps:
        a=child(p.node,'at')
        p.x,p.y=fnum(a[1]),fnum(a[2]);p.angle=int(round(fnum(a[3]))) if len(a)>3 else 0
    bb=outline_rect(new)
    if bb is None:raise AssertionError('Missing or non-rectangular outline')
    ox,oy,ex,ey=bb
    if not (ex>ox and ey>oy):raise AssertionError('Invalid board size')
    margin_bad=[]
    for p in ps:
        b=p.bbox()
        # Constraint-based edge parts use explicit edge offset; other components
        # retain the ordinary margin on every side.
        rule = direction_rules.get(p.name) if direction_rules else None
        if rule:
            p.edge_rule = rule
        if not valid_envelope(p,p.x-ox,p.y-oy,p.angle,ex-ox,ey-oy,margin):
            margin_bad.append((p.name,b))
    if margin_bad:raise AssertionError(f'Parts outside edge margin: {margin_bad}')
    hits=[]
    min_gap=float('inf')
    for i,a in enumerate(ps):
        A=a.bbox()
        for b in ps[i+1:]:
            B=b.bbox()
            d=bbox_gap(A,B)
            min_gap=min(min_gap,d)
            if rects_intersect(A,B,clearance-0.0003):hits.append((a.name,b.name,d))
            needed=required_rf_clearance(a,b)
            if needed and d<needed-0.0003:
                hits.append((a.name,b.name,d,f'RF keepout requires {needed} mm'))
    if hits:raise AssertionError(f'Overlaps or clearance violations: {hits[:10]}')
    at=[(p.name,p.x,p.y,p.angle) for p in ps]
    return {'footprints':len(ps),'nets':len(children(new,'net')),'pads':sum(len(p.pads) for p in ps),
            'board_width_mm':round(ex-ox,5),'board_height_mm':round(ey-oy,5),
            'board_area_mm2':round((ex-ox)*(ey-oy),2),
            'min_bounding_box_gap_mm':round(min_gap,4) if math.isfinite(min_gap) else None,
            'courtyard_missing':sorted(p.name for p in ps if not p.has_courtyard),
            'synthetic_footprints':sorted(p.name for p in ps if p.footprint.startswith('SYNTH_')),
            'pads_and_nets_unchanged':True,'perimeter_and_clearance_pass':True,
            'unique_placements':len(set((round(x,3),round(y,3)) for _,x,y,_ in at)),
            'validated_outward_facing': sorted(direction_rules or {}),
            'validated_rf_keepouts':sorted(guidance['hard_antenna_keepouts'])}


def preview(parts, report, out):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from matplotlib.patches import Rectangle
    ox,oy=report['origin_x'],report['origin_y'];W,H=report['width'],report['height']
    fig,ax=plt.subplots(figsize=(max(9,W/11),max(5,H/11)),dpi=155)
    ax.add_patch(Rectangle((ox,oy),W,H,facecolor='#e5f6e6',edgecolor='#14532d',lw=2.2))
    for p in parts:
        b=p.bbox();
        name=p.name
        if p.role=='antenna':col='#dbb4f6'
        elif p.role=='io':col='#ffc88a'
        elif p.role=='processor':col='#8ab4fa'
        elif p.role=='noisy':col='#f9bba8'
        elif len(p.pads)>=20:col='#a7b9f7'
        elif not p.has_courtyard:col='#f0a3bf'
        else: col='#b5d4de'
        ax.add_patch(Rectangle((b[0],b[1]),b[2]-b[0],b[3]-b[1],
                               edgecolor='#333a4a',facecolor=col,alpha=0.85,lw=0.8))
        fs=6.7 if b[2]-b[0]>5 or b[3]-b[1]>5 else 5.6
        ax.text((b[0]+b[2])/2,(b[1]+b[3])/2,name,fontsize=fs,
                ha='center',va='center',rotation=0,color='#17212d',weight='bold')
    ax.set_xlim(ox-3,ox+W+3);ax.set_ylim(oy+H+3,oy-3)
    ax.set_aspect('equal',adjustable='box');ax.set_xlabel('mm');ax.set_ylabel('mm')
    ax.set_title(f'HARP-PCB placement • {len(parts)} components • {W:.1f} × {H:.1f} mm')
    ax.grid(alpha=0.13);fig.tight_layout()
    fig.savefig(out,bbox_inches='tight')
    plt.close(fig)


def cli(argv=None):
    ap=argparse.ArgumentParser(description=__doc__,formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('input',type=Path)
    ap.add_argument('-o','--output',type=Path)
    ap.add_argument('--clearance',type=float,default=0.65,help='Footprint bounding-box clearance (mm)')
    ap.add_argument('--edge-margin',type=float,default=2.0,help='Minimum footprint board-edge margin (mm)')
    ap.add_argument('--iterations',type=int,default=8000,help='Local annealing iterations')
    ap.add_argument('--seed',type=int,default=42)
    ap.add_argument('--preview',type=Path,help='Optional PNG output')
    ap.add_argument('--report',type=Path,help='Optional JSON validation report')
    ap.add_argument('--verify',action='store_true',help='Only verify an already-placed board')
    ap.add_argument('--direction-rules',type=Path,help='Explicit mechanical opening direction JSON')
    ap.add_argument('--placement-rules',type=Path,
                    help='JSON with optional component functional roles and hard antenna footprint spacings')
    ap.add_argument('--electrical-rules',type=Path,
                    help='Optional validated pin-to-pin guidance JSON; automatic hints need no file')
    ap.add_argument('--footprint-replacements',type=Path,
                    help='JSON mapping references to reviewed local .kicad_mod files or same-value donors')
    ap.add_argument('--no-auto-reuse',action='store_true',
                    help='Disable safe best-effort reuse of non-synthetic same-value board footprints')
    ap.add_argument('--allow-unverified-footprints',action='store_true',help='Legacy override for --strict-preflight, geometry experiments only')
    ap.add_argument('--allow-unresolved-orientation',action='store_true',help='Legacy override for --strict-preflight, NOT safe for production')
    ap.add_argument('--preflight-only',action='store_true',help='Write/list footprint and orientation issues without placement')
    ap.add_argument('--strict-preflight', action='store_true',
                    help='Abort on synthetic or ambiguous facing instead of issuing warnings')
    ap.add_argument('--ai-preflight', action='store_true',
                    help='Opt in to an LLM advisory for flagged footprints; never changes geometry')
    ap.add_argument('--ai-base-url', default=DEFAULT_BASE_URL,
                    help='IntelCAD AI-router endpoint (HTTPS, host restricted)')
    ap.add_argument('--ai-model', default=DEFAULT_MODEL,
                    help='Model for optional advisory, e.g. cc/claude-haiku-5-5')
    ap.add_argument('--ai-timeout', type=float, default=20.0,
                    help='LLM request timeout in seconds (network failures always fall back)')
    ap.add_argument('--replace-nonrectangular-outline',action='store_true',help='Explicitly replace an existing curved/polygon board edge with a rectangular one')
    ap.add_argument('--stages-dir',type=Path,help='Save verified intermediate KiCad PCB snapshots and previews')
    ap.add_argument('--snapshot-every',type=int,default=0,help='Write intermediate anneal snapshots every N iterations (requires --stages-dir)')
    args=ap.parse_args(argv)
    if not math.isfinite(args.clearance) or not math.isfinite(args.edge_margin) or args.clearance<0 or args.edge_margin<0.5 or args.iterations<0 or args.snapshot_every<0:
        ap.error('Invalid clearance, margin, iterations or snapshot interval')
    if args.snapshot_every and not args.stages_dir:
        ap.error('--snapshot-every requires --stages-dir')
    source=args.input.read_text(encoding='utf-8')
    original_source=source
    fp_replacements=json.loads(args.footprint_replacements.read_text('utf-8')) if args.footprint_replacements else {}
    pinout_audit=check_known_pinout(decode_parts(parse(original_source)),part_value)
    blocked_refs={entry['ref'] for entry in pinout_audit if entry['conflicting_labels']}
    if blocked_refs:
        # A mismatched manufacturer-labelled pin cannot be silently fixed by
        # changing only the copper land pattern. Keep its original geometry.
        fp_replacements={**fp_replacements,'components':{
            ref:spec for ref,spec in fp_replacements.get('components',{}).items()
            if ref not in blocked_refs}}
    if not args.verify:
        source, footprint_resolution=apply_replacements(
            source, fp_replacements,
            base_dir=args.footprint_replacements.parent if args.footprint_replacements else args.input.parent,
            auto_reuse=not args.no_auto_reuse,
            parse=parse, span_children=span_children, footprint_audit=footprint_audit)
    else:
        footprint_resolution={'applied':[],'skipped':[],'manufacturing_verified':False}
    tree=parse(source)
    if tree[0]!='kicad_pcb':raise ValueError('Input is not a KiCad PCB file')
    ps=decode_parts(tree)
    rules_data=json.loads(args.direction_rules.read_text('utf-8')) if args.direction_rules else {}
    audit=apply_direction_rules(ps,rules_data)
    audit['footprint_resolution']=footprint_resolution
    audit['known_part_pinout_audit']=pinout_audit
    audit['original_synthetic_footprints']=[x['ref'] for x in
        footprint_audit(decode_parts(parse(original_source)))[0]]
    placement_data=json.loads(args.placement_rules.read_text('utf-8')) if args.placement_rules else {}
    guidance=apply_placement_guidance(ps,placement_data)
    audit['functional_placement']=guidance
    if guidance['rf_review_needed']:
        audit['nonverified_rf_refs']=guidance['rf_review_needed']
    edge_nodes=[g for g in tree if isinstance(g,list) and g and g[0].startswith('gr_')
                and get(g,'layer')=='Edge.Cuts']
    straight_rectangle = outline_rect(tree) is not None
    kicad_rect = len(edge_nodes)==1 and edge_nodes[0][0]=='gr_rect'
    audit['outline']={'present':bool(edge_nodes),
                      'nonrectangular_or_unsupported':bool(edge_nodes) and not (straight_rectangle or kicad_rect),
                      'will_be_replaced_by_rectangle':True}
    audit['existing_copper']={tag:len(children(tree,tag)) for tag in ('segment','arc','via','zone')}
    rule_map={p.name:p.edge_rule for p in ps if p.edge_rule}
    review_needed = bool(audit['synthetic_footprints'] or audit['unresolved_directions']
                         or guidance['rf_review_needed'])
    audit['review_required'] = review_needed
    audit['validation_mode'] = 'strict' if args.strict_preflight else 'best_effort'
    audit['nonblocking_warnings'] = []
    if blocked_refs:
        audit['nonblocking_warnings'].append(
            'Manufacturer pin labels disagree with existing pad assignments for: '
            + ', '.join(sorted(blocked_refs)) + '. Exact footprint substitutions were rejected; '
            'repair the schematic/pin mapping before manufacturing.')
    if footprint_resolution['applied']:
        audit['nonblocking_warnings'].append(
            'Guarded footprint geometries substituted; pad/net identities were preserved, '
            'but datasheet pin functions, mechanical fit and physical library source '
            'must still be reviewed before manufacturing.')
    if footprint_resolution['skipped']:
        audit['nonblocking_warnings'].append(
            'Some footprint substitutions were rejected and their existing geometry was retained.')
    if audit['synthetic_footprints']:
        audit['nonblocking_warnings'].append(
            'Synthetic/placeholder footprints are NOT repaired or verified; geometry-only output. '
            'Replace against datasheets and verified library footprints before manufacture.')
    if guidance['rf_review_needed']:
        audit['nonblocking_warnings'].append(
            'Antenna footprint detected without a datasheet-backed hard keepout for: '
            + ', '.join(guidance['rf_review_needed'])
            + '. Edge preference and RF separation are only heuristic; copper and ground-plane keepouts are NOT enforced.')
    if audit['unresolved_directions']:
        audit['nonblocking_warnings'].append(
            'Access-facing orientation cannot be inferred reliably for: '
            + ', '.join(audit['unresolved_directions'])
            + '. No outward-facing constraint was applied to these parts; review by hand.')
    audit['ai_review'] = review_preflight(
        audit, ps, enabled=args.ai_preflight,
        base_url=args.ai_base_url, model=args.ai_model, timeout=args.ai_timeout)
    if args.ai_preflight and audit['ai_review']['status'] not in ('advice_received', 'no_issues'):
        audit['nonblocking_warnings'].append(
            'Optional AI review unavailable or unconfigured; continuing with local geometry.')
    if args.preflight_only:
        print(json.dumps(audit,indent=2,ensure_ascii=False))
        if args.report:
            args.report.parent.mkdir(parents=True,exist_ok=True)
            args.report.write_text(json.dumps(audit,indent=2,ensure_ascii=False),encoding='utf-8')
        return 0
    if args.verify:
        result=verify(source,source,args.clearance,args.edge_margin,rule_map)
        print(json.dumps(result,indent=2,ensure_ascii=False));return 0
    if args.strict_preflight and audit['synthetic_footprints'] and not args.allow_unverified_footprints:
        raise ValueError('Synthetic/placeholder footprints detected: '+', '.join(x['ref'] for x in audit['synthetic_footprints'])+
                         '. Replace them with verified land patterns, or use --allow-unverified-footprints for geometry experiments only.')
    if args.strict_preflight and audit['unresolved_directions'] and not args.allow_unresolved_orientation:
        raise ValueError('Ambiguous access direction for: '+', '.join(audit['unresolved_directions'])+
                         '. Provide --direction-rules or explicitly use --allow-unresolved-orientation.')
    if audit['outline']['nonrectangular_or_unsupported'] and not args.replace_nonrectangular_outline:
        raise ValueError('Existing board outline is nonrectangular/unsupported. The rectangular placer cannot preserve it. '
                         'Do not silently destroy mechanical geometry; explicitly use --replace-nonrectangular-outline if acceptable.')
    if not args.output:ap.error('Expected -o/--output')
    if args.output.resolve()==args.input.resolve():ap.error('Refusing to overwrite input file')
    for tag in ('segment','arc','via','zone'):
        if children(tree,tag):
            raise ValueError(f'Input has existing {tag} objects; placement would invalidate routing/zones')
    if any(get(g,'layer')=='Edge.Cuts' and g[0] not in ('gr_line','gr_rect','gr_arc','gr_circle','gr_poly')
           for g in tree if isinstance(g,list) and g and g[0].startswith('gr_')):
        raise ValueError('Unsupported Edge.Cuts object detected')
    edges=make_edges(ps)
    electrical_data=json.loads(args.electrical_rules.read_text('utf-8')) if args.electrical_rules else {}
    pin_edges,pin_report=infer_pin_guidance(ps,edge_class=Edge,config=electrical_data)
    edges.extend(pin_edges)
    audit['electrical_guidance']=pin_report
    start=time.perf_counter()
    stages=[]
    if args.stages_dir:
        args.stages_dir.mkdir(parents=True,exist_ok=True)
    def save_checkpoint(name,components,W,H):
        if not args.stages_dir:return
        idx=len(stages)+1
        basename=f'{idx:02d}_{name}'
        fp=args.stages_dir/(basename+'.kicad_pcb')
        snap=edit_board(source,components,W,H,0.0,0.0)
        check=verify(source,snap,args.clearance,args.edge_margin,rule_map,placement_data)
        fp.write_text(snap,encoding='utf-8')
        pic=args.stages_dir/(basename+'.png')
        preview(components,{'origin_x':0.0,'origin_y':0.0,'width':W,'height':H},pic)
        stages.append({'name':name,'pcb':fp.name,'preview':pic.name,
                       'board_area_mm2':check['board_area_mm2']})
    result=place(ps,edges,args.clearance,args.edge_margin,args.seed,args.iterations,
                 checkpoint=save_checkpoint if args.stages_dir else None,
                 checkpoint_interval=args.snapshot_every)
    output=edit_board(source,ps,result['width'],result['height'],result['origin_x'],result['origin_y'])
    checks=verify(source,output,args.clearance,args.edge_margin,rule_map,placement_data)
    if args.stages_dir:
        last=args.stages_dir/(f'{len(stages)+1:02d}_final.kicad_pcb')
        last.write_text(output,encoding='utf-8')
        pic=args.stages_dir/(f'{len(stages)+1:02d}_final.png')
        preview(ps,result,pic)
        stages.append({'name':'final','pcb':last.name,'preview':pic.name,'board_area_mm2':checks['board_area_mm2']})
        (args.stages_dir/'manifest.json').write_text(json.dumps(stages,indent=2),encoding='utf-8')
    result.update(checks)
    result['proxy_wirelength_weighted_mm']=round(result.pop('wire_proxy'),3)
    result['runtime_seconds']=round(time.perf_counter()-start,2)
    result['footprint_audit']=audit
    result['footprint_resolution']=footprint_resolution
    result['original_pad_number_and_net_mapping_preserved']=True
    result['footprint_geometry_changed_from_input']=bool(footprint_resolution['applied'])
    result['manufacturer_pin_function_validation_performed']=False
    result['electrical_guidance']=pin_report
    result['functional_placement']=guidance
    result['role_soft_cost_final']=round(total_role_cost(ps,result['width'],result['height'],
                                                         result['origin_x'],result['origin_y']),3)
    result['role_placement']={p.name:{'role':p.role,
        'edge_distance_mm':round(max(0.0,min(p.bbox()[0]-result['origin_x'],
              p.bbox()[1]-result['origin_y'],
              result['origin_x']+result['width']-p.bbox()[2],
              result['origin_y']+result['height']-p.bbox()[3])),3),
        'center_distance_mm':round(math.hypot((p.bbox()[0]+p.bbox()[2])/2-
              (result['origin_x']+result['width']/2),
              (p.bbox()[1]+p.bbox()[3])/2-
              (result['origin_y']+result['height']/2)),3)}
              for p in ps if p.role!='general'}
    result['output_status']='GEOMETRY_ONLY_REVIEW_REQUIRED'
    result['manufacturing_ready']=False
    result['review_required_components']=sorted(set(
        r['ref'] for r in audit['synthetic_footprints']) | set(audit['unresolved_directions'])
        | set(guidance['rf_review_needed'])
        | {x['ref'] for x in footprint_resolution['applied']}
        | {x['ref'] for x in footprint_resolution['skipped']}
        | blocked_refs )
    result['stages']=stages
    result['warning']='Unrouted placement only. KiCad DRC, actual part footprints, opening direction and manufacturing review are required.'
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(output,encoding='utf-8')
    if args.preview:
        args.preview.parent.mkdir(parents=True,exist_ok=True)
        preview(ps,result,args.preview)
    if args.report:
        args.report.parent.mkdir(parents=True,exist_ok=True)
        args.report.write_text(json.dumps(result,indent=2,ensure_ascii=False),encoding='utf-8')
    print(json.dumps(result,indent=2,ensure_ascii=False))
    return 0

if __name__=='__main__':
    try:sys.exit(cli())
    except (ValueError,RuntimeError,AssertionError) as exc:
        print(f'ERROR: {exc}',file=sys.stderr);sys.exit(2)
