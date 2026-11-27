"""Classify contention without discarding native pages or employee drafts."""
import hashlib
import re
from .iam_oauth import IAMFailure

R90_SESSION_SHA256='6ca2eca0820f449377e3c68102fd10731ba82a43c577c5b6cfe0d5dd87114fe5'

def _once(text,old,new):
    if text.count(old)!=1:raise RuntimeError('R91_SESSION_ANCHOR_CHANGED')
    return text.replace(old,new,1)

def adapt_contention_session(original):
    if hashlib.sha256(original.encode('utf8')).hexdigest()!=R90_SESSION_SHA256:
        raise RuntimeError('R91_SESSION_SOURCE_CHANGED')
    text=_once(original,
        "const value=response.ok&&response.status!==204?await response.json():null;",
        "const value=(response.ok||response.status===409||response.status===503)&&response.status!==204?await response.json():null;")
    text=_once(text,"if(response.status===409&&!queueStopped){queueStopped=true;publish({status:'idle_released'});}",
        "if(response.status===409&&response.value?.detail==='personal_cloud_open_required'&&!queueStopped){queueStopped=true;publish({status:'idle_released'});}")
    text=_once(text,"if(probe.status===409){queueStopped=true;publish({status:'idle_released'});return false;}",
        "if(probe.status===409&&probe.value?.detail==='personal_cloud_open_required'){queueStopped=true;publish({status:'idle_released'});return false;}")
    text=_once(text,"if(response.status===409||response.status===429){queueStopped=true;publish({status:'queue_expired'});return false;}",
        "if((response.status===409&&['personal_cloud_queue_expired','personal_cloud_queue_cancelled'].includes(response.value?.detail))||response.status===429){queueStopped=true;publish({status:'queue_expired'});return false;}")
    text=_once(text,"if(response.status===409){void recovery.recover('host_missing');return;}",
        "if(response.status===409){let failure;try{failure=await response.json();}catch{}if(failure?.detail==='personal_cloud_open_required')void recovery.recover('host_missing');return;}")
    return text

def credential_manifest(html):
    # The pinned rc.3 native index uses this single authenticated same-origin
    # resource. Manifest requests otherwise omit credentials even same-origin.
    tags=re.findall(r'<link\b[^>]*\brel="manifest"[^>]*>',html,flags=re.I)
    accepted={f'<link rel="manifest" href="{href}"{end}'
              for href in ('/manifest.webmanifest','./manifest.webmanifest')
              for end in (' />','>')}
    if len(tags)!=1 or tags[0] not in accepted:
        raise IAMFailure(502,'native_manifest_link_changed')
    return html.replace(tags[0],'<link rel="manifest" href="/manifest.webmanifest" crossorigin="use-credentials">',1)
