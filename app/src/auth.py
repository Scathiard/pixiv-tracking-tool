import json
import hashlib
import os
import secrets
import base64
import webbrowser
import threading
import requests
from pathlib import Path
from datetime import datetime
from requests.structures import CaseInsensitiveDict

import gallery_dl.config as gdl_config

from . import config as _cfg


def _token_file():
    return _cfg.DATA_DIR / "pixiv_token.json"

# Web-based OAuth state
_pending_login_url = None
_pending_code_verifier = None
_received_code = None
_code_event = threading.Event()
_cancel_event = threading.Event()
_token_lock = threading.RLock()


def get_pending_login_url():
    return _pending_login_url


def submit_oauth_code(code):
    global _received_code
    _received_code = code
    _code_event.set()


def cancel_auth():
    """唤醒正在等待的 OAuth 流程，供应用优雅退出。"""
    _cancel_event.set()
    _code_event.set()


def validate_oauth_input(user_input):
    """校验用户粘贴的内容，返回 (code, error_msg)。"""
    from urllib.parse import urlparse, parse_qs

    user_input = user_input.strip()
    if not user_input:
        return None, "内容不能为空"
    if len(user_input) > 8192:
        return None, "授权内容过长，请重新复制 callback URL"

    # 纯 code 字符串
    if "=" not in user_input and not user_input.startswith(("http", "pixiv")):
        return user_input, None

    # URL：从 query 参数提取 code
    parsed = urlparse(user_input)
    params = parse_qs(parsed.query)
    code = params.get("code", [None])[0]
    if code:
        return code, None

    if "accounts.pixiv.net" in user_input or "code_challenge" in user_input:
        return None, "这是登录跳转过程中的临时 URL，没有 code。请按 Ctrl+H 打开浏览器历史记录，搜索 callback，找到 callback?state=...&code=... 那条，复制整条 URL"

    if "code=" in user_input:
        return user_input.rpartition("code=")[2].split("&")[0], None

    return None, "未找到 code 参数。请确认复制的是完整的 callback URL"

# Pixiv 官方移动客户端的公开兼容标识，并非用户账号密钥；用户 token 只写入 DATA_ROOT。
CLIENT_ID = "MOBrBDS8blbauoSck0ZfDbtuzpyT"
CLIENT_SECRET = "lsACyCD94FhDUtGTXi3QzcFE2uU1hqtDaKeqrdwj"
HASH_SECRET = "28c1fdd170a5204386cb1313c7077b34f83e4aaf4aa829ce78c231e05b0bae2c"

REDIRECT_URI = "https://app-api.pixiv.net/web/v1/users/auth/pixiv/callback"
TOKEN_URL = "https://oauth.secure.pixiv.net/auth/token"


def _save_token(access_token, refresh_token):
    """原子写入 token，避免断电或进程退出留下半截 JSON。"""
    token_path = _token_file()
    token_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = token_path.with_name(
        f"{token_path.name}.{secrets.token_hex(4)}.tmp"
    )
    payload = json.dumps(
        {"access_token": access_token, "refresh_token": refresh_token},
        ensure_ascii=False,
    )
    with _token_lock:
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, token_path)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _load_token():
    """损坏或暂时不可读的 token 按未登录处理，不阻断 OAuth 恢复流程。"""
    token_path = _token_file()
    with _token_lock:
        try:
            payload = json.loads(token_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError, TypeError):
            return {}
    return payload if isinstance(payload, dict) else {}


def _try_refresh_token():
    saved = _load_token()
    refresh_token = saved.get("refresh_token") or _cfg.PIXIV_REFRESH_TOKEN

    if not refresh_token:
        return None

    try:
        local_time = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S+00:00")
        headers = CaseInsensitiveDict({
            "x-client-time": local_time,
            "x-client-hash": hashlib.md5(
                (local_time + HASH_SECRET).encode("utf-8")
            ).hexdigest(),
            "app-os": "ios",
            "app-os-version": "14.6",
            "user-agent": "PixivIOSApp/7.13.3 (iOS 14.6; iPhone13,2)",
        })

        r = requests.post(TOKEN_URL, headers=headers, data={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "get_secure_url": "1",
        }, timeout=30)

        if r.status_code != 200:
            return None

        resp = r.json()
        next_refresh_token = resp.get("refresh_token") or refresh_token
        _save_token(resp["access_token"], next_refresh_token)
        return next_refresh_token

    except Exception:
        return None


