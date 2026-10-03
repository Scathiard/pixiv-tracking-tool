"""Production startup screen and real isolated conversion; no login or real library."""
import argparse
from contextlib import closing
import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import threading
import time


def stop_owned_tray(process):
    """pystray WM_STOP ends icon.run; run_tray then drains request_shutdown."""
    import psutil
    if process.poll() is not None:return
    owner=psutil.Process(process.pid);pids={owner.pid,*[child.pid for child in owner.children(recursive=True)]}
    user32=ctypes.windll.user32
    callback_type=ctypes.WINFUNCTYPE(wintypes.BOOL,wintypes.HWND,wintypes.LPARAM)
    user32.GetWindowThreadProcessId.argtypes=[wintypes.HWND,ctypes.POINTER(wintypes.DWORD)]
    user32.GetClassNameW.argtypes=[wintypes.HWND,wintypes.LPWSTR,ctypes.c_int]
    user32.PostMessageW.argtypes=[wintypes.HWND,wintypes.UINT,wintypes.WPARAM,wintypes.LPARAM]
    windows=[]
    def inspect(hwnd,_):
        pid=wintypes.DWORD();user32.GetWindowThreadProcessId(hwnd,ctypes.byref(pid))
        name=ctypes.create_unicode_buffer(256);user32.GetClassNameW(hwnd,name,256)
        if pid.value in pids and name.value.startswith('pixiv_tracker') and name.value.endswith('SystemTrayIcon'):windows.append(hwnd)
        return True
    callback=callback_type(inspect);user32.EnumWindows(callback,0)
    if not windows:raise RuntimeError('No owned tray window; refusing to address unrelated windows')
    for hwnd in windows:user32.PostMessageW(hwnd,0x400+10,0,0)
    return len(windows)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--artifacts',required=True,type=Path);parser.add_argument('--exe',type=Path)
    args=parser.parse_args();output=args.artifacts.resolve();output.mkdir(parents=True,exist_ok=False)
    os.environ['DATA_ROOT']=str(output/'configuration')
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
    from strict_legacy_fixture import strict_client_database
    from src import web
    from src.auth import _find_system_browser
    from src.core.database import SCHEMA_REVISION
    from playwright.sync_api import sync_playwright
    import uvicorn
    isolated_root=output/'synthetic root';data=isolated_root/'data';data.mkdir(parents=True);images=isolated_root/'images'
    source=strict_client_database(data/'pixiv_tracker.db')
    before=source.read_bytes()
    web._cfg.DATA_DIR=data;web._cfg.IMAGES_DIR=images
    with socket.socket() as probe:probe.bind(('127.0.0.1',0));port=probe.getsockname()[1]
    origin=f'http://127.0.0.1:{port}'
    server=None;worker=None;process=None;process_output=None
    if args.exe:
        exe=args.exe.resolve(strict=True)
        capture=output/'browser_capture.py'
        capture.write_text("import pathlib,sys\nwith pathlib.Path(__file__).with_name('opened_urls.txt').open('a',encoding='utf-8') as out:out.write(sys.argv[1]+'\\n')\n",encoding='utf-8')
        environment=dict(os.environ,DATA_ROOT=str(isolated_root),PORT=str(port),TEMP=str(output),TMP=str(output),PIXIV_REFRESH_TOKEN='',PYTHONIOENCODING='utf-8')
        environment['BROWSER']=f'"{Path(sys.executable).as_posix()}" "{capture.as_posix()}" "%s"'
        process_output=(output/'exe-output.txt').open('xb')
        process=subprocess.Popen([str(exe),'--offline'],cwd=output,env=environment,stdout=process_output,stderr=subprocess.STDOUT,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    else:
        server=uvicorn.Server(uvicorn.Config(web.create_app(authenticate=False,schedule=False,reading_ui=True),host='127.0.0.1',port=port,log_level='error',loop='src.networking:http_loop_factory'))
        worker=threading.Thread(target=server.run,daemon=True);worker.start()
    result={'checks':[],'geometry':[],'errors':[],'real_account_used':False,'conversion':'strict 0002 DDL from separate baseline main checkout; no work_revisions or revision columns','failure_screen':'browser response replay'}
    try:
        deadline=time.monotonic()+60
        if process:
            from urllib.request import urlopen
            while time.monotonic()<deadline:
                if process.poll() is not None:raise RuntimeError('Packaged startup exited before its server opened')
                try:
                    with urlopen(origin+'/api/startup',timeout=1) as response:
                        if response.status==200:break
                except OSError:time.sleep(.1)
            else:raise TimeoutError('Packaged upgrade server did not start')
            result['exe_sha256']=hashlib.sha256(exe.read_bytes()).hexdigest()
        else:
            while not server.started and time.monotonic()<deadline:time.sleep(.05)
            assert server.started
        with sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True,executable_path=_find_system_browser())
            page=browser.new_page(viewport={'width':1440,'height':960});page.set_default_timeout(15000)
            page.on('pageerror',lambda error:result['errors'].append(str(error)))
            page.goto(origin);page.get_by_role('heading',name='旧图库需要升级').wait_for()
            assert page.locator('.app-shell').count()==0
            for theme in ['dark','light']:
                page.evaluate('theme=>document.body.dataset.theme=theme',theme)
                for width in [1440,390,320]:
                    page.set_viewport_size({'width':width,'height':740})
                    assert page.locator('.upgrade-screen').evaluate('e=>e.scrollWidth<=e.clientWidth+1')
                    button=page.get_by_role('button',name='备份并升级图库',exact=True);button.scroll_into_view_if_needed();assert button.is_visible()
                    result['geometry'].append({'theme':theme,'width':width,'horizontal_overflow':False})
                    page.screenshot(path=str(output/f'upgrade-{theme}-{width}.png'))
            with closing(sqlite3.connect(source)) as connection:assert connection.execute('SELECT version_num FROM alembic_version').fetchone()[0]=='0002_client'
            assert source.read_bytes()==before
            assert page.request.get(origin+'/api/library').status==503
            result['checks'].append('required-confirmation-preserves-old-source-and-gates-library')
            page.get_by_role('button',name='备份并升级图库',exact=True).click()
            page.get_by_role('heading',name='我的图库',exact=True).wait_for(timeout=25000)
            assert page.locator('.upgrade-screen').count()==0
            assert page.request.get(origin+'/api/startup').json()['state']=='ready'
            with closing(sqlite3.connect(source)) as connection:assert connection.execute('SELECT version_num FROM alembic_version').fetchone()[0]==SCHEMA_REVISION
            assert page.request.get(origin+'/api/library?show_hidden=1').json()['total']==1
            backups=list((data/'upgrade').glob('*/backup.db'));assert len(backups)==1
            with closing(sqlite3.connect(backups[0])) as connection:assert connection.execute('SELECT version_num FROM alembic_version').fetchone()[0]=='0002_client'
            page.screenshot(path=str(output/'upgrade-ready-gallery.png'))
            result['checks'].append('actual-button-backup-validate-activate-runtime-start-gallery-and-old-backup')
            if process:
                records=list(output.glob('PixivTracker-*/rendezvous.json'));assert len(records)==1
                rendezvous=records[0].read_bytes()
                second=subprocess.Popen([str(exe),'--offline'],cwd=output,env=environment,stdout=process_output,stderr=subprocess.STDOUT,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                assert second.wait(timeout=30)==0
                assert process.poll() is None and records[0].read_bytes()==rendezvous
                assert len(list((data/'upgrade').glob('*/backup.db')))==1
                urls=(output/'opened_urls.txt').read_text(encoding='utf-8').splitlines()
                assert len(urls)>=2 and all(value==f'http://localhost:{port}' for value in urls)
                result['checks'].append('actual-exe-second-launch-reuses-single-owner-and-opens-same-url-no-second-upgrade')
            for activated in [False,True]:
                page.route('**/api/startup',lambda route,request,active=activated:route.fulfill(json={'state':'failed','stage':'启动未完成' if active else '升级未完成','message':'隔离故障反馈；备份与候选保留。','can_start':True,'activated':active,'backup':str(backups[0]),'candidate':str(backups[0].parent/'candidate.db')}))
                page.reload();page.get_by_role('heading',name='启动未完成' if activated else '升级未完成',exact=True).wait_for()
                assert page.get_by_role('button',name='重试启动图库' if activated else '重试备份并升级',exact=True).is_visible()
                assert page.get_by_text('旧库备份',exact=True).is_visible()
                page.screenshot(path=str(output/f'upgrade-failure-{activated}.png'));page.unroute('**/api/startup')
            result['checks'].append('failed-screen-retains-paths-and-distinguishes-upgrade-from-runtime-retry')
            assert not result['errors'],result['errors'];browser.close()
    finally:
        (output/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        if server:server.should_exit=True;worker.join(10)
        if process:
            result['owned_tray_windows_stop_sent']=stop_owned_tray(process)
            result['exe_exit_code']=process.wait(timeout=35);assert result['exe_exit_code']==0
            process_output.close()
            (output/'results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
