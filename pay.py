"""
Stripe Checkout 自动化支付脚本
用法:
  python pay.py <session_id> [--card N] [--config path] [--token TOKEN]

示例:
  python pay.py cs_live_a12H3g13P9TH6udPmljRCpWsmHiKRFH7VUiZBbcA1U60eMzFFI2wp3rtXL
"""

import argparse
import base64
import hashlib
import json
import os
import random
import re
import string
import sys
import time
import urllib.parse
import uuid
from datetime import datetime

import requests


LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "log.txt")

def _init_log():
    """清空并初始化 log.txt"""
    with open(LOG_FILE, "w", encoding="utf-8") as f:
        f.write(f"{'='*80}\n")
        f.write(f"  Stripe 自动化支付 日志  —  {datetime.now().isoformat()}\n")
        f.write(f"{'='*80}\n\n")

def _log(msg: str):
    """追加一行到 log.txt 并同时 print"""
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    line = f"[{ts}] {msg}"
    print(line)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(line + "\n")

def _log_raw(text: str):
    """追加原始文本到 log.txt（不 print）"""
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(text + "\n")

def _log_request(method: str, url: str, data=None, params=None, tag: str = ""):
    """记录 HTTP 请求详情"""
    _log_raw(f"\n{'─'*70}")
    _log_raw(f">>> REQUEST  {tag}")
    _log_raw(f"    {method} {url}")
    if params:
        _log_raw(f"    PARAMS: {json.dumps(params, ensure_ascii=False, indent=6)}")
    if data:
        # 脱敏卡号
        safe = dict(data) if isinstance(data, dict) else {}
        if "card[number]" in safe:
            safe["card[number]"] = "****" + str(safe["card[number]"])[-4:]
        if "card[cvc]" in safe:
            safe["card[cvc]"] = "***"
        _log_raw(f"    BODY: {json.dumps(safe, ensure_ascii=False, indent=6)}")

def _log_response(resp: requests.Response, tag: str = ""):
    """记录 HTTP 响应详情"""
    _log_raw(f"<<< RESPONSE {tag}  status={resp.status_code}")
    try:
        body = resp.json()
        _log_raw(f"    BODY: {json.dumps(body, ensure_ascii=False, indent=6)}")
    except Exception:
        _log_raw(f"    BODY(raw): {resp.text[:2000]}")
    _log_raw(f"{'─'*70}\n")

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
STRIPE_API = "https://api.stripe.com"
STRIPE_VERSION_FULL = "2025-03-31.basil; checkout_server_update_beta=v1; checkout_manual_approval_preview=v1"
STRIPE_VERSION_BASE = "2025-03-31.basil"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.0.0 Safari/537.36"
)
HCAPTCHA_SITE_KEY_FALLBACK = "c7faac4c-1cd7-4b1b-b2d4-42ba98d09c7a"

KNOWN_PUBLISHABLE_KEYS = {
    "1HOrSwC6h1nxGoI3": "pk_live_51HOrSwC6h1nxGoI3lTAgRjYVrz4dU3fVOabyCcKR3pbEJguCVAlqCxdxCUvoRh1XWwRacViovU3kLKvpkjh7IqkW00iXQsjo3n",
}

# ---------------------------------------------------------------------------
# 地域 / 浏览器配置 — 必须和代理 IP 出口一致
# ---------------------------------------------------------------------------
LOCALE_PROFILES = {
    "US": {
        "browser_locale": "en-US",
        "browser_timezone": "America/Chicago",
        "browser_tz_offset": 360,      # CST = UTC-6 → 360
        "browser_language": "en-US",
        "color_depth": 24,
        "screen_w": 1920, "screen_h": 1080, "dpr": 1,
    },
    "KR": {
        "browser_locale": "ko-KR",
        "browser_timezone": "Asia/Seoul",
        "browser_tz_offset": -540,     # KST = UTC+9 → -540
        "browser_language": "ko-KR",
        "color_depth": 24,
        "screen_w": 1920, "screen_h": 1080, "dpr": 1,
    },
}


APATA_RBA_ORG_ID = "8t63q4n4"

def _build_browser_fingerprint(locale_profile: dict) -> dict:
    """构建 RecordBrowserInfo 的完整设备指纹 payload"""
    sw = locale_profile["screen_w"]
    sh = locale_profile["screen_h"]
    dpr = locale_profile["dpr"]
    cd = locale_profile["color_depth"]
    lang = locale_profile["browser_language"]
    tz_name = locale_profile["browser_timezone"]
    tz_offset = locale_profile["browser_tz_offset"]

    # 可用高度 = 屏幕高度 - 任务栏 (48-60px)
    avail_h = sh - random.randint(40, 60)

    return {
        "navigator": {
            "mediaDevices": {"audioinput": random.randint(1, 3), "videoinput": random.randint(0, 2),
                             "audiooutput": random.randint(1, 3)},
            "battery": {"charging": True, "chargingTime": 0, "dischargingTime": None,
                        "level": round(random.uniform(0.5, 1.0), 2)},
            "appCodeName": "Mozilla", "appName": "Netscape",
            "appVersion": USER_AGENT.replace("Mozilla/", ""),
            "cookieEnabled": True, "doNotTrack": None,
            "hardwareConcurrency": random.choice([8, 12, 16, 32]),
            "language": lang,
            "languages": [lang, lang.split("-")[0]],
            "maxTouchPoints": 0, "onLine": True,
            "platform": "Win32", "product": "Gecko", "productSub": "20030107",
            "userAgent": USER_AGENT,
            "vendor": "Google Inc.", "vendorSub": "",
            "webdriver": False,
            "deviceMemory": random.choice([4, 8, 16]),
            "pdfViewerEnabled": True, "javaEnabled": False,
            "plugins": "PDF Viewer,Chrome PDF Viewer,Chromium PDF Viewer,Microsoft Edge PDF Viewer,WebKit built-in PDF",
            "connections": {
                "effectiveType": "4g",
                "downlink": round(random.uniform(1.0, 10.0), 2),
                "rtt": random.choice([50, 100, 150, 200, 250, 300, 350, 400]),
                "saveData": False,
            },
        },
        "screen": {
            "availHeight": avail_h, "availWidth": sw,
            "availLeft": 0, "availTop": 0,
            "colorDepth": cd, "height": sh, "width": sw,
            "pixelDepth": cd,
            "orientation": "landscape-primary",
            "devicePixelRatio": dpr,
        },
        "timezone": {"offset": tz_offset, "timezone": tz_name},
        "canvas": hashlib.sha256(os.urandom(32)).hexdigest(),
        "permissions": {
            "geolocation": "denied", "notifications": "denied",
            "midi": "denied", "camera": "denied", "microphone": "denied",
            "background-fetch": "prompt", "background-sync": "granted",
            "persistent-storage": "granted", "accelerometer": "granted",
            "gyroscope": "granted", "magnetometer": "granted",
            "clipboard-read": "denied", "clipboard-write": "denied",
            "screen-wake-lock": "denied", "display-capture": "denied",
            "idle-detection": "denied",
        },
        "audio": {"sum": 124.04347527516074},
        "browserBars": {
            "locationbar": True, "menubar": True, "personalbar": True,
            "statusbar": True, "toolbar": True, "scrollbars": True,
        },
        "sensors": {
            "accelerometer": True, "gyroscope": True, "linearAcceleration": True,
            "absoluteOrientation": True, "relativeOrientation": True,
            "magnetometer": False, "ambientLight": False, "proximity": False,
        },
        "storage": {
            "localStorage": True, "sessionStorage": True,
            "indexedDB": True, "openDatabase": False,
        },
        "webGl": {
            "dataHash": hashlib.sha256(os.urandom(32)).hexdigest(),
            "vendor": "Google Inc. (NVIDIA)",
            "renderer": "ANGLE (NVIDIA, NVIDIA GeForce RTX 4060 (0x00002882) Direct3D11 vs_5_0 ps_5_0, D3D11)",
        },
        "adblock": False,
        "clientRects": {
            "x": round(-10004 + random.uniform(-1, 1), 10),
            "y": round(2.35 + random.uniform(-0.01, 0.01), 10),
            "width": round(111.29 + random.uniform(-0.01, 0.01), 10),
            "height": round(111.29 + random.uniform(-0.01, 0.01), 10),
            "top": round(2.35 + random.uniform(-0.01, 0.01), 10),
            "bottom": round(113.64 + random.uniform(-0.01, 0.01), 10),
            "left": round(-10004 + random.uniform(-1, 1), 10),
            "right": round(-9893 + random.uniform(-1, 1), 10),
        },
        "fonts": {"installed_count": random.randint(40, 60), "not_installed_count": 0},
    }


