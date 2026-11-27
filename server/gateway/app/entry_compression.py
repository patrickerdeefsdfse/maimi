"""Compress frontend program transfers; never RPCs, tokens, files, or SSE."""
from starlette.middleware.gzip import GZipMiddleware
class EntryCompression:
    def __init__(self,app):
        self.app=app
        self.compressed=GZipMiddleware(app,minimum_size=1024,compresslevel=4)
    async def __call__(self,scope,receive,send):
        path=scope.get('path','')
        headers=dict(scope.get('headers',[]))
        eligible=(scope['type']=='http' and scope.get('method')=='GET' and b'range' not in headers
            and (path.startswith('/plugins/') or
                 (path.startswith(('/assets/','/maimi/web/static/','/maimi/web/cosmos/')) and path.endswith(('.js','.css')))))
        await (self.compressed if eligible else self.app)(scope,receive,send)
