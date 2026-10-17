"""Synthetic configurations only; no Docker, live configuration or reload."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1] / 'pilot_nginx.py'
SPEC = importlib.util.spec_from_file_location('tested_pilot_nginx', SOURCE)
nginx = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = nginx
SPEC.loader.exec_module(nginx)


def fixture():
    blocks = []
    for label, target in nginx.TARGETS.items():
        connection = 'upgrade' if target.websocket or label in {'native_api', 'native_plugins'} else ''
        directives = [f'        proxy_pass {target.old_proxy};',
                      '        proxy_http_version 1.1;',
                      f'        proxy_set_header Connection "{connection}";',
                      '        proxy_set_header Host $host;',
                      '        proxy_set_header Cookie $http_cookie;']
        if target.websocket:
            directives.append('        proxy_set_header Upgrade $http_upgrade;')
        if label == 'root_ws':
            directives.append('        proxy_set_header Origin $http_origin;')
        if label == 'native_page':
            directives.extend(['        proxy_set_header Accept-Encoding "";',
                               '        proxy_redirect / /megmeet-ai-native/;',
                               '        sub_filter_once on;',
                               '        sub_filter \'<head>\' \'<head><base href="/megmeet-ai-native/">\';',
                               '        # A comment with { } ; is not a directive.'])
        blocks.append('    location ' + ' '.join(target.header) + ' {\n' + '\n'.join(directives) + '\n    }')
    return '''# Original prefix comment remains byte-identical.
map $http_referer $dsh_native_api_upstream {
    default http://dsh-web-host:3084;
    ~^https://platform-ai\\.megmeet\\.com/megmeet-ai-native/ http://new-shared:3084;
}
server {
    listen 443 ssl;
    server_name platform-ai.megmeet.com;
    location / { root /usr/share/nginx/html/portal; try_files $uri $uri/ /index.html; }
    location /api/ { proxy_pass http://process-audit-cluster; }
    location /api/v1/ { auth_request /__platform_authz_8d; proxy_pass http://8d-review-cluster; }
    location /meeting/ { auth_request /__platform_authz_meeting; alias /srv/meeting/; }
    location = /v1/oauth/login { proxy_pass https://10.4.0.202; proxy_ssl_verify off; }
    location ^~ /megmeet-ai-r10/v1/ { proxy_pass http://old-gateway:8080/v1/; }
    location = /megmeet-ai-r10/health { proxy_pass http://old-gateway:8080/health; }
    location ^~ /megmeet-ai-r10/ {
        proxy_pass http://dsh-web-host:3084/;
        sub_filter '<head>' '<head><base href="/megmeet-ai-r10/">';
    }
''' + '\n'.join(blocks) + '''
}
server { server_name other.example; location = /api { return 404; } }
'''


class PilotNginxTests(unittest.TestCase):
    def setUp(self):
        self.original = fixture()
        _, _, nodes = nginx._locate(self.original)
        self.hashes = {label: nginx._sha(self.original[node.start:node.end]) for label, node in nodes.items()}
        patch = mock.patch.object(nginx, 'EXPECTED_BLOCK_SHA256', self.hashes)
        patch.start()
        self.addCleanup(patch.stop)

    def test_all_eight_proxy_destinations_and_prefix_semantics(self):
        result, report = nginx.patch_nginx(self.original)
        _, _, nodes = nginx._locate(result)
        for label, target in nginx.TARGETS.items():
            proxy = nginx._directives(nodes[label], 'proxy_pass')
            self.assertEqual([item.args for item in proxy], [(target.new_proxy,)])
        self.assertEqual(nginx.TARGETS['root_rpc'].new_proxy, nginx.UPSTREAM)
        self.assertEqual(nginx.TARGETS['native_api'].new_proxy, nginx.UPSTREAM + '/api/')
        self.assertEqual(nginx.TARGETS['native_plugins'].new_proxy, nginx.UPSTREAM + '/plugins/')
        self.assertTrue(report['changed'])
        self.assertTrue(report['other_location_bytes_and_order_unchanged'])
        self.assertEqual(report['before_sha256'], nginx._sha(self.original))
        self.assertEqual(report['after_sha256'], nginx._sha(result))

    def test_other_business_portal_sso_gateway_and_map_are_byte_identical(self):
        result, _ = nginx.patch_nginx(self.original)
        before_tree, _, before_targets = nginx._locate(self.original)
        after_tree, after_server, after_targets = nginx._locate(result)
        internal = [node for node in after_server.children if node.name == 'location'
                    and node.args in nginx.INTERNAL_HEADERS]
        self.assertEqual(nginx._non_target_locations(self.original, before_tree, before_targets),
                         nginx._non_target_locations(result, after_tree, after_targets, internal))
        before_map = next(node for node in before_tree if node.name == 'map')
        after_map = next(node for node in after_tree if node.name == 'map')
        self.assertEqual(self.original[before_map.start:before_map.end], result[after_map.start:after_map.end])
        self.assertIn('proxy_ssl_verify off;', result)
        self.assertIn('proxy_pass http://process-audit-cluster;', result)

    def test_page_base_and_filter_bytes_preserved(self):
        result, _ = nginx.patch_nginx(self.original)
        _, _, before = nginx._locate(self.original)
        _, _, after = nginx._locate(result)
        original = self.original[before['native_page'].start:before['native_page'].end]
        expected = original.replace(nginx.TARGETS['native_page'].old_proxy,
                                    nginx.TARGETS['native_page'].new_proxy, 1)
        self.assertEqual(result[after['native_page'].start:after['native_page'].end], expected)

    def test_ws_origin_and_upgrade_preserved_http_connection_cleared(self):
        result, _ = nginx.patch_nginx(self.original)
        _, _, nodes = nginx._locate(result)
        for label, target in nginx.TARGETS.items():
            connection = nginx._directives(nodes[label], 'proxy_set_header', 'connection')[0]
            self.assertEqual(connection.args, ('Connection', 'upgrade' if target.websocket else ''))
            if target.websocket:
                self.assertEqual(nginx._directives(nodes[label], 'proxy_set_header', 'origin')[0].args,
                                 ('Origin', '$http_origin'))
                self.assertEqual(len(nginx._directives(nodes[label], 'proxy_set_header', 'origin')), 1)
                self.assertEqual(nginx._directives(nodes[label], 'proxy_set_header', 'upgrade')[0].args,
                                 ('Upgrade', '$http_upgrade'))

    def test_legacy_redirect_has_no_query_and_specific_gateway_locations_remain(self):
        result, _ = nginx.patch_nginx(self.original)
        _, _, nodes = nginx._locate(result)
        legacy = nodes['legacy_page']
        self.assertEqual([(node.name, node.args) for node in legacy.children],
                         [('return', ('302', '/megmeet-ai-native/'))])
        self.assertNotIn('$request_uri', result[legacy.start:legacy.end])
        for text in ('location ^~ /megmeet-ai-r10/v1/', 'location = /megmeet-ai-r10/health'):
            self.assertIn(text, result)

    def test_both_public_internal_guards_are_exact_404(self):
        result, _ = nginx.patch_nginx(self.original)
        _, server, _ = nginx._locate(result)
        for header in nginx.INTERNAL_HEADERS:
            nodes = [node for node in server.children if node.name == 'location' and node.args == header]
            self.assertEqual(len(nodes), 1)
            self.assertEqual([(child.name, child.args) for child in nodes[0].children], [('return', ('404',))])

    def test_repeat_is_identical_and_no_duplicate_guards(self):
        first, report = nginx.patch_nginx(self.original)
        second, repeated = nginx.patch_nginx(first)
        self.assertEqual(first, second)
        self.assertFalse(repeated['changed'])
        self.assertEqual(report['after_sha256'], repeated['after_sha256'])
        self.assertEqual(first.count(nginx.MARKER), 1)

    def test_source_target_drift_rejected_before_patch(self):
        drift = self.original.replace('proxy_pass http://dsh-web-host:3084/api;',
                                      'proxy_pass http://unexpected:3084/api;', 1)
        with self.assertRaisesRegex(nginx.NginxPatchError, '^NGINX_SOURCE_BLOCK_DRIFT$'):
            nginx.patch_nginx(drift)

    def test_missing_and_duplicate_targets_rejected(self):
        missing = self.original.replace('location = /api {', 'location = /api-missing {', 1)
        with self.assertRaisesRegex(nginx.NginxPatchError, '^NGINX_TARGET_MISSING_OR_AMBIGUOUS$'):
            nginx.patch_nginx(missing)
        duplicated = self.original.replace('    listen 443 ssl;',
            '    listen 443 ssl;\n    location = /api { return 410; }', 1)
        with self.assertRaisesRegex(nginx.NginxPatchError, '^NGINX_TARGET_MISSING_OR_AMBIGUOUS$'):
            nginx.patch_nginx(duplicated)

    def test_existing_internal_guard_fails_closed(self):
        text = self.original.replace('    listen 443 ssl;',
            '    listen 443 ssl;\n    location = /megmeet-ai-native/internal { return 404; }', 1)
        with self.assertRaisesRegex(nginx.NginxPatchError, '^NGINX_INTERNAL_GUARD_ALREADY_EXISTS$'):
            nginx.patch_nginx(text)

    def test_managed_target_or_unrelated_drift_fails_closed(self):
        managed, _ = nginx.patch_nginx(self.original)
        for changed in (managed.replace(nginx.UPSTREAM + '/api;', 'http://foreign:8080/api;', 1),
                        managed.replace('proxy_ssl_verify off;', 'proxy_ssl_verify on;', 1),
                        managed + '# unrelated later edit\n'):
            with self.assertRaisesRegex(nginx.NginxPatchError, '^NGINX_MANAGED_CONFIG_DRIFT$'):
                nginx.patch_nginx(changed)

    def test_malformed_or_moved_marker_fails_closed(self):
        managed, _ = nginx.patch_nginx(self.original)
        for text in ('# first\n' + managed, nginx.MARKER + '{PRIVATE}\n' + self.original,
                     managed + nginx.MARKER + '{}\n'):
            with self.assertRaises(nginx.NginxPatchError) as error:
                nginx.patch_nginx(text)
            self.assertNotIn('PRIVATE', str(error.exception))

    def test_tokenizer_handles_quoted_braces_semicolons_variables_and_comments(self):
        text = 'set $x "${host}; { # not-comment }"; # ignored }\n location / { return 204; }'
        nodes = nginx._parse(text)
        self.assertEqual(nodes[0].args, ('$x', '${host}; { # not-comment }'))
        self.assertEqual(nodes[1].name, 'location')
        self.assertEqual(nodes[1].children[0].args, ('204',))

    def test_malformed_config_oversize_and_ambiguous_server_fail(self):
        cases = (self.original[:-2], ' ' * (nginx.MAX_CONFIG_BYTES + 1),
                 self.original + '\nserver { server_name platform-ai.megmeet.com; }\n')
        for text in cases:
            with self.assertRaises(nginx.NginxPatchError):
                nginx.patch_nginx(text)

    def test_managed_metadata_does_not_contain_raw_config(self):
        result, report = nginx.patch_nginx(self.original)
        encoded = json.dumps(report)
        for forbidden in ('sub_filter', 'Cookie', '10.4.0.202', '/srv/meeting', '$http_referer'):
            self.assertNotIn(forbidden, encoded)
        self.assertLess(len(result.splitlines()[0]), 8192)


if __name__ == '__main__':
    unittest.main()
