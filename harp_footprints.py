"""Guarded KiCad footprint geometry substitution.

A substituted footprint is never classified as electrically verified merely
because pad numbers match. In particular, datasheet pin-function matching is
out of scope. Only exact pad-number multisets and board net assignments are
preserved; failed replacements are skipped and reported.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
from pathlib import Path
import uuid

MAX_FOOTPRINT_BYTES = 1_000_000


def _direct_text(source, source_spans, tags):
    return [source[a:b] for tag, a, b in source_spans if tag in tags]


def _pad_net(pad):
    for item in pad:
        if isinstance(item, list) and item and item[0] == "net":
            return tuple(item)
    return None


def _child(node, key):
    return next((x for x in node if isinstance(x, list) and x and x[0] == key), None)


def _ref(fp):
    for p in fp:
        if isinstance(p, list) and len(p) > 2 and p[0] == 'property' and p[1] == 'Reference':
            return str(p[2])
    return None


def _value(fp):
    for p in fp:
        if isinstance(p, list) and len(p) > 2 and p[0] == 'property' and p[1] == 'Value':
            return str(p[2]).strip()
    return ''


def _pads(fp):
    return [p for p in fp if isinstance(p, list) and p and p[0] == 'pad']


def _pad_shape_tags():
    return {'pad', 'model', 'zone', 'fp_zone', 'fp_line', 'fp_rect', 'fp_circle',
            'fp_arc', 'fp_poly', 'fp_curve', 'fp_text', 'fp_text_box'}


def substitute_footprint(original_fp: str, donor_fp: str, library_id: str,
                         parse, span_children) -> str:
    """Replace only physical features, retaining reference/value/UUID and old net maps.

    Supports both modern (footprint ...) and older (module ...) .kicad_mod.
    Never changes pad numbers, net ids/names or duplicates, and never clones a
    donor's board-level reference/UUID. Source is re-parsed after modification.
    """
    old = parse(original_fp)
    donor = parse(donor_fp)
    if old[0] != 'footprint' or donor[0] not in ('module', 'footprint'):
        raise ValueError('Expected board footprint and KiCad .kicad_mod footprint')
    if not library_id or ':' not in library_id or len(library_id) > 180:
        raise ValueError('library_id must be Library:Footprint')
    if '"' in library_id or '\n' in library_id or '(' in library_id:
        raise ValueError('Unsafe library identifier')
    pads_before, pads_after = _pads(old), _pads(donor)
    numbers_before = Counter(str(p[1]) for p in pads_before)
    numbers_after = Counter(str(p[1]) for p in pads_after)
    if not pads_after or numbers_before != numbers_after:
        raise ValueError('Pad-number multiplicity mismatch: '+str(numbers_before)+' vs '+str(numbers_after))
    old_by_number = defaultdict(list)
    for p in pads_before:
        old_by_number[str(p[1])].append(p)

    original_spans = span_children(original_fp)
    donor_spans = span_children(donor_fp)
    donor_geometry = []
    donor_attrs = []
    donor_pads = iter(pads_after)
    for tag, a, b in donor_spans:
        if tag == 'attr':
            donor_attrs.append(donor_fp[a:b]);continue
        if tag not in _pad_shape_tags():
            continue
        text = donor_fp[a:b]
        if tag == 'fp_text':
            # Library placeholder labels must not create duplicate board refs.
            ft = parse(text)
            if len(ft) > 1 and str(ft[1]).lower() in ('reference', 'value'):
                continue
        if tag == 'pad':
            pad = next(donor_pads)
            old_pad = old_by_number[str(pad[1])].pop(0)
            # Reused board footprints carry another part's net assignments and
            # pad UUIDs: remove them before attaching THIS part's connections.
            for tag2, sa, sb in reversed(span_children(text)):
                if tag2 in ('net', 'pinfunction', 'pintype', 'uuid', 'tstamp'):
                    text = text[:sa] + text[sb:]
            additions = []
            old_net = _child(old_pad,'net')
            if old_net:
                import json
                additions.append('(net '+str(old_net[1])+' '+json.dumps(str(old_net[2]))+')')
            for tag2 in ('pinfunction', 'pintype'):
                meta = _child(old_pad,tag2)
                if meta:
                    import json
                    additions.append('('+tag2+' '+json.dumps(str(meta[1]))+')')
            # Prefer the original pad identity if one exists, otherwise new UUID.
            uid = _child(old_pad,'uuid') or _child(old_pad,'tstamp')
            if uid:
                import json
                additions.append('('+str(uid[0])+' '+json.dumps(str(uid[1]))+')')
            else:
                additions.append('(uuid "'+str(uuid.uuid4())+'")')
            text = text[:-1]+' '+' '.join(additions)+')'
        donor_geometry.append(text)

    old_children = []
    for tag, a, b in original_spans:
        if tag in _pad_shape_tags() or tag == 'attr':
            continue
        old_children.append(original_fp[a:b])
    # Library's physical attribute (smd/through_hole) replaces the synthetic one.
    modified = ('(footprint "'+library_id+'"\n  '
                +'\n  '.join(old_children+donor_attrs+donor_geometry)+'\n)')
    resulting = parse(modified)
    new_padlist = _pads(resulting)
    if Counter(str(p[1]) for p in new_padlist) != numbers_before:
        raise AssertionError('Replacement unexpectedly altered pad identifiers')
    net_before = sorted((str(p[1]),tuple(_pad_net(p) or ())) for p in pads_before)
    net_after = sorted((str(p[1]),tuple(_pad_net(p) or ())) for p in new_padlist)
    if net_before != net_after or _ref(old) != _ref(resulting) or _value(old) != _value(resulting):
        raise AssertionError('Replacement changed pad nets, value or reference')
    return modified


def apply_replacements(board_text: str, config: dict, *, base_dir: Path,
                       auto_reuse: bool, parse, span_children,
                       footprint_audit) -> tuple[str, dict]:
    """Safely process explicit replacements, and optionally same-value donors.

    Invalid requests never stop best-effort placement. The original geometry is
    retained and each failure is recorded. A hash may pin explicitly supplied
    local library content to a reviewed version.
    """
    tree = parse(board_text)
    source_spans = [(a,b,board_text[a:b]) for tag,a,b in span_children(board_text) if tag=='footprint']
    footprints = { _ref(parse(s)):(a,b,s) for a,b,s in source_spans }
    config = config or {}
    if not isinstance(config,dict) or set(config)-{'components'} or not isinstance(config.get('components',{}),dict):
        raise ValueError('Footprint replacement map expects {"components": {...}}')
    entries = dict(config.get('components',{}))
    # Only the synthetic signal is needed here; the full directional preflight
    # runs later on decoded Pad objects with real x/y attributes.
    suspicious = {
        ref for ref, (_, _, raw) in footprints.items()
        if str(parse(raw)[1]).upper().startswith('SYNTH_')
        or 'NOT A REAL LAND PATTERN' in raw.upper()
    }
    skipped=[]
    if auto_reuse:
        for ref in sorted(suspicious):
            if ref in entries or ref not in footprints:continue
            target = parse(footprints[ref][2])
            if not _value(target):continue
            candidates=[]
            for other,(a,b,s) in footprints.items():
                if other == ref or other in suspicious: continue
                candidate=parse(s)
                if _value(candidate).casefold()!=_value(target).casefold():continue
                if Counter(str(p[1]) for p in _pads(candidate))!=Counter(str(p[1]) for p in _pads(target)):
                    continue
                candidates.append(other)
            if len(candidates)==1:
                entries[ref]={'clone_from_ref':candidates[0]}
            elif len(candidates)>1:
                skipped.append({'ref':ref,'reason':'Multiple matching same-value donor footprints'})
    edits=[]; applied=[]
    for ref,spec in sorted(entries.items()):
        try:
            if ref not in footprints or not isinstance(spec,dict):
                raise ValueError('Unknown reference or invalid replacement specification')
            if set(spec)-{'file','clone_from_ref','library_id','sha256'}:
                raise ValueError('Unexpected replacement mapping keys')
            if ('file' in spec)==('clone_from_ref' in spec):
                raise ValueError('Exactly one of file or clone_from_ref required')
            if 'file' in spec:
                path=Path(spec['file'])
                if not path.is_absolute():path=base_dir/path
                if not path.is_file() or path.suffix!='.kicad_mod' or path.stat().st_size>MAX_FOOTPRINT_BYTES:
                    raise ValueError('Library footprint not found or too large')
                data=path.read_text(encoding='utf-8')
                digest=hashlib.sha256(data.encode()).hexdigest()
                if spec.get('sha256') and spec['sha256'].lower()!=digest:
                    raise ValueError('SHA256 mismatch for reviewed library file')
                library_id=spec.get('library_id')
                if library_id is None:raise ValueError('library_id is required for file replacements')
                evidence='user-supplied library file'
            else:
                donor_ref=spec['clone_from_ref']
                if donor_ref not in footprints or donor_ref==ref or donor_ref in suspicious:
                    raise ValueError('Clone donor must be a distinct, non-synthetic footprint')
                data=footprints[donor_ref][2]
                library_id=str(parse(data)[1])
                if ":" not in library_id:
                    library_id="BoardReuse:"+library_id
                evidence='same-value board donor; requires mechanical review'
                if _value(parse(data)).casefold()!=_value(parse(footprints[ref][2])).casefold():
                    raise ValueError('Donor and target Value mismatch')
                digest=hashlib.sha256(data.encode()).hexdigest()
            a,b,old=footprints[ref]
            replacement=substitute_footprint(old,data,library_id,parse,span_children)
            edits.append((a,b,replacement))
            applied.append({'ref':ref,'library_id':library_id,'source':evidence,
                            'sha256':digest,'status':'pad_and_net_preserved; electrical_and_mechanical_review_required'})
        except (ValueError,TypeError,UnicodeError,AssertionError,KeyError,OSError) as exc:
            skipped.append({'ref':str(ref),'reason':str(exc)[:210]})
    output=board_text
    for a,b,replacement in sorted(edits,reverse=True):
        output=output[:a]+replacement+output[b:]
    # Global postcondition: board nets and every original pad number->net preserved.
    source_tree=parse(board_text); final_tree=parse(output)
    original_nets=[tuple(x) for x in source_tree if isinstance(x,list) and x and x[0]=='net']
    new_nets=[tuple(x) for x in final_tree if isinstance(x,list) and x and x[0]=='net']
    if original_nets!=new_nets:raise AssertionError('Replacement changed board net table')
    original_fp={_ref(x):x for x in source_tree if isinstance(x,list) and x and x[0]=='footprint'}
    final_fp={_ref(x):x for x in final_tree if isinstance(x,list) and x and x[0]=='footprint'}
    if original_fp.keys()!=final_fp.keys():raise AssertionError('Replacement changed footprint refs')
    for ref in original_fp:
        orig=sorted((str(p[1]),tuple(_pad_net(p) or ())) for p in _pads(original_fp[ref]))
        now=sorted((str(p[1]),tuple(_pad_net(p) or ())) for p in _pads(final_fp[ref]))
        if orig!=now: raise AssertionError('Replacement changed net assignments for '+ref)
    return output, {'applied':applied,'skipped':skipped,'manufacturing_verified':False}