def _gen_fingerprint():
    def _id():
        return str(uuid.uuid4()).replace("-", "") + uuid.uuid4().hex[:6]
    return _id(), _id(), _id()



_PLUGINS_STR = (
    "PDF Viewer,internal-pdf-viewer,application/pdf,pdf++text/pdf,pdf, "
    "Chrome PDF Viewer,internal-pdf-viewer,application/pdf,pdf++text/pdf,pdf, "
    "Chromium PDF Viewer,internal-pdf-viewer,application/pdf,pdf++text/pdf,pdf, "
    "Microsoft Edge PDF Viewer,internal-pdf-viewer,application/pdf,pdf++text/pdf,pdf, "
    "WebKit built-in PDF,internal-pdf-viewer,application/pdf,pdf++text/pdf,pdf"
)
_CANVAS_FPS = [
    "0100100101111111101111101111111001110010110111110111111",
    "0100100101111111101111101111111001110010110111110111110",
    "0100100101111111101111101111111001110010110111110111101",
]
_AUDIO_FPS = [
    "d331ca493eb692cfcd19ae5db713ad4b",
    "a7c5f72e1b3d4e8f9c0d2a6b7e8f1c3d",
    "e4b8d6f2a0c3d5e7f9b1c3d5e7f9a0b2",
]


def _encode_m6(payload: dict) -> str:
    """JSON → urlencode → base64 (m.stripe.com/6 编码格式)"""
    raw = json.dumps(payload, separators=(",", ":"))
    return base64.b64encode(urllib.parse.quote(raw, safe="").encode()).decode()


def _b64url_seg(n: int = 32) -> str:
    return base64.urlsafe_b64encode(os.urandom(n)).rstrip(b"=").decode()


def register_fingerprint(http: "requests.Session") -> tuple[str, str, str]:
    """向 m.stripe.com/6 发送 4 次指纹上报, 返回服务端分配的 (guid, muid, sid)。
    如果请求失败, 返回本地随机生成的值。
    """
    # 本地备用值
    guid, muid, sid = _gen_fingerprint()
    fp_id = uuid.uuid4().hex

    # 屏幕参数 (US 常见配置)
    screens = [(1920, 1080, 1), (1536, 864, 1.25), (2560, 1440, 1), (1440, 900, 1)]
    sw, sh, dpr = random.choice(screens)
    vh = sh - random.randint(40, 70)  # viewport = screen - chrome
    cpu = random.choice([4, 8, 12, 16])
    canvas_fp = random.choice(_CANVAS_FPS)
    audio_fp = random.choice(_AUDIO_FPS)

    def _build_full(v2: int, inc_ids: bool) -> dict:
        s1, s2, s3, s4, s5 = (_b64url_seg() for _ in range(5))
        ts_now = int(time.time() * 1000)
        return {
            "v2": v2, "id": fp_id,
            "t": round(random.uniform(3, 120), 1),
            "tag": "$npm_package_version", "src": "js",
            "a": {
                "a": {"v": "true", "t": 0},
                "b": {"v": "true", "t": 0},
                "c": {"v": "en-US", "t": 0},
                "d": {"v": "Win32", "t": 0},
                "e": {"v": _PLUGINS_STR, "t": round(random.uniform(0, 0.5), 1)},
                "f": {"v": f"{sw}w_{vh}h_24d_{dpr}r", "t": 0},
                "g": {"v": str(cpu), "t": 0},
                "h": {"v": "false", "t": 0},
                "i": {"v": "sessionStorage-enabled, localStorage-enabled", "t": round(random.uniform(0.5, 2), 1)},
                "j": {"v": canvas_fp, "t": round(random.uniform(5, 120), 1)},
                "k": {"v": "", "t": 0},
                "l": {"v": USER_AGENT, "t": 0},
                "m": {"v": "", "t": 0},
                "n": {"v": "false", "t": round(random.uniform(3, 50), 1)},
                "o": {"v": audio_fp, "t": round(random.uniform(20, 30), 1)},
            },
            "b": {
                "a": f"https://{s1}.{s2}.{s3}/",
                "b": f"https://{s1}.{s3}/{s4}/{s5}/{_b64url_seg()}",
                "c": _b64url_seg(),
                "d": muid if inc_ids else "NA",
                "e": sid if inc_ids else "NA",
                "f": False, "g": True, "h": True,
                "i": ["location"], "j": [],
                "n": round(random.uniform(800, 2000), 1),
                "u": "chatgpt.com", "v": "auth.openai.com",
                "w": f"{ts_now}:{hashlib.sha256(os.urandom(32)).hexdigest()}",
            },
            "h": os.urandom(10).hex(),
        }

    def _build_mouse(source: str) -> dict:
        return {
            "muid": muid, "sid": sid,
            "url": f"https://{_b64url_seg()}.{_b64url_seg()}/{_b64url_seg()}/{_b64url_seg()}/{_b64url_seg()}",
            "source": source,
            "data": [random.randint(1, 8) for _ in range(10)],
        }

    m6_headers = {
        "User-Agent": USER_AGENT,
        "Content-Type": "text/plain;charset=UTF-8",
        "Accept": "*/*",
        "Origin": "https://m.stripe.network",
        "Referer": "https://m.stripe.network/",
    }
    m6_url = "https://m.stripe.com/6"
    _log("[1/6] 注册浏览器指纹 ...")

    # #1 完整指纹 (v2=1, 无 ID)
    try:
        r1 = http.post(m6_url, data=_encode_m6(_build_full(1, False)), headers=m6_headers, timeout=10)
        if r1.status_code == 200:
            j = r1.json()
            muid = j.get("muid", muid)
            guid = j.get("guid", guid)
            sid = j.get("sid", sid)
            _log(f"      指纹上报 1/4 完成")
    except Exception as e:
        _log(f"      指纹上报 1/4 失败: {e}")

    # #2 完整指纹 (v2=2, 带 ID)
    try:
        r2 = http.post(m6_url, data=_encode_m6(_build_full(2, True)), headers=m6_headers, timeout=10)
        if r2.status_code == 200:
            j = r2.json()
            guid = j.get("guid", guid)
            _log(f"      指纹上报 2/4 完成")
    except Exception as e:
        _log(f"      指纹上报 2/4 失败: {e}")

    # #3 鼠标行为 (mouse-timings-10-v2)
    try:
        http.post(m6_url, data=_encode_m6(_build_mouse("mouse-timings-10-v2")), headers=m6_headers, timeout=10)
        _log("      指纹上报 3/4 完成 (鼠标行为)")
    except Exception:
        pass

    # #4 鼠标行为 (mouse-timings-10)
    try:
        http.post(m6_url, data=_encode_m6(_build_mouse("mouse-timings-10")), headers=m6_headers, timeout=10)
        _log("      指纹上报 4/4 完成 (鼠标轨迹)")
    except Exception:
        pass

    _log(f"      浏览器指纹注册完成")
    return guid, muid, sid


def _gen_elements_session_id():
    """生成类似 elements_session_15hfldlRpSm 的 session id"""
    import random, string
    chars = string.ascii_letters + string.digits
    return "elements_session_" + "".join(random.choices(chars, k=11))


def _stripe_headers():
    return {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Origin": "https://js.stripe.com",
        "Referer": "https://js.stripe.com/",
    }
def parse_checkout_url(raw: str) -> tuple[str, str]:
    """解析输入，返回 (session_id, stripe_checkout_url)

    支持以下格式:
      - 裸 session_id: cs_live_xxx / cs_test_xxx
      - Stripe URL: https://checkout.stripe.com/c/pay/cs_live_xxx
      - ChatGPT URL: https://chatgpt.com/checkout/openai_llc/cs_live_xxx
    """
    raw = raw.strip()
    m = re.search(r"(cs_(?:live|test)_[A-Za-z0-9]+)", raw)
    if not m:
        raise ValueError(f"无法从输入中提取 checkout_session_id: {raw[:120]}...")
    session_id = m.group(1)

    # 构建用于 Playwright 等回退方案的 Stripe checkout URL
    # 如果输入是 checkout.stripe.com 的链接则直接使用，否则用标准格式构建
    if "checkout.stripe.com" in raw:
        stripe_url = raw
    else:
        stripe_url = f"https://checkout.stripe.com/c/pay/{session_id}"

    return session_id, stripe_url

