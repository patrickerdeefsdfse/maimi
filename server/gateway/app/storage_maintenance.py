"""Bounded maintenance entry for the gateway's existing service lifecycle."""
import asyncio
import logging
from . import storage_lifecycle as lifecycle

logger = logging.getLogger('maimi.storage')


async def cycle():
    if not lifecycle.configured():
        return {'disabled': True}
    files = await asyncio.to_thread(lifecycle.default_store().cleanup)
    content = await asyncio.to_thread(lifecycle.purge_enterprise_content)
    from .storage_cache_delivery import cycle as deliver_cache_expiry
    cache = await deliver_cache_expiry()
    from .source_reclamation import cycle as reclaim_sources
    sources = await reclaim_sources()
    return {'files': files, 'content': content, 'cache': cache, 'sources': sources}


async def run(stop):
    while not stop.is_set():
        try:
            await cycle()
        except Exception:
            # Never return/log raw paths, database values or file names.
            logger.warning('MANAGED_STORAGE_MAINTENANCE_INCOMPLETE')
        try:
            await asyncio.wait_for(stop.wait(), timeout=30)
        except asyncio.TimeoutError:
            pass
