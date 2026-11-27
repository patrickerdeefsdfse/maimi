"""Bounded OOXML editing helper for the reviewed company template, Python stdlib only.

This is not a rendering engine. Existing DSH execution, Office inspection/conversion
and present tools remain responsible for authoring orchestration and delivery.
"""
from pathlib import Path, PurePosixPath
from xml.dom import minidom, Node
import argparse, collections, hashlib, io, json, posixpath, re, uuid, zipfile

P = 'http://schemas.openxmlformats.org/presentationml/2006/main'
A = 'http://schemas.openxmlformats.org/drawingml/2006/main'
R = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
REL = 'http://schemas.openxmlformats.org/package/2006/relationships'
CT = 'http://schemas.openxmlformats.org/package/2006/content-types'
MC = 'http://schemas.openxmlformats.org/markup-compatibility/2006'
TEMPLATE_SHA = '0e742339972cbfb37f9e5c10c8f907f0f6fd95cfcf5409118a076c7258869e99'
DROP_REL_TYPES = {'notesSlide', 'tags', 'comments', 'commentAuthors'}
MARKERS = ('点击此处', 'XXX', 'XXXX', '数据截止2026年4月', '市场策划部')

class Invalid(ValueError):
    pass

def require(value, code):
    if not value:
        raise Invalid(code)

def sha(data):
    return hashlib.sha256(data).hexdigest()

def xml(data):
    require(len(data) <= 16 * 1024 * 1024 and b'<!DOCTYPE' not in data.upper() and b'<!ENTITY' not in data.upper(), 'XML_UNSUPPORTED')
    return minidom.parseString(data)

def encode(doc):
    # minidom keeps prefixes and xmlns declarations, including those referenced
    # only by QName-valued attributes such as mc:Ignorable and a14 properties.
    return doc.toxml(encoding='utf-8')

def elements(node, ns, local):
    return list(node.getElementsByTagNameNS(ns, local))

def text(node):
    return ''.join(child.data for child in node.childNodes if child.nodeType in (Node.TEXT_NODE, Node.CDATA_SECTION_NODE))

def set_text(node, value):
    for child in list(node.childNodes):
        node.removeChild(child)
    node.appendChild(node.ownerDocument.createTextNode(value))

def relpath(part):
    return posixpath.join(posixpath.dirname(part), '_rels', posixpath.basename(part) + '.rels')

def target(part, value):
    require(isinstance(value, str) and value and '\\' not in value and not any(c in value for c in ('?', '#', '\0')), 'REL_TARGET_INVALID')
    result = posixpath.normpath(posixpath.join(posixpath.dirname(part), value)) if not value.startswith('/') else value[1:]
    require(not result.startswith('../') and result not in ('.', '..') and not result.startswith('/'), 'REL_TARGET_OUTSIDE_PACKAGE')
    return result

def archive(path, *, template=False):
    with Path(path).open('rb') as stream:
        data = stream.read(64 * 1024 * 1024 + 1)
    require(len(data) <= 64 * 1024 * 1024, 'PACKAGE_TOO_LARGE')
    if template:
        require(sha(data) == TEMPLATE_SHA, 'TEMPLATE_HASH_MISMATCH')
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        rows = z.infolist()
        require(len(rows) <= 5000 and len({row.filename for row in rows}) == len(rows), 'ZIP_MEMBERS_INVALID')
        require(sum(row.file_size for row in rows) <= 256 * 1024 * 1024, 'ZIP_EXPANSION_TOO_LARGE')
        for row in rows:
            require(not row.flag_bits & 1 and not row.filename.startswith('/') and '..' not in PurePosixPath(row.filename).parts and '\\' not in row.filename, 'ZIP_MEMBER_INVALID')
        return {row.filename: z.read(row) for row in rows if not row.is_dir()}

def relationship_map(parts, part):
    name = relpath(part)
    if name not in parts:
        return {}
    rows = elements(xml(parts[name]), REL, 'Relationship')
    result = {}
    for row in rows:
        key = row.getAttribute('Id')
        require(key and key not in result, 'DUPLICATE_RELATIONSHIP_ID')
        result[key] = row
    return result

