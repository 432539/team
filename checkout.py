"""
AT -> Checkout Session ID
通过 ChatGPT backend-api 获取 Stripe 支付会话
"""
import json
import uuid
from typing import Tuple, Optional, Callable

try:
    from curl_cffi import requests as cffi_requests
    HAS_CFFI = True
except ImportError:
    cffi_requests = None
    HAS_CFFI = False

import requests

CHATGPT_ORIGIN = "https://chatgpt.com"
CHATGPT_BACKEND_API = f"{CHATGPT_ORIGIN}/backend-api"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

CURRENCY_MAP = {
    "US": "USD", "KR": "KRW", "JP": "JPY", "GB": "GBP",
    "CA": "CAD", "DE": "EUR", "FR": "EUR", "AU": "AUD",
    "SG": "SGD", "HK": "HKD", "TW": "TWD", "IN": "INR",
}

PLAN_CONFIGS = {
    "plus": {
        "plan_name": "chatgptplusplan",
        "promo": {"promo_campaign_id": "plus-1-month-free", "is_coupon_from_query_param": False},
    },
    "team": {
        "plan_name": "chatgptteamplan",
        "promo": None,
        "team_plan_data": {
            "workspace_name": "AutoTeam",
            "price_interval": "month",
            "seat_quantity": 2,
        },
    },
}


def _do_request(
    method: str,
    url: str,
    headers: dict,
    json_body: dict = None,
    proxy_url: str = None,
    timeout: int = 30,
) -> Tuple[int, dict]:
    proxies = {"http": proxy_url, "https": proxy_url} if proxy_url else None

    if HAS_CFFI:
        try:
            with cffi_requests.Session() as s:
                kw = {"headers": headers, "timeout": timeout, "impersonate": "chrome"}
                if proxies:
                    kw["proxies"] = proxies
                if json_body and method.upper() == "POST":
                    kw["json"] = json_body
                r = s.post(url, **kw) if method.upper() == "POST" else s.get(url, **kw)
                try:
                    return r.status_code, r.json()
                except Exception:
                    return r.status_code, {"raw": (r.text or "")[:1000]}
        except Exception as e:
            return 0, {"error": f"curl_cffi: {e}"}

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


def _api_headers(access_token: str, account_id: str = None) -> dict:
    h = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}",
        "Origin": CHATGPT_ORIGIN,
        "Referer": f"{CHATGPT_ORIGIN}/",
        "User-Agent": UA,
    }
    if account_id:
        h["Chatgpt-Account-Id"] = account_id
    return h


def get_account_info(
    access_token: str,
    proxy_url: str = None,
    log_cb: Callable = None,
) -> Tuple[Optional[str], str, bool]:
    """获取账户信息, 返回 (account_id, plan_type, has_subscription)"""
    if log_cb:
        log_cb("      查询账户信息 ...")

    url = f"{CHATGPT_BACKEND_API}/accounts/check/v4-2023-04-27?timezone_offset_min=300"
    status, data = _do_request("GET", url, _api_headers(access_token), proxy_url=proxy_url)

    if status != 200:
        if log_cb:
            log_cb(f"      账户查询失败 (HTTP {status})")
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
    country: str = "KR",
    currency: str = None,
    log_cb: Callable = None,
) -> Tuple[Optional[str], str]:
    """
    AT -> checkout_session_id
    返回 (cs_id, error_message), cs_id 为 None 表示失败
    """
    at = (access_token or "").strip()
    if not at:
        return None, "Access Token 为空"

    plan_cfg = PLAN_CONFIGS.get(plan_type, PLAN_CONFIGS["plus"])
    cur = (currency or CURRENCY_MAP.get(country, "USD")).upper()

    if log_cb:
        log_cb(f"      创建 {plan_type.upper()} 结账会话 (地区: {country}) ...")

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
    status, data = _do_request("POST", url, _api_headers(at), json_body=payload, proxy_url=proxy_url)

    if status != 200:
        err = ""
        if isinstance(data, dict):
            err = data.get("detail") or data.get("error") or data.get("raw", "")
            if isinstance(err, dict):
                err = err.get("message", str(err))
        return None, f"创建结账失败 (HTTP {status}): {str(err)[:200]}"

    cs_id = (data.get("checkout_session_id") or "").strip()
    if not cs_id or not cs_id.startswith("cs_"):
        return None, f"未返回有效 checkout_session_id: {json.dumps(data, ensure_ascii=False)[:200]}"

    if log_cb:
        log_cb(f"      支付会话已创建: {cs_id[:35]}...")

    return cs_id, ""