def fetch_publishable_key(session: requests.Session, session_id: str, stripe_checkout_url: str) -> str:
    checkout_url = stripe_checkout_url

    _log("[2/6] 获取商户密钥 ...")

    for acct_id_part, known_pk in KNOWN_PUBLISHABLE_KEYS.items():
        try:
            url = f"{STRIPE_API}/v1/payment_pages/{session_id}/init"
            post_data = {"key": known_pk, "_stripe_version": STRIPE_VERSION_BASE,
                      "browser_locale": "en-US"}
            _log_request("POST", url, data=post_data, tag="[2/6] pk探测")
            test_resp = session.post(url, data=post_data, headers=_stripe_headers(), timeout=15)
            _log_response(test_resp, tag="[2/6] pk探测")
            if test_resp.status_code == 200:
                _log(f"      商户密钥已匹配 (缓存命中)")
                return known_pk
        except Exception as e:
            _log(f"      密钥探测异常: {e}")

    pk = _fetch_pk_playwright(checkout_url)
    if pk:
        _log(f"      商户密钥已获取 (浏览器解析)")
        return pk

    raise RuntimeError("无法提取商户密钥, 请检查支付链接是否有效")


def _fetch_pk_playwright(checkout_url: str) -> str | None:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None

    pk = None

    def on_request(request):
        nonlocal pk
        if pk:
            return
        if "api.stripe.com" in request.url and "init" in request.url:
            post = request.post_data or ""
            m = re.search(r"key=(pk_(?:live|test)_[A-Za-z0-9]+)", post)
            if m:
                pk = m.group(1)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.on("request", on_request)
            try:
                page.goto(checkout_url, wait_until="domcontentloaded", timeout=20000)
                for _ in range(10):
                    if pk:
                        break
                    page.wait_for_timeout(1000)
            except Exception:
                pass
            browser.close()
    except Exception:
        return None

    return pk


def init_checkout(session: requests.Session, session_id: str, pk: str, locale_profile: dict = None) -> tuple[dict, str, dict]:
    """返回 (init_resp, stripe_ver, ctx) — ctx 包含后续步骤需要的上下文"""
    locale_profile = locale_profile or LOCALE_PROFILES["US"]
    url = f"{STRIPE_API}/v1/payment_pages/{session_id}/init"
    stripe_js_id = str(uuid.uuid4())
    elements_session_id = _gen_elements_session_id()

    for version in [STRIPE_VERSION_BASE, STRIPE_VERSION_FULL]:
        data = {
            "browser_locale": locale_profile["browser_locale"],
            "browser_timezone": locale_profile["browser_timezone"],
            "elements_session_client[elements_init_source]": "custom_checkout",
            "elements_session_client[referrer_host]": "chatgpt.com",
            "elements_session_client[stripe_js_id]": stripe_js_id,
            "elements_session_client[locale]": locale_profile["browser_locale"],
            "elements_session_client[is_aggregation_expected]": "false",
            "key": pk,
            "_stripe_version": version,
        }
        if version == STRIPE_VERSION_FULL:
            data["elements_session_client[client_betas][0]"] = "custom_checkout_server_updates_1"
            data["elements_session_client[client_betas][1]"] = "custom_checkout_manual_approval_1"

        _log(f"      初始化结账会话 ...")
        _log_request("POST", url, data=data, tag="[2b/6] init")
        resp = session.post(url, data=data, headers=_stripe_headers())
        _log_response(resp, tag="[2b/6] init")
        if resp.status_code == 200:
            ctx = {
                "stripe_js_id": stripe_js_id,
                "elements_session_id": elements_session_id,
            }
            return resp.json(), version, ctx
        if resp.status_code == 400 and "beta" in resp.text.lower():
            _log(f"      当前API版本不匹配, 切换备用版本 ...")
            continue
        raise RuntimeError(f"init 失败 [{resp.status_code}]: {resp.text[:500]}")

    raise RuntimeError("init 失败: 所有 Stripe API 版本均不可用")


def extract_hcaptcha_config(init_resp: dict) -> dict:
    raw = json.dumps(init_resp)
    result = {"site_key": HCAPTCHA_SITE_KEY_FALLBACK, "rqdata": ""}

    if init_resp.get("site_key"):
        result["site_key"] = init_resp["site_key"]
    m = re.search(r'"hcaptcha_site_key"\s*:\s*"([^"]+)"', raw)
    if m and not init_resp.get("site_key"):
        result["site_key"] = m.group(1)

    m = re.search(r'"hcaptcha_rqdata"\s*:\s*"([^"]+)"', raw)
    if m:
        result["rqdata"] = m.group(1)

    return result


def fetch_elements_session(
    session: requests.Session,
    pk: str,
    session_id: str,
    ctx: dict,
    stripe_ver: str = STRIPE_VERSION_FULL,
    locale_profile: dict = None,
) -> dict:
    """调用 elements/sessions, 返回响应 dict 并更新 ctx 中的 elements_session_id"""
    locale_profile = locale_profile or LOCALE_PROFILES["US"]
    locale_short = locale_profile["browser_locale"].split("-")[0]  # HAR: "zh" 而非 "zh-CN"
    stripe_js_id = ctx.get("stripe_js_id", str(uuid.uuid4()))
    url = f"{STRIPE_API}/v1/elements/sessions"
    params = {
        "client_betas[0]": "custom_checkout_server_updates_1",
        "client_betas[1]": "custom_checkout_manual_approval_1",
        "deferred_intent[mode]": "subscription",
        "deferred_intent[amount]": "0",
        "deferred_intent[currency]": "usd",
        "deferred_intent[setup_future_usage]": "off_session",
        "deferred_intent[payment_method_types][0]": "card",
        "currency": "usd",
        "key": pk,
        "_stripe_version": stripe_ver,
        "elements_init_source": "custom_checkout",
        "referrer_host": "chatgpt.com",
        "stripe_js_id": stripe_js_id,
        "locale": locale_short,
        "type": "deferred_intent",
        "checkout_session_id": session_id,
    }
    _log("      获取支付元素会话 ...")
    _log_request("GET", url, params=params, tag="[2c] elements/sessions")
    resp = session.get(url, params=params, headers=_stripe_headers())
    _log_response(resp, tag="[2c] elements/sessions")

    if resp.status_code == 200:
        data = resp.json()
        # 提取真实的 elements_session_id (如果有)
        real_es_id = data.get("session_id") or data.get("id")
        if real_es_id:
            ctx["elements_session_id"] = real_es_id
            _log(f"      支付元素会话已建立")
        # 提取 config_id
        config_id = data.get("config_id")
        if config_id:
            ctx["config_id"] = config_id
            _log(f"      配置标识已获取")
        return data
    else:
        _log(f"      支付元素获取失败 ({resp.status_code}), 使用本地会话继续")
        return {}



def lookup_consumer(
    session: requests.Session,
    pk: str,
    email: str,
    stripe_ver: str = STRIPE_VERSION_FULL,
):
    """查询 Stripe Link 消费者会话，模拟真实浏览器的两次 lookup"""
    url = f"{STRIPE_API}/v1/consumers/sessions/lookup"
    surfaces = [
        ("web_link_authentication_in_payment_element", "default_value"),
        ("web_elements_controller", "default_value"),
    ]
    for surface, source in surfaces:
        data = {
            "request_surface": surface,
            "email_address": email,
            "email_source": source,
            "session_id": str(uuid.uuid4()),
            "key": pk,
            "_stripe_version": stripe_ver,
        }
        if surface == "web_elements_controller":
            data["do_not_log_consumer_funnel_event"] = "true"
        try:
            _log(f"      查询 Link 消费者信息 ...")
            _log_request("POST", url, data=data, tag="[2d] consumer/lookup")
            resp = session.post(url, data=data, headers=_stripe_headers(), timeout=10)
            _log_response(resp, tag="[2d] consumer/lookup")
        except Exception as e:
            _log(f"      Link 查询异常 (不影响流程): {e}")
        time.sleep(random.uniform(0.3, 0.8))


