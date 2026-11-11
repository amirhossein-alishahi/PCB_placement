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
    parts=[]
    for fp in children(tree,'footprint'):
        ref=next((str(p[2]) for p in children(fp,'property') if len(p)>2 and p[1]=='Reference'),None)
        if not ref:
            raise ValueError('Footprint with no reference property')
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
        if not pads:
            raise ValueError(f'Footprint {ref} has no pads; needs explicit geometry handling')
        courtyard=[]
        for n in fp:
            if not isinstance(n,list) or not n or not n[0].startswith('fp_'):continue
            if get(n,'layer') in ('F.CrtYd','B.CrtYd'):
                b=shape_bounds(n)
                if b: courtyard.append(b)
        for x0,y0,x1,y1 in courtyard:
            bounds.extend([(x0,y0),(x1,y1)])
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
    if not within(b,W,H,margin):return False
    return all(not rects_intersect(b,parts[j].bbox(),clearance) for j in placed if j!=idx)


def greedy_place(parts,edges,W,H,clearance,margin,rng):
    n=len(parts)
    touch=grouped_edges(edges,n)
    # Sort biggest first; connected medium parts placed early for useful anchors.
    root=max(range(n),key=lambda i:(parts[i].area,len(parts[i].pads)))
    bb0=parts[root].offsets(90 if parts[root].outline[3]-parts[root].outline[1] >
                                 parts[root].outline[2]-parts[root].outline[0] else 0)
    p=parts[root]
    p.angle=90 if (p.outline[3]-p.outline[1]) > 1.25*(p.outline[2]-p.outline[0]) else 0
    o=p.offsets();p.x=W/2-(o[0]+o[2])/2;p.y=H/2-(o[1]+o[3])/2
    if not within(p.bbox(),W,H,margin): return False
    placed={root};rem=set(range(n))-{root}
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
                # Soft non-electrical placement preferences; connector near edge.
                b=(x+o[0],y+o[1],x+o[2],y+o[3])
                if p.is_connector:
                    cost+=0.035*min(b[0],b[1],W-b[2],H-b[3])
                cost+=0.001*((cx-W/2)**2+(cy-H/2)**2)**0.5
                # Tight designs: prefer locations adjacent to existing bbox union.
                if best is None or cost<best[0]:best=(cost,x,y,angle)
        if best is None:return False
        _,p.x,p.y,p.angle=best
        placed.add(i);rem.remove(i)
    return True


def optimize_local(parts,edges,W,H,clearance,margin,iterations,seed):
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
        if not legal_candidate(parts,idx,nx,ny,na,(k for k in range(n) if k!=idx),W,H,clearance,margin):continue
        current=sum(wire_between(e,parts) for e in touch[idx]);
        new=sum(wire_between(e,parts,{idx:(nx,ny,na)}) for e in touch[idx]);
        # Soft aesthetic penalty, tiny compared with electrical terms.
        currect=p.bbox();newrect=p.bbox(nx,ny,na)
        center_pen=lambda b:0.001*math.hypot((b[0]+b[2])/2-W/2,(b[1]+b[3])/2-H/2)
        if p.is_connector:
            edge_pen=lambda b:0.035*min(b[0],b[1],W-b[2],H-b[3])
            current+=edge_pen(currect);new+=edge_pen(newrect)
        current+=center_pen(currect);new+=center_pen(newrect)
        if idx==big: new+=0.02*math.hypot(nx-x,ny-y)
        temp=1.0*(1-step/iterations)**1.3+0.007
        if new<=current or rng.random()<math.exp(min(0.0,(current-new)/temp)):
            p.x,p.y,p.angle=nx,ny,na
            accepted+=1
    return accepted


def compact(parts,edge_margin,clearance):
    """Shrink exact board rectangle around legal footprints (without moving any part)."""
    rects=[p.bbox() for p in parts]
    x0=min(b[0] for b in rects)-edge_margin
    y0=min(b[1] for b in rects)-edge_margin
    x1=max(b[2] for b in rects)+edge_margin
    y1=max(b[3] for b in rects)+edge_margin
    return x0,y0,x1-x0,y1-y0


def place(parts,edges,clearance=0.65,margin=2.0,seed=42,iterations=8000):
    if np is None:
        raise RuntimeError('numpy is required for deterministic candidate generation')
    W,H=target_dims(parts,margin=margin)
    for growth in range(11):
        # Two independent greedy orders per area setting, scoring final wire cost.
        solutions=[]
        for trial in range(2):
            rr=random.Random(seed+growth*1009+trial*8191)
            if greedy_place(parts,edges,W,H,clearance,margin,rr):
                solutions.append((wire_sum(edges,parts),[(p.x,p.y,p.angle) for p in parts]))
        if solutions:
            _,locations=min(solutions,key=lambda s:s[0])
            for p,(x,y,a) in zip(parts,locations):p.x,p.y,p.angle=x,y,a
            accepted=optimize_local(parts,edges,W,H,clearance,margin,iterations,seed+1000003)
            # Source file has no shape constraints. Bounding rectangle is minimized
            # after final placement. No rewrite if legalization would be invalid.
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
            ref=next((str(c[2]) for c in children(fp_node,'property') if len(c)>2 and c[1]=='Reference'),None)
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


