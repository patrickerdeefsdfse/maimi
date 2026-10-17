"""Best-effort public Bing HTML search, only after selected-tool authorization.

Fixed HTTPS origin; pinned public DNS/TLS transport, bounded cancellable child.
No provider token, cookies, chat history, private routes, CAPTCHA workaround,
result-link fetches, paid fallback or automatic retries.
"""
from __future__ import annotations
import asyncio
import base64
import http.client
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import time
from html.parser import HTMLParser
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

if __package__:
    from . import connector_web_reader as reader
else:
    # Isolated stdlib-only subprocess: fixed adjacent reviewed source, not a
    # user-controlled import path or PYTHONPATH. No site initializers run.
    spec=importlib.util.spec_from_file_location('reviewed_public_reader',Path(__file__).with_name('connector_web_reader.py'))
    reader=importlib.util.module_from_spec(spec);spec.loader.exec_module(reader)

BING_ORIGIN='https://cn.bing.com'
MAX_BYTES=1024*1024
_search_slots=asyncio.Semaphore(4)
CHALLENGE=re.compile(r'(?:^|/)(?:captcha|challenge|verify|sorry)(?:/|[?.#]|$)',re.I)
TITLE_CHALLENGE=re.compile(r'^(?:captcha|verify (?:you|your)|verification|security check|人机验证|安全验证|请完成验证)',re.I)

class SearchFailure(Exception):
    def __init__(self,status,code):self.status,self.code=status,code;super().__init__(code)

def arguments(args):
    if not isinstance(args,dict) or set(args)-{'query','count','freshness'}:raise SearchFailure(400,'connector_arguments_invalid')
    query,count,freshness=args.get('query'),args.get('count',5),args.get('freshness')
    if (not isinstance(query,str) or not 1<=len(query.strip())<=400 or any(ord(c)<32 or ord(c)==127 for c in query)
        or type(count) is not int or not 1<=count<=10 or freshness not in (None,'pd','pw','pm','py')):
        raise SearchFailure(400,'connector_arguments_invalid')
    # No documented, tested date filter is inferred from private Bing syntax.
    if freshness is not None:raise SearchFailure(400,'web_search_freshness_unsupported')
    return {'query':query,'count':count,'freshness':None}

def result_url(value):
    try:
        if not isinstance(value,str) or len(value)>8192:raise ValueError()
        value=urljoin(BING_ORIGIN,value);p=urlsplit(value)
        if p.hostname in {'cn.bing.com','www.bing.com'} and p.path.rstrip('/')=='/ck/a':
            if p.scheme!='https' or p.username or p.password or p.port not in (None,443):raise ValueError()
            pairs=parse_qsl(p.query,max_num_fields=32);targets=[v for k,v in pairs if k=='u']
            if len(targets)!=1 or not targets[0].startswith('a1'):raise ValueError()
            token=targets[0][2:]
            if not token or not re.fullmatch(r'[A-Za-z0-9_-]+={0,2}',token):raise ValueError()
            value=base64.b64decode(token+'='*((-len(token))%4),altchars=b'-_',validate=True).decode('utf-8')
            p=urlsplit(value)
        if p.hostname in {'cn.bing.com','www.bing.com'} and p.path.rstrip('/')=='/ck/a':raise ValueError()
        return reader.public_url(urlunsplit((p.scheme,p.netloc,p.path,p.query,'')))
    except (ValueError,UnicodeError,reader.WebReadFailure):
        return None

