"""Offline shared-NAS helpers require both chat and execution owners excluded."""
from contextlib import asynccontextmanager

@asynccontextmanager
async def shared_files_exclusion(store,op):
    if not getattr(getattr(store,'capacity',None),'pool',None):
        yield
        return
    async with store.shared_files_exclusion(op):
        yield
