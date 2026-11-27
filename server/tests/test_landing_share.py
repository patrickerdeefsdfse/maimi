"""Exercise the home download link independently of production artwork and storage."""
import os
import unittest
from html.parser import HTMLParser
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'gateway'))
from app import cosmos_assets

with patch.object(cosmos_assets, 'load_assets', return_value={}):
    from app import production_landing as landing


class DownloadLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        link = dict(attrs)
        if tag == 'a' and link.get('class') == 'mm-choice':
            self.links.append(link)


class WindowsDownload(unittest.TestCase):
    version = '1.2.3'
    share = 'https://alidocs.dingtalk.com/share/test-only?x=one&y=two'

    def render(self, version, url):
        config = dict(MAIMI_WINDOWS_DOWNLOAD_SHARE_VERSION=version, MAIMI_WINDOWS_DOWNLOAD_SHARE_URL=url)
        with patch.dict(os.environ, config), patch.object(landing, 'current_release', return_value=(self.version, 'Maimi.exe', 128)):
            html = landing.render_home()
        links = DownloadLinks()
        links.feed(html)
        return links.links[1], html

    def test_internal_fallback(self):
        for version, url in [
            ('', ''), ('', self.share), ('1.2.2', self.share),
            (self.version, 'http://alidocs.dingtalk.com/test'),
            (self.version, 'https://dingtalk.com.example.invalid/test'),
            (self.version, 'https://outside.invalid@alidocs.dingtalk.com/test'),
            (self.version, 'https://alidocs.dingtalk.com:8443/test'),
            (self.version, 'https://[invalid/test'),
        ]:
            with self.subTest(url=url, version=version):
                link, _ = self.render(version, url)
                self.assertEqual(link['href'], landing.PREFIX + 'Maimi.exe')
                self.assertIn('download', link)
                self.assertNotIn('target', link)

    def test_share_matches_published_installer(self):
        link, html = self.render(self.version, self.share)
        self.assertEqual(link['href'], self.share)
        self.assertIn('&amp;y=two', html)
        self.assertEqual(link['target'], '_blank')
        self.assertEqual(set(link['rel'].split()), {'noopener', 'noreferrer'})
        self.assertNotIn('download', link)

    def test_standing_company_file_is_independent_of_update_feed(self):
        with patch.dict(os.environ, {'MAIMI_WINDOWS_INSTALLER_SHARE_URL': self.share}), patch.object(landing, 'current_release') as feed:
            html = landing.render_home()
        feed.assert_not_called()
        links = DownloadLinks()
        links.feed(html)
        self.assertEqual(links.links[1]['href'], self.share)
        self.assertIn('钉钉网盘 · Windows x64', html)


if __name__ == '__main__':
    unittest.main()
