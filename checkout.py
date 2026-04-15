"""
AT -> Checkout Session ID
通过 ChatGPT backend-api 获取 Stripe 支付会话
"""
import contextlib
import json
import os
import random
import threading
import time
import uuid
from typing import Tuple, Optional, Callable

try:
    from curl_cffi import requests as cffi_requests
    HAS_CFFI = True
except ImportError:
    cffi_requests = None
    HAS_CFFI = False

try:
    from curl_cffi import CurlHttpVersion
    _CURL_HTTP11 = CurlHttpVersion.V1_1
except Exception:
    try:
        from curl_cffi.const import CurlHttpVersion
        _CURL_HTTP11 = CurlHttpVersion.V1_1
    except Exception:
        _CURL_HTTP11 = None

import requests

CHATGPT_ORIGIN = "https://chatgpt.com"
CHATGPT_BACKEND_API = f"{CHATGPT_ORIGIN}/backend-api"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

# 获取支付会话 / 解析 stripe_hosted_url 专用线路（US）；不在界面展示。
# 设环境变量 CHECKOUT_LINK_PROXY 可覆盖；设为空白字符串则直连。
CHECKOUT_BUILTIN_PROXY_URL = (
    "http://p9mx1124350-region-SG-sid-k3V7eBpN-t-5:iy2lmzpy@us.arxlabs.io:3010"
)

_gate_tls = threading.local()


def effective_checkout_proxy() -> Optional[str]:
    v = os.environ.get("CHECKOUT_LINK_PROXY")
    if v is not None:
        v = v.strip()
        return v if v else None
    u = (CHECKOUT_BUILTIN_PROXY_URL or "").strip()
    return u or None


def _us_gate_profile() -> dict:
    """随机新加坡浏览器 / 语言指纹（用于 ChatGPT 结账与 Stripe init 网关请求）。"""
    chrome_maj = random.choice([124, 126, 128, 130, 131, 132, 133, 134, 135, 136])
    if random.random() < 0.22:
        ua = (
            f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            f"AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{chrome_maj}.0.0.0 Safari/537.36"
        )
    else:
        ua = (
            f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            f"AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{chrome_maj}.0.0.0 Safari/537.36"
        )
    accept_language = random.choice(
        [
            "en-SG,en;q=0.9",
            "en-SG,en;q=0.8",
            "en,en-SG;q=0.9",
            "en-SG,en;q=0.9,zh;q=0.4",
        ]
    )
    return {
        "user_agent": ua,
        "accept_language": accept_language,
        "timezone_offset_min": -480,
        "browser_locale": "en-SG",
    }


def _active_gate_profile() -> dict:
    p = getattr(_gate_tls, "profile", None)
    return p if isinstance(p, dict) else _us_gate_profile()


def _session_is_cffi(sess) -> bool:
    mod = getattr(type(sess), "__module__", "") or ""
    return "curl_cffi" in mod


CURRENCY_MAP = {
    # 美洲
    "US": "USD", "CA": "CAD", "MX": "MXN", "BR": "BRL", "AR": "ARS",
    # 欧洲欧元区
    "DE": "EUR", "FR": "EUR", "ES": "EUR", "IT": "EUR", "NL": "EUR",
    "BE": "EUR", "AT": "EUR", "PT": "EUR", "FI": "EUR", "IE": "EUR",
    "GR": "EUR", "LU": "EUR", "SK": "EUR", "SI": "EUR", "EE": "EUR",
    "LV": "EUR", "LT": "EUR", "MT": "EUR", "CY": "EUR",
    # 欧洲非欧元
    "GB": "GBP", "CH": "CHF", "SE": "SEK", "NO": "NOK", "DK": "DKK",
    "PL": "PLN", "CZ": "CZK", "HU": "HUF", "RO": "RON",
    # 亚太
    "JP": "JPY", "KR": "KRW", "AU": "AUD", "NZ": "NZD",
    "SG": "SGD", "HK": "HKD", "TW": "TWD", "IN": "INR",
    "TH": "THB", "MY": "MYR", "ID": "IDR", "PH": "PHP", "VN": "VND",
    # 中东 / 非洲
    "AE": "AED", "SA": "SAR", "IL": "ILS", "TR": "TRY", "ZA": "ZAR",
    "NG": "NGN", "KE": "KES",
}

