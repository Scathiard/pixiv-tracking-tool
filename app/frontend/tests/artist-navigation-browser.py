"""Focused real-App replay: artist entry and source-check feedback, no account/backend."""
import argparse
import json
import os
from playwright.sync_api import expect, sync_playwright
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument('--url', default='http://127.0.0.1:5293/reader/tests/gallery-entry.html')
parser.add_argument('--artifacts', required=True, type=Path)
args = parser.parse_args()
args.artifacts.mkdir(parents=True, exist_ok=False)
os.environ['TEMP'] = os.environ['TMP'] = str(args.artifacts.resolve())
results = []
with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True, executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe')
    try:
        for width in (1440, 390):
            page = browser.new_page(viewport={'width': width, 'height': 900})
            page.set_default_timeout(5000)
            page.add_init_script("localStorage.setItem('pixiv-local-home-scope', 'q=old-search&types=manga&tags=old-tag&favorites=1')")
            page.goto(args.url, wait_until='domcontentloaded', timeout=30000)
            expect(page.locator('.work-card').first).to_be_visible()
            page.evaluate("""() => {
              const before=window.fetch,dispatch=window.dispatchEvent.bind(window);
              // Card completion must work independently of TaskPanel notifications.
              window.dispatchEvent=event=>event.type==='task-completed'?true:dispatch(event);
              window.artistNavigation={reads:[],pending:new Map(),cancelled:[],fail:false,sourceOutcome:'failed'};
              window.fetch=(target,options={})=>{
                const request=new URL(String(target),location.href),path=request.pathname;
                if(path==='/api/artists') return Promise.resolve(Response.json({items:Array.from({length:8},(_,i)=>i+1).map(id=>({id,pixiv_id:String(id),name:'合成画师 '+id,mode:'active',work_count:1,recent_works:[]})),total:8}));
                if(path==='/api/artists/check-status') return Promise.resolve(Response.json({items:{}}));
                if(path==='/api/artists/1/check-source') return Promise.resolve(Response.json({operation_id:'synthetic-source'}));
                if(path==='/api/tasks/synthetic-source') return Promise.resolve(Response.json({id:'synthetic-source',label:'合成核对',state:'finished',phase:{name:'checking',done:1,total:1,unit:'artists',detail:''},result:{outcome:artistNavigation.sourceOutcome},items:[{state:artistNavigation.sourceOutcome==='failed'?'failed':'succeeded',result:{message:'网络核对未完成，最近来源事实保留'}}]}));
                if(path==='/api/library/cursor'&&request.searchParams.has('artist_ids')){
                  const id=request.searchParams.get('artist_ids');artistNavigation.reads.push(request.search);
                  if(artistNavigation.fail)return Promise.resolve(Response.json({detail:{code:'read_timeout',message:'合成画师图库超时'}},{status:504}));
                  return new Promise(resolve=>{
                    const response={items:[],total:0,offset:0,ceiling:120,as_of:'2026-10-10T00:00:00Z',generation:1,has_more:false};
                    artistNavigation.pending.set(id,()=>{artistNavigation.pending.delete(id);resolve(Response.json(response))});
                    options.signal.addEventListener('abort',()=>artistNavigation.cancelled.push(id),{once:true});
                  });
                }
                return before(target,options);
              };
              galleryEntryFixture.router.push('/artists');
            }""")
            buttons = page.locator('.artist-controls > .text-link')
            profile = page.locator('.artist-profile').last
            buttons.last.click()
            page.wait_for_function("artistNavigation.pending.has('8')")
            expect(buttons.last).to_have_text('正在读取…')
            assert page.locator('.artists-heading').bounding_box()['y'] < 0
            assert buttons.last.bounding_box()['y'] > 0
            assert page.locator('.artist-controls > .text-link[aria-busy="true"]').count() == 1
            query = page.evaluate('artistNavigation.reads[0]')
            assert all(key not in query for key in ('q=', 'types=', 'tags=', 'favorites=')), query
            page.evaluate('artistNavigation.fail=true')
            buttons.last.click()
            expect(profile.get_by_role('alert')).to_be_visible()
            expect(profile.get_by_role('alert')).to_contain_text('合成画师图库超时')
            assert page.locator('.artists-heading').bounding_box()['y'] < 0
            page.evaluate('artistNavigation.fail=false')
            profile.get_by_role('alert').get_by_role('button', name='重试').click()
            page.wait_for_function('artistNavigation.reads.length===3')
            assert 'artist_ids=8' in page.evaluate('artistNavigation.reads[2]')
            buttons.nth(1).click()
            page.wait_for_function("artistNavigation.pending.has('2')")
            page.evaluate("artistNavigation.pending.get('2')()")
            page.wait_for_function("location.search.includes('artist_ids=2')")
            page.evaluate("artistNavigation.pending.get('8')()")
            page.wait_for_timeout(100)
            assert 'artist_ids=2' in page.url
            assert page.evaluate("artistNavigation.cancelled.includes('8')")
            results.append(f'{width}: scrolled card waiting/error/retry visible; all works clears old filters; latest artist wins')

            page.evaluate("galleryEntryFixture.router.push('/artists')")
            profile = page.locator('.artist-profile').first
            check = profile.get_by_role('button', name='核对主站主页', exact=True)
            check.click()
            expect(check).to_be_enabled()
            expect(profile.locator('.artist-source-check')).to_contain_text('网络核对未完成')
            page.evaluate("artistNavigation.sourceOutcome='success'")
            check.click()
            expect(check).to_be_enabled()
            expect(profile.locator('.artist-source-check')).to_have_text('主页核对已完成')
            results.append(f'{width}: source failure/success leaves card feedback and releases button without completion events')
            page.close()
    finally:
        (args.artifacts / 'results.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
        browser.close()
print(json.dumps(results, ensure_ascii=False))
