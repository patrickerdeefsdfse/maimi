"""Bounded Windows interaction protocol. No shell, elevation or clipboard reads."""
import base64
import ctypes as c
from ctypes import wintypes as w
import io
import json
import sys
import time

def perform(request):
    if sys.platform != 'win32': raise ValueError('WINDOWS_REQUIRED')
    user=c.WinDLL('user32',use_last_error=True)
    try:user.SetProcessDPIAware()
    except Exception:pass
    user.GetForegroundWindow.restype=w.HWND
    user.IsWindow.argtypes=[w.HWND]
    user.IsWindowVisible.argtypes=[w.HWND]
    user.GetWindowRect.argtypes=[w.HWND,c.POINTER(w.RECT)]
    user.SetForegroundWindow.argtypes=[w.HWND]
    user.GetWindowTextLengthW.argtypes=[w.HWND]
    user.GetWindowTextW.argtypes=[w.HWND,w.LPWSTR,c.c_int]
    user.GetWindowThreadProcessId.argtypes=[w.HWND,c.POINTER(w.DWORD)]
    def describe(hwnd):
        rect=w.RECT()
        if not user.IsWindow(hwnd) or not user.IsWindowVisible(hwnd) or not user.GetWindowRect(hwnd,c.byref(rect)):raise ValueError('WINDOW_NOT_AVAILABLE')
        if rect.right<=rect.left or rect.bottom<=rect.top or rect.right-rect.left>16000 or rect.bottom-rect.top>16000:raise ValueError('WINDOW_BOUNDS_INVALID')
        text=c.create_unicode_buffer(min(user.GetWindowTextLengthW(hwnd)+1,512));user.GetWindowTextW(hwnd,text,len(text))
        pid=w.DWORD();user.GetWindowThreadProcessId(hwnd,c.byref(pid))
        return {'window_id':int(hwnd),'title':text.value,'pid':pid.value,'left':rect.left,'top':rect.top,'width':rect.right-rect.left,'height':rect.bottom-rect.top}
    action=request.get('action')
    if action=='windows':
        rows=[]
        callback=c.WINFUNCTYPE(w.BOOL,w.HWND,w.LPARAM)
        @callback
        def collect(hwnd,_):
            try:
                value=describe(hwnd)
                if value['title']:rows.append(value)
            except ValueError:pass
            return len(rows)<80
        user.EnumWindows(collect,0)
        return {'windows':rows}
    hwnd=request.get('window_id')
    if type(hwnd) is not int or hwnd<=0:raise ValueError('WINDOW_ID_REQUIRED')
    target=describe(hwnd)
    if action=='screenshot':
        from PIL import ImageGrab
        # Bring only the approved target forward; capture only its rectangle.
        if not user.SetForegroundWindow(hwnd) and int(user.GetForegroundWindow() or 0)!=hwnd:raise ValueError('WINDOW_FOCUS_DENIED')
        time.sleep(.15)
        target=describe(hwnd)
        image=ImageGrab.grab(bbox=(target['left'],target['top'],target['left']+target['width'],target['top']+target['height']),all_screens=True)
        image.thumbnail((1600,1200));out=io.BytesIO();image.save(out,format='PNG')
        if out.tell()>4_000_000:raise ValueError('SCREENSHOT_TOO_LARGE')
        return {**target,'image_width':image.width,'image_height':image.height,'image':{'mimeType':'image/png','data':base64.b64encode(out.getvalue()).decode('ascii')}}
    if not user.SetForegroundWindow(hwnd) and int(user.GetForegroundWindow() or 0)!=hwnd:raise ValueError('WINDOW_FOCUS_DENIED')
    time.sleep(.12)
    if int(user.GetForegroundWindow() or 0)!=hwnd:raise ValueError('WINDOW_FOCUS_CHANGED')
    pointer=c.c_size_t
    class MI(c.Structure):_fields_=[('dx',w.LONG),('dy',w.LONG),('mouseData',w.DWORD),('dwFlags',w.DWORD),('time',w.DWORD),('extra',pointer)]
    class KI(c.Structure):_fields_=[('vk',w.WORD),('scan',w.WORD),('flags',w.DWORD),('time',w.DWORD),('extra',pointer)]
    class U(c.Union):_fields_=[('mi',MI),('ki',KI)]
    class INPUT(c.Structure):_fields_=[('type',w.DWORD),('u',U)]
    user.SendInput.argtypes=[w.UINT,c.POINTER(INPUT),c.c_int]
    def send(events):
        array=(INPUT*len(events))(*events)
        if user.SendInput(len(events),array,c.sizeof(INPUT))!=len(events):raise ValueError('INPUT_BLOCKED')
    def key(vk,up=False,scan=0):return INPUT(1,U(ki=KI(vk,scan,(2 if up else 0)|(4 if scan else 0),0,0)))
    if action=='type':
        text=request.get('text')
        if not isinstance(text,str) or not text or len(text)>4000 or '\x00' in text:raise ValueError('TEXT_INVALID')
        encoded=text.encode('utf-16-le');events=[]
        for n in range(0,len(encoded),2):
            code=int.from_bytes(encoded[n:n+2],'little');events += [key(0,scan=code),key(0,True,code)]
        send(events)
    elif action=='key':
        value=request.get('key','').upper();keys={'ENTER':13,'TAB':9,'ESC':27,'BACKSPACE':8,'DELETE':46,'UP':38,'DOWN':40,'LEFT':37,'RIGHT':39,'HOME':36,'END':35}
        if value in keys:send([key(keys[value]),key(keys[value],True)])
        elif value in ['CTRL+A','CTRL+C','CTRL+V','CTRL+S']:
            vk=ord(value[-1]);send([key(17),key(vk),key(vk,True),key(17,True)])
        else:raise ValueError('KEY_NOT_ALLOWED')
    elif action=='click':
        x,y=request.get('x'),request.get('y');button=request.get('button','left')
        if type(x)is not int or type(y)is not int or not(0<=x<target['width'] and 0<=y<target['height']) or button not in ['left','right']:raise ValueError('CLICK_INVALID')
        user.SetCursorPos(target['left']+x,target['top']+y)
        flags=(2,4) if button=='left' else (8,16)
        send([INPUT(0,U(mi=MI(0,0,0,f,0,0))) for f in flags])
    elif action=='scroll':
        notches=request.get('notches')
        if type(notches)is not int or not -10<=notches<=10:raise ValueError('SCROLL_INVALID')
        user.SetCursorPos(target['left']+target['width']//2,target['top']+target['height']//2)
        send([INPUT(0,U(mi=MI(0,0,(notches*120)&0xffffffff,0x0800,0,0)))])
    else:raise ValueError('ACTION_INVALID')
    return {'window_id':hwnd,'action':action,'completed':True}

if __name__=='__main__':
    try:
        raw=sys.stdin.buffer.read(24001)
        if len(raw)>24000:raise ValueError('REQUEST_TOO_LARGE')
        value=perform(json.loads(raw))
        print(json.dumps({'ok':True,'result':value},ensure_ascii=True))
    except Exception as exc:
        print(json.dumps({'ok':False,'error':str(exc) if isinstance(exc,ValueError) else type(exc).__name__}))
