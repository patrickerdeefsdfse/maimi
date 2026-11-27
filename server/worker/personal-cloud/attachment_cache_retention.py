"""Bounded offline cleanup of authenticated, stopped owners' derived cache.

Only fixed cache-* directories with exact markers and two reviewed file paths
are accepted. No glob deletion, symlink following or user-original traversal.
The manager holds both pool fences until this helper is independently stopped.
"""
from datetime import datetime,timezone
from cache_empty_shell import clean_empty_shell
import hashlib
import json
import os
import re
import stat
import time
import uuid
from itertools import islice

PROTOCOL='cache-retention-v1'
MARKER='maimi-cache-entry.json'
SCHEMA='maimi-r73-attachment-cache/v1'
MAX_ENTRIES=128
MAX_BYTES=128*1024*1024
FIELDS={'schema','owner','generation','cache_id','session_id','created_at','expires_at','source_expires_at','file_id','bytes','sha256','pins','uncertain','relative_files'}


def request(body):
    if not isinstance(body,dict) or set(body)!={'protocol','operation_id','owner','maximum_chat_generation'}:raise ValueError('CACHE_REQUEST')
    if body['protocol']!=PROTOCOL or not re.fullmatch('[a-f0-9]{32}',str(body['owner'])):raise ValueError('CACHE_OWNER')
    if str(uuid.UUID(body['operation_id']))!=body['operation_id']:raise ValueError('CACHE_OPERATION')
    n=body['maximum_chat_generation']
    if type(n) is not int or not 0<=n<=2147483647:raise ValueError('CACHE_GENERATION')
    return body


def result(value,body):
    fields={'operation_id','status','deleted_entries','retained_entries','reclaimed_bytes','scan_capped'}
    if not isinstance(value,dict) or set(value)!=fields or value['operation_id']!=body['operation_id']:raise ValueError('CACHE_ACK')
    if value['status'] not in ('COMPLETE','PARTIAL') or type(value['scan_capped']) is not bool:raise ValueError('CACHE_ACK')
    for key in ('deleted_entries','retained_entries','reclaimed_bytes'):
        if type(value[key]) is not int or value[key]<0:raise ValueError('CACHE_ACK')
    if value['deleted_entries']>MAX_ENTRIES or value['retained_entries']>MAX_ENTRIES or value['reclaimed_bytes']>MAX_ENTRIES*MAX_BYTES*2:raise ValueError('CACHE_ACK')
    if (value['status']=='COMPLETE') != (not value['retained_entries'] and not value['scan_capped']):raise ValueError('CACHE_ACK')
    return value


def timestamp(value):
    if not isinstance(value,str) or len(value)>40:raise ValueError('CACHE_TIME')
    parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
    if parsed.tzinfo is None or parsed.utcoffset().total_seconds()!=0:raise ValueError('CACHE_TIME')
    return parsed.timestamp()


def marker(value,name,body,now):
    if not isinstance(value,dict) or set(value)!=FIELDS:raise ValueError('CACHE_MARKER')
    if value['schema']!=SCHEMA or value['owner']!=body['owner'] or value['cache_id']!=name:raise ValueError('CACHE_IDENTITY')
    if not re.fullmatch(r'cache-[A-Za-z0-9]{6}',name):raise ValueError('CACHE_NAME')
    if type(value['generation']) is not int or not 1<=value['generation']<=body['maximum_chat_generation']:raise ValueError('CACHE_GENERATION')
    if not re.fullmatch(r'(?:session-)?[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}',str(value['session_id'])):raise ValueError('CACHE_SESSION')
    if str(uuid.UUID(value['file_id']))!=value['file_id']:raise ValueError('CACHE_FILE')
    if type(value['bytes']) is not int or not 0<=value['bytes']<=MAX_BYTES:raise ValueError('CACHE_BYTES')
    sha=value['sha256']
    if not re.fullmatch('[a-f0-9]{64}',str(sha)):raise ValueError('CACHE_SHA')
    if type(value['pins']) is not int or value['pins']<0 or type(value['uncertain']) is not bool:raise ValueError('CACHE_PINS')
    created,expires,source=map(timestamp,(value['created_at'],value['expires_at'],value['source_expires_at']))
    if expires>min(created+300,source)+.001 or expires<created:raise ValueError('CACHE_TTL')
    if expires>now:raise ValueError('CACHE_NOT_EXPIRED')
    rows=value['relative_files']
    if not isinstance(rows,list) or len(rows)!=2:raise ValueError('CACHE_FILES')
    prefix='attachments/v1/'
    paths=[prefix+'file-objects/'+sha[:2]+'/'+sha]
    if not isinstance(rows[1],dict):raise ValueError('CACHE_FILES')
    alias=rows[1].get('path','');start=prefix+'files/'+sha[:2]+'/'+sha+'/'
    tail=alias[len(start):] if alias.startswith(start) else ''
    if not tail or tail in ('.','..') or len(tail)>255 or any(ord(c)<32 or c in '/\\' for c in tail):raise ValueError('CACHE_ALIAS')
    paths.append(alias)
    for row,path in zip(rows,paths):
        if row!={'path':path,'sha256':sha,'bytes':value['bytes']}:raise ValueError('CACHE_FILES')
    return paths


def identity(info):
    return (info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns,info.st_mode,info.st_nlink)