def update_payment_page_address(
    session: requests.Session,
    pk: str,
    session_id: str,
    card: dict,
    ctx: dict,
    stripe_ver: str = STRIPE_VERSION_FULL,
):
    """模拟浏览器逐字段提交地址/税区信息, 共 6 次 POST"""
    url = f"{STRIPE_API}/v1/payment_pages/{session_id}"
    addr = card.get("address", {})
    elements_session_id = ctx.get("elements_session_id", _gen_elements_session_id())
    stripe_js_id = ctx.get("stripe_js_id", str(uuid.uuid4()))

    # 基础字段 — 每次 update 都要带
    base = {
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[session_id]": elements_session_id,
        "elements_session_client[stripe_js_id]": stripe_js_id,
        "elements_session_client[locale]": "en-US",
        "elements_session_client[is_aggregation_expected]": "false",
        "client_attribution_metadata[merchant_integration_additional_elements][0]": "payment",
        "client_attribution_metadata[merchant_integration_additional_elements][1]": "address",
        "key": pk,
        "_stripe_version": stripe_ver,
    }

    # HAR 中的逐字段提交顺序: country → (重复一次) → line1 → city → state → postal_code
    address_steps = [
        {"tax_region[country]": addr.get("country", "US")},
        {},  # 重复提交 (无新字段, 模拟用户切换焦点)
        {"tax_region[line1]": addr.get("line1", "")},
        {"tax_region[city]": addr.get("city", "")},
        {"tax_region[state]": addr.get("state", "")},
        {"tax_region[postal_code]": addr.get("postal_code", "")},
    ]

    _log("      开始提交账单地址 ...")
    accumulated = {}
    for step_idx, new_fields in enumerate(address_steps):
        accumulated.update(new_fields)
        data = dict(base)
        data.update(accumulated)

        step_name = list(new_fields.keys())[0].split("]")[-1] if new_fields else "焦点变更"
        _log(f"      提交地址字段 {step_idx + 1}/6: {step_name}")
        _log_request("POST", url, data=data, tag=f"[2e] update_address({step_idx + 1}/6)")
        resp = session.post(url, data=data, headers=_stripe_headers())
        _log_response(resp, tag=f"[2e] update_address({step_idx + 1}/6)")

        if resp.status_code != 200:
            _log(f"      地址字段 {step_idx + 1} 返回 {resp.status_code}, 继续 ...")

        # 模拟人类输入间隔 (2-5 秒)
        time.sleep(random.uniform(2.0, 4.5))

def send_telemetry(
    session: requests.Session,
    event_type: str,
    session_id: str,
    ctx: dict,
):
    """向 r.stripe.com/b 发送遥测事件, 模拟 stripe.js 行为上报"""
    url = "https://r.stripe.com/b"
    muid = ctx.get("muid", "")
    sid = ctx.get("sid", "")
    guid = ctx.get("guid", "")

    payload = {
        "v2": 1,
        "tag": event_type,
        "src": "js",
        "pid": "checkout_" + session_id[:20],
        "muid": muid,
        "sid": sid,
        "guid": guid,
    }
    headers = {
        "User-Agent": USER_AGENT,
        "Content-Type": "text/plain;charset=UTF-8",
        "Accept": "*/*",
        "Origin": "https://js.stripe.com",
        "Referer": "https://js.stripe.com/",
    }
    try:
        body = base64.b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()
        session.post(url, data=body, headers=headers, timeout=5)
    except Exception:
        pass


def send_telemetry_batch(
    session: requests.Session,
    session_id: str,
    ctx: dict,
    phase: str = "init",
):
    """按阶段批量发送遥测事件"""
    events_map = {
        "init": ["checkout.init", "elements.create", "payment_element.mount"],
        "address": ["address.update", "address.focus", "address.blur"],
        "card_input": ["card.focus", "card.input", "card.blur", "cvc.input"],
        "confirm": ["checkout.confirm.start", "payment_method.create", "checkout.confirm.intent"],
        "3ds": ["three_ds2.start", "three_ds2.fingerprint", "three_ds2.authenticate"],
        "poll": ["checkout.poll", "checkout.complete"],
    }
    events = events_map.get(phase, [])
    for evt in events:
        send_telemetry(session, evt, session_id, ctx)
        time.sleep(random.uniform(0.05, 0.2))


def submit_apata_fingerprint(
    session: requests.Session,
    three_ds_server_trans_id: str,
    three_ds_method_url: str,
    notification_url: str,
    locale_profile: dict,
    ctx: dict,
):


    # 1) POST acs-method.apata.io/v1/houston/method — 提交 threeDSMethodData
    _log("      3DS 设备指纹: 提交认证数据 ...")
    method_data = base64.b64encode(json.dumps({
        "threeDSServerTransID": three_ds_server_trans_id,
        "threeDSMethodNotificationURL": notification_url,
    }, separators=(",", ":")).encode()).decode()

    try:
        method_url = three_ds_method_url or "https://acs-method.apata.io/v1/houston/method"
        resp = session.post(
            method_url,
            data={"threeDSMethodData": method_data},
            headers={
                "User-Agent": USER_AGENT,
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": "https://js.stripe.com",
                "Referer": "https://js.stripe.com/",
            },
            timeout=15,
        )
        _log(f"      3DS 认证数据已提交 ({resp.status_code})")
    except Exception as e:
        _log(f"      3DS 认证数据提交异常: {e}")

    time.sleep(random.uniform(0.5, 1.0))

    # 2) POST acs-method.apata.io/v1/RecordBrowserInfo — 设备指纹上报
    _log("      3DS 设备指纹: 上报浏览器信息 ...")
    # 生成 possessionDeviceId (localStorage acsRbaDeviceId 模拟)
    possession_device_id = ctx.get("apata_device_id") or str(uuid.uuid4())
    ctx["apata_device_id"] = possession_device_id

    fp_data = _build_browser_fingerprint(locale_profile)
    record_payload = {
        "threeDSServerTransID": three_ds_server_trans_id,
        "computedValue": hashlib.sha256(os.urandom(32)).hexdigest()[:20],
        "possessionDeviceId": possession_device_id,
    }
    record_payload.update(fp_data)

    try:
        record_url = "https://acs-method.apata.io/v1/RecordBrowserInfo"
        resp = session.post(
            record_url,
            json=record_payload,
            headers={
                "User-Agent": USER_AGENT,
                "Content-Type": "application/json",
                "Origin": "https://acs-method.apata.io",
                "Referer": "https://acs-method.apata.io/",
            },
            timeout=15,
        )
        _log(f"      浏览器信息已上报 ({resp.status_code})")
    except Exception as e:
        _log(f"      浏览器信息上报异常: {e}")

    time.sleep(random.uniform(0.5, 1.0))

    # 3) GET rba.apata.io/xxx.js — 模拟 RBA profile 脚本加载
    _log("      3DS 设备指纹: 加载风控脚本 ...")
    rba_session_id = ctx.get("rba_session_id") or str(uuid.uuid4())
    ctx["rba_session_id"] = rba_session_id
    try:
        # HAR 中的 URL 格式: rba.apata.io/<random>.js?<random_param>=<org_id>&<random_param>=<session_id>
        rba_script_name = ''.join(random.choices(string.ascii_lowercase + string.digits, k=16)) + ".js"
        rba_param1 = ''.join(random.choices(string.ascii_lowercase + string.digits, k=16))
        rba_param2 = ''.join(random.choices(string.ascii_lowercase + string.digits, k=16))
        rba_url = f"https://rba.apata.io/{rba_script_name}?{rba_param1}={APATA_RBA_ORG_ID}&{rba_param2}={rba_session_id}"
        resp = session.get(rba_url, headers={"User-Agent": USER_AGENT}, timeout=10)
        _log(f"      风控脚本已加载 ({resp.status_code})")
    except Exception as e:
        _log(f"      风控脚本加载异常: {e}")

    # 4) 模拟 aa.online-metrix.net CONNECT (WebRTC beacon 不可模拟, 仅日志标记)
    _log("      WebRTC 信标已跳过 (协议模式不支持)")

    # 总等待: 让 Apata 有时间处理指纹结果 (HAR 中这个窗口约 8-12 秒)
    wait = random.uniform(5.0, 8.0)
    _log(f"      等待指纹处理 ({wait:.1f}s) ...")
    time.sleep(wait)

