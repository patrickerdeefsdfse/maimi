"""Small signed-out entry page and immutable representations of fixed browser programs."""
import base64
import gzip
import hashlib
from functools import lru_cache
from fastapi.responses import HTMLResponse, Response
from .portal_assets import accepts_gzip

LOGIN = '/v1/iam/login?return_to=%2Fmaimi%2Fweb%2F'
# Share the per-tab guard with the existing recovery/expired-session flow.
_AUTO = """(()=>{try {
  const key='megmeet.web.iam-attempt.v1', raw=sessionStorage.getItem(key), now=Date.now();
  if(raw!==null && Number.isFinite(Number(raw)) && now-Number(raw)<120000)return;
  sessionStorage.setItem(key,String(now));
  if(sessionStorage.getItem(key)===String(now))location.replace('/v1/iam/login?return_to=%2Fmaimi%2Fweb%2F');
}catch{}})();"""
_STYLE = """*{box-sizing:border-box}html{color-scheme:light}body{margin:0;background:#edf4e9;color:#21382e;font:16px/1.6 system-ui,"Microsoft YaHei",sans-serif;min-height:100vh;display:grid;place-items:center}main{width:min(90vw,26rem);padding:2rem}h1{font-size:1.65rem;margin:.4em 0}p{margin:.4em 0 1.4em}a{display:inline-block;color:white;background:#28574a;border-radius:.5rem;padding:.55em 1.15em;text-decoration:none}a:focus-visible{outline:3px solid #14659a;outline-offset:3px}"""

def _csp_hash(text):
    return "'sha256-" + base64.b64encode(hashlib.sha256(text.encode()).digest()).decode() + "'"

@lru_cache(maxsize=2)
def _signed_out_view(auto):
    script='<script>'+_AUTO+'</script>' if auto else ''
    title='正在进入麦米' if auto else '登录麦米办公助手'
    msg='正在连接公司登录页。若未跳转，请点击下方按钮。' if auto else '通过公司账号进入个人工作区。'
    html=('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'+script
          +'<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="theme-color" content="#edf4e9">'
          +'<title>'+title+'</title><style>'+_STYLE+'</style></head><body><main><h1>'+title+'</h1><p>'+msg
          +'</p><a href="'+LOGIN+'">通过公司账号登录</a></main></body></html>')
    policy=("default-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'; "
            +"style-src "+_csp_hash(_STYLE)+"; script-src "+(_csp_hash(_AUTO) if auto else "'none'"))
    return html,policy

def signed_out_view(auto, headers):
    html,policy=_signed_out_view(auto)
    return HTMLResponse(html,headers={**headers,'Content-Security-Policy':policy})

@lru_cache(maxsize=5)
def _program(text):
    raw=text.encode()
    return hashlib.sha256(raw).hexdigest()[:24],raw,gzip.compress(raw,compresslevel=4,mtime=0)

def program_url(name,text):
    return '/maimi/web/'+name+'.js?v='+_program(text)[0]

def program_response(request,text,headers):
    version,raw,zipped=_program(text)
    headers=dict(headers)
    if list(request.query_params.multi_items())==[('v',version)]:
        headers={k:v for k,v in headers.items() if k.lower() not in ('cache-control','pragma')}
        headers['Cache-Control']='private, max-age=31536000, immutable'
    headers['Vary']='Accept-Encoding'
    if not request.headers.get('range') and accepts_gzip(request.headers.get('accept-encoding','')):
        headers['Content-Encoding']='gzip'
        raw=zipped
    return Response(raw,media_type='text/javascript',headers=headers)