def slide_order(parts):
    doc = xml(parts['ppt/presentation.xml'])
    rows = elements(doc, P, 'sldId')
    links = relationship_map(parts, 'ppt/presentation.xml')
    result = []
    ids = set()
    for row in rows:
        require(row.getAttribute('id') not in ids, 'DUPLICATE_PRESENTATION_SLIDE_ID')
        ids.add(row.getAttribute('id'))
        rel = links[row.getAttributeNS(R, 'id')]
        require(rel.getAttribute('Type').endswith('/slide'), 'SLIDE_RELATIONSHIP_INVALID')
        result.append(target('ppt/presentation.xml', rel.getAttribute('Target')))
    require(len(set(result)) == len(result), 'SLIDE_PART_REUSED')
    return result

def shapes(doc):
    ids = set(); result = {}
    for identity in elements(doc, P, 'cNvPr'):
        key = identity.getAttribute('id')
        require(key and key not in ids, 'DUPLICATE_SHAPE_ID_WITHIN_SLIDE')
        ids.add(key)
        shape = identity.parentNode.parentNode
        if shape.namespaceURI != P or shape.localName not in ('sp', 'graphicFrame', 'pic', 'cxnSp'):
            continue
        result[key] = (shape, identity)
    return result

def string_runs(shape):
    return [text(node) for node in elements(shape, A, 't')]

def inspect_template(path, pages=None):
    parts = archive(path, template=True)
    order = slide_order(parts)
    output = []
    for index, part in enumerate(order, 1):
        if pages is not None and index not in pages:
            continue
        doc = xml(parts[part]); rows = []
        for key, (shape, identity) in shapes(doc).items():
            runs = string_runs(shape)
            if not any(run.strip() for run in runs):
                continue
            rows.append({'shape_id': key, 'shape_name': identity.getAttribute('name'), 'runs': runs,
                         'character_count': sum(len(run) for run in runs)})
        output.append({'source_page': index, 'guide_page_not_for_report': index in (1, 2), 'text_shapes': rows})
    return {'schema': 'maimi-company-ppt-inventory-v1', 'template_sha256': TEMPLATE_SHA, 'slides': output,
            'selection_key': 'source_page + shape_id; shape names and cross-slide IDs are not globally unique'}

def remove_nodes(doc, ns, local):
    for node in elements(doc, ns, local):
        node.parentNode.removeChild(node)