class BingResults(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True);self.stack=[];self.item=None;self.item_depth=0
        self.heading=0;self.paragraph=0;self.anchor=0;self.skip=0;self.entries=[]
        self.title_depth=0;self.title=[];self.challenge=False;self.no_results=False
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs);void=tag in {'area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr'}
        if not void:self.stack.append(tag)
        depth=len(self.stack)
        if tag in {'script','style','noscript','svg'}:self.skip+=1
        if tag=='title':self.title_depth=depth
        if tag in {'form','iframe'}:
            target=attrs.get('action' if tag=='form' else 'src','')
            identity=attrs.get('id','')+' '+attrs.get('name','')
            if CHALLENGE.search(target) or re.search(r'captcha|challenge',identity,re.I):self.challenge=True
        classes=attrs.get('class','').split()
        if 'b_no' in classes or attrs.get('id')=='b_no':self.no_results=True
        if tag=='li' and 'b_algo' in classes and self.item is None:
            self.item={'title':[],'snippet':[],'href':None};self.item_depth=depth
        if self.item is not None:
            if tag=='h2':self.heading=depth
            if tag=='p':self.paragraph=depth
            if tag=='a' and self.heading and self.item['href'] is None:
                self.item['href']=attrs.get('href','');self.anchor=depth
    def handle_endtag(self,tag):
        if tag in {'script','style','noscript','svg'} and self.skip:self.skip-=1
        if tag not in self.stack:return
        depth=len(self.stack)-self.stack[::-1].index(tag)
        if self.item is not None and depth==self.item_depth and tag=='li':
            if len(self.entries)<100:self.entries.append(self.item)
            self.item=None;self.item_depth=0
        if self.heading>=depth:self.heading=0
        if self.paragraph>=depth:self.paragraph=0
        if self.anchor>=depth:self.anchor=0
        if self.title_depth>=depth:self.title_depth=0
        del self.stack[depth-1:]
    def handle_data(self,data):
        if self.skip:return
        if self.title_depth and sum(map(len,self.title))<1024:self.title.append(data[:1024])
        if self.item is not None:
            if self.heading and self.anchor and sum(map(len,self.item['title']))<1000:self.item['title'].append(data[:1000])
            elif self.paragraph and sum(map(len,self.item['snippet']))<4000:self.item['snippet'].append(data[:4000])

def project_html(text,query,count):
    parser=BingResults();parser.feed(text);parser.close()
    if parser.challenge or TITLE_CHALLENGE.search(''.join(parser.title).strip()):raise SearchFailure(503,'web_search_challenge_required')
    if not parser.entries and not parser.no_results:raise SearchFailure(502,'web_search_response_unrecognized')
    items=[];seen=set();filtered=False
    for entry in parser.entries:
        url=result_url(entry['href']);title=' '.join(''.join(entry['title']).split())[:300]
        if not url or not title:filtered=True;continue
        if url in seen:continue
        seen.add(url);items.append({'title':title,'url':url,'snippet':' '.join(''.join(entry['snippet']).split())[:2000]})
        if len(items)>=count:break
    return {'query':query,'sources':items,'untrusted_external_content':True,'provider':'bing_public_web','best_effort':True,
        'search_status':'results' if items else ('no_usable_results' if parser.entries else 'no_results'),'filtered_results_present':filtered}