def solve_hcaptcha(captcha_cfg: dict, hcaptcha_config: dict, max_retries: int = 3) -> tuple[str, str]:
    """返回 (token, ekey) 元组"""
    api_url = captcha_cfg.get("api_url", "https://api.yescaptcha.com")
    client_key = captcha_cfg["api_key"]
    site_key = hcaptcha_config["site_key"]
    rqdata = hcaptcha_config.get("rqdata", "")

    for retry in range(max_retries):
        if retry > 0:
            _log(f"      [重试 {retry + 1}/{max_retries}] 重新发起验证码请求")

        _log(f"      正在请求人机验证解题 ...")

        # 创建 1 个任务
        task_body = {
            "type": "HCaptchaTaskProxyless",
            "websiteURL": "https://b.stripecdn.com/stripethirdparty-srv/assets/v32.1/HCaptchaInvisible.html",
            "websiteKey": site_key,
            "isEnterprise": True,
            "userAgent": USER_AGENT,
        }
  

        create_payload = {"clientKey": client_key, "task": task_body}
        try:
            create_url = f"{api_url}/createTask"
            _log_request("POST", create_url, data=create_payload, tag="[captcha] createTask")
            create_resp = requests.post(create_url, json=create_payload, timeout=15)
            _log_response(create_resp, tag="[captcha] createTask")
            data = create_resp.json()
            if data.get("errorId", 1) != 0:
                _log(f"      验证码任务创建失败: {data.get('errorDescription', '?')}")
                time.sleep(3)
                continue
            task_id = data["taskId"]
        except Exception as e:
            _log(f"      验证码任务创建异常: {e}")
            time.sleep(3)
            continue

        _log(f"      验证码任务已创建, 等待平台解题 ...")

     
        for attempt in range(60):
            time.sleep(3)
            try:
                result_url = f"{api_url}/getTaskResult"
                result_payload = {"clientKey": client_key, "taskId": task_id}
                result_resp = requests.post(result_url, json=result_payload, timeout=10)
                result_data = result_resp.json()
            except Exception:
                continue

            if result_data.get("errorId", 0) != 0:
                error_code = result_data.get("errorCode", "")
                if error_code == "ERROR_TASK_TIMEOUT":
                    _log("      验证码解题超时, 重新发起 ...")
                    break
                continue

            if result_data.get("status") == "ready":
                solution = result_data["solution"]
                _log_raw(f"      solution keys: {list(solution.keys())}")
                _log_raw(f"      solution full: {json.dumps(solution, ensure_ascii=False)[:500]}")
                token = solution["gRecaptchaResponse"]
                # eKey 可能在不同字段名下
                ekey = solution.get("eKey", "") or solution.get("respKey", "") or solution.get("ekey", "")
                _log(f"      验证码已解决 (token长度: {len(token)})")
                _log_raw(f"      captcha_token(前100): {token[:100]}...")
                if ekey:
                    _log_raw(f"      captcha_ekey(前100): {ekey[:100]}...")
                return token, ekey

            if attempt % 5 == 4:
                _log(f"      解题中, 请耐心等待 ... ({attempt + 1}/60)")

    raise RuntimeError(f"人机验证解题失败 (已重试 {max_retries} 轮, 请检查打码平台余额)")


def create_payment_method(
    session: requests.Session,
    pk: str,
    card: dict,
    captcha_token: str,
    session_id: str,
    stripe_ver: str = STRIPE_VERSION_BASE,
    ctx: dict = None,
) -> str:
    ctx = ctx or {}
    guid = ctx.get("guid") or _gen_fingerprint()[0]
    muid = ctx.get("muid") or _gen_fingerprint()[0]
    sid  = ctx.get("sid")  or _gen_fingerprint()[0]
    addr = card.get("address", {})

    data = {
        "billing_details[name]": card["name"],
        "billing_details[email]": card["email"],
        "billing_details[address][country]": addr.get("country", "US"),
        "billing_details[address][line1]": addr.get("line1", ""),
        "billing_details[address][city]": addr.get("city", ""),
        "billing_details[address][postal_code]": addr.get("postal_code", ""),
        "billing_details[address][state]": addr.get("state", ""),
        "type": "card",
        "card[number]": card["number"],
        "card[cvc]": card["cvc"],
        "card[exp_year]": card["exp_year"],
        "card[exp_month]": card["exp_month"],
        "allow_redisplay": "unspecified",

        "payment_user_agent": "stripe.js/5412f474d5; stripe-js-v3/5412f474d5; payment-element; deferred-intent",
        "referrer": "https://chatgpt.com",
        # time_on_page: 模拟从页面加载到提交的真实耗时 (HAR: 31368ms / 249421ms)
        "time_on_page": str(ctx.get("time_on_page", random.randint(25000, 55000))),
        "client_attribution_metadata[client_session_id]": str(uuid.uuid4()),
        "client_attribution_metadata[checkout_session_id]": session_id,
        "client_attribution_metadata[merchant_integration_source]": "elements",
        "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
        "client_attribution_metadata[merchant_integration_version]": "2021",
        "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
        "client_attribution_metadata[payment_method_selection_flow]": "automatic",
        "guid": guid,
        "muid": muid,
        "sid": sid,
        "key": pk,
        "_stripe_version": stripe_ver,
    }
    if captcha_token:
        data["radar_options[hcaptcha_token]"] = captcha_token

    url = f"{STRIPE_API}/v1/payment_methods"
    _log("[4/6] 创建支付方式 ...")
    _log_request("POST", url, data=data, tag="[4/6] create_payment_method")
    resp = session.post(url, data=data, headers=_stripe_headers())
    _log_response(resp, tag="[4/6] create_payment_method")
    if resp.status_code != 200:
        raise RuntimeError(f"创建 payment_method 失败 [{resp.status_code}]: {resp.text[:500]}")

    pm = resp.json()
    pm_id = pm["id"]
    brand = pm.get("card", {}).get("display_brand", "unknown")
    last4 = pm.get("card", {}).get("last4", "????")
    _log(f"      支付方式创建成功: {brand} ****{last4}")
    return pm_id

def confirm_payment(
    session: requests.Session,
    pk: str,
    session_id: str,
    pm_id: str,
    captcha_token: str,
    init_resp: dict,
    stripe_ver: str = STRIPE_VERSION_BASE,
    captcha_cfg: dict = None,
    captcha_ekey: str = "",
    ctx: dict = None,
    locale_profile: dict = None,
) -> dict:
    ctx = ctx or {}
    locale_profile = locale_profile or LOCALE_PROFILES["US"]
    guid = ctx.get("guid") or _gen_fingerprint()[0]
    muid = ctx.get("muid") or _gen_fingerprint()[0]
    sid  = ctx.get("sid")  or _gen_fingerprint()[0]

    expected_amount = "0"
    line_items = init_resp.get("line_items", [])
    if line_items:
        total = sum(item.get("amount", 0) for item in line_items)
        expected_amount = str(total)


    init_checksum = init_resp.get("init_checksum", "")
    config_id = init_resp.get("config_id", "")
    stripe_js_id = ctx.get("stripe_js_id", str(uuid.uuid4()))
    elements_session_id = ctx.get("elements_session_id", _gen_elements_session_id())
    checkout_url = init_resp.get("url") or init_resp.get("stripe_hosted_url") or ""


    ver = STRIPE_VERSION_FULL

    data = {
        "guid": guid,
        "muid": muid,
        "sid": sid,
        "payment_method": pm_id,
        "expected_amount": expected_amount,
        "expected_payment_method_type": "card",
        "consent[terms_of_service]": "accepted",
        "key": pk,
        "_stripe_version": ver,
  
        "init_checksum": init_checksum,
     
        "version": "5412f474d5",
      
        "return_url": checkout_url,
    
        "elements_session_client[elements_init_source]": "custom_checkout",
        "elements_session_client[referrer_host]": "chatgpt.com",
        "elements_session_client[stripe_js_id]": stripe_js_id,
        "elements_session_client[locale]": locale_profile.get("browser_locale", "en-US"),
        "elements_session_client[is_aggregation_expected]": "false",
        "elements_session_client[session_id]": elements_session_id,
        "elements_session_client[client_betas][0]": "custom_checkout_server_updates_1",
        "elements_session_client[client_betas][1]": "custom_checkout_manual_approval_1",
  
        "client_attribution_metadata[client_session_id]": stripe_js_id,
        "client_attribution_metadata[checkout_session_id]": session_id,
        "client_attribution_metadata[checkout_config_id]": config_id,
        "client_attribution_metadata[elements_session_id]": elements_session_id,
        "client_attribution_metadata[elements_session_config_id]": str(uuid.uuid4()),
        "client_attribution_metadata[merchant_integration_source]": "checkout",
        "client_attribution_metadata[merchant_integration_subtype]": "payment-element",
        "client_attribution_metadata[merchant_integration_version]": "custom",
        "client_attribution_metadata[payment_intent_creation_flow]": "deferred",
        "client_attribution_metadata[payment_method_selection_flow]": "automatic",
        "client_attribution_metadata[merchant_integration_additional_elements][0]": "payment",
        "client_attribution_metadata[merchant_integration_additional_elements][1]": "address",
    }

  
    if captcha_token:
        data["passive_captcha_token"] = captcha_token
  

    url = f"{STRIPE_API}/v1/payment_pages/{session_id}/confirm"
    _log("[5/6] 确认支付 ...")
    _log_request("POST", url, data=data, tag="[5/6] confirm")
    resp = session.post(url, data=data, headers=_stripe_headers())
    _log_response(resp, tag="[5/6] confirm")
    if resp.status_code != 200:
        raise RuntimeError(f"confirm 失败 [{resp.status_code}]: {resp.text[:500]}")

    confirm_data = resp.json()

    next_action = confirm_data.get("next_action")
    if not next_action:
        seti = _find_setup_intent(confirm_data)
        if seti and seti.get("next_action"):
            next_action = seti["next_action"]

    if next_action and next_action.get("type") == "use_stripe_sdk":
        _log("      银行要求 3DS 安全验证, 正在处理 ...")
        _handle_3ds(session, pk, confirm_data, captcha_token, stripe_ver, captcha_cfg,
                    locale_profile=locale_profile, ctx=ctx)

    return confirm_data


