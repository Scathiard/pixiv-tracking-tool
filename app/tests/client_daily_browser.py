"""Daily UI with synthetic backend plus explicitly replayed source/identity responses."""
import argparse
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit


def main():
    parser = argparse.ArgumentParser(); parser.add_argument("--artifacts", required=True, type=Path)
    output = parser.parse_args().artifacts.resolve(); output.mkdir(parents=True, exist_ok=False)
    os.environ["DATA_ROOT"] = str(output / "configuration")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from client_fixture import make_library
    from src.core.models import WorkTag,Operation
    from src.auth import _find_system_browser
    from src.web import create_app
    from src.scheduler import stop_scheduler
    import uvicorn
    from playwright.sync_api import sync_playwright
    runtime, remote = make_library(output / "fixture", works=144)
    with runtime.database.write() as session:
        session.add_all([WorkTag(work_id=1, tag=f"标签 {index:05}") for index in range(10005)])
    with socket.socket() as probe: probe.bind(("127.0.0.1", 0)); port = probe.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    server = uvicorn.Server(uvicorn.Config(create_app(lambda: runtime, authenticate=False, schedule=False, reading_ui=True), host="127.0.0.1", port=port, log_level="error", loop="src.networking:http_loop_factory"))
    worker = threading.Thread(target=server.run, daemon=True); worker.start()
    result = {"checks":[], "errors":[], "geometry":[], "real_account_used":False,
              "source_and_identity":"browser replay", "settings_and_tags":"real synthetic backend"}
    source_total = 1000
    try:
        deadline = time.monotonic()+20
        while not server.started and time.monotonic()<deadline: time.sleep(.05)
        assert server.started
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, executable_path=_find_system_browser())
            context = browser.new_context(viewport={"width":1440,"height":960})
            page = context.new_page(); page.on("pageerror", lambda error: result["errors"].append(str(error)))
            page.set_default_timeout(12000)
            def source(route):
                parameters = parse_qs(urlsplit(route.request.url).query); offset=int(parameters.get("offset",[0])[0]); limit=int(parameters.get("limit",[50])[0])
                kind=parameters.get("kind",["works"])[0]
                items=[{"kind":"work","id":index+1,"pixiv_id":str(index+1),"title":f"来源 {index+1}","availability":{"state":"unavailable","confirmation":"confirmed","effective":True,"reason":None,"checked_at":"2026-10-03T00:00:00Z"},"local":{"state":"incomplete","missing":[{"label":"第 2 页原图","kind":"original","role":"image","page":1}]}} for index in range(offset,min(offset+limit,source_total))]
                route.fulfill(json={"items":items if kind=="works" else [],"total":source_total if kind=="works" else 0,"offset":offset,"limit":limit,"counts":{"works":{"deleted":0,"unavailable":source_total},"artists":{"deleted":0,"unavailable":0}}})
            page.route("**/api/library/source-status?*", source)
            def identity(route):
                query=parse_qs(urlsplit(route.request.url).query).get('query',[''])[0]
                if query=='同名画师':route.fulfill(json={'items':[{'user_id':'987','name':'同名画师甲'},{'user_id':'988','name':'同名画师乙'}],'identity':False})
                else:route.fulfill(json={"items":[{"user_id":"1","name":"身份预览","biography":"完整身份简介","avatar_url":None,"artist_id":1}],"identity":True})
            page.route("**/api/artists/preview?*",identity)
            page.route("**/api/artists/preview/988",lambda route:route.fulfill(json={'user_id':'988','name':'同名画师乙','biography':'第二个身份详情','avatar_url':None,'artist_id':None}))
            page.goto(origin); page.locator(".work-card").first.wait_for()
            page.goto(origin+"/artists"); page.locator(".artist-profile").first.wait_for()
            for theme in ['dark','light']:
                page.evaluate("theme=>{document.body.dataset.theme=theme;localStorage.setItem('pixiv-local-theme',theme)}",theme)
                for collapsed in [False,True]:
                    page.set_viewport_size({"width":1440,"height":960})
                    if (page.locator('.app-shell').evaluate("e=>e.classList.contains('sidebar-collapsed')")) != collapsed:
                        page.locator('.sidebar-toggle').click()
                    for width in [1440,2560,850,390,320]:
                        page.set_viewport_size({"width":width,"height":960}); page.wait_for_timeout(100)
                        page.get_by_role("button",name="网格",exact=True).click(); page.wait_for_timeout(80)
                        grid=page.locator(".artist-profile").first.evaluate("e=>{const strip=e.querySelector('.artist-recent-strip'), cards=[...e.querySelectorAll('.artist-work')].slice(0,3).map(x=>x.getBoundingClientRect());return {size:cards[0].width,three:cards.length===3&&cards[2].right<=strip.getBoundingClientRect().right+.6,visible:[...e.querySelectorAll('.artist-work')].filter(x=>x.getBoundingClientRect().right<=strip.getBoundingClientRect().right+.6).length}}")
                        assert grid["three"], (width,grid)
                        page.get_by_role("button",name="列表",exact=True).click(); page.wait_for_timeout(80)
                        row=page.locator(".artist-profile").first.evaluate("e=>{const s=e.querySelector('.artist-recent-strip');return {size:e.querySelector('.artist-work').getBoundingClientRect().width,visible:[...e.querySelectorAll('.artist-work')].filter(x=>x.getBoundingClientRect().right<=s.getBoundingClientRect().right+.6).length}}")
                        assert abs(grid["size"]-row["size"]) < .1,(grid,row)
                        if width>=850: assert row["visible"]>3,(width,row)
                        result["geometry"].append({"width":width,"theme":theme,"collapsed":collapsed,"grid":grid,"row":row})
                        page.screenshot(path=str(output/f"artists-{width}-{theme}-{collapsed}.png"))
            result["checks"].append("grid-three-complete-list-more-identical-size-five-widths")
            page.set_viewport_size({"width":1440,"height":960})
            page.get_by_role("button",name="添加画师",exact=True).click()
            page.locator(".identity-query input").fill("https://www.pixiv.net/users/1")
            page.get_by_role("button",name="查询身份",exact=True).click()
            page.get_by_text("完整身份简介",exact=True).wait_for()
            assert page.get_by_role("button",name="查看现有画师",exact=True).is_visible()
            page.locator(".identity-query input").fill("另一个人")
            assert page.locator(".identity-confirm").count()==0
            result["checks"].append("identity-preview-duplicate-and-input-invalidation")
            additions=[]
            def add_identity(route):
                if route.request.method=='POST':additions.append(route.request.post_data_json);route.fulfill(json={'operation_id':'synthetic-add'})
                else:route.continue_()
            page.route('**/api/artists',add_identity)
            page.locator('.identity-query input').fill('同名画师');page.get_by_role('button',name='查询身份',exact=True).click()
            page.locator('.identity-choice').filter(has_text='同名画师乙').click()
            page.get_by_text('第二个身份详情',exact=True).wait_for();assert not additions
            page.get_by_role('button',name='确认身份并添加',exact=True).click()
            page.wait_for_function("!document.querySelector('.artist-add-panel')")
            assert additions==[{'pixiv_id':'988'}]
            result['checks'].append('name-search-select-details-before-confirming-exact-identity-replayed-add')
            with page.expect_response(lambda response:urlsplit(response.url).path=='/api/tasks' and response.request.method=='POST') as submitted:
                page.locator('.artist-profile').first.get_by_role('button',name='检查这位画师新作',exact=True).click()
            operation_id=submitted.value.json()['operation_id']
            with runtime.database.read() as session:
                operation=session.get(Operation,operation_id)
                assert operation.parameters['artist_ids']==[1] and operation.kind=='check'
            page.wait_for_function("[...document.querySelectorAll('.artist-profile')].find(e=>e.querySelector('a.artist-pixiv-link')?.getAttribute('href')==='https://www.pixiv.net/users/1')?.querySelector('.artist-last-check').innerText.includes('上次检查完成')",timeout=15000)
            assert page.request.get(origin+'/api/artists/1/check-status').json()['operation']['id']==operation_id
            assert runtime.queue.details(operation_id)['result']['outcome']=='succeeded' and remote.write_count==0
            result['checks'].append('actual-single-artist-check-operation-completion-time-and-no-bookmark-write')
            page.goto(origin+"/settings"); page.get_by_role("heading",name="自动检查",exact=True).wait_for()
            settings_heights=[]
            for count in [0,50,1000]:
                source_total=count; page.reload(); page.get_by_role("heading",name="手机访问",exact=True).wait_for()
                page.wait_for_timeout(100); settings_heights.append(page.locator(".settings-page").evaluate("e=>e.scrollHeight"))
                assert page.locator(".source-status-item").count()==0
            assert max(settings_heights)-min(settings_heights)<2,settings_heights
            page.get_by_role("button",name="查看清单",exact=True).click(); page.locator(".source-status-item").first.wait_for()
            assert page.locator(".source-status-item").count()==50
            page.get_by_role("button",name="下一页",exact=True).click(); page.get_by_text("来源 51",exact=True).wait_for()
            page.get_by_role("button",name="返回设置",exact=False).click()
            assert page.locator(".source-status-item").count()==0
            page.get_by_label("检查周期",exact=True).fill("2"); page.get_by_label("检查周期单位",exact=True).select_option("hours")
            page.get_by_role("button",name="保存周期",exact=True).click()
            page.wait_for_function("document.querySelector('.settings-page').innerText.includes('每 120 分钟')")
            assert page.request.get(origin+"/api/settings").json()["check_interval_minutes"]==120
            assert page.locator("select[aria-label='图库浏览方式'] option[value='500']").count()==1
            result["checks"].append("settings-fixed-height-source-page-pagination-back-interval-hours-500-option")
            page.goto(origin); page.locator(".work-card").first.wait_for(); page.get_by_role("button",name="筛选",exact=True).click()
            dialog=page.get_by_role("dialog",name="筛选作品"); dialog.locator(".filter-section-title").filter(has_text="标签").click()
            dialog.locator(".filter-candidate").first.wait_for()
            assert dialog.locator(".candidate-viewport").evaluate("e=>getComputedStyle(e).overflowY") == "visible"
            assert dialog.locator(".filter-candidate").count()<100
            dialog.locator(".filter-scroll").hover(); page.mouse.wheel(0,1000000)
            dialog.locator(".filter-scroll").evaluate("e=>e.scrollTop=e.scrollHeight-500")
            dialog.locator(".filter-candidate[data-value='标签 10004']").wait_for()
            dialog.get_by_label("搜索筛选标签").fill("标签 10004")
            dialog.locator(".filter-candidate[data-value='标签 10004']").click()
            assert dialog.locator(".filter-candidate[data-value='标签 10004']").get_attribute("aria-pressed")=="true"
            section=dialog.locator(".filter-section-body:has(input[aria-label='搜索筛选标签'])")
            section.get_by_role("button",name="反选",exact=True).click(); assert dialog.locator(".filter-candidate").get_attribute("aria-pressed")=="false"
            section.get_by_role("button",name="全选",exact=True).click(); assert dialog.locator(".filter-candidate").get_attribute("aria-pressed")=="true"
            section.get_by_role("button",name="清空选择",exact=True).click(); assert dialog.locator(".filter-candidate").get_attribute("aria-pressed")=="false"
            page.screenshot(path=str(output/"filters-tail.png")); result["checks"].append("ten-thousand-continuous-wrapped-tags-bounded-dom-tail-selection-actions")
            for width in [390,320]:
                dialog.get_by_role('button',name='关闭筛选',exact=True).click();page.set_viewport_size({'width':width,'height':740})
                page.get_by_role('button',name='筛选',exact=True).click();dialog.locator('.filter-section-title').filter(has_text='标签').click()
                dialog.locator('.filter-candidate').first.wait_for();scroll=dialog.locator('.filter-scroll')
                assert dialog.locator('.candidate-viewport').evaluate('e=>getComputedStyle(e).overflowY')=='visible'
                before=scroll.evaluate('e=>e.scrollTop');box=scroll.bounding_box()
                cdp=context.new_cdp_session(page);cdp.send('Emulation.setTouchEmulationEnabled',{'enabled':True,'maxTouchPoints':1})
                x=box['x']+box['width']/2;y=box['y']+box['height']-25
                cdp.send('Input.dispatchTouchEvent',{'type':'touchStart','touchPoints':[{'x':x,'y':y}]})
                for step in range(1,9):
                    cdp.send('Input.dispatchTouchEvent',{'type':'touchMove','touchPoints':[{'x':x,'y':y-step*25}]});page.wait_for_timeout(25)
                cdp.send('Input.dispatchTouchEvent',{'type':'touchEnd','touchPoints':[]});page.wait_for_timeout(180)
                assert scroll.evaluate('e=>e.scrollTop')>before+30
                scroll.evaluate('e=>e.scrollTop=e.scrollHeight-500');dialog.locator(".filter-candidate[data-value='标签 10004']").wait_for()
                assert dialog.locator('.filter-candidate').count()<100
                assert dialog.evaluate('e=>e.scrollWidth<=e.clientWidth+1')
                page.screenshot(path=str(output/f'filters-touch-tail-{width}.png'));cdp.detach()
            result['checks'].append('390-and-320-real-cdp-touch-scroll-shared-drawer-and-full-tail-reachable')
            assert not result["errors"], result["errors"]
            browser.close()
    finally:
        (output/"results.json").write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding="utf-8")
        server.should_exit=True; worker.join(5); stop_scheduler(); runtime.close()
    print(json.dumps(result,ensure_ascii=False))

if __name__=="__main__": main()