def _oauth_pkce():
    global _pending_login_url, _pending_code_verifier, _received_code
    _code_event.clear()
    _cancel_event.clear()
    _received_code = None

    code_verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    code_challenge = base64.urlsafe_b64encode(
        hashlib.sha256(code_verifier.encode()).digest()
    ).rstrip(b"=").decode()

    login_url = (
        "https://app-api.pixiv.net/web/v1/login"
        f"?code_challenge={code_challenge}"
        "&code_challenge_method=S256"
        "&client=pixiv-android"
    )

    print("\n" + "=" * 55)
    print("  Pixiv 需要浏览器授权登录")
    print("=" * 55)

    # 尝试 Playwright 自动捕获（使用系统浏览器）
    print("\n  尝试自动打开浏览器...")
    code = _browser_oauth(login_url)

    # Playwright 不可用时：打开浏览器，等待网页端提交 code
    if not code:
        print("\n  请在打开的浏览器页面中完成登录，")
        print("  然后回到 Web 界面粘贴授权 code。\n")

        _pending_login_url = login_url
        _pending_code_verifier = code_verifier

        try:
            webbrowser.open(login_url)
        except Exception:
            pass

        # 等待网页端提交 code（最长等 3 分钟）
        if not _code_event.wait(180) or _cancel_event.is_set():
            _pending_login_url = None
            _pending_code_verifier = None
            print("\n[X] 等待授权超时。\n")
            return None

        code = _extract_code(_received_code)
        _pending_login_url = None
        _pending_code_verifier = None

    if not code:
        return None

    # 用 code 换取 token
    headers = {
        "User-Agent": "PixivAndroidApp/5.0.234 (Android 11; Pixel 5)",
    }
    data = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "code": code,
        "code_verifier": code_verifier,
        "grant_type": "authorization_code",
        "include_policy": "true",
        "redirect_uri": REDIRECT_URI,
        "get_secure_url": "1",
    }

    r = requests.post(TOKEN_URL, headers=headers, data=data, timeout=30)

    if r.status_code != 200:
        resp_data = r.json()
        if resp_data.get("error") in ("invalid_request", "invalid_grant"):
            print("\n[X] code 已过期，请重新启动程序再试一次。\n")
        else:
            print(f"\n[X] Token 交换失败: HTTP {r.status_code}")
            print(f"  {r.text}\n")
        return None

    resp = r.json()
    if "error" in resp:
        print(f"\n[X] {resp}\n")
        return None

    _save_token(resp["access_token"], resp["refresh_token"])
    print("\n[OK] 登录成功！\n")
    return resp["refresh_token"]


def _find_system_browser():
    """查找系统中可用的 Chromium 浏览器。"""
    import shutil
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ]
    for name in ["chrome", "chromium", "msedge"]:
        p = shutil.which(name)
        if p:
            candidates.append(p)
    for p in candidates:
        if Path(p).exists():
            return p
    return None


