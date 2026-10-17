"""Cold-entry login recovery only; no live DSH session renewal or logout."""
from __future__ import annotations

from html import escape
import json
from urllib.parse import quote, urlencode

RELEASE_MARKER = "MEGMEET_ENTRY_LOGIN_ONLY_V1"
NATIVE_PATH = "/megmeet-ai-native/"
SCRIPT_PATH = NATIVE_PATH + "__megmeet/login.js"
IDENTITY_PATH = NATIVE_PATH + "__megmeet/identity"
REFRESH_PATH = "/v1/auth/refresh-token"
LOGIN_HEADERS = {
    "Cache-Control": "private, no-store",
    "Vary": "Cookie, Accept",
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; connect-src 'self'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'none'; object-src 'none'",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}
SCRIPT_HEADERS = {
    "Cache-Control": "private, no-store",
    "X-Content-Type-Options": "nosniff",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Referrer-Policy": "no-referrer",
}


def endpoints(public_origin: str) -> dict[str, str]:
    """Use the validated configuration origin, never request headers or returns."""
    native = public_origin + NATIVE_PATH
    query = urlencode({"redirect_uri": native, "client_type": "[web]"}, quote_via=quote)
    return {
        "native": native,
        "refresh": public_origin + REFRESH_PATH,
        "identity": public_origin + IDENTITY_PATH,
        "login": public_origin + "/v1/oauth/login?" + query,
    }


def login_page(public_origin: str) -> str:
    urls = endpoints(public_origin)
    return f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="megmeet-entry-scope" content="ENTRY_LOGIN_ONLY">
<meta name="megmeet-entry-release" content="{RELEASE_MARKER}">
<title>MEGMEET AI · 公司登录</title>
<script type="module" src="{escape(public_origin + SCRIPT_PATH, quote=True)}"></script>
</head><body><main><h1>请先登录公司门户</h1>
<p id="megmeet-login-status" role="status" aria-live="polite">正在检查公司登录状态，请稍候。</p>
<p><a id="megmeet-login-link" href="{escape(urls['login'], quote=True)}">公司登录</a></p>
<p><button id="megmeet-login-retry" type="button" disabled>重试登录检查</button></p>
<noscript><p>请点击“公司登录”完成登录，再返回个人工作区。</p></noscript>
</main></body></html>'''


# Separate public module: no inline code, credentials, client-selected URLs,
# browser storage, background retry loop, or native RPC replay.
LOGIN_JAVASCRIPT = r'''
const MESSAGES = Object.freeze({
  checking: '正在检查公司登录状态，请稍候。',
  login: '登录状态已失效，请点击“公司登录”。完成后将返回个人工作区。',
  denied: '当前账号尚未开通个人工作区，请联系管理员。',
  temporary: '暂时无法验证公司登录，请稍后点击“重试登录检查”。',
  unexpected: '登录服务返回异常，请稍后重试。',
  ready: '已确认公司登录，正在进入个人工作区。',
});

function state(code) {
  return Object.freeze({ code, message: MESSAGES[code], canLogin: ['login', 'temporary', 'unexpected'].includes(code),
    canRetry: ['login', 'temporary', 'unexpected'].includes(code) });
}

class InvalidIdentity extends Error {}

async function request(fetcher, url, method, readIdentity = false) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 12000);
  try {
    const response = await fetcher(url, { method, credentials: 'include', mode: 'same-origin',
      cache: 'no-store', redirect: 'error', headers: { Accept: 'application/json' },
      signal: controller.signal });
    let payload;
    if (readIdentity && response.status < 500) payload = await identityPayload(response);
    else await response.body?.cancel();
    return { status: response.status, ok: response.ok, payload };
  } finally {
    clearTimeout(timeout);
  }
}

async function identityPayload(response) {
  if (!/^application\/json(?:\s*;|$)/i.test(response.headers.get('content-type') || '')) {
    throw new InvalidIdentity();
  }
  const raw = await response.text();
  if (raw.length > 16384) throw new InvalidIdentity();
  let value;
  try { value = JSON.parse(raw); } catch { throw new InvalidIdentity(); }
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new InvalidIdentity();
  }
  return value;
}

export function createRecovery({ fetcher, navigate, show = () => {} }) {
  let running = false;
  return async function recover() {
    if (running) return;
    running = true;
    show(state('checking'));
    try {
      const refreshed = await request(fetcher, CONFIG.refresh, 'POST');
      if (refreshed.status === 401 || refreshed.status === 403) {
        show(state('login'));
        return;
      }
      if (refreshed.status >= 500) {
        show(state('temporary'));
        return;
      }
      if (!refreshed.ok || refreshed.status < 200 || refreshed.status >= 300) {
        show(state('unexpected'));
        return;
      }
      // Set-Cookie has reached this browser before this separate same-origin
      // identity request. A successful refresh response alone grants nothing.
      const identity = await request(fetcher, CONFIG.identity, 'GET', true);
      if (identity.status >= 500) {
        show(state('temporary'));
        return;
      }
      const value = identity.payload;
      if ((identity.status === 401 || identity.status === 403)
          && value.detail === 'authentication required') {
        show(state('login'));
        return;
      }
      if (identity.status === 403
          && value.detail === 'account not enabled for personal pilot') {
        show(state('denied'));
        return;
      }
      if (identity.status !== 200 || value.authenticated !== true
          || typeof value.user_id !== 'string'
          || !/^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(value.user_id)
          || typeof value.workspace_id !== 'string'
          || !/^[A-Za-z0-9_-]{1,128}$/.test(value.workspace_id)) {
        show(state('unexpected'));
        return;
      }
      show(state('ready'));
      navigate(CONFIG.native);
    } catch (error) {
      // Do not expose upstream response text or raw exception messages.
      show(state(error instanceof InvalidIdentity ? 'unexpected' : 'temporary'));
    } finally {
      running = false;
    }
  };
}

if (typeof document !== 'undefined') {
  const status = document.getElementById('megmeet-login-status');
  const retry = document.getElementById('megmeet-login-retry');
  const login = document.getElementById('megmeet-login-link');
  if (status && retry && login) {
    const recover = createRecovery({
      fetcher: globalThis.fetch.bind(globalThis),
      navigate: url => window.location.replace(url),
      show: value => {
        status.textContent = value.message;
        retry.disabled = !value.canRetry;
        login.hidden = !value.canLogin;
      },
    });
    retry.addEventListener('click', () => { void recover(); });
    // Exactly one automatic recovery attempt per denied-page load. Entering
    // company login requires a click, so failed callbacks cannot redirect-loop.
    void recover();
  }
}
'''


def login_script(public_origin: str) -> str:
    # Escape HTML metacharacters as well, even though this is an external module.
    config = json.dumps(endpoints(public_origin), ensure_ascii=True).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
    return f"// {RELEASE_MARKER} scope=ENTRY_LOGIN_ONLY\nconst CONFIG = Object.freeze({config});\n" + LOGIN_JAVASCRIPT
