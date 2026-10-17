"""Drawing text via the existing isolated Office service; no local OCR install."""
import asyncio
from contextlib import suppress
from pathlib import PurePosixPath

import anyio
import httpx

from . import office_remote as office
from maimi_projects.heatcost_flow import DrawingTextUnavailable

MAX_INPUT=20*1024*1024
MAX_PAGES=3
PAGE_TEXT_CHARS=6000
TIMEOUT=150
POLL=.5
_slots=asyncio.Semaphore(2)
_transport=None  # Synthetic transport in isolated local tests only.


def fail(code='project_drawing_text_unavailable'):
    raise DrawingTextUnavailable(code)


def text_page(number,text,method):
    if type(number) is not int or not 1<=number<=MAX_PAGES or not isinstance(text,str):fail()
    # Source text is evidence, never instructions; preserve line breaks only.
    clean=''.join(c for c in text if c in '\n\t' or ord(c)>=32).replace('\x7f','')
    return {'page':number,'text':clean[:PAGE_TEXT_CHARS], 'method':method,
            'truncated':len(clean)>PAGE_TEXT_CHARS}


def parse_result(job,suffix,size,total_pages):
    result=job.get('result')
    if (not isinstance(result,dict) or result.get('format')!=suffix[1:]
            or result.get('bytes')!=size or job.get('outputs')!=[]):fail()
    if suffix=='.pdf':
        pages=result.get('pages')
        if (type(result.get('page_count')) is not int or result['page_count']!=total_pages
                or not isinstance(pages,list) or len(pages)!=min(MAX_PAGES,total_pages)):fail()
        output=[]
        for number,page in enumerate(pages,1):
            if (not isinstance(page,dict) or page.get('page')!=number
                    or page.get('method') not in {'embedded-text','ocr'}):fail()
            output.append(text_page(number,page.get('text'),page['method']))
        return output
    if total_pages!=1:fail()
    return [text_page(1,result.get('text'),'ocr')]


async def read_drawing_text(actor,source,total_pages,authorize):
    """Only called by a context-bound ProjectRuntime callback with read bytes.

    No caller supplied URL, path, identity headers, key, operation or options.
    Authorization is checked before every request, after poll and on completion.
    """
    suffix=PurePosixPath(source.get('name','')).suffix.lower()
    data=source.get('data')
    if (suffix not in {'.pdf','.png','.jpg','.jpeg'} or not isinstance(data,bytes)
            or not 0<len(data)<=MAX_INPUT or type(total_pages) is not int or not 1<=total_pages<=1000):fail()
    await authorize()
    try:
        base,secret=office.service_configuration()
    except office.IAMFailure:
        fail()
    job_id=None;client=None
    try:
        async with asyncio.timeout(TIMEOUT),_slots:
            await authorize()
            client=httpx.AsyncClient(timeout=httpx.Timeout(12,connect=3,pool=3),trust_env=False,
                follow_redirects=False,transport=_transport,
                headers={**office.worker_headers(actor,secret),'Accept-Encoding':'identity'})
            async def request(method,url,**kwargs):
                await authorize()
                async with client.stream(method,url,**kwargs) as response:
                    if response.status_code==429:fail('project_drawing_text_busy')
                    if response.status_code not in {200,201,202}:fail()
                    raw=bytearray()
                    async for chunk in response.aiter_bytes():
                        raw.extend(chunk)
                        if len(raw)>office.MAX_JSON:fail()
                try:
                    value=office.strict_json(raw);office.validate_job(value,'status')
                except (ValueError,TypeError,KeyError):fail()
                if job_id is not None and value['job_id']!=job_id:fail()
                await authorize()
                return value
            name='drawing'+suffix;url=base+'/v1/office/jobs'
            job=await request('POST',url,json={'protocol':office.PROTOCOL,
                'request':{'operation':'read','input':name,'options':{'ocr':True,'start_page':1,'max_pages':MAX_PAGES,'limit':PAGE_TEXT_CHARS*MAX_PAGES}},
                'inputs':[{'path':name,'size_bytes':len(data)}]})
            job_id=job['job_id'];url+='/'+job_id
            await request('PUT',url+'/inputs/0',content=data,
                headers={'Content-Type':'application/octet-stream','Content-Length':str(len(data))})
            job=await request('POST',url+'/run',content=b'{}')
            while job['state'] in {'queued','running'}:
                await asyncio.sleep(POLL)
                job=await request('GET',url)
            if job['state']!='succeeded':fail()
            pages=parse_result(job,suffix,len(data),total_pages)
            await authorize()
            return pages
    except (TimeoutError,httpx.TimeoutException):
        fail('project_drawing_text_timeout')
    except httpx.HTTPError:
        fail()
    finally:
        # Bounded best-effort cancellation. Office also owns its timeout and TTL.
        with anyio.move_on_after(4,shield=True):
            if client is not None:
                if job_id is not None:
                    with suppress(Exception):
                        await asyncio.wait_for(client.delete(base+'/v1/office/jobs/'+job_id),3)
                await client.aclose()