def _browser_oauth(login_url):
    """用 Playwright 打开浏览器，自动截获回调请求中的 code。"""
    from playwright.sync_api import sync_playwright

    code = None
    browser_closed = False

    browser_path = _find_system_browser()
    if not browser_path:
        print("\n  [!] 未找到系统浏览器，无法自动捕获。\n")
        return None

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False, executable_path=browser_path)
            page = browser.new_page()

            def on_request(request):
                nonlocal code
                if code:
                    return
                url = request.url
                if "callback" in url and "code=" in url:
                    extracted = _extract_code(url)
                    if extracted:
                        code = extracted

            def set_browser_closed():
                nonlocal browser_closed
                browser_closed = True

            page.on("request", on_request)
            page.on("close", set_browser_closed)

            page.goto(login_url)
            print("  (等待登录完成，不要关闭浏览器窗口。最长等待 3 分钟)\n")

            elapsed = 0
            while (
                not code
                and not browser_closed
                and not _cancel_event.is_set()
                and elapsed < 180
            ):
                # 同时检查当前页面 URL（以防 request 事件没捕获到）
                url = page.url
                if "callback" in url and "code=" in url:
                    extracted = _extract_code(url)
                    if extracted:
                        code = extracted
                        break
                page.wait_for_timeout(1000)
                elapsed += 1

            if not browser_closed:
                browser.close()

    except Exception as e:
        print(f"\n  [!] 浏览器流程出错: {e}\n")

    if not code:
        print("\n[X] 未能捕获授权码。请确认在浏览器中完成了登录。\n")

    return code


def _extract_code(user_input):
    """从用户输入中提取 authorization code。

    用户可能粘贴：
    - 纯 code 字符串：直接返回
    - callback URL (code=xxx)：提取 code 参数
    - 中间跳转 URL（没有 code 参数）：报错并提示正确操作
    """
    from urllib.parse import urlparse, parse_qs

    if not user_input:
        return None

    user_input = user_input.strip()

    # 纯 code 字符串（不含 = 号，不以 http 开头）
    if "=" not in user_input and not user_input.startswith(("http", "pixiv")):
        return user_input

    # URL：尝试从 query 参数中提取 code
    parsed = urlparse(user_input)
    params = parse_qs(parsed.query)
    code = params.get("code", [None])[0]

    if code:
        return code

    # 没有 code 参数——分析用户错误粘贴了什么
    if "accounts.pixiv.net" in user_input or "code_challenge" in user_input:
        print("\n  [!] 这是登录跳转过程中的 URL，里面没有 code 参数。")
        print("  请按 Ctrl+H 打开浏览器历史记录，搜索 'callback'")
        print("  找到 callback?state=...&code=... 那条记录，复制整条 URL\n")
        return None

    # URL 里有 code= 但 parse_qs 没提取到（可能是嵌套 URL），兜底提取
    if "code=" in user_input:
        return user_input.rpartition("code=")[2].split("&")[0]

    print("\n  [!] 未找到 code 参数。请确认操作正确。\n")
    return None


def configure_gallery_dl(refresh_token):
    from .config import IMAGES_DIR
    project_root = Path(__file__).parent.parent

    # gallery-dl 路径：base-directory + directory 组成完整路径
    images_dir = IMAGES_DIR
    if images_dir.is_absolute():
        gdl_config.set((), 'base-directory', str(images_dir))
        gdl_config.set((), 'extractor', {
            'pixiv': {
                'refresh-token': refresh_token,
                'directory': ['{user[name]} {user[id]}'],
                'archive': str(_cfg.DATA_DIR / 'gallery_dl_archive.db'),
                'sleep-request': 1.0,
            }
        })
    else:
        gdl_config.set((), 'base-directory', str(project_root))
        gdl_config.set((), 'extractor', {
            'pixiv': {
                'refresh-token': refresh_token,
                'directory': [str(images_dir.relative_to(project_root)), '{user[name]} {user[id]}'],
                'archive': str(_cfg.DATA_DIR / 'gallery_dl_archive.db'),
                'sleep-request': 1.0,
            }
        })

    gdl_config.set((), 'output', {
        'mode': 'auto',
        'skip': 'true',
        'progress': 'false',
    })


def auth():
    # 1. 尝试 refresh_token 恢复
    token = _try_refresh_token()
    if token:
        configure_gallery_dl(token)
        return

    # 2. OAuth PKCE 流程
    token = _oauth_pkce()
    if token:
        configure_gallery_dl(token)
        return

    raise RuntimeError("Pixiv 登录失败。请通过浏览器 OAuth 流程授权。")
