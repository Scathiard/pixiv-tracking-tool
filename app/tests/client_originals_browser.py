"""Missing-originals feedback and artist links in a synthetic local library."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import sys
import threading
import time
import traceback


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifacts", type=Path, required=True)
    args = parser.parse_args()
    artifacts = args.artifacts.resolve()
    artifacts.mkdir(parents=True, exist_ok=False)
    os.environ["DATA_ROOT"] = str(artifacts / "fixture")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from PIL import Image
    import uvicorn
    from playwright.sync_api import expect, sync_playwright
    from client_fixture import SyntheticRemote
    from sqlalchemy import select
    from src.auth import _find_system_browser
    from src.core.catalogue_revision import bump_catalogue_revision
    from src.core.models import ApplicationState, ArchiveIntent, Artist, Asset, Derivative, TrackingPolicy, Work
    from src.core.runtime import Runtime
    from src.web import create_app

    remote = SyntheticRemote()
    runtime = Runtime(artifacts / "fixture/data/pixiv_tracker.db", artifacts / "fixture/images", create=True, client=remote)
    runtime.account_id = runtime.library.account_id = "90001"
    runtime.images.mkdir(parents=True, exist_ok=True)
    with Image.new("RGB", (360, 360), "#459abd") as image:
        image.save(runtime.images / "local.webp")
        image.save(runtime.images / "local.gif")
    checked = datetime.now(timezone.utc).isoformat()
    with runtime.database.write() as session:
        session.add(Artist(id=7, pixiv_user_id="777001", name="隔离画师", biography="离线可读的简介", avatar_path="local.webp"))
        session.add(TrackingPolicy(artist_id=7, mode="paused"))
        for work_id in (1, 2, 3):
            metadata = {"original_backfill_requires_selection": True}
            if work_id == 1:
                metadata["remote_availability"] = {"state": "unavailable", "checked_at": checked,
                    "reason": "作品资料接口无法获取此作品；尚不能确认是否删除，已有存档保留"}
            work = Work(id=work_id, pixiv_illust_id=str(700000 + work_id), artist_id=7,
                title={1:"无法访问的旧动图",2:"仍可补齐的动图",3:"暂时网络失败的动图"}[work_id],
                type="ugoira", page_count=1, source_metadata=metadata)
            work.assets = [Asset(role="ugoira_zip", state="missing_deferred"), Asset(role="ugoira_timing", state="missing")]
            work.derivatives = [Derivative(kind="gif", relative_path="local.gif", state="verified"),
                Derivative(kind="thumbnail", relative_path="local.webp", state="verified")]
            session.add_all([work, ArchiveIntent(pixiv_illust_id=work.pixiv_illust_id)])
        session.add(ApplicationState(key="maintenance_work:3", value={"status":"waiting", "kind":"repair",
            "reason":"网络暂时中断，稍后自动重试", "next_retry_at":checked,
            "tracks":{"remote":{"status":"waiting","kind":"repair","reason":"网络暂时中断，稍后自动重试", "next_retry_at":checked}}}))

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(lambda:runtime, authenticate=False, schedule=False, reading_ui=True),
        host="127.0.0.1", port=port, log_level="error", loop="src.networking:http_loop_factory"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    checks, metrics, errors, submitted = [], {}, [], []
    origin = f"http://127.0.0.1:{port}"
    page = None
    try:
        deadline = time.monotonic() + 20
        while not server.started and time.monotonic() < deadline:
            time.sleep(.05)
        assert server.started
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, executable_path=_find_system_browser())
            for width, height in ((1440,900),(390,844),(320,700)):
                context = browser.new_context(viewport={"width":width,"height":height}, is_mobile=width<500, has_touch=width<500)
                page = context.new_page()
                page.on("pageerror", lambda error:errors.append(str(error)))
                fail_missing = [False]
                def missing(route):
                    if fail_missing[0]:
                        route.fulfill(status=503, content_type="application/json", body='{"detail":"隔离列表暂时不可读"}')
                    else:
                        route.continue_()
                def tasks(route):
                    submitted.append(route.request.post_data_json)
                    route.fulfill(status=200, content_type="application/json", body=json.dumps({"operation_id":"synthetic-submitted", "created":True}))
                page.route("**/api/library/missing-originals?*", missing)
                page.route("**/api/tasks", tasks)
                page.goto(origin + "/settings", wait_until="domcontentloaded")
                source = page.locator('.missing-work[data-work-id="1"]')
                expect(source).to_contain_text("原作已无法访问")
                expect(source).to_contain_text("上次核对")
                expect(source.get_by_role("button", name="重新核对", exact=True)).to_be_enabled()
                assert "已删除" not in source.inner_text()
                expect(page.locator('.missing-work[data-work-id="3"]')).not_to_contain_text("原作已无法访问")
                if width == 1440:
                    source.get_by_role("button", name="重新核对", exact=True).click()
                    page.wait_for_function("document.querySelector('.toast')?.textContent.includes('排队') || document.querySelector('.toast')?.textContent.includes('提交')")
                    assert submitted[-1]["kind"] == "backfill" and submitted[-1]["work_ids"] == [1]
                    page.get_by_role("button", name="补下载", exact=True).click()
                    assert submitted[-1]["kind"] == "repair" and not submitted[-1].get("work_ids")
                    checks.append("manual-repair-covers-current-scope-and-explicit-unavailable-recheck")
                    # Failed refresh must retain the evidence already shown.
                    fail_missing[0] = True
                    page.evaluate("window.dispatchEvent(new Event('archive-updated'))")
                    expect(page.locator('.missing-originals-error')).to_contain_text("暂时不可读")
                    expect(source).to_contain_text("原作已无法访问")
                    fail_missing[0] = False
                    page.get_by_role("button", name="重读缺失清单", exact=True).click()
                    expect(page.locator('.missing-originals-error')).to_have_count(0)
                    with runtime.database.write() as session:
                        for asset in session.scalars(select(Asset).where(Asset.work_id == 2)):
                            asset.state = "verified"
                        bump_catalogue_revision(session)
                    page.evaluate("window.dispatchEvent(new Event('archive-updated'))")
                    expect(page.locator('.missing-work[data-work-id="2"]')).to_have_count(0)
                    expect(source).to_contain_text("原作已无法访问")
                    checks.append("completion-refresh-removes-filled-item-keeps-unavailable-and-retries-read-failure")
                    if page.locator('.missing-work[data-work-id="3"]').count():
                        expect(page.locator('.missing-work[data-work-id="3"]')).to_contain_text("网络暂时中断")
                    checks.append("temporary-failure-is-described-without-deletion-claim")
                source.scroll_into_view_if_needed()
                page.screenshot(path=str(artifacts / f"missing-originals-{width}.png"))
                metrics[f"settings_{width}_width"] = page.evaluate("document.documentElement.scrollWidth")
                assert metrics[f"settings_{width}_width"] <= width + 1
                checks.append(f"missing-originals-feedback-readable-{width}")
                page.goto(origin + "/artist/7", wait_until="domcontentloaded")
                link = page.get_by_role("link", name="在 Pixiv 查看 隔离画师", exact=True)
                expect(link).to_be_visible()
                assert link.get_attribute("href") == "https://www.pixiv.net/users/777001"
                assert link.get_attribute("target") == "_blank"
                assert set(link.get_attribute("rel").split()) >= {"noopener","noreferrer"}
                count_box = page.locator('.artists-toolbar>.muted').bounding_box()
                assert count_box and count_box['height'] <= 30, "artist count must not be squeezed into a vertical label"
                metrics[f"artist_{width}_count_height"] = count_box['height']
                page.screenshot(path=str(artifacts / f"artist-link-{width}.png"))
                metrics[f"artist_{width}_width"] = page.evaluate("document.documentElement.scrollWidth")
                assert metrics[f"artist_{width}_width"] <= width + 1
                checks.append(f"artist-main-site-link-uses-remote-id-and-remains-readable-{width}")
                context.close()
            assert not errors, errors
            browser.close()
    except BaseException:
        failure = traceback.format_exc()
        if page and not page.is_closed():
            try:
                page.screenshot(path=str(artifacts / "failure.png"))
            except Exception:
                pass  # Playwright may already have closed after the failing assertion.
        (artifacts / "failure.txt").write_text(failure, encoding="utf-8")
        raise
    finally:
        (artifacts / "result.json").write_text(json.dumps({"checks":checks,"metrics":metrics,"errors":errors,"submitted":submitted}, ensure_ascii=False, indent=2), encoding="utf-8")
        server.should_exit = True
        thread.join(10)
        runtime.close()


if __name__ == "__main__":
    main()
