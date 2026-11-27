"""Explicitly published, immutable text resources. Every read uses current ACL.

Retrieval is bounded PostgreSQL lexical search, not an unverified vector index.
No local directory scanning, automatic upload, shell runner or shared DSH process.
"""
from __future__ import annotations

import hashlib
import re
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Query, Request
from pydantic import Field

from . import enterprise_tasks as cloud

router = APIRouter(prefix='/v1/cloud', tags=['workspace-resources'], route_class=cloud.CloudRoute)


DocumentReference = cloud.DocumentReference


class PublishDocument(cloud.ClosedModel):
    name: str = Field(min_length=1, max_length=200)
    content: str = Field(max_length=262144)
    visibility: Literal['private', 'workspace'] = 'private'
    explicit_upload: Literal[True]


class UpdateDocument(PublishDocument):
    expected_version: int = Field(ge=1)


class RetrievalRequest(cloud.ClosedModel):
    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=5, ge=1, le=20)


def _validate_document(body):
    data = body.content.encode('utf-8')
    if len(data) > cloud.MAX_FILE_BYTES or '\x00' in body.content:
        raise cloud.CloudError(413, 'document_size_or_type_rejected')
    if body.name != body.name.strip() or any(c in body.name for c in '/\\\x00\r\n:') or body.name in {'.', '..'}:
        raise cloud.CloudError(400, 'document_name_invalid')
    return hashlib.sha256(data).hexdigest(), len(data)


def _view(row, user, *, include_content=False):
    result = {k: row[k] for k in ('name','version','sha256','size_bytes','visibility','updated_at')}
    result.update(document_id=str(row['id']), workspace_id=str(row['workspace_id']),
                  owned=row['owner_user_no'] == user, can_edit=bool(row.get('can_edit')),storage='server')
    if include_content:
        result['content'] = row['content']
    return result


def document(cur, actor, document_id, version=None, *, workspace_id=None):
    user, _ = cloud.actor_fields(actor)
    cur.execute('''SELECT d.*,v.version,v.name,v.content,v.sha256,v.size_bytes
        FROM platform.hybrid_documents d JOIN platform.hybrid_document_versions v
          ON v.document_id=d.id AND v.version=COALESCE(%s,d.current_version)
        WHERE d.id=%s AND d.enabled''', (version, document_id))
    row = cur.fetchone()
    if not row or (row['owner_user_no'] != user and row['visibility'] != 'workspace'):
        raise cloud.CloudError(404, 'document_not_found')
    if workspace_id is not None and str(row['workspace_id']) != workspace_id:
        raise cloud.CloudError(403, 'document_scope_denied')
    w = cloud.workspace(cur, actor, str(row['workspace_id']))
    if not w['can_retrieve']:
        raise cloud.CloudError(403, 'document_read_denied')
    return {**dict(row),'can_edit':w['can_publish']}


def authorize_refs(cur, actor, workspace_id, refs):
    if len(refs) > 16:
        raise cloud.CloudError(400, 'too_many_document_references')
    user, _ = cloud.actor_fields(actor)
    seen, docs = set(), []
    for raw in refs:
        ref = raw.model_dump(mode='json') if isinstance(raw, DocumentReference) else raw
        item = DocumentReference.model_validate(ref)
        pair = (str(item.document_id), item.version)
        if pair in seen:
            raise cloud.CloudError(400, 'duplicate_document_reference')
        seen.add(pair)
        docs.append(_view(document(cur, actor, pair[0], pair[1], workspace_id=workspace_id), user, include_content=True))
    return docs


def list_documents(actor, workspace_id):
    user, _ = cloud.actor_fields(actor)
    with cloud._connect() as conn, conn.cursor() as cur:
        w = cloud.workspace(cur, actor, workspace_id)
        if not w['can_retrieve']:
            raise cloud.CloudError(403, 'document_read_denied')
        cur.execute('''SELECT d.*,v.version,v.name,v.sha256,v.size_bytes
            FROM platform.hybrid_documents d JOIN platform.hybrid_document_versions v
            ON v.document_id=d.id AND v.version=d.current_version
            WHERE d.workspace_id=%s AND d.enabled AND (d.owner_user_no=%s OR d.visibility='workspace')
            ORDER BY d.updated_at DESC,d.id LIMIT 200''', (workspace_id,user))
        return {'items': [_view({**dict(r),'can_edit':w['can_publish']},user) for r in cur.fetchall()]}


def read_document(actor, document_id, version=None):
    user, _ = cloud.actor_fields(actor)
    with cloud._connect() as conn, conn.cursor() as cur:
        return _view(document(cur,actor,document_id,version),user,include_content=True)


