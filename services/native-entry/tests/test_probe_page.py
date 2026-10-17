"""Pure static native-page URL guards; no runtime, browser or network calls."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
import unittest

SOURCE = Path(__file__).resolve().parents[1] / 'probe_page.py'
SPEC = importlib.util.spec_from_file_location('tested_probe_page', SOURCE)
page = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = page
SPEC.loader.exec_module(page)
PUBLIC = 'https://platform-ai.megmeet.com'


def document(*, entries=None, batches=None, head='', assignment='window.__DSH_BOOT__'):
    graph = {'entries': [{'id': 'native-module', 'url': '/plugins/native.js'}] if entries is None else entries}
    if batches is not None:
        graph['batches'] = batches
    return '<html><head>' + head + '<script>' + assignment + ' = ' + json.dumps(graph) + ';</script></head></html>'


class PageTests(unittest.TestCase):
    def rejected(self, html, expected=None, secret=None):
        with self.assertRaises(page.PageCheckError) as raised:
            page.validated_page_paths(html, PUBLIC)
        self.assertRegex(str(raised.exception), r'^PAGE_[A-Z_]+$')
        if expected:
            self.assertEqual(str(raised.exception), expected)
        if secret:
            self.assertNotIn(secret, str(raised.exception))

    def test_relative_and_public_https_assets_are_deduplicated_paths(self):
        html = document(head='<script src="assets/main.js?v=2"></script>'
                             '<link rel="stylesheet" href="/assets/site.css">'
                             '<link rel="preload" href="https://platform-ai.megmeet.com/assets/font.woff2">'
                             '<link rel="modulepreload" href="/plugins/native.js">')
        self.assertEqual(page.validated_page_paths(html, PUBLIC),
                         ['/assets/main.js?v=2', '/assets/site.css', '/assets/font.woff2', '/plugins/native.js'])

    def test_protocol_relative_public_and_default_port_are_same_origin(self):
        html = document(entries=[{'url': '//platform-ai.megmeet.com/assets/one.js'},
                                 {'url': PUBLIC + ':443/assets/two.js#fragment'}])
        self.assertEqual(page.validated_page_paths(html, PUBLIC), ['/assets/one.js', '/assets/two.js'])

    def test_loopback_other_authority_and_scheme_are_rejected(self):
        for url in ['http://127.0.0.1:3085/assets/main.js', '//127.0.0.1:3085/plugins/native.js',
                    'https://localhost/plugins/native.js', 'https://platform-ai.megmeet.com:8443/main.js',
                    'http://platform-ai.megmeet.com/main.js', 'https://foreign.invalid/main.js']:
            with self.subTest(url=url):
                self.rejected(document(entries=[{'url': url}]), secret=url)

    def test_base_must_keep_public_root(self):
        for href in ['/', PUBLIC + '/', PUBLIC]:
            with self.subTest(href=href):
                self.assertEqual(page.validated_page_paths(document(head='<base href="' + href + '">'), PUBLIC),
                                 ['/plugins/native.js'])
        for href in ['/nested/', '/?v=2', '/#anchor', '//127.0.0.1:3085/']:
            with self.subTest(href=href):
                self.rejected(document(head='<base href="' + href + '">'))

    def test_secret_query_empty_case_and_encoded_names_are_rejected(self):
        for query in ['token=never-print', 'TOKEN=', '%74oken=value', 'api_key=x',
                      'ACCESS_TOKEN=x', 'refresh_token=x']:
            with self.subTest(query=query):
                self.rejected(document(entries=[{'url': '/plugins/native.js?' + query}]),
                              expected='PAGE_URL_SECRET_QUERY', secret=query)

    def test_html_entities_are_decoded_before_query_validation(self):
        self.rejected(document(head='<script src="/asset.js?v=2&amp;token=secret"></script>'),
                      expected='PAGE_URL_SECRET_QUERY')

    def test_credentials_control_and_backslash_are_rejected(self):
        for url in [PUBLIC.replace('://', '://private:secret@') + '/asset.js',
                    '/assets/\nmain.js', '/assets/\\main.js', ' /asset.js', '/asset.js\x7f']:
            with self.subTest(url=url):
                self.rejected(document(entries=[{'url': url}]), secret='private:secret')

    def test_bad_json_duplicate_keys_non_json_number_and_graph_shape_are_controlled(self):
        for source in ['{broken}', '{"entries":[],"entries":[{"url":"/x.js"}]}',
                       '{"entries":[{"url":NaN}]}', '{"entries":{}}']:
            with self.subTest(source=source):
                self.rejected('<script>window.__DSH_BOOT__=' + source + ';</script>')

    def test_oversized_html_and_more_than_100_urls_are_rejected(self):
        self.rejected(document() + 'a' * page.MAX_HTML_BYTES, expected='PAGE_HTML_LIMIT')
        self.rejected(document(entries=[{'url': '/module-' + str(i) + '.js'} for i in range(101)]),
                      expected='PAGE_URL_LIMIT')
        self.assertEqual(len(page.validated_page_paths(document(entries=[{'url': '/m-' + str(i) + '.js'} for i in range(100)]), PUBLIC)), 100)

    def test_at_least_one_module_url_is_required(self):
        self.rejected(document(entries=[], head='<script src="/asset.js"></script>'),
                      expected='PAGE_MODULE_URL_MISSING')

    def test_batches_are_preferred_and_empty_batches_fall_back_to_entries(self):
        html = document(entries=[{'url': 'http://127.0.0.1:3085/unused.js'}],
                        batches=[{'url': '/plugins/batch.js'}])
        self.assertEqual(page.validated_page_paths(html, PUBLIC), ['/plugins/batch.js'])
        self.assertEqual(page.validated_page_paths(document(batches=[]), PUBLIC), ['/plugins/native.js'])
        self.rejected(document(batches=[{'url': None}]), expected='PAGE_URL_TYPE')

    def test_supported_boot_assignment_shapes_and_comments_are_not_graphs(self):
        for assignment in ['globalThis.__DSH_BOOT__', "window['__DSH_BOOT__']", 'globalThis["__DSH_BOOT__"]']:
            self.assertEqual(page.validated_page_paths(document(assignment=assignment), PUBLIC), ['/plugins/native.js'])
        self.rejected('<!-- <script>window.__DSH_BOOT__={"entries":[{"url":"/fake.js"}]};</script> -->',
                      expected='PAGE_BOOT_ASSIGNMENT_COUNT')

    def test_duplicate_url_attributes_and_multiple_boot_assignments_fail_closed(self):
        self.rejected(document(head='<script src="/safe.js" src="http://127.0.0.1/a.js"></script>'),
                      expected='PAGE_DUPLICATE_URL_ATTRIBUTE')
        self.rejected(document() + document(), expected='PAGE_BOOT_ASSIGNMENT_COUNT')

    def test_public_origin_rejects_credentials_and_nonroot_paths(self):
        for origin in ['http://platform-ai.megmeet.com', PUBLIC + '/nested/',
                       'https://u:p@platform-ai.megmeet.com', PUBLIC + '?token=secret']:
            with self.subTest(origin=origin), self.assertRaises(page.PageCheckError):
                page.validated_page_paths(document(), origin)


if __name__ == '__main__':
    unittest.main()