def _find_setup_intent(data: dict) -> dict | None:
    si = data.get("setup_intent")
    if si:
        return si
    pm_obj = data.get("payment_method_object")
    if pm_obj and isinstance(pm_obj, dict):
        return pm_obj.get("setup_intent")
    raw = json.dumps(data)
    m = re.search(r"seti_[A-Za-z0-9]+", raw)
    if m:
        return {"id": m.group(0)}
    return None


def _handle_3ds(
    session: requests.Session,
    pk: str,
    confirm_data: dict,
    captcha_token: str,
    stripe_ver: str = STRIPE_VERSION_BASE,
    captcha_cfg: dict = None,
    locale_profile: dict = None,
    ctx: dict = None,
):
    """处理 3DS2 认证流程 (模拟浏览器: captcha → verify_challenge → Apata指纹 → 3ds2/authenticate)"""
    locale_profile = locale_profile or LOCALE_PROFILES["US"]
    ctx = ctx or {}
    raw = json.dumps(confirm_data)

    # 查找 setatt_ (直接在 confirm 响应中)
    source_match = re.search(r"(setatt_[A-Za-z0-9]+)", raw)
    source = source_match.group(1) if source_match else None

    seti_match = re.search(r"(seti_[A-Za-z0-9]+)", raw)
    seti_id = seti_match.group(1) if seti_match else None

    client_secret = None
    if seti_id:
        cs_match = re.search(rf"({re.escape(seti_id)}_secret_[A-Za-z0-9]+)", raw)
        if cs_match:
            client_secret = cs_match.group(1)

    _log(f"      3DS 验证参数: source={'已获取' if source else '未获取'}, intent={'已获取' if seti_id else '未获取'}")


    challenge_site_key = None
    challenge_rqdata = ""
    # 从 setup_intent.next_action.use_stripe_sdk.stripe_js 提取
    seti_obj = _find_setup_intent(confirm_data)
    if seti_obj and isinstance(seti_obj, dict):
        na = seti_obj.get("next_action", {})
        sdk_info = na.get("use_stripe_sdk", {})
        stripe_js = sdk_info.get("stripe_js", {})
        if stripe_js.get("site_key"):
            challenge_site_key = stripe_js["site_key"]
            challenge_rqdata = stripe_js.get("rqdata", "")
            _log(f"      检测到确认验证码 (需二次验证)")

    CHALLENGE_MAX_ATTEMPTS = 5
    if challenge_site_key and seti_id and client_secret and captcha_cfg:
        challenge_hcaptcha_cfg = {
            "site_key": challenge_site_key,
            "rqdata": challenge_rqdata,
        }

        for challenge_attempt in range(1, CHALLENGE_MAX_ATTEMPTS + 1):
            _log(f"      解确认验证码 (第 {challenge_attempt}/{CHALLENGE_MAX_ATTEMPTS} 次) ...")
            challenge_token, challenge_ekey = solve_hcaptcha(captcha_cfg, challenge_hcaptcha_cfg, max_retries=3)

            verify_url = f"{STRIPE_API}/v1/setup_intents/{seti_id}/verify_challenge"
            _log(f"      提交验证码到 Stripe ...")
            verify_data = {
                "client_secret": client_secret,
                "challenge_response_token": challenge_token,
                "captcha_vendor_name": "hcaptcha",
                "key": pk,
                "_stripe_version": STRIPE_VERSION_FULL,
            }
          
            _log_request("POST", verify_url, data=verify_data, tag=f"[5/6] verify_challenge({challenge_attempt}/{CHALLENGE_MAX_ATTEMPTS})")
            resp = session.post(verify_url, data=verify_data, headers=_stripe_headers())
            _log_response(resp, tag=f"[5/6] verify_challenge({challenge_attempt}/{CHALLENGE_MAX_ATTEMPTS})")

            if resp.status_code != 200:
                err_text = resp.text[:300]
                _log(f"      验证码提交失败 ({resp.status_code})")
                if "no valid challenge" in err_text.lower():
                    raise RuntimeError(f"验证挑战已过期, 请使用新的支付链接重试")
                break  # 其他非 200 退出循环

            verify_result = resp.json()
            verify_status = verify_result.get("status", "unknown")
            _log(f"      验证状态: {verify_status}")

            # 检测 captcha challenge 失败
            setup_error = verify_result.get("last_setup_error", {})
            if setup_error:
                err_code = setup_error.get("code", "")
                err_msg = setup_error.get("message", "")
                if "captcha" in err_msg.lower() or "authentication_failure" in err_code:
                    if challenge_attempt < CHALLENGE_MAX_ATTEMPTS:
                        _log(f"      验证码被 Stripe 拒绝, 重试 ...")
                        continue  # 重新解 captcha 再试
                    else:
                        raise RuntimeError(f"验证码连续 {CHALLENGE_MAX_ATTEMPTS} 次被拒绝, 建议更换卡片或代理后重试")

            # verify 成功, 从响应中提取 setatt_
            verify_raw = json.dumps(verify_result)
            new_source = re.search(r"(setatt_[A-Za-z0-9]+)", verify_raw)
            if new_source:
                source = new_source.group(1)
                _log(f"      验证通过, 已获取新的认证凭据")
            break  # 成功, 退出循环

    elif seti_id and client_secret and not source:
        # 没有 challenge 但也没有 setatt_, 尝试原始 verify_challenge
        verify_url = f"{STRIPE_API}/v1/setup_intents/{seti_id}/verify_challenge"
        _log(f"      提交备用验证 ...")
        verify_data = {
            "client_secret": client_secret,
            "challenge_response_token": captcha_token,
            "captcha_vendor_name": "hcaptcha",
            "key": pk,
            "_stripe_version": STRIPE_VERSION_FULL,
        }
        _log_request("POST", verify_url, data=verify_data, tag="[5/6] verify_challenge(fallback)")
        resp = session.post(verify_url, data=verify_data, headers=_stripe_headers())
        _log_response(resp, tag="[5/6] verify_challenge(fallback)")
        if resp.status_code == 200:
            si_result = resp.json()
            _log(f"      备用验证状态: {si_result.get('status', 'unknown')}")
            # 检测 captcha challenge 失败
            setup_error = si_result.get("last_setup_error", {})
            if setup_error:
                err_code = setup_error.get("code", "")
                err_msg = setup_error.get("message", "")
                if "captcha" in err_msg.lower() or "authentication_failure" in err_code:
                    raise RuntimeError(f"验证码被 Stripe 拒绝, 建议更换卡片或代理: {err_msg}")
            verify_raw = json.dumps(si_result)
            new_source = re.search(r"(setatt_[A-Za-z0-9]+)", verify_raw)
            if new_source:
                source = new_source.group(1)
        else:
            _log(f"      备用验证失败 ({resp.status_code})")

   
    send_telemetry_batch(session, "", ctx, phase="3ds")

 
    three_ds_trans_id = None
    three_ds_method_url = None
    notification_url = None
   
    for search_blob in [raw]:
        m_tid = re.search(r'"server_transaction_id"\s*:\s*"([^"]+)"', search_blob)
        if m_tid:
            three_ds_trans_id = m_tid.group(1)
        m_murl = re.search(r'"three_ds_method_url"\s*:\s*"([^"]+)"', search_blob)
        if m_murl:
            three_ds_method_url = m_murl.group(1)

    if source and three_ds_trans_id:
        # 构建 notification URL (HAR 中的格式)
        acct_match = re.search(r'(acct_[A-Za-z0-9]+)', raw)
        acct_id = acct_match.group(1) if acct_match else "acct_unknown"
        notification_url = f"https://hooks.stripe.com/3d_secure_2/fingerprint/{acct_id}/{source}"

        submit_apata_fingerprint(
            session=session,
            three_ds_server_trans_id=three_ds_trans_id,
            three_ds_method_url=three_ds_method_url,
            notification_url=notification_url,
            locale_profile=locale_profile,
            ctx=ctx,
        )
    elif source:
      
        wait = random.uniform(8.0, 12.0)
        _log(f"      等待 3DS 指纹处理 ({wait:.1f}s) ...")
        time.sleep(wait)

   
    if source:
        auth_url = f"{STRIPE_API}/v1/3ds2/authenticate"
        _log(f"      3DS2 认证请求中 ...")
        auth_data = {
            "source": source,
            "browser": json.dumps({
                "fingerprintAttempted": True,
                "fingerprintData": None,
                "challengeWindowSize": None,
                "threeDSCompInd": "Y",
                "browserJavaEnabled": False,
                "browserJavascriptEnabled": True,
                "browserLanguage": locale_profile.get("browser_language", "en-US"),
                "browserColorDepth": str(locale_profile.get("color_depth", 24)),
                "browserScreenHeight": str(locale_profile.get("screen_h", 1080)),
                "browserScreenWidth": str(locale_profile.get("screen_w", 1920)),
                "browserTZ": str(locale_profile.get("browser_tz_offset", 360)),
                "browserUserAgent": USER_AGENT,
            }),
            "one_click_authn_device_support[hosted]": "false",
            "one_click_authn_device_support[same_origin_frame]": "false",
            "one_click_authn_device_support[spc_eligible]": "true",
            "one_click_authn_device_support[webauthn_eligible]": "true",
            "one_click_authn_device_support[publickey_credentials_get_allowed]": "true",
            "key": pk,
            "_stripe_version": STRIPE_VERSION_FULL,
        }
        _log_request("POST", auth_url, data=auth_data, tag="[5/6] 3ds2/authenticate")
        resp = session.post(auth_url, data=auth_data, headers=_stripe_headers())
        _log_response(resp, tag="[5/6] 3ds2/authenticate")
        if resp.status_code == 200:
            result = resp.json()
            state = result.get("state", "unknown")
            trans_status = result.get("ares", {}).get("transStatus", "?")
            status_map = {"Y": "通过 (免验证)", "C": "需银行验证 (OTP/APP)", "N": "被拒绝", "R": "被拒绝"}
            status_desc = status_map.get(trans_status, trans_status)
            _log(f"      3DS2 认证结果: {status_desc}")
        else:
            _log(f"      3DS2 认证请求失败 ({resp.status_code})")
    else:
        _log("      3DS 认证凭据缺失, 无法完成验证")
        raise RuntimeError("3DS 验证失败: 未获取认证凭据, 请更换支付链接重试")

  
    if seti_id and client_secret:
        time.sleep(3)
        poll_url = f"{STRIPE_API}/v1/setup_intents/{seti_id}"
        poll_params = {
            "client_secret": client_secret,
            "is_stripe_sdk": "false",
            "key": pk,
            "_stripe_version": STRIPE_VERSION_FULL,
        }
        _log(f"      查询 3DS 验证最终状态 ...")
        _log_request("GET", poll_url, params=poll_params, tag="[5/6] setup_intent状态")
        poll_resp = session.get(poll_url, params=poll_params, headers=_stripe_headers())
        _log_response(poll_resp, tag="[5/6] setup_intent状态")
        if poll_resp.status_code == 200:
            si_status = poll_resp.json().get("status", "unknown")
            si_status_map = {"succeeded": "验证通过", "requires_action": "需要银行验证 (OTP)", "requires_payment_method": "支付方式被拒"}
            _log(f"      3DS 最终状态: {si_status_map.get(si_status, si_status)}")
    else:
        _log("      3DS 状态查询跳过 (参数不完整)")


