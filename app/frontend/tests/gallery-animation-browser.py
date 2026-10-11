"""Real card scroll/brief-exit continuity; synthetic localhost media, no backend."""
import argparse
import io
import json
import os
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

parser = argparse.ArgumentParser()
parser.add_argument('--artifacts', required=True, type=Path)
parser.add_argument('--url', default='http://127.0.0.1:5198/reader/tests/gallery-animation.html', help='Existing isolated Vite fixture URL')
parser.add_argument('--media-format', choices=('webp', 'gif'), default='webp')
args = parser.parse_args()
args.artifacts.mkdir(parents=True, exist_ok=False)
artifacts = args.artifacts.resolve()
os.environ['TEMP'] = os.environ['TMP'] = str(artifacts)
frames = [Image.new('RGB', (80, 80), (index * 4, 30, 80)) for index in range(60)]
gif = io.BytesIO()
frames[0].save(gif, format=args.media_format.upper(), lossless=True, save_all=True, append_images=frames[1:], duration=100, loop=0)
thumbnail = io.BytesIO()
Image.new('RGB', (80, 80), (0, 255, 0)).save(thumbnail, format='PNG')
checks = []

def frame(image, name):
    box = image.bounding_box()
    rendered = Image.open(io.BytesIO(image.page.screenshot(path=str(artifacts / name)))).convert('RGB')
    color = rendered.getpixel((round(box['x'] + box['width'] / 2), round(box['y'] + box['height'] / 2)))
    assert color[1:] == (30, 80), color
    return round(color[0] / 4)

with sync_playwright() as pw:
    browser = pw.chromium.launch(headless=True, executable_path='C:/Program Files/Google/Chrome/Application/chrome.exe')
    try:
        for width in (1440, 390, 320):
            page = browser.new_page(viewport={'width': width, 'height': 700})
            page.set_default_timeout(10000)
            page.route('**/synthetic-animation-clock.webp?*', lambda route: route.fulfill(body=gif.getvalue(), content_type='image/' + args.media_format))
            page.route('**/synthetic-animation-thumb.png', lambda route: route.fulfill(body=thumbnail.getvalue(), content_type='image/png'))
            page.goto(args.url)
            image = page.locator('.media-image').first.locator('img[data-media-source="animation"]')
            image.wait_for()
            page.wait_for_function("document.querySelector('img[data-media-source=animation]')?.complete")
            page.wait_for_timeout(1200)
            assert page.locator('.media-image').first.locator('.media-placeholder').count() == 0
            assert page.locator('.media-image').nth(1).locator('.media-placeholder').count() == 0
            before = frame(image, f'gallery-{width}-before.png')
            assert before > 5, before
            page.evaluate("window.startedImage=document.querySelector('img[data-media-source=animation]');document.querySelector('.gallery-viewport').scrollTop=20")
            page.wait_for_function("document.querySelector('.media-image').classList.contains('animation-masked')")
            assert page.evaluate("startedImage===document.querySelector('img[data-media-source=animation]')")
            assert page.locator('.media-image').first.locator('.media-placeholder').count() == 0
            page.wait_for_timeout(300)
            after = frame(image, f'gallery-{width}-resume.png')
            assert after >= before, (before, after)
            checks.append({'width': width, 'case': 'actual scroll resumes same animation node', 'before': before, 'after': after})

            # A bounded mounted card may briefly exit and return without restarting.
            page.evaluate("document.querySelector('.gallery-viewport').scrollTop=250")
            page.wait_for_timeout(300)
            assert page.evaluate("startedImage===document.querySelector('img[data-media-source=animation]') && getComputedStyle(startedImage).visibility==='hidden'")
            page.evaluate("document.querySelector('.gallery-viewport').scrollTop=0")
            page.wait_for_timeout(300)
            returned = frame(image, f'gallery-{width}-returned.png')
            assert returned >= after, (after, returned)
            checks.append({'width': width, 'case': 'mounted brief exit resumes same timeline', 'after_return': returned})

            # New cards do not acquire animation while scrolling remains suspended.
            page.evaluate("document.querySelector('.gallery-viewport').scrollTop=700;animationFixture.suspend()")
            page.wait_for_timeout(100)
            assert page.locator('.media-image').nth(1).locator('img[data-media-source=animation]').count() == 0
            page.evaluate('animationFixture.resume()')
            page.locator('.media-image').nth(1).locator('img[data-media-source=animation]').wait_for()
            checks.append({'width': width, 'case': 'newly visible animation waits for idle'})

            # Background and virtual teardown release decoder ownership.
            page.evaluate("Object.defineProperty(document,'visibilityState',{configurable:true,value:'hidden'});document.dispatchEvent(new Event('visibilitychange'))")
            page.wait_for_timeout(50)
            assert page.locator('img[data-media-source=animation]').count() == 0
            assert page.evaluate("!startedImage.hasAttribute('src')")
            page.evaluate("Object.defineProperty(document,'visibilityState',{configurable:true,value:'visible'});document.dispatchEvent(new Event('visibilitychange'));document.querySelector('.gallery-viewport').scrollTop=0")
            page.wait_for_timeout(300)
            image.wait_for()
            page.evaluate("window.teardownImage=document.querySelector('img[data-media-source=animation]');animationFixture.mounted.value=false")
            page.wait_for_timeout(50)
            assert page.evaluate("!teardownImage.isConnected && !teardownImage.hasAttribute('src')")
            checks.append({'width': width, 'case': 'background and unmount release media'})
            page.close()
        print(json.dumps(checks, ensure_ascii=False))
        (artifacts / 'result.json').write_text(json.dumps(checks, ensure_ascii=False, indent=2), encoding='utf-8')
    finally:
        browser.close()
