"""Fixed production entry page; identity and metrics remain behind IAM."""
import os
import re
from html import escape
from urllib.parse import urlsplit
from fastapi import APIRouter,HTTPException
from fastapi.responses import HTMLResponse,RedirectResponse
router=APIRouter()
from .portal_assets import version_links
from .cosmic_entry import LANDING_HTML as HTML
from .distribution import router as distribution_router, current_release, PREFIX
from .plugin_admin import router as plugin_admin_router
router.include_router(plugin_admin_router)
router.include_router(distribution_router)

def dingtalk_share(url):
    if not url or '\\' in url or any(ord(c) < 33 or ord(c) == 127 for c in url):
        return None
    try:
        target = urlsplit(url)
        if (target.scheme != 'https' or target.username is not None or target.port not in (None, 443)
                or not re.fullmatch(r'(?:[a-z0-9-]+\.)*dingtalk\.com', target.hostname or '')):
            return None
    except ValueError:
        return None
    return url

def matching_download_share(version):
    if os.getenv('MAIMI_WINDOWS_DOWNLOAD_SHARE_VERSION') == version:
        return dingtalk_share(os.getenv('MAIMI_WINDOWS_DOWNLOAD_SHARE_URL', ''))

def render_home():
    company_file = dingtalk_share(os.getenv('MAIMI_WINDOWS_INSTALLER_SHARE_URL', ''))
    release = None if company_file else current_release()
    if company_file:
        windows = (f'<a class="mm-choice" href="{escape(company_file, quote=True)}" target="_blank" rel="noopener noreferrer">'
                   '<span class="choice-icon windows" aria-hidden="true"></span>'
                   '<span><strong>下载 Windows 版</strong><small>钉钉网盘 · Windows x64</small></span>'
                   '<span class="choice-arrow" aria-hidden="true">↓</span></a>')
    elif release is None:
        windows = '<div class="mm-choice unavailable"><span class="choice-icon windows" aria-hidden="true"></span><span><strong>Windows 桌面版</strong><small>安装包准备中</small></span></div>'
    else:
        version, name, size = release
        share = matching_download_share(version)
        link = (f'href="{escape(share, quote=True)}" target="_blank" rel="noopener noreferrer"'
                if share else f'href="{PREFIX}{escape(name, quote=True)}" download')
        delivery = '钉钉网盘 · ' if share else ''
        windows = (f'<a class="mm-choice" {link}>'
                   '<span class="choice-icon windows" aria-hidden="true"></span>'
                   f'<span><strong>下载 Windows 版</strong><small>{delivery}{escape(version)} · x64 · {size/1024**2:.0f} MiB</small></span>'
                   '<span class="choice-arrow" aria-hidden="true">↓</span></a>')
    head = HTML.split('</head>', 1)[0].replace('content="#040815"', 'content="#111a30"')
    if '/maimi/plugins/assets/entry.css' not in head:
        head += '<link rel="stylesheet" href="/maimi/plugins/assets/entry.css">'
    return version_links(head) + """</head><body class="mm-home">
<div class="mm-sky" aria-hidden="true"><img src="/maimi/web/cosmos/8b6e086edfbca846bb00-maimi-nebula.jpg" width="2000" height="1512" alt="" fetchpriority="high"></div>
<header class="mm-top"><a class="mm-brand" href="/maimi/" aria-label="麦米办公助手首页">MEGMEET</a>
<a class="mm-monitor" href="/maimi/monitor/">运行监控 <span aria-hidden="true">↗</span></a></header>
<main class="mm-hero"><img class="mm-mark" src="/maimi/web/cosmos/17432e1a34777091a186-maimi.png" width="106" height="106" alt="">
<h1>麦米办公助手</h1>
<nav class="mm-actions" aria-label="工作入口">
<a class="mm-choice" href="/maimi/web/"><span class="choice-icon browser" aria-hidden="true"></span>
<span><strong>打开网页版</strong><small>进入云端工作区</small></span><span class="choice-arrow" aria-hidden="true">→</span></a>
""" + windows + """
<a class="mm-choice" href="/maimi/plugins/"><span class="choice-icon plugins" aria-hidden="true"></span>
<span><strong>企业插件管理</strong><small>查看插件与授权</small></span><span class="choice-arrow" aria-hidden="true">→</span></a>
</nav></main></body></html>"""

def enabled():
 if os.getenv('MAIMI_DEPLOYMENT_MODE')!='production-ha':raise HTTPException(404)
@router.api_route('/',methods=['GET','HEAD'],include_in_schema=False)
@router.api_route('/maimi',methods=['GET','HEAD'],include_in_schema=False)
def redirect():
 enabled();return RedirectResponse('/maimi/',status_code=307,headers={'Cache-Control':'no-store'})
@router.api_route('/maimi/',methods=['GET','HEAD'],include_in_schema=False)
def landing():
 enabled();return HTMLResponse(render_home().replace('</head>', '<script defer src="/maimi/web/web-idle.js"></script></head>', 1),headers={'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Referrer-Policy':'no-referrer','Content-Security-Policy':"default-src 'none'; style-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"})
