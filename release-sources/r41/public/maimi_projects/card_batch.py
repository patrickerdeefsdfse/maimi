"""Bounded, page-aligned print volumes using reviewed artwork, never business paths."""
from __future__ import annotations
import asyncio
import hashlib
import io
import json
from pathlib import Path
import tempfile
import zipfile
from PIL import Image
from .card_renderer import CANVAS_SIZE, PRINT_LAYOUT, CardValidationError, create_document, render_card
from .meeting_flow import cpu
from .service import CARD_MEDIA, ProjectFailure

MAX_FILE = 32 * 1024 * 1024
VOLUME_BUDGET = 28 * 1024 * 1024
PER_PAGE = 10
PRINT_FORMAT_VERSION = 2


def prepare_artwork(data, output):
    with Image.open(io.BytesIO(data)) as source:
        if source.size != CANVAS_SIZE:
            raise CardValidationError('工卡预览尺寸不正确，请重新生成预览。')
        source.load()
        with source.transpose(Image.Transpose.ROTATE_270) as rotated:
            rotated.save(output, format='PNG', dpi=(500,500))


def plan_volumes(paths, budget=VOLUME_BUDGET):
    """Group whole pages by actual embedded bytes; each image is transformed once."""
    volumes, current, size = [], [], 262144
    for start in range(0, len(paths), PER_PAGE):
        page = paths[start:start+PER_PAGE]
        page_bytes = sum(p.stat().st_size + 8192 for p in page)
        if page_bytes + 262144 > budget:
            raise ProjectFailure(413, 'project_card_page_too_large')
        if current and size + page_bytes > budget:
            volumes.append(current); current=[]; size=262144
        current.extend(page); size += page_bytes
    if current: volumes.append(current)
    return volumes


async def generate_print(service, ctx, job, selected, layout):
    identity = {'format_version': PRINT_FORMAT_VERSION, 'layout': {**PRINT_LAYOUT, **layout},
                'employees': [(e['id'], e.get('preview'), e.get('photo'), e.get('name'), e.get('english_name')) for e in selected]}
    signature = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    if job.get('print_signature') == signature and job.get('documents'):
        # A second click after a completed write reuses the exact same references.
        await service._card_progress('cards_publish', len(job['documents']), len(job['documents']))
        return
    with tempfile.TemporaryDirectory(prefix='maimi-card-print-') as directory:
        base=Path(directory);paths=[]
        await service._card_progress('cards_prepare', 0, len(selected))
        for index, employee in enumerate(selected):
            await service._authorize(ctx,'employee-cards')
            if employee.get('preview') and employee.get('status') == 'ready':
                artifact = await service._attachment(ctx,employee['preview']['attachment_id'],{'.png'},MAX_FILE)
                data=artifact['data']
            else:
                photo=await service._attachment(ctx,employee['photo']['attachment_id'],{'.png'},10*1024*1024)
                source=base/f'source-{index}.png'
                await cpu(render_card,employee,photo['data'],source,service.assets_dir)
                data=source.read_bytes()
            output=base/f'print-{index}.png'
            try: await cpu(prepare_artwork,data,output)
            except (CardValidationError, OSError, ValueError, Image.DecompressionBombError):
                raise ProjectFailure(400,'project_card_preview_invalid') from None
            paths.append(output)
            await service._card_progress('cards_prepare',index+1,len(selected))
        volumes=plan_volumes(paths)
        built=[];completed_pages=0;total_pages=(len(paths)+9)//10
        await service._card_progress('cards_document',0,total_pages)
        for index, volume in enumerate(volumes):
            count_pages=(len(volume)+9)//10
            filename='员工工卡.docx' if len(volumes)==1 else f'员工工卡-第{completed_pages+1:03d}-{completed_pages+count_pages:03d}页.docx'
            output=base/filename
            await cpu(lambda: create_document(volume,output,prepared=True,**layout))
            if output.stat().st_size>MAX_FILE:
                raise ProjectFailure(413,'project_card_page_too_large')
            built.append(output);completed_pages+=count_pages
            await service._card_progress('cards_document',completed_pages,total_pages)
        pending = job.get('print_pending', {})
        saved = pending.get('parts', {}) if pending.get('signature') == signature else {}
        # References are stored only in this owner's encrypted card job. A failed
        # later volume is retried without republishing already committed files.
        async def publish(path, media):
            content = path.read_bytes()
            key = hashlib.sha256(path.name.encode() + b'\x00' + content).hexdigest()
            artifact = saved.get(key)
            if not artifact:
                artifact = await service.files.write(ctx, path.name, content, media)
                saved[key] = artifact
                job['print_pending'] = {'signature': signature, 'parts': dict(saved)}
                await service.state.put(ctx, 'cards:' + job['id'], job)
            return artifact
        documents=[]
        await service._card_progress('cards_publish',0,len(built))
        for index,path in enumerate(built):
            await service._authorize(ctx,'employee-cards')
            documents.append(await publish(path,CARD_MEDIA))
            await service._card_progress('cards_publish',index+1,len(built))
        # Optional all-in-one ZIP is offered only when it fits the unchanged
        # authenticated attachment boundary. Larger batches expose every volume.
        archive=None
        if len(built)>1 and sum(p.stat().st_size for p in built)+65536<MAX_FILE:
            zipped=base/'员工工卡-全部打印文件.zip'
            def pack():
                with zipfile.ZipFile(zipped,'w',compression=zipfile.ZIP_STORED) as z:
                    for p in built:
                        entry=zipfile.ZipInfo(p.name,(2020,1,1,0,0,0))
                        z.writestr(entry,p.read_bytes())
            await cpu(pack)
            archive=await publish(zipped,'application/zip')
        job.update(documents=documents,document=documents[0],print_signature=signature,
                   layout={'pages':total_pages,'columns':2,'rows':5,'count':len(paths)})
        job.pop('print_pending',None)
        if archive:job['archive']=archive
        else:job.pop('archive',None)