def edit_slide(data, specification, output_index, warnings):
    doc = xml(data); lookup = shapes(doc)
    require(set(specification) <= {'source_page', 'replace', 'keep', 'omit'}, 'SLIDE_PLAN_FIELDS_INVALID')
    replace = specification.get('replace', []); keep = specification.get('keep', []); omit = specification.get('omit', [])
    require(isinstance(replace, list) and isinstance(keep, list) and isinstance(omit, list), 'SLIDE_PLAN_LISTS_REQUIRED')
    used = set()
    for row in replace + keep:
        require(isinstance(row, dict) and set(row) == {'shape_id', 'runs'}, 'TEXT_EDIT_FIELDS_INVALID')
        key, values = row['shape_id'], row['runs']
        require(isinstance(key, str) and key in lookup and key not in used, 'SHAPE_TARGET_INVALID_OR_DUPLICATE')
        used.add(key); shape, _ = lookup[key]; nodes = elements(shape, A, 't')
        require(isinstance(values, list) and len(values) == len(nodes) and all(isinstance(v, str) and len(v) <= 2000 and not any(ord(c) < 32 and c not in '\n\t' for c in v) for v in values), 'TEXT_RUNS_INVALID')
        before = [text(node) for node in nodes]
        if row in keep:
            require(values == before, 'KEEP_TEXT_CHANGED')
        normalized = re.sub(r'\s+', '', ''.join(values)).upper()
        require(not any(re.sub(r'\s+', '', marker).upper() in normalized for marker in MARKERS), 'TEMPLATE_SAMPLE_TEXT_REMAINS')
        if sum(len(v) for v in values) > max(20, sum(len(v) for v in before) * 1.2):
            warnings.append({'code': 'TEXT_EXPANSION_REQUIRES_LAYOUT_CHECK', 'output_page': output_index, 'shape_id': key})
        for node, value in zip(nodes, values):
            set_text(node, value)
    for key in omit:
        require(isinstance(key, str) and key in lookup and key not in used, 'SHAPE_TARGET_INVALID_OR_DUPLICATE')
        used.add(key); shape, _ = lookup[key]
        # Removing the text shape could break connector/timing references. Clear
        # its text instead; empty styled placeholders remain geometrically stable.
        for node in elements(shape, A, 't'):
            set_text(node, '')
    needed = {key for key, (shape, _) in lookup.items() if any(value.strip() for value in string_runs(shape))}
    require(needed <= used, 'UNREVIEWED_TEMPLATE_TEXT_REMAINS')
    require(sum(len(text(node)) for node in elements(doc, A, 't')) <= 16000, 'SLIDE_TEXT_TOO_LARGE')
    # Notes/tags are not presentation content. Their relationships are stripped
    # separately; remove matching metadata references without altering layouts.
    remove_nodes(doc, P, 'custDataLst')
    for node in doc.getElementsByTagName('*'):
        if node.localName == 'creationId':
            identity = str(uuid.uuid5(uuid.NAMESPACE_URL, f'maimi:{output_index}:{node.toxml()}'))
            if node.hasAttribute('id'):
                node.setAttribute('id', '{' + identity.upper() + '}')
            if node.hasAttribute('val'):
                node.setAttribute('val', str(uuid.UUID(identity).int % (2**32)))
    return encode(doc)

def strip_slide_rels(data):
    doc = xml(data)
    for row in elements(doc, REL, 'Relationship'):
        kind = row.getAttribute('Type').rsplit('/', 1)[-1]
        if kind in DROP_REL_TYPES:
            row.parentNode.removeChild(row)
        else:
            require(kind in {'slideLayout', 'image', 'audio', 'video'}, 'MUTABLE_OR_UNKNOWN_SLIDE_DEPENDENCY_UNSUPPORTED')
            require(row.getAttribute('TargetMode') != 'External', 'EXTERNAL_SLIDE_RESOURCE_UNSUPPORTED')
    return encode(doc)

def reachable_parts(parts):
    keep = {'[Content_Types].xml'}; pending = ['']
    while pending:
        part = pending.pop()
        name = '_rels/.rels' if not part else relpath(part)
        if name not in parts:
            continue
        keep.add(name)
        for row in elements(xml(parts[name]), REL, 'Relationship'):
            if row.getAttribute('TargetMode') == 'External':
                continue
            dest = target(part, row.getAttribute('Target'))
            require(dest in parts, 'DANGLING_PART_RELATIONSHIP')
            if dest not in keep:
                keep.add(dest); pending.append(dest)
    return {name: data for name, data in parts.items() if name in keep}