PLAN_CONFIGS = {
    "plus": {
        "plan_name": "chatgptplusplan",
        "promo": {"promo_campaign_id": "plus-1-month-free", "is_coupon_from_query_param": False},
    },
    # PRO: 服务端枚举为 chatgptpro（不是 chatgptproplan）；可用 test_pro_checkout.py --scan-plan-names 复核
    "pro": {
        "plan_name": "chatgptpro",
        "promo": None,
    },
    # 与 ChatGPT 网页/抓包对齐：5 席 + team-1-month-free，is_coupon_from_query_param 为 true
    "team": {
        "plan_name": "chatgptteamplan",
        "promo": {
            "promo_campaign_id": "team-1-month-free",
            "is_coupon_from_query_param": True,
        },
        "team_plan_data": {
            "workspace_name": "AutoTeam",
            "price_interval": "month",
            "seat_quantity": 5,
        },
    },
}


def _default_timeout() -> int:
    try:
        return max(15, int(os.environ.get("CHATGPT_HTTP_TIMEOUT", "75")))
    except Exception:
        return 75


def _do_request_on_session(
    session,
    method: str,
    url: str,
    headers: dict,
    json_body: dict = None,
    timeout: int = None,
) -> Tuple[int, dict]:
    """在同一 Session 上发起请求（HTTP 长连接 / 连接复用）。"""
    if timeout is None:
        timeout = _default_timeout()
    is_chatgpt = "chatgpt.com" in (url or "")

    if _session_is_cffi(session):
        cffi_last = None
        for attempt in range(3):
            try:
                kw = {"headers": headers, "timeout": timeout, "impersonate": "chrome"}
                if _CURL_HTTP11 is not None and attempt >= 1:
                    kw["http_version"] = _CURL_HTTP11
                if json_body and method.upper() == "POST":
                    kw["json"] = json_body
                    r = session.post(url, **kw)
                else:
                    r = session.get(url, **kw)
                try:
                    return r.status_code, r.json()
                except Exception:
                    return r.status_code, {"raw": (r.text or "")[:1000]}
            except Exception as e:
                cffi_last = str(e)
                if attempt < 2:
                    time.sleep(1.2 * (attempt + 1))
                    continue
                break

        fb_timeout = min(18, max(8, timeout // 4)) if is_chatgpt else timeout
        if os.environ.get("CHATGPT_SKIP_REQUESTS_FALLBACK", "").strip() in ("1", "true", "yes"):
            return 0, {"error": f"curl_cffi: {cffi_last}"}
        try:
            pu = None
            if getattr(session, "proxies", None):
                pu = session.proxies.get("https") or session.proxies.get("http")
            kw = {"headers": headers, "timeout": fb_timeout}
            if pu:
                kw["proxies"] = {"http": pu, "https": pu}
            if json_body and method.upper() == "POST":
                kw["json"] = json_body
            r = requests.post(url, **kw) if method.upper() == "POST" else requests.get(url, **kw)
            try:
                return r.status_code, r.json()
            except Exception:
                return r.status_code, {"raw": (r.text or "")[:1000]}
        except Exception as e2:
            return 0, {"error": f"curl_cffi: {cffi_last} | requests_fallback: {e2}"}

    last_err = None
    for attempt in range(3):
        try:
            kw = {"headers": headers, "timeout": timeout}
            if json_body and method.upper() == "POST":
                kw["json"] = json_body
                r = session.post(url, **kw)
            else:
                r = session.get(url, **kw)
            try:
                return r.status_code, r.json()
            except Exception:
                return r.status_code, {"raw": (r.text or "")[:1000]}
        except Exception as e:
            last_err = str(e)
            if attempt < 2:
                time.sleep(1.2 * (attempt + 1))
                continue
            return 0, {"error": f"requests: {last_err}"}
    return 0, {"error": f"requests: {last_err}"}


def _do_request(
    method: str,
    url: str,
    headers: dict,
    json_body: dict = None,
    timeout: int = None,
    http_session=None,
) -> Tuple[int, dict]:
    if http_session is not None:
        return _do_request_on_session(
            http_session, method, url, headers, json_body, timeout
        )
    if timeout is None:
        timeout = _default_timeout()
    gate_proxy = effective_checkout_proxy()
    proxies = {"http": gate_proxy, "https": gate_proxy} if gate_proxy else None
    is_chatgpt = "chatgpt.com" in (url or "")

    if HAS_CFFI:
        cffi_last = None
        for attempt in range(3):
            try:
                with cffi_requests.Session() as s:
                    kw = {"headers": headers, "timeout": timeout, "impersonate": "chrome"}
                    if proxies:
                        kw["proxies"] = proxies
                    if _CURL_HTTP11 is not None and attempt >= 1:
                        kw["http_version"] = _CURL_HTTP11
                    if json_body and method.upper() == "POST":
                        kw["json"] = json_body
                    r = s.post(url, **kw) if method.upper() == "POST" else s.get(url, **kw)
                    try:
                        return r.status_code, r.json()
                    except Exception:
                        return r.status_code, {"raw": (r.text or "")[:1000]}
            except Exception as e:
                cffi_last = str(e)
                if attempt < 2:
                    time.sleep(1.2 * (attempt + 1))
                    continue
                break

        fb_timeout = min(18, max(8, timeout // 4)) if is_chatgpt else timeout
        if os.environ.get("CHATGPT_SKIP_REQUESTS_FALLBACK", "").strip() in ("1", "true", "yes"):
            return 0, {"error": f"curl_cffi: {cffi_last}"}
        try:
            kw = {"headers": headers, "timeout": fb_timeout}
            if proxies:
                kw["proxies"] = proxies
            if json_body and method.upper() == "POST":
                kw["json"] = json_body
            r = requests.post(url, **kw) if method.upper() == "POST" else requests.get(url, **kw)
            try:
                return r.status_code, r.json()
            except Exception:
                return r.status_code, {"raw": (r.text or "")[:1000]}
        except Exception as e2:
            return 0, {"error": f"curl_cffi: {cffi_last} | requests_fallback: {e2}"}

    try:
        kw = {"headers": headers, "timeout": timeout}
        if proxies:
            kw["proxies"] = proxies
        if json_body and method.upper() == "POST":
            kw["json"] = json_body
        r = requests.post(url, **kw) if method.upper() == "POST" else requests.get(url, **kw)
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, {"raw": (r.text or "")[:1000]}
    except Exception as e:
        return 0, {"error": f"requests: {e}"}


@contextlib.contextmanager
def session_for_checkout():
    """
    为「获取支付会话」创建可复用的 Session（内置 JP 代理 + 固定随机日本指纹）。
    与 get_account_info / get_checkout_session / pay_openai_url_with_stripe_fragment 同块使用。
    """
    _gate_tls.profile = _us_gate_profile()
    proxy = effective_checkout_proxy()
    sess = None
    try:
        if HAS_CFFI:
            sess = cffi_requests.Session()
            if proxy:
                sess.proxies = {"http": proxy, "https": proxy}
        else:
            sess = requests.Session()
            if proxy:
                sess.proxies = {"http": proxy, "https": proxy}
        yield sess
    finally:
        if sess is not None:
            try:
                sess.close()
            except Exception:
                pass
        if hasattr(_gate_tls, "profile"):
            delattr(_gate_tls, "profile")


def _gate_api_headers(
    access_token: str, account_id: str = None, prof: dict = None
) -> dict:
    prof = prof if isinstance(prof, dict) else _active_gate_profile()
    h = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}",
        "Origin": CHATGPT_ORIGIN,
        "Referer": f"{CHATGPT_ORIGIN}/",
        "User-Agent": prof["user_agent"],
        "Accept-Language": prof["accept_language"],
    }
    if account_id:
        h["Chatgpt-Account-Id"] = account_id
    return h


def _api_headers(access_token: str, account_id: str = None) -> dict:
    """兼容旧调用；获取结账会话请用 session_for_checkout + _gate_api_headers 流程。"""
    return _gate_api_headers(access_token, account_id, prof=None)


def get_account_info(
    access_token: str,
    proxy_url: str = None,
    log_cb: Callable = None,
    http_session=None,
) -> Tuple[Optional[str], str, bool]:
    """获取账户信息, 返回 (account_id, plan_type, has_subscription)

    proxy_url 已弃用（结账网关固定走内置 JP 代理，不在日志中展示）。
    传入 http_session 可与 get_checkout_session 等同一会话复用连接。
    """
    if log_cb:
        log_cb("      查询账户信息 ...")

    prof = _active_gate_profile()
    url = (
        f"{CHATGPT_BACKEND_API}/accounts/check/v4-2023-04-27"
        f"?timezone_offset_min={prof['timezone_offset_min']}"
    )
    status, data = _do_request(
        "GET",
        url,
        _gate_api_headers(access_token, prof=prof),
        http_session=http_session,
    )

    if status != 200:
        if log_cb:
            hint = ""
            if isinstance(data, dict) and data.get("error"):
                hint = f" — {str(data.get('error'))[:180]}"
            log_cb(f"      账户查询失败 (HTTP {status}){hint}")
        return None, "unknown", False

    accounts = data.get("accounts", {})
    ordering = data.get("account_ordering", [])
    account_id = next((a for a in ordering if a != "default"), None) or (ordering[0] if ordering else None)

    if not account_id or account_id not in accounts:
        if log_cb:
            log_cb("      未找到有效账户")
        return None, "unknown", False

    acc = accounts[account_id]
    plan_type = acc.get("account", {}).get("plan_type", "unknown")
    has_sub = acc.get("entitlement", {}).get("has_active_subscription", False)

    if log_cb:
        log_cb(f"      账户类型: {plan_type}, 已有订阅: {'是' if has_sub else '否'}")

    return account_id, plan_type, has_sub


def get_checkout_session(
    access_token: str,
    proxy_url: str = None,
    plan_type: str = "plus",
    country: str = "US",
    currency: str = None,
    log_cb: Callable = None,
    account_id: Optional[str] = None,
    quiet: bool = False,
    http_session=None,
) -> Tuple[Optional[str], str]:
    """
    AT -> checkout_session_id
    返回 (cs_id, error_message), cs_id 为 None 表示失败

    proxy_url 已弃用；请与 session_for_checkout() 搭配 http_session 复用长连接。
    """
    at = (access_token or "").strip()
    if not at:
        return None, "Access Token 为空"

    plan_key = (plan_type or "plus").lower().strip()
    plan_cfg = PLAN_CONFIGS.get(plan_key)
    if not plan_cfg:
        return None, f"未知计划类型: {plan_type!r}，支持: {', '.join(sorted(PLAN_CONFIGS))}"
    cur = (currency or CURRENCY_MAP.get(country, "USD")).upper()

    if log_cb and not quiet:
        log_cb(f"      创建 {plan_key.upper()} 结账会话 (plan_name={plan_cfg['plan_name']}, 地区: {country}) ...")

    payload = {
        "plan_name": plan_cfg["plan_name"],
        "billing_details": {"country": country, "currency": cur},
        "checkout_ui_mode": "custom",
    }
    if plan_cfg.get("promo"):
        payload["promo_campaign"] = plan_cfg["promo"]
    if plan_cfg.get("team_plan_data"):
        payload["team_plan_data"] = plan_cfg["team_plan_data"]

    url = f"{CHATGPT_BACKEND_API}/payments/checkout"
    prof = _active_gate_profile()
    headers = _gate_api_headers(at, account_id=account_id, prof=prof)
    status, data = _do_request(
        "POST", url, headers, json_body=payload, http_session=http_session
    )

    if status == 402:
        err = ""
        if isinstance(data, dict):
            err = data.get("detail") or data.get("error") or data.get("raw", "")
            if isinstance(err, dict):
                err = err.get("message", str(err))
        return None, f"创建结账失败 (HTTP 402): {str(err)[:200]}"

    if status != 200:
        raw_detail = json.dumps(data, ensure_ascii=False) if isinstance(data, dict) else str(data)
        err = ""
        if isinstance(data, dict):
            err = data.get("detail") or data.get("error") or data.get("raw", "")
            if isinstance(err, dict):
                err = err.get("message", str(err))
        if log_cb:
            log_cb(f"      [DEBUG 完整响应] {raw_detail[:600]}")
        return None, f"创建结账失败 (HTTP {status}): {str(err)[:200]}"

    cs_id = (data.get("checkout_session_id") or "").strip()
    if not cs_id or not cs_id.startswith("cs_"):
        return None, f"未返回有效 checkout_session_id: {json.dumps(data, ensure_ascii=False)[:200]}"

    if log_cb and not quiet:
        log_cb(f"      支付会话已创建: {cs_id[:35]}...")

    return cs_id, ""


def confirm_checkout_via_openai_backend(
    access_token: str,
    checkout_session_id: str,
    payment_method_id: str,
    account_id: Optional[str] = None,
    log_cb: Callable = None,
    http_session=None,
) -> Tuple[bool, dict]:
    """
    针对 EUR/Elements-only 会话：直接调用 OpenAI 后端 confirm 端点，
    将 Stripe PM-ID 提交给 OpenAI，由 OpenAI 后端在其 Stripe 账户上创建订阅并确认支付。

    返回 (success: bool, response_data: dict)。
    """
    prof = _active_gate_profile()
    headers = _gate_api_headers(access_token, account_id=account_id, prof=prof)
    cs_id = checkout_session_id.strip()
    pm_id = payment_method_id.strip()

    # OpenAI 可能使用的 confirm 端点变体（按可能性降序）
    # 每项格式: (method, url, body_or_None)
    endpoints = [
        # 同 cs_id 路径
        ("POST", f"{CHATGPT_BACKEND_API}/payments/checkout/{cs_id}/confirm",
                 {"payment_method_id": pm_id}),
        ("POST", f"{CHATGPT_BACKEND_API}/payments/checkout/{cs_id}/confirm",
                 {"payment_method": pm_id}),
        ("POST", f"{CHATGPT_BACKEND_API}/payments/checkout/{cs_id}/complete",
                 {"payment_method_id": pm_id}),
        ("POST", f"{CHATGPT_BACKEND_API}/payments/checkout/{cs_id}/complete",
                 {"payment_method": pm_id}),
        # checkout_session_id 放 body
        ("POST", f"{CHATGPT_BACKEND_API}/payments/checkout/confirm",
                 {"checkout_session_id": cs_id, "payment_method_id": pm_id}),
        ("POST", f"{CHATGPT_BACKEND_API}/payments/checkout/complete",
                 {"checkout_session_id": cs_id, "payment_method_id": pm_id}),
        # PATCH / PUT 变体（有些 confirm 用 PATCH）
        ("PATCH", f"{CHATGPT_BACKEND_API}/payments/checkout/{cs_id}",
                  {"payment_method_id": pm_id, "status": "confirm"}),
        # 直接 PUT session
        ("PUT", f"{CHATGPT_BACKEND_API}/payments/checkout/{cs_id}",
                {"payment_method_id": pm_id}),
        # 其他路径
        ("POST", f"{CHATGPT_BACKEND_API}/payments/session/{cs_id}/confirm",
                 {"payment_method_id": pm_id}),
        ("POST", f"{CHATGPT_BACKEND_API}/payments/checkout/{cs_id}/payment_method",
                 {"payment_method_id": pm_id}),
    ]

    # 405 = 方法不对但路由存在，也继续尝试下一条
    _skip_statuses = {400, 402, 403, 404, 405, 422, 429}

    for method, url, body in endpoints:
        short_path = url.split("/backend-api")[-1]
        if log_cb:
            log_cb(f"      [EUR] 尝试: {method} {short_path}")
        try:
            status, data = _do_request(
                method, url, headers, json_body=body, http_session=http_session
            )
        except Exception as exc:
            if log_cb:
                log_cb(f"      [EUR] 请求异常: {exc}")
            continue

        if log_cb:
            raw = json.dumps(data, ensure_ascii=False) if isinstance(data, dict) else str(data)
            log_cb(f"      [EUR] HTTP {status}  {raw[:250]}")

        if status == 200:
            return True, data
        if status in _skip_statuses:
            continue
        # 5xx 或其他非预期 → 停止（避免重复触发限流）
        if log_cb:
            log_cb(f"      [EUR] HTTP {status} 非预期，终止探测")
        break

    return False, {}


# 批量任务：由 app.py 按链执行「本档结账会话 + 完整 pay.run」。
# pro:  先尝试 PRO 付费订阅（无 promo），失败后尝试 TEAM。
# plus: 先尝试 PLUS 0 元体验（plus-1-month-free），失败后升档 PRO 再降回 PLUS。
# team: 同上模式。
CHECKOUT_FALLBACK_CHAINS = {
    "pro":  ["pro", "team"],
    "plus": ["plus", "pro", "plus"],
    "team": ["team", "pro", "team"],
}


def checkout_urls_from_session_id(session_id: str) -> dict:
    """
    由 cs_live_ / cs_test_ 拼出常见支付入口，便于人工核对「短链」形态。
    实际以 Stripe / ChatGPT 返回为准。

    若需与浏览器复制的 OpenAI 链接一致（含 #fidn...），请用
    pay_openai_url_with_stripe_fragment（依赖 Stripe init 返回的 stripe_hosted_url）。
    """
    sid = (session_id or "").strip()
    if not sid.startswith("cs_"):
        return {}
    return {
        "stripe_checkout": f"https://checkout.stripe.com/c/pay/{sid}",
        "chatgpt_checkout_path": f"{CHATGPT_ORIGIN}/checkout/openai_llc/{sid}",
        "pay_openai_c_pay": f"https://pay.openai.com/c/pay/{sid}",
    }


def _stripe_init_headers() -> dict:
    return {
        "User-Agent": UA,
        "Accept": "application/json",
        "Origin": "https://js.stripe.com",
        "Referer": "https://js.stripe.com/",
    }


def _stripe_gate_headers(prof: dict = None) -> dict:
    p = prof if isinstance(prof, dict) else _active_gate_profile()
    return {
        "User-Agent": p["user_agent"],
        "Accept": "application/json",
        "Origin": "https://js.stripe.com",
        "Referer": "https://js.stripe.com/",
        "Accept-Language": p["accept_language"],
    }


def _post_stripe_payment_pages_init(
    url: str,
    data: dict,
    headers: dict,
    timeout: int,
    http_session=None,
):
    """Stripe payment_pages/init 为 application/x-www-form-urlencoded。"""
    if http_session is not None:
        if _session_is_cffi(http_session):
            last = None
            for attempt in range(3):
                try:
                    kw = {
                        "data": data,
                        "headers": headers,
                        "timeout": timeout,
                        "impersonate": "chrome",
                    }
                    if _CURL_HTTP11 is not None and attempt >= 1:
                        kw["http_version"] = _CURL_HTTP11
                    return http_session.post(url, **kw)
                except Exception as e:
                    last = str(e)
                    if attempt < 2:
                        time.sleep(1.2 * (attempt + 1))
                        continue
                    raise RuntimeError(last) from e
        return http_session.post(url, data=data, headers=headers, timeout=timeout)
    gate_proxy = effective_checkout_proxy()
    proxies = {"http": gate_proxy, "https": gate_proxy} if gate_proxy else None
    return requests.post(
        url, data=data, headers=headers, proxies=proxies, timeout=timeout
    )


def fetch_stripe_hosted_url(
    session_id: str,
    proxy_url: Optional[str] = None,
    timeout: Optional[int] = None,
    http_session=None,
) -> Tuple[Optional[str], str]:
    """
    POST Stripe payment_pages/{session_id}/init，读取 stripe_hosted_url。
    该字段一般为 checkout.stripe.com/c/pay/cs_xxx#fidn...（fragment 由 Stripe 生成）。

    proxy_url 已弃用；默认与获取结账会话相同内置代理 + 日本指纹。
    http_session 与 session_for_checkout() 同一会话时可复用长连接。
    """
    sid = (session_id or "").strip()
    if not sid.startswith("cs_"):
        return None, "无效的 session_id"
    t = timeout if timeout is not None else _default_timeout()
    t = min(max(t, 10), 45)

    try:
        from pay import KNOWN_PUBLISHABLE_KEYS, STRIPE_VERSION_BASE
    except ImportError as e:
        return None, f"需要同目录 pay.py: {e}"

    pk = next(iter(KNOWN_PUBLISHABLE_KEYS.values()), None)
    if not pk:
        return None, "KNOWN_PUBLISHABLE_KEYS 为空"

    url = f"https://api.stripe.com/v1/payment_pages/{sid}/init"
    prof = _active_gate_profile()
    data = {
        "key": pk,
        "_stripe_version": STRIPE_VERSION_BASE,
        "browser_locale": prof["browser_locale"],
    }
    try:
        r = _post_stripe_payment_pages_init(
            url,
            data,
            _stripe_gate_headers(prof=prof),
            t,
            http_session=http_session,
        )
    except Exception as e:
        return None, f"Stripe init 请求失败: {e}"

    if r.status_code != 200:
        return None, f"Stripe init HTTP {r.status_code}: {(r.text or '')[:220]}"

    try:
        body = r.json()
    except Exception:
        return None, "Stripe init 响应非 JSON"

    hosted = (body.get("stripe_hosted_url") or "").strip()
    if not hosted:
        return None, "响应中无 stripe_hosted_url"
    return hosted, ""


def pay_openai_url_with_stripe_fragment(
    session_id: str,
    proxy_url: Optional[str] = None,
    timeout: Optional[int] = None,
    http_session=None,
) -> Tuple[Optional[str], str]:
    """
    将 stripe_hosted_url 的主机从 checkout.stripe.com 换成 pay.openai.com，
    路径与 # 后 fragment 原样保留，与 ChatGPT 支付页地址栏复制结果对齐。
    """
    hosted, err = fetch_stripe_hosted_url(
        session_id, proxy_url=proxy_url, timeout=timeout, http_session=http_session
    )
    if not hosted:
        return None, err
    if "checkout.stripe.com" not in hosted:
        return None, f"非预期 stripe_hosted_url: {hosted[:120]}..."
    return hosted.replace("https://checkout.stripe.com", "https://pay.openai.com", 1), ""


# ---------------------------------------------------------------------------
# team.aimizy.com 外部 API — 获取 TEAM 支付链接
# ---------------------------------------------------------------------------

AIMIZY_API_URL = "https://team.aimizy.com/api/public/generate-payment-link"

def generate_team_payment_link_via_aimizy(
    access_token: str,
    country: str = "SG",
    currency: str = "SGD",
    log_cb: Callable = None,
    timeout: int = 30,
) -> Tuple[Optional[str], Optional[str], str]:
    """
    通过 team.aimizy.com 外部 API 获取 TEAM 支付链接。

    返回 (pay_url, checkout_session_id, error_message)
      - 成功: (url, cs_id, "")
      - 失败: (None, None, error_msg)
    """
    at = (access_token or "").strip()
    if not at:
        return None, None, "Access Token 为空"

    cur = (currency or "SGD").upper()
    cty = (country or "SG").upper()

    payload = {
        "access_token": at,
        "check_card_proxy": False,
        "country": cty,
        "currency": cur,
        "is_coupon_from_query_param": True,
        "is_short_link": False,
        "plan_name": "chatgptteamplan",
        "price_interval": "month",
        "promo_campaign_id": "team-1-month-free",
        "seat_quantity": 5,
    }

    if log_cb:
        log_cb(f"      调用 aimizy API 获取 TEAM 支付链接 (country={cty}) ...")

    try:
        r = requests.post(
            AIMIZY_API_URL,
            json=payload,
            timeout=timeout,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Origin": "https://team.aimizy.com",
                "Referer": "https://team.aimizy.com/",
            },
        )
    except Exception as e:
        return None, None, f"aimizy API 请求失败: {e}"

    if r.status_code != 200:
        detail = ""
        try:
            body = r.json()
            detail = body.get("detail") or body.get("error") or ""
        except Exception:
            detail = (r.text or "")[:300]
        return None, None, f"aimizy API HTTP {r.status_code}: {str(detail)[:300]}"

    try:
        body = r.json()
    except Exception:
        return None, None, "aimizy API 响应非 JSON"

    if not body.get("success"):
        return None, None, f"aimizy API 返回失败: {json.dumps(body, ensure_ascii=False)[:300]}"

    cs_id = (body.get("checkout_session_id") or "").strip()
    pay_url = (body.get("url") or "").strip()

    if not cs_id:
        return None, None, f"aimizy API 未返回 checkout_session_id"

    if log_cb:
        log_cb(f"      aimizy TEAM 支付链接已获取: {cs_id[:35]}...")

    return pay_url or None, cs_id, ""
