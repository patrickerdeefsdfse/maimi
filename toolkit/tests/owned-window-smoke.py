"""Exercise the shipping driver only against this process's disposable window."""
import base64
from concurrent.futures import ThreadPoolExecutor
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import tkinter as tk

assert sys.platform=='win32'
repo=Path(__file__).resolve().parents[3]
driver=repo/'new-system/toolkit/windows.py'
python=repo/'output/upgrade-20260920/candidate-alpha2/office/py/python.exe'
evidence=repo/'output/toolkit-20260920/owned-window'
evidence.mkdir(parents=True,exist_ok=True)
root=tk.Tk();root.title('MEGMEET synthetic driver verification');root.geometry('640x320+100+100')
label=tk.Label(root,text='Synthetic fixture only — this window closes automatically.');label.pack(pady=20)
entry=tk.Entry(root,font=('Arial',16));entry.pack(padx=35,pady=15,fill='x')
root.update();root.lift();entry.focus_force();root.update()
user=ctypes.WinDLL('user32');user.GetAncestor.restype=wintypes.HWND;user.GetAncestor.argtypes=[wintypes.HWND,wintypes.UINT]
hwnd=int(user.GetAncestor(root.winfo_id(),2));rect=wintypes.RECT();user.GetWindowRect.argtypes=[wintypes.HWND,ctypes.POINTER(wintypes.RECT)]
assert user.GetWindowRect(hwnd,ctypes.byref(rect))
checks=[]
env={k:os.environ[k] for k in ('SystemRoot','WINDIR','TEMP') if k in os.environ}
env['PYTHONUTF8']='1'
def invoke(action,**args):
    request={'action':action,'window_id':hwnd,**args}
    with ThreadPoolExecutor(max_workers=1) as pool:
        future=pool.submit(subprocess.run,[str(python),'-I','-B',str(driver)],input=json.dumps(request),text=True,
                           capture_output=True,timeout=20,env=env,creationflags=subprocess.CREATE_NO_WINDOW)
        while not future.done():root.update();time.sleep(.01)
        response=future.result()
    assert response.returncode==0,response.stderr[:300]
    result=json.loads(response.stdout);assert result.get('ok'),result
    root.update();return result['result']
def passed(label):checks.append(label);print(label+': PASS',flush=True)
try:
    x=entry.winfo_rootx()+40-rect.left;y=entry.winfo_rooty()+entry.winfo_height()//2-rect.top
    invoke('click',x=x,y=y)
    invoke('type',text='MEGMEET 测试 123')
    assert entry.get()=='MEGMEET 测试 123',repr(entry.get());passed('OWNED_WINDOW_CLICK_AND_UNICODE_INPUT')
    shot=invoke('screenshot');assert shot['window_id']==hwnd and shot['pid']==os.getpid()
    data=base64.b64decode(shot['image']['data']);assert data.startswith(b'\x89PNG\r\n')
    (evidence/'fixture.png').write_bytes(data);passed('ONLY_OWNED_WINDOW_SCREENSHOT')
    invoke('key',key='CTRL+A');invoke('key',key='BACKSPACE')
    assert entry.get()=='';passed('OWNED_WINDOW_KEY_SEQUENCE')
    invoke('scroll',notches=1);passed('BOUNDED_OWNED_WINDOW_SCROLL')
    (evidence/'result.json').write_text(json.dumps({'passed':True,'checks':checks,'scope':'only disposable window created by this test'},indent=2),encoding='utf-8')
finally:
    root.destroy()