def poll_result(session: requests.Session, pk: str, session_id: str, stripe_ver: str = STRIPE_VERSION_BASE) -> dict:
    url = f"{STRIPE_API}/v1/payment_pages/{session_id}/poll"
    params = {
        "key": pk,
        "_stripe_version": stripe_ver,
    }

    _log("[6/6] 查询支付结果 ...")
    last_state = None
    stuck_count = 0
    for attempt in range(40):
        time.sleep(2)
        _log_request("GET", url, params=params, tag=f"[6/6] poll({attempt+1}/40)")
        resp = session.get(url, params=params, headers=_stripe_headers())
        _log_response(resp, tag=f"[6/6] poll({attempt+1}/40)")
        if resp.status_code != 200:
            _log(f"      查询异常 ({resp.status_code}), 重试 ...")
            continue

        data = resp.json()
        state = data.get("state") or "unknown"
        payment_status = data.get("payment_object_status") or ""

        if state == "succeeded":
            _log(f"")
            _log(f"  ============ 支付成功! ============")
            _log(f"  支付状态: {payment_status}")
            _log(f"  模式: {data.get('mode', '?')}")
            _log(f"  ====================================")
            _log(f"")
            return data

        if state in ("failed", "expired", "canceled"):
            state_map = {"failed": "支付失败", "expired": "支付链接已过期", "canceled": "支付已取消"}
            _log(f"\n  {state_map.get(state, state)}")
            _log_raw(f"  完整 poll 响应: {json.dumps(data, ensure_ascii=False, indent=4)}")
            return data

        status_hint = {
            "requires_action": "等待银行验证(OTP)",
            "requires_payment_method": "支付方式被拒",
            "processing": "处理中",
        }
        hint = status_hint.get(payment_status, payment_status or "无支付状态")
        _log(f"      等待中 ({attempt + 1}/40) - 会话: {state}, 支付: {hint}")

        cur_state = f"{state}|{payment_status}"
        if cur_state == last_state:
            stuck_count += 1
        else:
            stuck_count = 0
            last_state = cur_state

        if stuck_count >= 10 and not payment_status:
            _log(f"      连续 10 次无支付状态, 该卡类型可能不被支持 (如银联)")
            data["state"] = "failed"
            return data

        if stuck_count >= 12 and payment_status == "requires_action":
            _log(f"      连续 12 次等待银行验证, 该卡需要真人OTP验证, 协议无法完成")
            data["state"] = "failed"
            return data

    raise TimeoutError("查询超时 (约100s), 支付可能仍在处理中")