def publish_document(actor, workspace_id, body, document_id=None):
    user, _ = cloud.actor_fields(actor)
    sha, size = _validate_document(body)
    with cloud._connect() as conn, conn.cursor() as cur:
        w = cloud.workspace(cur,actor,workspace_id)
        if not w['can_publish'] or not w['can_retrieve']:
            raise cloud.CloudError(403,'document_publish_denied')
        if body.visibility == 'workspace' and not w['can_share']:
            raise cloud.CloudError(403,'cloud_share_denied')
        cloud._storage_lock(cur,workspace_id)
        if document_id:
            cur.execute('SELECT * FROM platform.hybrid_documents WHERE id=%s FOR UPDATE',(document_id,))
            old=cur.fetchone()
            if not old or str(old['workspace_id']) != workspace_id or not old['enabled'] or (
                    old['owner_user_no'] != user and old['visibility']!='workspace'):
                raise cloud.CloudError(404,'document_not_found')
            if old['owner_user_no']!=user and body.visibility!=old['visibility']:
                raise cloud.CloudError(403,'document_visibility_owner_required')
            if old['current_version'] != body.expected_version:
                raise cloud.CloudError(409,'document_version_conflict')
            version=old['current_version']+1
            cur.execute('''UPDATE platform.hybrid_documents SET name=%s,current_version=%s,visibility=%s,updated_at=now()
                WHERE id=%s''',(body.name,version,body.visibility,document_id))
        else:
            document_id,version=str(uuid4()),1
            cur.execute('''INSERT INTO platform.hybrid_documents(id,workspace_id,owner_user_no,name,current_version,visibility)
                VALUES(%s,%s,%s,%s,%s,%s)''',(document_id,workspace_id,user,body.name,version,body.visibility))
        cloud._storage_capacity(cur,workspace_id,size)
        cur.execute('''INSERT INTO platform.hybrid_document_versions(document_id,version,name,content,sha256,size_bytes,created_by)
            VALUES(%s,%s,%s,%s,%s,%s,%s)''',(document_id,version,body.name,body.content,sha,size,user))
        cur.execute('''INSERT INTO platform.hybrid_audit(workspace_id,actor_user_no,action,resource_id)
            VALUES(%s,%s,%s,%s)''',(workspace_id,user,'document:publish:'+str(version),document_id))
        return _view(document(cur,actor,document_id,version),user)


def update_document(actor, document_id, body):
    # Resolve workspace through an authorized read, then recheck under write lock.
    current=read_document(actor,document_id)
    return publish_document(actor,current['workspace_id'],body,document_id)


def retrieve(actor, workspace_id, body):
    user, _ = cloud.actor_fields(actor)
    query=body.query.strip()
    if not query:
        raise cloud.CloudError(400,'retrieval_query_required')
    # Escaped literal ILIKE plus PostgreSQL text matching supports Chinese and
    # Latin text without sending unauthorized documents to an external index.
    term='%' + query.replace('\\','\\\\').replace('%','\\%').replace('_','\\_') + '%'
    with cloud._connect() as conn, conn.cursor() as cur:
        w=cloud.workspace(cur,actor,workspace_id)
        if not w['can_retrieve']:
            raise cloud.CloudError(403,'document_read_denied')
        cur.execute('''SELECT d.*,v.version,v.name,v.sha256,v.size_bytes,v.content
            FROM platform.hybrid_documents d JOIN platform.hybrid_document_versions v
              ON v.document_id=d.id AND v.version=d.current_version
            WHERE d.workspace_id=%s AND d.enabled AND (d.owner_user_no=%s OR d.visibility='workspace')
              AND (v.content ILIKE %s OR v.name ILIKE %s OR
                   to_tsvector('simple',v.name || ' ' || v.content) @@ plainto_tsquery('simple',%s))
            ORDER BY d.updated_at DESC,d.id LIMIT %s''',(workspace_id,user,term,term,query,body.limit))
        items=[]
        for row in cur.fetchall():
            item=_view({**dict(row),'can_edit':w['can_publish']},user)
            pos=row['content'].casefold().find(query.casefold())
            start=max(0,pos-120)
            item['excerpt']=row['content'][start:start+600]
            items.append(item)
        return {'items':items,'retrieval':'postgres-lexical-v1','storage':'server'}


@router.get('/capabilities')
async def capabilities(request: Request):
    await cloud._actor(request)
    return {'protocol':'megmeet-workspaces','version':2,
            'storage':{'personal':'local','cloud':'server','hybrid':'explicit-selected-sources'},
            'features':['department','group','versioned-text-documents','lexical-retrieval','hybrid-task',
                        'private-cloud-history','explicit-share','fixed-runner-manifests']}


@router.get('/workspaces/{workspace_id}/documents')
async def get_documents(workspace_id:UUID,request:Request):
    return await cloud._call(list_documents,await cloud._actor(request),str(workspace_id))


@router.post('/workspaces/{workspace_id}/documents')
async def post_document(workspace_id:UUID,body:PublishDocument,request:Request):
    return await cloud._call(publish_document,await cloud._actor(request),str(workspace_id),body)


@router.get('/documents/{document_id}')
async def get_document(document_id:UUID,request:Request,version:int|None=Query(None,ge=1)):
    return await cloud._call(read_document,await cloud._actor(request),str(document_id),version)


@router.put('/documents/{document_id}')
async def put_document(document_id:UUID,body:UpdateDocument,request:Request):
    return await cloud._call(update_document,await cloud._actor(request),str(document_id),body)


@router.post('/workspaces/{workspace_id}/retrieve')
async def post_retrieval(workspace_id:UUID,body:RetrievalRequest,request:Request):
    return await cloud._call(retrieve,await cloud._actor(request),str(workspace_id),body)
