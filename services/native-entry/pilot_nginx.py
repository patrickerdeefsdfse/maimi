"""Pure, fail-closed Nginx patch for the verified two-account native pilot.

No filesystem, Docker or Nginx operations occur here. Source block fingerprints
come from the user's 2026-09-16 focused inspection. The caller owns atomic
installation, nginx -t, reload and restoring its saved configuration on failure.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re


DOMAIN = 'platform-ai.megmeet.com'
UPSTREAM = 'http://megmeet-native-personal-entry:8080'
MARKER = '# MEGMEET_NATIVE_PERSONAL_ENTRY_V1 '
MAX_CONFIG_BYTES = 2 * 1024 * 1024


class NginxPatchError(ValueError):
    """Fixed diagnostic codes only; never contains configuration content."""


def _require(value, code):
    if not value:
        raise NginxPatchError(code)


def _sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


@dataclass(frozen=True)
class Target:
    header: tuple[str, ...]
    old_proxy: str
    new_proxy: str
    websocket: bool = False


TARGETS = {
    'root_api': Target(('=', '/api'), 'http://dsh-web-host:3084/api', UPSTREAM + '/api'),
    'root_ws': Target(('=', '/api/remote.mux'), '$dsh_native_api_upstream/api/remote.mux',
                      UPSTREAM + '/api/remote.mux', True),
    'root_plugins': Target(('^~', '/plugins/'), 'http://dsh-web-host:3084', UPSTREAM),
    'root_rpc': Target(('~', '^/api/(dynamicCordisRunner|credentials|session|directoryPicker|llm|pluginInventory|settings|workspace|agentPresets)(/|$)'),
                       '$dsh_native_api_upstream', UPSTREAM),
    'native_ws': Target(('=', '/megmeet-ai-native/api/remote.mux'),
                        'http://megmeet-ai-platform-new-dsh-web-host-1:3084/api/remote.mux',
                        UPSTREAM + '/api/remote.mux', True),
    'native_api': Target(('^~', '/megmeet-ai-native/api/'),
                         'http://megmeet-ai-platform-new-dsh-web-host-1:3084/api/', UPSTREAM + '/api/'),
    'native_plugins': Target(('^~', '/megmeet-ai-native/plugins/'),
                             '$dsh_native_api_upstream/plugins/', UPSTREAM + '/plugins/'),
    'native_page': Target(('^~', '/megmeet-ai-native/'),
                          'http://megmeet-ai-platform-new-dsh-web-host-1:3084/', UPSTREAM + '/'),
}
LEGACY_HEADER = ('^~', '/megmeet-ai-r10/')
INTERNAL_HEADERS = (('=', '/megmeet-ai-native/internal'), ('^~', '/megmeet-ai-native/internal/'))
EXPECTED_BLOCK_SHA256 = {
    'root_api': '11b732075815f5ed465c211fe13d250823a5732fb1752199f26e49a0c5be7322',
    'root_ws': '71c326dab9ca4d325b51b51bc9ca1560e13ae6e70855cc00eb682d16fbc99748',
    'root_plugins': '8b5b04eebb528f85f9a01d175e5b22e1bfb588ef48faf7ab515319b075c0dd25',
    'root_rpc': '2fd0257af87b6dd39b1e40010cc845b14bc530e052e1052dd261a5355afe7b49',
    'native_ws': '40e9f834ce4f5c2481d1ae690572d99f47ebb0d42cd6fd863e5b42cb37060526',
    'native_api': '5f32ca42f787cc35405cda045b9ef9c928a965949188b906d98e8d857a612815',
    'native_plugins': 'a94dbb6550c7c0f9a6f9079a2540fb7b2837b25ffcce0b0e74caaf3449db01c0',
    'native_page': 'cedb0bc4cc59848f7102820d7a5b23a8e10999329baa9605fd5b4367e7c53790',
    'legacy_page': 'aa0149cd2e1c2e903dfb04a5234f78fac2c4b76903210ae6579bd4f7e4136666',
}


@dataclass(frozen=True)
class _Token:
    value: str
    start: int
    end: int
    delimiter: bool = False


@dataclass
class _Node:
    words: list[_Token]
    start: int
    end: int
    children: list['_Node']

    @property
    def name(self):
        return self.words[0].value

    @property
    def args(self):
        return tuple(word.value for word in self.words[1:])


def _tokens(text):
    i = 0
    while i < len(text):
        if text[i].isspace():
            i += 1
            continue
        if text[i] == '#':
            end = text.find('\n', i)
            i = len(text) if end < 0 else end + 1
            continue
        start = i
        if text[i] in '{};':
            yield _Token(text[i], i, i + 1, True)
            i += 1
            continue
        value, quote = [], None
        while i < len(text):
            char = text[i]
            if char == '\\' and i + 1 < len(text):
                value.extend((char, text[i + 1]))
                i += 2
                continue
            if quote:
                if char == quote:
                    quote = None
                else:
                    value.append(char)
                i += 1
                continue
            if char in '\"\'':
                quote = char
                i += 1
                continue
            if text.startswith('${', i):
                end = text.find('}', i + 2)
                _require(end >= 0, 'NGINX_VARIABLE_UNCLOSED')
                value.append(text[i:end + 1])
                i = end + 1
                continue
            if char.isspace() or char in '{};#':
                break
            value.append(char)
            i += 1
        _require(quote is None and i > start, 'NGINX_TOKEN_INVALID')
        yield _Token(''.join(value), start, i)


def _parse(text):
    stream = list(_tokens(text))
    _require(len(stream) <= 150000, 'NGINX_TOKEN_LIMIT')
    position = 0

    def scope(depth=0):
        nonlocal position
        _require(depth <= 64, 'NGINX_NESTING_LIMIT')
        result = []
        while position < len(stream):
            token = stream[position]
            if token.delimiter and token.value == '}':
                _require(depth > 0, 'NGINX_UNEXPECTED_CLOSE')
                position += 1
                return result, token.end
            words = []
            while position < len(stream) and not stream[position].delimiter:
                words.append(stream[position])
                position += 1
            _require(words and position < len(stream), 'NGINX_DIRECTIVE_INCOMPLETE')
            delimiter = stream[position]
            position += 1
            children, end = [], delimiter.end
            if delimiter.value == '{':
                children, end = scope(depth + 1)
            else:
                _require(delimiter.value == ';', 'NGINX_DIRECTIVE_INVALID')
            result.append(_Node(words, words[0].start, end, children))
        _require(depth == 0, 'NGINX_BLOCK_UNCLOSED')
        return result, len(text)

    return scope()[0]


def _walk(nodes):
    for node in nodes:
        yield node
        yield from _walk(node.children)


def _locate(text):
    tree = _parse(text)
    servers = [node for node in _walk(tree) if node.name == 'server'
               and any(child.name == 'server_name' and DOMAIN in child.args for child in node.children)]
    _require(len(servers) == 1, 'NGINX_SERVER_AMBIGUOUS')
    server = servers[0]
    locations = [node for node in _walk(server.children) if node.name == 'location']
    result = {}
    headers = {label: target.header for label, target in TARGETS.items()}
    headers['legacy_page'] = LEGACY_HEADER
    for label, header in headers.items():
        found = [node for node in locations if node.args == header]
        _require(len(found) == 1 and any(found[0] is child for child in server.children),
                 'NGINX_TARGET_MISSING_OR_AMBIGUOUS')
        result[label] = found[0]
    return tree, server, result


def _non_target_locations(text, tree, targets, internal=()):
    excluded = {id(node) for node in (*targets.values(), *internal)}
    return [(node.args, _sha(text[node.start:node.end]))
            for node in _walk(tree) if node.name == 'location' and id(node) not in excluded]


def _ordered_sha(records):
    return _sha(json.dumps(records, ensure_ascii=True, separators=(',', ':')))


def _directives(node, name, first_arg=None):
    return [child for child in node.children if child.name == name
            and (first_arg is None or child.args and child.args[0].lower() == first_arg)]


def _indent(text, start):
    prefix = text[text.rfind('\n', 0, start) + 1:start]
    return prefix if not prefix.strip() else '        '


def _edit(text, edits):
    chunks, last = [], 0
    for start, end, value in sorted(edits, key=lambda item: (item[0], item[1])):
        _require(0 <= last <= start <= end <= len(text), 'NGINX_EDITS_OVERLAP')
        chunks.extend((text[last:start], value))
        last = end
    chunks.append(text[last:])
    return ''.join(chunks)


def _verify_result(body, metadata):
    tree, server, targets = _locate(body)
    for label, target in TARGETS.items():
        proxies = _directives(targets[label], 'proxy_pass')
        _require(len(proxies) == 1 and proxies[0].args == (target.new_proxy,), 'NGINX_PATCHED_PROXY_DRIFT')
        if target.websocket:
            origins = _directives(targets[label], 'proxy_set_header', 'origin')
            _require(len(origins) == 1 and origins[0].args[1:] == ('$http_origin',), 'NGINX_PATCHED_ORIGIN_DRIFT')
    legacy = targets['legacy_page']
    _require(len(legacy.children) == 1 and legacy.children[0].name == 'return'
             and legacy.children[0].args == ('302', '/megmeet-ai-native/'), 'NGINX_PATCHED_LEGACY_DRIFT')
    internal = []
    for header in INTERNAL_HEADERS:
        found = [node for node in _walk(server.children) if node.name == 'location' and node.args == header]
        _require(len(found) == 1 and any(found[0] is child for child in server.children), 'NGINX_INTERNAL_GUARD_DRIFT')
        node = found[0]
        _require(len(node.children) == 1 and node.children[0].name == 'return'
                 and node.children[0].args == ('404',), 'NGINX_INTERNAL_GUARD_DRIFT')
        internal.append(node)
    hashes = {label: _sha(body[node.start:node.end]) for label, node in targets.items()}
    _require(hashes == metadata['targets_after_sha256'], 'NGINX_PATCHED_BLOCK_DRIFT')
    untouched = _non_target_locations(body, tree, targets, internal)
    _require(len(untouched) == metadata['untouched_location_count']
             and _ordered_sha(untouched) == metadata['untouched_locations_sha256'], 'NGINX_OTHER_LOCATION_DRIFT')


def patch_nginx(text: str) -> tuple[str, dict]:
    """Return patched text and hash-only audit metadata; reject any target drift.

    A generated comment records exact resulting bytes and original block hashes.
    An identical repeat is a no-op. Any later managed-file edit fails closed and
    must be reviewed; the function offers no force or fingerprint-bypass option.
    """
    try:
        _require(isinstance(text, str) and len(text.encode('utf-8')) <= MAX_CONFIG_BYTES,
                 'NGINX_CONFIG_SIZE')
        if MARKER in text:
            _require(text.startswith(MARKER) and text.count(MARKER) == 1, 'NGINX_MANAGED_MARKER_INVALID')
            line, separator, body = text.partition('\n')
            _require(separator and len(line) <= 8192, 'NGINX_MANAGED_MARKER_INVALID')
            metadata = json.loads(line[len(MARKER):])
            keys = {'schema', 'before_sha256', 'body_sha256', 'targets_before_sha256',
                    'targets_after_sha256', 'untouched_location_count', 'untouched_locations_sha256'}
            _require(isinstance(metadata, dict) and set(metadata) == keys and metadata['schema'] == 1,
                     'NGINX_MANAGED_MARKER_INVALID')
            _require(line == MARKER + json.dumps(metadata, sort_keys=True, separators=(',', ':')),
                     'NGINX_MANAGED_MARKER_INVALID')
            _require(metadata['targets_before_sha256'] == EXPECTED_BLOCK_SHA256
                     and re.fullmatch(r'[0-9a-f]{64}', metadata['before_sha256']) is not None
                     and _sha(body) == metadata['body_sha256'], 'NGINX_MANAGED_CONFIG_DRIFT')
            _verify_result(body, metadata)
            return text, {**metadata, 'changed': False, 'after_sha256': _sha(text),
                          'other_location_bytes_and_order_unchanged': True}

        tree, server, targets = _locate(text)
        for label, node in targets.items():
            _require(_sha(text[node.start:node.end]) == EXPECTED_BLOCK_SHA256[label], 'NGINX_SOURCE_BLOCK_DRIFT')
        _require(not any(node.name == 'location' and node.args in INTERNAL_HEADERS
                         for node in _walk(server.children)), 'NGINX_INTERNAL_GUARD_ALREADY_EXISTS')
        untouched = _non_target_locations(text, tree, targets)
        edits = []
        for label, target in TARGETS.items():
            node = targets[label]
            proxies = _directives(node, 'proxy_pass')
            _require(len(proxies) == 1 and proxies[0].args == (target.old_proxy,), 'NGINX_SOURCE_PROXY_MISMATCH')
            proxy = proxies[0]
            argument = proxy.words[1]
            edits.append((argument.start, argument.end, target.new_proxy))
            if target.websocket:
                origins = _directives(node, 'proxy_set_header', 'origin')
                _require(len(origins) <= 1, 'NGINX_SOURCE_ORIGIN_AMBIGUOUS')
                if origins:
                    _require(origins[0].args[1:] == ('$http_origin',), 'NGINX_SOURCE_ORIGIN_MISMATCH')
                else:
                    edits.append((proxy.end, proxy.end, '\n' + _indent(text, proxy.start)
                                  + 'proxy_set_header Origin $http_origin;'))
            else:
                connections = _directives(node, 'proxy_set_header', 'connection')
                _require(len(connections) <= 1 and all(len(item.args) == 2 for item in connections),
                         'NGINX_SOURCE_CONNECTION_AMBIGUOUS')
                if connections and connections[0].args[1] != '':
                    argument = connections[0].words[2]
                    edits.append((argument.start, argument.end, '""'))
        legacy = targets['legacy_page']
        indent = _indent(text, legacy.start)
        replacement = 'location ^~ /megmeet-ai-r10/ {\n' + indent + '    return 302 /megmeet-ai-native/;\n' + indent + '}'
        edits.append((legacy.start, legacy.end, replacement))
        indent = _indent(text, targets['native_page'].start)
        guards = ''.join('\n' + indent + 'location ' + ' '.join(header) + ' {\n'
                         + indent + '    return 404;\n' + indent + '}\n' for header in INTERNAL_HEADERS)
        edits.append((targets['native_page'].start, targets['native_page'].start, guards + indent))
        body = _edit(text, edits)
        _, _, patched_targets = _locate(body)
        metadata = {
            'schema': 1, 'before_sha256': _sha(text), 'body_sha256': _sha(body),
            'targets_before_sha256': dict(EXPECTED_BLOCK_SHA256),
            'targets_after_sha256': {label: _sha(body[node.start:node.end]) for label, node in patched_targets.items()},
            'untouched_location_count': len(untouched),
            'untouched_locations_sha256': _ordered_sha(untouched),
        }
        _verify_result(body, metadata)
        result = MARKER + json.dumps(metadata, sort_keys=True, separators=(',', ':')) + '\n' + body
        return result, {**metadata, 'changed': True, 'after_sha256': _sha(result),
                        'other_location_bytes_and_order_unchanged': True}
    except NginxPatchError:
        raise
    except Exception:
        raise NginxPatchError('NGINX_PATCH_PARSE_INVALID') from None