def endpoint(value,query):
    try:
        if not isinstance(value,str) or len(value)>4096 or any(ord(c)<33 or ord(c)==127 for c in value) or '\\' in value:raise ValueError()
        p=urlsplit(value)
        if p.scheme!='https' or p.hostname!='cn.bing.com' or p.port not in (None,443) or p.username or p.password or p.fragment or p.path!='/search':raise ValueError()
        pairs=parse_qsl(p.query,keep_blank_values=True,strict_parsing=True,max_num_fields=12);values=dict(pairs)
        if len(values)!=len(pairs) or values.get('q')!=query or set(values)-{'q','setlang','mkt','cc','ensearch','form'}:raise ValueError()
        if any(k!='q' and not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',v) for k,v in pairs):raise ValueError()
        return p
    except (ValueError,UnicodeError):raise SearchFailure(502,'web_search_redirect_denied') from None

def search_bing_sync(args):
    args=arguments(args);query=args['query'];url=BING_ORIGIN+'/search?'+urlencode({'q':query,'setlang':'zh'});started=time.monotonic();seen=set()
    try:
        for hop in range(3):
            p=endpoint(url,query)
            if url in seen:raise SearchFailure(502,'web_search_redirect_denied')
            seen.add(url);address=reader.resolve_public(p.hostname);connection=reader.PinnedHTTPSConnection(p.hostname,address,timeout=4)
            try:
                connection.request('GET',p.path+'?'+p.query,headers={'Accept':'text/html','Accept-Encoding':'identity','User-Agent':'MEGMEET-Office-Assistant/1.0 (+public-search)'})
                response=connection.getresponse()
                if response.status in {301,302,303,307,308}:
                    location=response.getheader('Location','')
                    if CHALLENGE.search(location):raise SearchFailure(503,'web_search_challenge_required')
                    if hop==2:raise SearchFailure(502,'web_search_redirect_denied')
                    url=urljoin(url,location);endpoint(url,query);continue
                if response.status==429:raise SearchFailure(429,'web_search_provider_rate_limit')
                if response.status in {401,403}:raise SearchFailure(503,'web_search_provider_denied')
                if response.status!=200:raise SearchFailure(502,'web_search_provider_unavailable')
                if response.getheader('Content-Type','').split(';',1)[0].lower() not in {'text/html','application/xhtml+xml'}:raise SearchFailure(502,'web_search_response_invalid')
                if response.getheader('Content-Encoding','identity').lower()!='identity':raise SearchFailure(502,'web_search_encoding_denied')
                data=bytearray()
                while True:
                    if time.monotonic()-started>18:raise SearchFailure(504,'web_search_provider_timeout')
                    part=response.read1(16384)
                    if not part:break
                    data.extend(part)
                    if len(data)>MAX_BYTES:raise SearchFailure(502,'web_search_response_too_large')
                return project_html(data.decode('utf-8',errors='replace'),query,args['count'])
            finally:connection.close()
    except SearchFailure:raise
    except (TimeoutError,):raise SearchFailure(504,'web_search_provider_timeout') from None
    except reader.WebReadFailure:raise SearchFailure(502,'web_search_address_denied') from None
    except (OSError,ValueError,http.client.HTTPException):raise SearchFailure(502,'web_search_provider_unavailable') from None
    raise SearchFailure(502,'web_search_provider_unavailable')

async def search_bing(args):
    args=arguments(args);process=None
    try:await asyncio.wait_for(_search_slots.acquire(),timeout=1)
    except TimeoutError:raise SearchFailure(429,'web_search_busy') from None
    try:
        async with asyncio.timeout(20):
            env={'PATH':os.defpath}
            if os.name=='nt' and os.environ.get('SYSTEMROOT'):env['SYSTEMROOT']=os.environ['SYSTEMROOT']
            process=await asyncio.create_subprocess_exec(sys.executable,'-I','-S','-B',os.path.abspath(__file__),stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL,env=env)
            output,_=await process.communicate(json.dumps(args,ensure_ascii=True).encode('ascii'))
            if process.returncode or len(output)>256*1024:raise SearchFailure(502,'web_search_response_invalid')
            value=json.loads(output)
            if not isinstance(value,dict):raise ValueError()
            if 'error' in value:raise SearchFailure(value['status'],value['error'])
            return value
    except SearchFailure:raise
    except TimeoutError:raise SearchFailure(504,'web_search_provider_timeout') from None
    except (OSError,ValueError,TypeError):raise SearchFailure(502,'web_search_provider_unavailable') from None
    finally:
        if process is not None and process.returncode is None:
            try:process.kill()
            except ProcessLookupError:pass
            await process.wait()
        _search_slots.release()

if __name__=='__main__':
    try:result=search_bing_sync(json.loads(sys.stdin.buffer.read(8193)))
    except SearchFailure as exc:result={'error':exc.code,'status':exc.status}
    except Exception:result={'error':'web_search_response_invalid','status':502}
    sys.stdout.write(json.dumps(result,ensure_ascii=True))