def validate_parts(parts, expected_count=None):
    require('[Content_Types].xml' in parts and '_rels/.rels' in parts, 'PACKAGE_ROOT_MISSING')
    for name, data in parts.items():
        if not name.endswith(('.xml', '.rels')):
            continue
        doc = xml(data)
        if name.endswith('.rels'):
            part = '' if name == '_rels/.rels' else posixpath.join(posixpath.dirname(posixpath.dirname(name)), posixpath.basename(name)[:-5])
            rows = elements(doc, REL, 'Relationship'); ids = [row.getAttribute('Id') for row in rows]
            require(len(ids) == len(set(ids)), 'DUPLICATE_RELATIONSHIP_ID')
            for row in rows:
                if row.getAttribute('TargetMode') != 'External':
                    require(target(part, row.getAttribute('Target')) in parts, 'DANGLING_PART_RELATIONSHIP')
            continue
        relationships = relationship_map(parts, name)
        for node in doc.getElementsByTagName('*'):
            for attribute in list(node.attributes.values()):
                if attribute.namespaceURI == R:
                    require(attribute.value in relationships, 'DANGLING_XML_RELATIONSHIP')
            for prefix in node.getAttributeNS(MC, 'Ignorable').split():
                ancestor = node; defined = False
                while ancestor.nodeType == Node.ELEMENT_NODE:
                    if ancestor.hasAttribute('xmlns:' + prefix):
                        defined = True; break
                    ancestor = ancestor.parentNode
                require(defined, 'UNBOUND_IGNORABLE_PREFIX')
        if doc.documentElement.namespaceURI == P and doc.documentElement.localName == 'sld':
            shapes(doc)
    order = slide_order(parts)
    if expected_count is not None:
        require(len(order) == expected_count, 'PAGE_COUNT_MISMATCH')
    return {'status': 'STRUCTURE_VERIFIED', 'slide_count': len(order), 'part_count': len(parts), 'visual_layout_verified': False}