class CacheStore:
    def __init__(self,root='/workspace'):
        self.root=root

    def execute(self,body):
        body=request(body);deadline=time.monotonic()+60
        summary={'operation_id':body['operation_id'],'status':'COMPLETE','deleted_entries':0,'retained_entries':0,'reclaimed_bytes':0,'scan_capped':False}
        descriptors=[]
        try:
            # Fixed absolute root: every ancestor is opened without following a link.
            if not os.path.isabs(self.root):raise ValueError('CACHE_ROOT')
            root=os.open('/',os.O_RDONLY|os.O_DIRECTORY);descriptors.append(root)
            for part in self.root.split('/')[1:]:
                if not part or part in ('.','..'):raise ValueError('CACHE_ROOT')
                root=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=root);descriptors.append(root)
            try:root=os.open('.maimi-cache',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=root)
            except FileNotFoundError:return summary
            descriptors.append(root)
            with os.scandir(root) as entries:names=[entry.name for entry in islice(entries,MAX_ENTRIES+1)]
            for name in names[:MAX_ENTRIES]:
                if time.monotonic()>deadline:summary['scan_capped']=True;break
                result=self.remove(root,name,body,deadline)
                summary['deleted_entries' if result is not None else 'retained_entries']+=1
                if result is not None:summary['reclaimed_bytes']+=result
            if len(names)>MAX_ENTRIES:summary['scan_capped']=True
        except (OSError,ValueError,TypeError):summary['retained_entries']+=1
        finally:
            for fd in reversed(descriptors):os.close(fd)
        if summary['retained_entries'] or summary['scan_capped']:summary['status']='PARTIAL'
        return summary

    def remove(self,parent,name,body,deadline):
        descriptors=[];files=[];directories={}
        try:
            if not re.fullmatch(r'cache-[A-Za-z0-9]{6}',name):return None
            root=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent);descriptors.append(root)
            root_info=os.fstat(root)
            try:marker_fd=os.open(MARKER,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=root)
            except FileNotFoundError:return clean_empty_shell(parent,name,root,deadline)
            descriptors.append(marker_fd)
            info=os.fstat(marker_fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_size>8192:return None
            raw=os.read(marker_fd,8193)
            if len(raw)!=info.st_size:return None
            paths=marker(json.loads(raw),name,body,time.time())
            expected={'':{MARKER}}
            for path in paths:
                parts=path.split('/')
                for index,part in enumerate(parts):expected.setdefault('/'.join(parts[:index]),set()).add(part)
            directories['']=root
            for relative in sorted(expected,key=lambda p:len(p.split('/'))):
                if relative:
                    parts=relative.split('/');up='/'.join(parts[:-1])
                    fd=os.open(parts[-1],os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=directories[up]);descriptors.append(fd);directories[relative]=fd
                if set(os.listdir(directories[relative]))!=expected[relative]:return None
            total=0
            for path in paths:
                parts=path.split('/');folder=directories['/'.join(parts[:-1])]
                fd=os.open(parts[-1],os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=folder);descriptors.append(fd)
                info_file=os.fstat(fd)
                value=json.loads(raw)
                if not stat.S_ISREG(info_file.st_mode) or info_file.st_size!=value['bytes'] or info_file.st_nlink not in (1,2):return None
                digest=hashlib.sha256();count=0
                while True:
                    if time.monotonic()>deadline:return None
                    chunk=os.read(fd,1024*1024)
                    if not chunk:break
                    count+=len(chunk)
                    if count>value['bytes']:return None
                    digest.update(chunk)
                if count!=value['bytes'] or digest.hexdigest()!=value['sha256']:return None
                files.append((folder,parts[-1],fd,info_file));total+=count
            same=(files[0][3].st_dev,files[0][3].st_ino)==(files[1][3].st_dev,files[1][3].st_ino)
            if any(f[3].st_nlink!=(2 if same else 1) for f in files):return None
            if identity(os.fstat(marker_fd))!=identity(info) or identity(os.stat(MARKER,dir_fd=root,follow_symlinks=False))!=identity(info):return None
            if (root_info.st_dev,root_info.st_ino)!=(os.stat(name,dir_fd=parent,follow_symlinks=False).st_dev,os.stat(name,dir_fd=parent,follow_symlinks=False).st_ino):return None
            for folder,leaf,fd,original in files:
                if identity(original)!=identity(os.fstat(fd)) or identity(original)!=identity(os.stat(leaf,dir_fd=folder,follow_symlinks=False)):return None
            for _,_,fd,_ in files:
                os.close(fd);descriptors.remove(fd)
            os.close(marker_fd);descriptors.remove(marker_fd)
            for folder,leaf,fd,original in files:
                if not os.path.samestat(original,os.stat(leaf,dir_fd=folder,follow_symlinks=False)):return None
                os.unlink(leaf,dir_fd=folder);os.fsync(folder)
            for relative in sorted((p for p in directories if p),key=lambda p:len(p.split('/')),reverse=True):
                parts=relative.split('/');os.rmdir(parts[-1],dir_fd=directories['/'.join(parts[:-1])])
            os.unlink(MARKER,dir_fd=root);os.fsync(root);os.rmdir(name,dir_fd=parent);os.fsync(parent)
            return total//2 if same else total
        except (OSError,ValueError,TypeError,KeyError,OverflowError):return None
        finally:
            for fd in reversed(descriptors):os.close(fd)