def verify(source_text, output_text,clearance=0.65,margin=2.0):
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
        if not (b[0]>=ox+margin-1e-4 and b[1]>=oy+margin-1e-4 and
                b[2]<=ex-margin+1e-4 and b[3]<=ey-margin+1e-4):
            margin_bad.append((p.name,b))
    if margin_bad:raise AssertionError(f'Parts outside edge margin: {margin_bad}')
    hits=[]
    min_gap=float('inf')
    for i,a in enumerate(ps):
        A=a.bbox()
        for b in ps[i+1:]:
            B=b.bbox()
            dx=max(A[0]-B[2],B[0]-A[2],0)
            dy=max(A[1]-B[3],B[1]-A[3],0)
            d=math.hypot(dx,dy)
            min_gap=min(min_gap,d)
            if rects_intersect(A,B,clearance-0.0003):hits.append((a.name,b.name,d))
    if hits:raise AssertionError(f'Overlaps or clearance violations: {hits[:10]}')
    at=[(p.name,p.x,p.y,p.angle) for p in ps]
    return {'footprints':len(ps),'nets':len(children(new,'net')),'pads':sum(len(p.pads) for p in ps),
            'board_width_mm':round(ex-ox,5),'board_height_mm':round(ey-oy,5),
            'board_area_mm2':round((ex-ox)*(ey-oy),2),
            'min_bounding_box_gap_mm':round(min_gap,4),
            'courtyard_missing':sorted(p.name for p in ps if not p.has_courtyard),
            'synthetic_footprints':sorted(p.name for p in ps if p.footprint.startswith('SYNTH_')),
            'pads_and_nets_unchanged':True,'perimeter_and_clearance_pass':True,
            'unique_placements':len(set((round(x,3),round(y,3)) for _,x,y,_ in at))}


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
        if p.is_connector:col='#ffc88a'
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
    args=ap.parse_args(argv)
    if args.clearance<0 or args.edge_margin<0.5 or args.iterations<0:
        ap.error('Clearance/iterations cannot be negative; edge margin must be >=0.5')
    source=args.input.read_text(encoding='utf-8')
    tree=parse(source)
    if tree[0]!='kicad_pcb':raise ValueError('Input is not a KiCad PCB file')
    if args.verify:
        # Structural validator needs original for comparison. Self-verification still
        # ensures physical validity and net-table internal consistency.
        result=verify(source,source,args.clearance,args.edge_margin)
        print(json.dumps(result,indent=2,ensure_ascii=False));return 0
    if not args.output:ap.error('Expected -o/--output')
    if args.output.resolve()==args.input.resolve():ap.error('Refusing to overwrite input file')
    for tag in ('segment','arc','via','zone'):
        if children(tree,tag):
            raise ValueError(f'Input has existing {tag} objects; placement would invalidate routing/zones')
    if any(get(g,'layer')=='Edge.Cuts' and g[0] not in ('gr_line','gr_rect','gr_arc','gr_circle','gr_poly')
           for g in tree if isinstance(g,list) and g and g[0].startswith('gr_')):
        raise ValueError('Unsupported Edge.Cuts object detected')
    ps=decode_parts(tree)
    edges=make_edges(ps)
    start=time.perf_counter()
    result=place(ps,edges,args.clearance,args.edge_margin,args.seed,args.iterations)
    output=edit_board(source,ps,result['width'],result['height'],result['origin_x'],result['origin_y'])
    checks=verify(source,output,args.clearance,args.edge_margin)
    result.update(checks)
    result['proxy_wirelength_weighted_mm']=round(result.pop('wire_proxy'),3)
    result['runtime_seconds']=round(time.perf_counter()-start,2)
    result['warning']='Not routed; KiCad DRC and footprint/manufacturing review required.'
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(output,encoding='utf-8')
    if args.preview:preview(ps,result,args.preview)
    if args.report:args.report.write_text(json.dumps(result,indent=2,ensure_ascii=False),encoding='utf-8')
    print(json.dumps(result,indent=2,ensure_ascii=False))
    return 0

if __name__=='__main__':
    try:sys.exit(cli())
    except (ValueError,RuntimeError,AssertionError) as exc:
        print(f'ERROR: {exc}',file=sys.stderr);sys.exit(2)
