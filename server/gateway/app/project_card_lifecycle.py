"""Employee-card reference lifetime helpers. No arbitrary path or model policy.

The PostgreSQL ledger is authoritative for publication. Native card-v1 storage
independently enforces the same immutable deadline and owns physical deletion.
This module never removes ordinary native attachments or other project files.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
import secrets
from uuid import UUID
from .iam_oauth import IAMFailure
from . import storage_lifecycle as managed_storage

RETENTION_SECONDS = 172800
MAX_CARD_BYTES = 128 * 1024 * 1024
ROLES = frozenset({'input', 'photo', 'preview', 'print', 'template', 'intermediate'})
ARTIFACT = re.compile(r'^pjf_[A-Za-z0-9_-]{32}$')
LOGICAL_JOB = re.compile(r'^[A-Za-z0-9_-]{1,128}$')
RETENTION_NOTICE = '工卡文件及中间文件保留48小时，查看和下载不会延长保留时间。'
EXPIRED_NOTICE = '工卡文件已到期（保留48小时）。请重新导入名单制作工卡。'
PARTIAL_EXPIRED_NOTICE = '部分工卡文件已到期（保留48小时），已移除过期下载。请重新导入名单制作工卡。'


class CardLifecycleError(IAMFailure):
    def __init__(self, code='project_card_lifecycle_invalid', status=409):
        super().__init__(status, code)


def canonical(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(',', ':'), allow_nan=False)
    except (TypeError, ValueError):
        raise CardLifecycleError() from None


def reference_key(item, creation_job_id, role):
    """New job => new opaque reference; a retry never renews the old one."""
    if role not in ROLES or not isinstance(creation_job_id, str):
        raise CardLifecycleError()
    return hashlib.sha256(canonical({'domain': 'employee-card-ref-v1',
        'creation_job_id': creation_job_id, 'role': role, 'item': item}).encode()).hexdigest()


def validate_logical_job(value):
    if value is not None and (not isinstance(value, str) or not LOGICAL_JOB.fullmatch(value)):
        raise CardLifecycleError()
    return value


def artifact_ids(value, *, maximum=2048):
    """Bounded internal reference traversal; never returns business values."""
    found, remaining = set(), [value]
    visited = 0
    while remaining:
        node = remaining.pop()
        visited += 1
        if visited > 50000:
            raise CardLifecycleError()
        if isinstance(node, dict):
            candidate = node.get('attachment_id')
            if isinstance(candidate, str) and ARTIFACT.fullmatch(candidate):
                found.add(candidate)
                if len(found) > maximum:
                    raise CardLifecycleError()
            remaining.extend(node.values())
        elif isinstance(node, list):
            remaining.extend(node)
    return found


def project_payload(value, expired, *, add_notice=True):
    """Use the existing result-card contract; do not invent an expired status.

    Only employee-card payloads are projected. Their persisted encrypted result
    remains unchanged. Other connector results retain their exact structure.
    """
    if not isinstance(value, dict):
        return value
    if isinstance(value.get('project_result'), dict):
        projected = project_payload(value['project_result'], expired, add_notice=add_notice)
        if projected is value['project_result']:
            return value
        result = dict(value)
        result['project_result'] = projected
        result['status'] = projected.get('status', result.get('status'))
        return result
    if value.get('connector') != 'employee-cards':
        return value
    result = copy.deepcopy(value)
    original = result.get('artifacts', [])
    if not isinstance(original, list):
        raise CardLifecycleError()
    removed = any(isinstance(item, dict) and item.get('attachment_id') in expired for item in original)
    result['artifacts'] = [item for item in original
        if not (isinstance(item, dict) and item.get('attachment_id') in expired)]
    if removed:
        message = ('部分文件已到期，请重新上传所需资料。' if result['artifacts'] else '文件已到期，请重新上传资料后制作。') if managed_storage.configured() else (PARTIAL_EXPIRED_NOTICE if result['artifacts'] else EXPIRED_NOTICE)
        result.update(status='needs_input', summary=message, actions=[])
        result['sections'] = [{'type': 'notice', 'level': 'warning', 'text': message}]
    elif add_notice:
        notice = '企业文件临时保留：任务使用结束后清理输入和中间文件；成果最长保留1小时，下载传输后可在10分钟内补下。' if managed_storage.configured() else RETENTION_NOTICE
        sections = result.get('sections', [])
        if not isinstance(sections, list):
            raise CardLifecycleError()
        if not any(isinstance(section, dict) and section.get('type') == 'notice'
                   and section.get('text') == notice for section in sections):
            sections.append({'type': 'notice', 'level': 'info', 'text': notice})
        result['sections'] = sections
    return result


def require_live(row, now):
    if row is None:
        raise CardLifecycleError()
    if int(row['expires_at']) <= now:
        raise CardLifecycleError('project_card_file_expired', 410)
    if row['status'] not in {'pending', 'active'}:
        raise CardLifecycleError('project_card_cleanup_pending', 409)


def validate_saved(value, descriptor):
    expected = {'kind': 'file', 'ref': {'attachmentId': 'sha256:' + descriptor['sha256'],
        'name': descriptor['name'], 'bytes': descriptor['bytes']},
        'storage_kind': 'card-v1', 'storage_id': descriptor['storage_id'],
        'expires_at': descriptor['expires_at']}
    if value != expected:
        raise CardLifecycleError()
    return value


def database_now(cur):
    cur.execute('SELECT floor(extract(epoch FROM clock_timestamp()))::bigint AS now')
    return int(cur.fetchone()['now'])


def quota_usage(cur, access, *, excluding=None):
    # An intent is counted until either its registry row takes over accounting
    # or a native deletion acknowledgement commits its terminal tombstone.
    # This also preserves obligations after project_scope ON DELETE CASCADE.
    cur.execute('''SELECT COALESCE(sum(file_bytes),0) AS total,
        COALESCE(sum(file_bytes) FILTER (WHERE session_id=%s),0) AS session,
        count(*) AS total_count,count(*) FILTER (WHERE session_id=%s) AS session_count
      FROM (
        SELECT session_id,file_bytes FROM platform.project_artifacts WHERE owner_key=%s
        UNION ALL
        SELECT c.session_id,c.file_bytes FROM platform.card_file_refs c
          WHERE c.owner_key=%s AND c.status NOT IN ('deleted','retained_shared')
            AND (%s::text IS NULL OR c.artifact_id<>%s)
            AND NOT EXISTS (SELECT 1 FROM platform.project_artifacts a
              WHERE a.owner_key=c.owner_key AND a.session_id=c.session_id AND a.artifact_id=c.artifact_id)
      ) charged''', (access.session_id, access.session_id, access.owner, access.owner, excluding, excluding))
    return cur.fetchone()


def check_quota(cur, access, amount, *, excluding=None, new_count=1):
    row = quota_usage(cur, access, excluding=excluding)
    if row['total_count'] + new_count > 10000 or row['session_count'] + new_count > 1024:
        raise CardLifecycleError('project_artifact_quota', 413)
    if row['total'] + amount > 2 * 1024**3:
        raise CardLifecycleError('project_file_owner_quota', 413)
    if row['session'] + amount > 512 * 1024**2:
        raise CardLifecycleError('project_file_session_quota', 413)


def reserve(cur, runtime, access, item, media_type, role, logical_job_id=None):
    """Call only inside Database.tx(write=True), which holds the owner fence."""
    if (access.permit.get('connector') != 'employee-cards' or not access.job_id
            or access.fence is None or access.permit.get('source') == 'read'):
        raise CardLifecycleError('project_card_lifecycle_invalid', 403)
    try:
        if str(UUID(access.job_id)) != access.job_id:
            raise ValueError()
    except (ValueError, TypeError):
        raise CardLifecycleError() from None
    logical_job_id = validate_logical_job(logical_job_id)
    reference_hash = reference_key(item, access.job_id, role)
    cur.execute('''SELECT * FROM platform.card_file_refs WHERE owner_key=%s AND session_id=%s
        AND creation_job_id=%s AND role=%s AND reference_hash=%s FOR UPDATE''',
        (*access.identity, access.job_id, role, reference_hash))
    row = cur.fetchone()
    now = database_now(cur)
    if row:
        require_live(row, now)
        if str(row['scope_epoch']) != access.epoch or row['logical_job_id'] != logical_job_id:
            raise CardLifecycleError()
        descriptor = runtime.db.unseal(access, 'card-file:' + row['artifact_id'], row['encrypted_descriptor'])
        if descriptor['media_type'] != media_type:
            raise CardLifecycleError()
        return row, descriptor
    ref = item['ref']
    if item.get('kind') != 'file' or not 1 <= ref['bytes'] <= MAX_CARD_BYTES:
        raise CardLifecycleError('project_artifact_invalid', 413)
    check_quota(cur, access, ref['bytes'])
    artifact_id = 'pjf_' + secrets.token_urlsafe(24)
    descriptor = {'storage_kind': 'card-v1', 'storage_id': 'cfs_' + secrets.token_hex(16),
        'owner_key': access.owner, 'artifact_id': artifact_id,
        'creation_job_id': access.job_id, 'sha256': ref['attachmentId'][7:],
        'bytes': ref['bytes'], 'name': ref['name'], 'media_type': media_type,
        'created_at': now, 'expires_at': now + RETENTION_SECONDS}
    sealed = runtime.db.seal(access, 'card-file:' + artifact_id, descriptor)
    cur.execute('''INSERT INTO platform.card_file_refs
      (owner_key,session_id,artifact_id,creation_job_id,logical_job_id,role,provenance,
       storage_kind,storage_id,reference_hash,sha256,file_bytes,scope_epoch,encrypted_descriptor,created_at)
      VALUES (%s,%s,%s,%s,%s,%s,'new-card-write','card-v1',%s,%s,%s,%s,%s,%s,%s) RETURNING *''',
      (*access.identity, artifact_id, access.job_id, logical_job_id, role, descriptor['storage_id'],
       reference_hash, descriptor['sha256'], ref['bytes'], access.epoch, sealed, now))
    return cur.fetchone(), descriptor


def activate(cur, runtime, access, descriptor, item, media_type, *, card_print):
    artifact_id = descriptor['artifact_id']
    cur.execute('SELECT * FROM platform.card_file_refs WHERE owner_key=%s AND session_id=%s AND artifact_id=%s FOR UPDATE',
                (*access.identity, artifact_id))
    row = cur.fetchone()
    require_live(row, database_now(cur))
    if (str(row['scope_epoch']) != access.epoch or str(row['creation_job_id']) != access.job_id
            or row['storage_id'] != descriptor['storage_id']):
        raise CardLifecycleError()
    saved = runtime.db.unseal(access, 'card-file:' + artifact_id, row['encrypted_descriptor'])
    if saved != descriptor:
        raise CardLifecycleError()
    validate_saved(item, descriptor)
    cur.execute('SELECT artifact_id FROM platform.project_artifacts WHERE owner_key=%s AND session_id=%s AND artifact_id=%s',
                (*access.identity, artifact_id))
    existing = cur.fetchone()
    if existing:
        if row['status'] != 'active':
            raise CardLifecycleError()
        return artifact_id
    if row['status'] != 'pending':
        raise CardLifecycleError()
    # Recheck at the final commit. Exclude precisely our already charged intent.
    check_quota(cur, access, row['file_bytes'], excluding=artifact_id)
    payload = {**item, 'media_type': media_type,
               **({'output_kind': 'card-print-v1'} if card_print else {})}
    sealed = runtime.db.seal(access, 'artifact:' + artifact_id, payload)
    cur.execute('''INSERT INTO platform.project_artifacts
      (owner_key,session_id,artifact_id,source,reference_hash,encrypted_payload,file_bytes,output_kind,created_at)
      VALUES (%s,%s,%s,'project_output',%s,%s,%s,%s,to_timestamp(%s))''',
      (*access.identity, artifact_id, row['reference_hash'], sealed, row['file_bytes'],
       'card-print-v1' if card_print else 'ordinary', row['created_at']))
    cur.execute("UPDATE platform.card_file_refs SET status='active' WHERE owner_key=%s AND session_id=%s AND artifact_id=%s",
                (*access.identity, artifact_id))
    return artifact_id


def expired_ids(cur, access, ids):
    if not ids:
        return set()
    if len(ids) > 2048:
        raise CardLifecycleError()
    if managed_storage.configured():
        cur.execute('''SELECT a.artifact_id FROM platform.project_artifacts a
          LEFT JOIN platform.storage_files f ON f.file_id=a.managed_file_id
          WHERE a.owner_key=%s AND a.session_id=%s AND a.artifact_id=ANY(%s)
          AND (a.managed_file_id IS NULL OR f.file_id IS NULL OR f.status<>'active' OR f.expires_at<=clock_timestamp())''',
            (*access.identity, list(ids)))
        return {row['artifact_id'] for row in cur.fetchall()}
    cur.execute('''SELECT artifact_id FROM platform.card_file_refs WHERE owner_key=%s AND session_id=%s
        AND artifact_id=ANY(%s) AND (expires_at<=floor(extract(epoch FROM clock_timestamp()))::bigint
          OR status IN ('cleanup_pending','deleted','retained_shared'))
        UNION
        SELECT a.artifact_id FROM platform.project_artifacts a WHERE a.owner_key=%s AND a.session_id=%s
          AND a.artifact_id=ANY(%s) AND a.source='project_output'
          AND a.created_at+interval '48 hours'<=clock_timestamp()
          AND NOT EXISTS(SELECT 1 FROM platform.card_file_refs c WHERE c.owner_key=a.owner_key
            AND c.session_id=a.session_id AND c.artifact_id=a.artifact_id)''',
        (*access.identity, list(ids), *access.identity, list(ids)))
    # This is only a card-result/dependency projection. A legacy opaque aid
    # shared with another skill keeps its registry, quota and FilePort access.
    # Ordinary original uploads (native_message) are not card-owned expiry.
    return {row['artifact_id'] for row in cur.fetchall()}


async def project_values(runtime, access, values):
    card_values = [value for value in values if isinstance(value, dict)
        and (value.get('connector') == 'employee-cards'
             or isinstance(value.get('project_result'), dict)
             and value['project_result'].get('connector') == 'employee-cards')]
    ids = set()
    for value in card_values:
        ids.update(artifact_ids(value))
    expired = await runtime.db.tx(access, lambda cur, _: expired_ids(cur, access, ids)) if ids else set()
    return [project_payload(value, expired) for value in values]
