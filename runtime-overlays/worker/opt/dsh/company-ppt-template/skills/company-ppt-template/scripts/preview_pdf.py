"""Render an existing workspace PDF with the installed Office Python, sequentially.

Does not run LibreOffice, contact a service or read credentials. Parent owns one
child process and always reaps it. Successful output is a new PNG directory and
manifest; no promise is made about visual quality without viewing those images.
"""
import argparse, hashlib, json, math, os, subprocess, sys
from pathlib import Path

CAP = 128 * 1024 * 1024
TIMEOUT = 120

def require(value, code):
    if not value: raise ValueError(code)

def render(source, destination, pages, dpi):
    import pypdfium2 as pdfium
    source=Path(source).resolve(); destination=Path(destination).resolve()
    require(source.is_file() and source.suffix.lower()=='.pdf','PDF_INPUT_REQUIRED')
    require(not destination.exists() and destination.parent.is_dir(),'OUTPUT_DIRECTORY_MUST_BE_NEW')
    with source.open('rb') as stream: data=stream.read(CAP+1)
    require(0<len(data)<=CAP and data[:5]==b'%PDF-','PDF_SIZE_OR_HEADER_INVALID')
    doc=pdfium.PdfDocument(data)
    try:
        require(1<=len(doc)<=80,'PDF_PAGE_COUNT_LIMIT')
        selected=list(range(1,len(doc)+1)) if pages is None else pages
        require(selected and len(selected)<=80 and len(selected)==len(set(selected)) and all(1<=p<=len(doc) for p in selected),'PAGE_SELECTION_INVALID')
        destination.mkdir()
        output=[]; total=0
        for number in selected:
            page=doc[number-1]
            try:
                width,height=page.get_size()
                require(all(math.isfinite(n) and n>0 for n in (width,height)),'PAGE_SIZE_INVALID')
                scale=min(dpi/72,2400/max(width,height),math.sqrt(4_000_000/(width*height)))
                bitmap=page.render(scale=scale)
                try:
                    image=bitmap.to_pil()
                    try:
                        target=destination/('page-%03d.png'%number)
                        image.save(target)
                        size=target.stat().st_size;total+=size
                        require(size<=16*1024*1024 and total<=128*1024*1024,'PREVIEW_OUTPUT_LIMIT')
                        output.append({'page':number,'file':target.name,'width':image.width,'height':image.height,'bytes':size})
                    finally:image.close()
                finally:bitmap.close()
            finally:page.close()
        result={'schema':'maimi-pdf-preview-v1','status':'RENDERED','input_sha256':hashlib.sha256(data).hexdigest(),'source_page_count':len(doc),'pages':output,'visual_review':'REQUIRED','network_requests':0,'conversion_requests':0}
        (destination/'manifest.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        return result
    finally:doc.close()

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',required=True);parser.add_argument('--output-dir',required=True)
    parser.add_argument('--pages',nargs='+',type=int);parser.add_argument('--dpi',type=int,default=110)
    parser.add_argument('--child',action='store_true',help=argparse.SUPPRESS)
    args=parser.parse_args();require(36<=args.dpi<=144,'DPI_RANGE_INVALID')
    if args.child:
        print(json.dumps(render(args.input,args.output_dir,args.pages,args.dpi),ensure_ascii=False))
        return
    command=[sys.executable,'-I','-B',str(Path(__file__).resolve()),'--child','--input',args.input,'--output-dir',args.output_dir,'--dpi',str(args.dpi)]
    if args.pages is not None:command+=['--pages',*[str(n) for n in args.pages]]
    environment=dict(os.environ,OMP_NUM_THREADS='1',OPENBLAS_NUM_THREADS='1',MKL_NUM_THREADS='1')
    child=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.PIPE,env=environment)
    try:
        stdout,stderr=child.communicate(timeout=TIMEOUT)
        require(len(stdout)<=256*1024 and len(stderr)<=8192,'PREVIEW_OUTPUT_PROTOCOL_LIMIT')
        if child.returncode:
            try: result=json.loads(stdout)
            except Exception:result={'status':'FAILED','reason':'PDF_RENDER_FAILED'}
            print(json.dumps(result));return 1
        result=json.loads(stdout)
        require(result.get('status')=='RENDERED','PREVIEW_RESULT_INVALID')
        print(json.dumps(result,ensure_ascii=False))
    except subprocess.TimeoutExpired:
        child.kill();child.communicate(timeout=10)
        print(json.dumps({'status':'FAILED','reason':'PDF_RENDER_TIMEOUT','partial_previews_may_exist':True}));return 1
    finally:
        if child.poll() is None:child.kill()
        child.wait(timeout=10)

if __name__=='__main__':
    try:sys.exit(main() or 0)
    except Exception as exc:
        known=str(exc) if type(exc) is ValueError and str(exc).replace('_','').isalnum() else 'PDF_RENDER_FAILED'
        print(json.dumps({'status':'FAILED','reason':known}));sys.exit(1)