def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def run(checkout_input: str, card_index: int = 0, config_path: str = "config.json", manual_token: str = ""):
    _init_log()  # 初始化日志文件

    cfg = load_config(config_path)
    cards = cfg["cards"]
    if card_index >= len(cards):
        raise ValueError(f"卡索引 {card_index} 超出范围，共 {len(cards)} 张卡")
    card = cards[card_index]
    captcha_cfg = cfg["captcha"]

   
    _FIRST_NAMES = ["JAMES", "JOHN", "ROBERT", "MICHAEL", "WILLIAM", "DAVID", "RICHARD", "JOSEPH",
                    "THOMAS", "CHARLES", "DANIEL", "MATTHEW", "ANTHONY", "MARK", "STEVEN",
                    "MARY", "PATRICIA", "JENNIFER", "LINDA", "ELIZABETH", "BARBARA", "SUSAN",
                    "JESSICA", "SARAH", "KAREN", "NANCY", "LISA", "BETTY", "MARGARET", "SANDRA"]
    _LAST_NAMES = ["SMITH", "JOHNSON", "WILLIAMS", "BROWN", "JONES", "GARCIA", "MILLER",
                   "DAVIS", "RODRIGUEZ", "MARTINEZ", "WILSON", "ANDERSON", "TAYLOR", "THOMAS",
                   "MOORE", "JACKSON", "MARTIN", "LEE", "THOMPSON", "WHITE", "HARRIS", "CLARK"]
    card["name"] = f"{random.choice(_FIRST_NAMES)} {random.choice(_LAST_NAMES)}"

    email_user = ''.join(random.choices(string.ascii_lowercase + string.digits, k=random.randint(8, 12)))
    _EMAIL_DOMAINS = ["gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com", "protonmail.com"]
    card["email"] = f"{email_user}@{random.choice(_EMAIL_DOMAINS)}"

    addr = card.get("address", {})
    line1 = addr.get("line1", "")

    new_line1 = re.sub(r"^\d+", str(random.randint(100, 999)), line1)
    if new_line1 == line1 and line1:
        new_line1 = f"{random.randint(100, 999)} {line1}"
    addr["line1"] = new_line1
    card["address"] = addr

   
    locale_key = cfg.get("locale", addr.get("country", "US")).upper()
    locale_profile = LOCALE_PROFILES.get(locale_key, LOCALE_PROFILES["US"])
    _log(f"  地域: {locale_key} ({locale_profile['browser_timezone']})")

    _log(f"")
    _log(f"  ====== GPT协议绑卡 开始 ======")
    _log(f"  银行卡: ****{card['number'][-4:]}  ({card['name']})")
    _log(f"  邮箱: {card['email']}")
    _log(f"  地址: {addr.get('line1', '')}")
    _log(f"  ================================")
    _log(f"")

  
    _log("[1/6] 解析支付链接 ...")
    session_id, stripe_checkout_url = parse_checkout_url(checkout_input)
    _log(f"      Session: {session_id[:40]}...")
    if "chatgpt.com" in checkout_input:
        _log(f"      检测到 ChatGPT 链接, 已自动转换")
    _log(f"      支付页面: {stripe_checkout_url[:60]}...")

    http = requests.Session()
    http.headers.update({"User-Agent": USER_AGENT})

    # 代理配置
    proxy_cfg = cfg.get("proxy")
    if proxy_cfg:
        host = proxy_cfg["host"]
        port = proxy_cfg["port"]
        user = proxy_cfg.get("user", "")
        pwd  = proxy_cfg.get("pass", "")
        if user and pwd:
            proxy_url = f"http://{user}:{pwd}@{host}:{port}"
        else:
            proxy_url = f"http://{host}:{port}"
        http.proxies = {"http": proxy_url, "https": proxy_url}
        _log(f"      代理: {host}:{port}")
    else:
        _log("      代理: 直连 (未配置代理)")


    reg_guid, reg_muid, reg_sid = register_fingerprint(http)

 
    pk = fetch_publishable_key(http, session_id, stripe_checkout_url)


    init_resp, stripe_ver, init_ctx = init_checkout(http, session_id, pk, locale_profile=locale_profile)
    init_ctx["guid"] = reg_guid
    init_ctx["muid"] = reg_muid
    init_ctx["sid"] = reg_sid
 
    init_ctx["page_load_ts"] = int(time.time() * 1000)
    mode = init_resp.get("mode", "unknown")
    display_name = init_resp.get("account_settings", {}).get("display_name", "?")
    _log(f"      商户: {display_name} | 模式: {mode}")
    _log(f"      结账会话初始化完成")


    send_telemetry_batch(http, session_id, init_ctx, phase="init")

   
    _log("      获取支付元素 ...")
    fetch_elements_session(http, pk, session_id, init_ctx, stripe_ver=stripe_ver, locale_profile=locale_profile)

   
    _log("      查询消费者信息 ...")
    lookup_consumer(http, pk, card["email"], stripe_ver=stripe_ver)

  
    _log("      提交账单地址 ...")
    update_payment_page_address(http, pk, session_id, card, init_ctx, stripe_ver=stripe_ver)

    
    send_telemetry_batch(http, session_id, init_ctx, phase="address")


    init_ctx["time_on_page"] = int(time.time() * 1000) - init_ctx.get("page_load_ts", int(time.time() * 1000))

    hcaptcha_cfg = extract_hcaptcha_config(init_resp)
    has_captcha = bool(hcaptcha_cfg.get("site_key"))
    _log(f"      人机验证: {'检测到' if has_captcha else '未检测到'}")

    send_telemetry_batch(http, session_id, init_ctx, phase="card_input")

    if manual_token:
        _log(f"[3/6] 使用手动 token (长度: {len(manual_token)})")
        captcha_token = manual_token
        captcha_ekey = ""
        pm_id = create_payment_method(http, pk, card, captcha_token, session_id, stripe_ver, ctx=init_ctx)
        # ---- 遥测: confirm 阶段 ----
        send_telemetry_batch(http, session_id, init_ctx, phase="confirm")
        confirm_payment(http, pk, session_id, pm_id, captcha_token, init_resp, stripe_ver, captcha_cfg,
                        captcha_ekey=captcha_ekey, ctx=init_ctx, locale_profile=locale_profile)
    else:
     
        _log("[3/6] 尝试直接提交 (跳过验证码) ...")
        try:
            pm_id = create_payment_method(http, pk, card, "", session_id, stripe_ver, ctx=init_ctx)
            send_telemetry_batch(http, session_id, init_ctx, phase="confirm")
            confirm_payment(http, pk, session_id, pm_id, "", init_resp, stripe_ver, captcha_cfg,
                            captcha_ekey="", ctx=init_ctx, locale_profile=locale_profile)
        except RuntimeError as e:
            err_msg = str(e).lower()
            if any(kw in err_msg for kw in ["captcha", "hcaptcha", "blocked", "denied", "radar"]):
                _log(f"[3/6] 需要人机验证, 开始解题 ...")
                captcha_token, captcha_ekey = solve_hcaptcha(captcha_cfg, hcaptcha_cfg)
              
                init_ctx["time_on_page"] = int(time.time() * 1000) - init_ctx.get("page_load_ts", int(time.time() * 1000))
                pm_id = create_payment_method(http, pk, card, captcha_token, session_id, stripe_ver, ctx=init_ctx)
                send_telemetry_batch(http, session_id, init_ctx, phase="confirm")
                confirm_payment(http, pk, session_id, pm_id, captcha_token, init_resp, stripe_ver, captcha_cfg,
                                captcha_ekey=captcha_ekey, ctx=init_ctx, locale_profile=locale_profile)
            else:
                raise

  
    send_telemetry_batch(http, session_id, init_ctx, phase="poll")

    # Step 6
    result = poll_result(http, pk, session_id, stripe_ver)
    _log(f"  流程结束, 日志已保存")
    return result



def main():
    parser = argparse.ArgumentParser(
        description="Stripe Checkout 自动化支付",
        epilog="示例: python pay.py cs_live_a12H3g13P9TH6udPmljRCpWsmHiKRFH7VUiZBbcA1U60eMzFFI2wp3rtXL",
    )
    parser.add_argument("session_id", help="Checkout Session ID (cs_live_xxx 或 cs_test_xxx)")
    parser.add_argument("--card", type=int, default=0, help="使用第 N 张卡 (0-based, 默认 0)")
    parser.add_argument("--config", default="config.json", help="配置文件路径 (默认 config.json)")
    parser.add_argument("--token", default="", help="手动传入 hCaptcha token (跳过打码平台)")
    args = parser.parse_args()

    try:
        run(args.session_id, card_index=args.card, config_path=args.config, manual_token=args.token)
    except Exception as e:
        err_msg = f"\n[ERROR] {type(e).__name__}: {e}"
        print(err_msg, file=sys.stderr)
        # 也写入日志
        try:
            import traceback
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"\n{'!'*60}\n")
                f.write(err_msg + "\n")
                f.write(traceback.format_exc())
                f.write(f"{'!'*60}\n")
        except Exception:
            pass
        sys.exit(1)


if __name__ == "__main__":
    main()