def compose(template, plan, output):
    source = Path(template).resolve(); destination = Path(output).resolve()
    require(source != destination and not destination.exists(), 'OUTPUT_MUST_BE_NEW_FILE')
    require(destination.suffix.lower() == '.pptx', 'OUTPUT_EXTENSION_INVALID')
    require(isinstance(plan, dict) and set(plan) == {'schema', 'slides'} and plan['schema'] == 'maimi-company-ppt-plan-v1', 'PLAN_SCHEMA_INVALID')
    require(isinstance(plan['slides'], list) and 1 <= len(plan['slides']) <= 80, 'PLAN_SLIDE_COUNT_INVALID')
    parts = archive(source, template=True); original_order = slide_order(parts)
    presentation = xml(parts['ppt/presentation.xml']); lists = elements(presentation, P, 'sldIdLst')
    require(len(lists) == 1, 'PRESENTATION_SLIDE_LIST_INVALID')
    slide_list = lists[0]
    for child in list(slide_list.childNodes):
        slide_list.removeChild(child)
    for local in ('custShowLst', 'notesMasterIdLst', 'custDataLst', 'extLst'):
        remove_nodes(presentation, P, local)
    presentation_rels = xml(parts['ppt/_rels/presentation.xml.rels'])
    for row in elements(presentation_rels, REL, 'Relationship'):
        if row.getAttribute('Type').rsplit('/', 1)[-1] in {'slide', 'notesMaster', 'tags', 'commentAuthors'}:
            row.parentNode.removeChild(row)
    existing_ids = {row.getAttribute('Id') for row in elements(presentation_rels, REL, 'Relationship')}
    content_types = xml(parts['[Content_Types].xml']); warnings = []
    for index, specification in enumerate(plan['slides'], 1):
        require(isinstance(specification, dict), 'SLIDE_PLAN_REQUIRED')
        page = specification.get('source_page')
        require(type(page) is int and 3 <= page <= len(original_order), 'GUIDE_OR_UNKNOWN_PAGE_NOT_REPORT_CONTENT')
        source_part = original_order[page - 1]; output_part = f'ppt/slides/maimiSlide{index}.xml'
        require(output_part not in parts, 'OUTPUT_PART_COLLISION')
        parts[output_part] = edit_slide(parts[source_part], specification, index, warnings)
        if relpath(source_part) in parts:
            parts[relpath(output_part)] = strip_slide_rels(parts[relpath(source_part)])
        rid = f'rIdMaimi{index}'
        require(rid not in existing_ids, 'PRESENTATION_REL_ID_COLLISION')
        relation = presentation_rels.createElementNS(REL, 'Relationship')
        for key, value in {'Id': rid, 'Type': R + '/slide', 'Target': posixpath.relpath(output_part, 'ppt')}.items():
            relation.setAttribute(key, value)
        presentation_rels.documentElement.appendChild(relation)
        slide_id = presentation.createElementNS(P, 'p:sldId'); slide_id.setAttribute('id', str(256 + index))
        slide_id.setAttributeNS(R, 'r:id', rid); slide_list.appendChild(slide_id)
        override = content_types.createElementNS(CT, 'Override'); override.setAttribute('PartName', '/' + output_part)
        override.setAttribute('ContentType', 'application/vnd.openxmlformats-officedocument.presentationml.slide+xml')
        content_types.documentElement.appendChild(override)
    parts['ppt/presentation.xml'] = encode(presentation)
    parts['ppt/_rels/presentation.xml.rels'] = encode(presentation_rels)
    root_rels = xml(parts['_rels/.rels'])
    for row in elements(root_rels, REL, 'Relationship'):
        if row.getAttribute('Type').rsplit('/', 1)[-1] in {'thumbnail', 'custom-properties'}:
            row.parentNode.removeChild(row)
    parts['_rels/.rels'] = encode(root_rels)
    if 'docProps/app.xml' in parts:
        app = xml(parts['docProps/app.xml'])
        for node in list(app.documentElement.childNodes):
            if node.nodeType != Node.ELEMENT_NODE:
                continue
            if node.localName in {'TitlesOfParts', 'HeadingPairs'}:
                node.parentNode.removeChild(node)
            elif node.localName in {'Slides', 'Notes', 'HiddenSlides'}:
                set_text(node, str(len(plan['slides'])) if node.localName == 'Slides' else '0')
        parts['docProps/app.xml'] = encode(app)
    parts = reachable_parts(parts)
    for node in elements(content_types, CT, 'Override'):
        if node.getAttribute('PartName').lstrip('/') not in parts:
            node.parentNode.removeChild(node)
    parts['[Content_Types].xml'] = encode(content_types)
    report = validate_parts(parts, len(plan['slides']))
    # Exclusive output creation prevents overwriting a user file. Partial local
    # output is deleted on error; the original template is never modified.
    created = False
    try:
        with destination.open('xb') as stream:
            created = True
            with zipfile.ZipFile(stream, 'w', compression=zipfile.ZIP_DEFLATED) as z:
                for name, data in parts.items():
                    z.writestr(name, data)
    except BaseException:
        if created and destination.exists():
            destination.unlink()
        raise
    return {**report, 'schema': 'maimi-company-ppt-compose-v1', 'template_sha256': TEMPLATE_SHA,
            'output_sha256': sha(destination.read_bytes()), 'warnings': warnings, 'visual_review': 'REQUIRED',
            'independent_slide_parts': True, 'sample_notes_removed': True, 'source_template_modified': False}

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='action', required=True)
    inspect = sub.add_parser('inspect'); inspect.add_argument('--template', required=True); inspect.add_argument('--out', required=True); inspect.add_argument('--pages', type=int, nargs='+')
    build = sub.add_parser('compose'); build.add_argument('--template', required=True); build.add_argument('--plan', required=True); build.add_argument('--out', required=True)
    check = sub.add_parser('check'); check.add_argument('--input', required=True); check.add_argument('--count', type=int)
    args = parser.parse_args()
    if args.action == 'inspect':
        require(args.pages is None or (len(args.pages) <= 24 and all(1 <= page <= 24 for page in args.pages)), 'INSPECTION_PAGE_RANGE_INVALID')
        result = inspect_template(args.template, args.pages)
        with Path(args.out).open('x', encoding='utf-8') as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
        print(json.dumps({'status': 'INVENTORY_WRITTEN', 'slides': len(result['slides'])}))
    elif args.action == 'compose':
        require(Path(args.plan).stat().st_size <= 1024 * 1024, 'PLAN_TOO_LARGE')
        result = compose(args.template, json.loads(Path(args.plan).read_text(encoding='utf-8-sig')), args.out)
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(json.dumps(validate_parts(archive(args.input), args.count)))

if __name__ == '__main__':
    main()
