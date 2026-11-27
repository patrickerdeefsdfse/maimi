"""Task admission in ordinary chat; old bookmarks lead to the same workspace."""
from fastapi import APIRouter
from fastapi.responses import RedirectResponse, Response

router = APIRouter()
HEADERS = {'Cache-Control': 'private, no-store', 'Pragma': 'no-cache',
           'X-Content-Type-Options': 'nosniff', 'Referrer-Policy': 'no-referrer',
           'X-Frame-Options': 'DENY'}
PUBLIC_PATHS = frozenset(('/maimi/confidential','/maimi/confidential/entry.js'))


@router.get('/maimi/confidential')
def page():
    return RedirectResponse('/maimi/web/',status_code=307,headers=HEADERS)


@router.get('/maimi/confidential/entry.js')
def entry_guard():
    return Response('/* Superseded by server-side task admission. */',media_type='text/javascript',headers=HEADERS)
