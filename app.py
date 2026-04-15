"""
黑金控台 · Codex × ChatGPT — PLUS/TEAM Web UI (批量版)
"""
import json
import os
import queue
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from flask import Flask, Response, jsonify, render_template, request

import checkout
import pay

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True

_APP_DIR = os.path.dirname(os.path.abspath(__file__))
_CONFIG_JSON_PATH = os.path.join(_APP_DIR, "config.json")
_APP_SETTINGS_PATH = os.path.join(_APP_DIR, "app_settings.json")
# 缓存签名: (config.json mtime, app_settings.json mtime) -> 解析出的密钥
_captcha_key_cache: tuple[tuple[float, float], str] | tuple[None, str] = (None, "")


def _captcha_key() -> str:
    """YesCaptcha 密钥：环境变量 > 同目录 config.json(captcha.api_key) > app_settings.json。"""
    env = (os.environ.get("YESCAPTCHA_API_KEY") or "").strip()
    if env:
        return env
    global _captcha_key_cache
    try:
        mc = os.path.getmtime(_CONFIG_JSON_PATH)
    except OSError:
        mc = -1.0
    try:
        ms = os.path.getmtime(_APP_SETTINGS_PATH)
    except OSError:
        ms = -1.0
    sig = (mc, ms)
    if _captcha_key_cache[0] == sig:
        return _captcha_key_cache[1]

    key = ""
    try:
        with open(_CONFIG_JSON_PATH, "r", encoding="utf-8") as f:
            cj = json.load(f)
        if isinstance(cj, dict):
            cap = cj.get("captcha")
            if isinstance(cap, dict):
                key = (cap.get("api_key") or "").strip()
    except (OSError, json.JSONDecodeError, TypeError):
        pass

    if not key:
        try:
            with open(_APP_SETTINGS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                key = (
                    data.get("yescaptcha_api_key") or data.get("YESCAPTCHA_API_KEY") or ""
                ).strip()
        except (OSError, json.JSONDecodeError, TypeError):
            pass

    _captcha_key_cache = (sig, key)
    return key


# --- 任务管理 ---
_tasks = {}       # task_id → {queue, status, at_mask, card_mask, result}
_batches = {}     # batch_id → {task_ids, plan, status}
_lock = threading.Lock()
_tls = threading.local()


def _threaded_log(msg: str):
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    q = getattr(_tls, "queue", None)
    if q:
        try:
            q.put({"type": "log", "message": line})
        except Exception:
            pass

pay._log = _threaded_log


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/batch", methods=["POST"])
def batch_start():
    if not _captcha_key():
        return jsonify({
            "error": "未配置 YesCaptcha：在同目录 config.json 的 captcha.api_key、或 app_settings.json 的 yescaptcha_api_key、或环境变量 YESCAPTCHA_API_KEY",
        }), 503
    data = request.json
    ats = [a.strip() for a in (data.get("ats") or "").strip().splitlines() if a.strip()]
    cards_raw = [c.strip() for c in (data.get("cards") or "").strip().splitlines() if c.strip()]

    if not ats:
        return jsonify({"error": "请输入至少一行：Access Token 或支付链接"}), 400
    for i, ln in enumerate(ats):
        if not _line_is_valid_batch_input(ln):
            return jsonify({
                "error": (
                    f"第 {i + 1} 行格式无效：应为支付链接 / cs_live_… / cs_test_…，"
                    f"或长度足够的 Access Token（JWT）"
                ),
            }), 400
    if not cards_raw:
        return jsonify({"error": "请输入至少一条银行卡信息"}), 400

    holder_global = (data.get("cardholder_name") or "").strip()
    cards = []
    for i, raw in enumerate(cards_raw):
        parts = re.split(r"[|\t,]", raw)
        if len(parts) < 4:
            return jsonify({
                "error": f"银行卡第 {i+1} 行格式错误, 需要: 卡号|月|年|CVC (可选第五段姓名覆盖该行)",
            }), 400
        entry = {
            "number": parts[0].replace(" ", ""),
            "exp_month": parts[1].strip(),
            "exp_year": parts[2].strip(),
            "cvc": parts[3].strip(),
        }
        if len(parts) >= 5 and parts[4].strip():
            entry["name"] = parts[4].strip()
        elif holder_global:
            entry["name"] = holder_global
        cards.append(entry)

    batch_id = uuid.uuid4().hex[:10]
    plan_type = data.get("plan_type", "plus").lower()
    proxy_raw = (data.get("proxy") or "").strip()
    # 支持多个代理轮询: 一行一个，或使用英文逗号分隔。
    proxy_pool = [p.strip() for p in re.split(r"[\r\n,，;；]+", proxy_raw) if p.strip()]
    address = {
        "country": data.get("country", "US"),
        "state": data.get("state", ""),
        "city": data.get("city", ""),
        "line1": data.get("line1", ""),
        "postal_code": data.get("postal_code", ""),
    }
    sms_poll_url = (data.get("sms_poll_url") or "").strip()
    sms_phone = (data.get("sms_phone") or "").strip()
    _mo = data.get("manual_otp")
    manual_otp = _mo is True or (
        isinstance(_mo, str) and _mo.lower() in ("1", "true", "on", "yes")
    )
    sms_otp_batch = (
        {
            "phone": sms_phone,
            "poll_url": sms_poll_url,
            "manual_otp": manual_otp,
        }
        if manual_otp or sms_poll_url
        else None
    )
    # 并发范围: 1~20。前端可传任意值，后端统一夹紧避免异常。
    try:
        req_threads = int(data.get("threads", 3))
    except Exception:
        req_threads = 3
    max_threads = max(1, min(req_threads, 20))

    task_ids = []
    for i, at in enumerate(ats):
        # 卡数量不足时循环复用; 卡数量充足时按序一一对应。
        card = cards[i % len(cards)]
        task_id = f"{batch_id}_{i}"
        q = queue.Queue()
        at_mask = _mask_token_or_link(at)
        card_mask = "****" + card["number"][-4:] if len(card["number"]) >= 4 else "****"

        with _lock:
            _tasks[task_id] = {
                "queue": q, "status": "pending",
                "at_mask": at_mask, "card_mask": card_mask, "result": None,
            }
        task_ids.append({
            "id": task_id, "at_mask": at_mask, "card_mask": card_mask,
        })

    with _lock:
        _batches[batch_id] = {"task_ids": [t["id"] for t in task_ids], "plan": plan_type, "status": "running"}

    def run_batch():
        with ThreadPoolExecutor(max_workers=max_threads) as pool:
            futures = []
            for i, at in enumerate(ats):
                # 每个 AT 只执行一次。失败不会中断其它任务，批次会继续跑完。
                card = cards[i % len(cards)]
                tid = f"{batch_id}_{i}"
                futures.append(pool.submit(
                    _run_single_task,
                    tid,
                    at,
                    card,
                    plan_type,
                    (proxy_pool[i % len(proxy_pool)] if proxy_pool else ""),
                    address,
                    sms_otp_batch,
                ))
            for f in futures:
                # 任一任务异常不影响其它任务继续执行
                try:
                    f.result()
                except Exception as e:
                    print(f"[batch] task future error ignored: {e}", flush=True)
        with _lock:
            _batches[batch_id]["status"] = "done"

    threading.Thread(target=run_batch, daemon=True).start()

    return jsonify({"batch_id": batch_id, "tasks": task_ids})


@app.route("/api/stream/<task_id>")
def stream(task_id):
    with _lock:
        task = _tasks.get(task_id)
    if not task:
        return "Task not found", 404
    q = task["queue"]

    def gen():
        while True:
            try:
                msg = q.get(timeout=30)
                if msg is None:
                    yield f"data: {json.dumps({'type': 'done'}, ensure_ascii=False)}\n\n"
                    break
                yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"
            except queue.Empty:
                yield f"data: {json.dumps({'type': 'ping'})}\n\n"

    return Response(
        gen(),
        content_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/batch-status/<batch_id>")
def batch_status(batch_id):
    with _lock:
        batch = _batches.get(batch_id)
    if not batch:
        return jsonify({"error": "not found"}), 404

    statuses = []
    with _lock:
        for tid in batch["task_ids"]:
            t = _tasks.get(tid, {})
            statuses.append({
                "id": tid,
                "status": t.get("status", "unknown"),
                "at_mask": t.get("at_mask", ""),
                "card_mask": t.get("card_mask", ""),
                "result": t.get("result"),
            })
    return jsonify({"batch_status": batch["status"], "tasks": statuses})


def _parse_proxy(raw: str) -> dict | None:
    raw = raw.strip()
    if not raw:
        return None
    m = re.match(r"(?:https?://)?(?:([^:]+):([^@]+)@)?([^:]+):(\d+)", raw)
    if not m:
        return None
    return {"host": m.group(3), "port": m.group(4),
            "user": m.group(1) or "", "pass": m.group(2) or ""}


def _is_pay_checkout_input(s: str) -> bool:
    """是否为支付链接或可解析的 Checkout Session ID（非 Access Token 流程）。"""
    s = (s or "").strip()
    if not s:
        return False
    low = s.lower()
    if s.startswith("cs_live_") or s.startswith("cs_test_"):
        return True
    if "pay.openai.com" in low or "/c/pay/" in low:
        return True
    if "checkout.stripe.com" in low:
        return True
    if "chatgpt.com" in low and (
        "checkout" in low or "/pay/" in low or "cs_live_" in s or "cs_test_" in s
    ):
        return True
    return False


def _line_is_valid_batch_input(s: str) -> bool:
    s = (s or "").strip()
    if not s:
        return False
    if _is_pay_checkout_input(s):
        return True
    # Access Token（JWT 等）通常较长
    if len(s) >= 80:
        return True
    return False


def _mask_token_or_link(line: str) -> str:
    line = (line or "").strip()
    if not line:
        return "?"
    if _is_pay_checkout_input(line):
        m = re.search(r"(cs_(?:live|test)_[A-Za-z0-9]+)", line)
        if m:
            cid = m.group(1)
            return cid[:12] + "…" + cid[-6:] if len(cid) > 20 else cid
        return (line[:40] + "…") if len(line) > 40 else line
    if len(line) > 20:
        return line[:8] + "…" + line[-6:]
    return line[:12] + "…"


def _write_task_config(
    task_id: str,
    card: dict,
    address: dict,
    captcha_key: str,
    proxy_cfg,
    sms_otp: dict | None = None,
) -> str:
    """写入单任务 config.json，返回路径。"""
    config_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), f"_cfg_{task_id}.json"
    )
    country = address.get("country", "US")
    config = {
        "cards": [{
            "number": card["number"],
            "cvc": card["cvc"],
            "exp_month": card["exp_month"],
            "exp_year": card["exp_year"],
            **({"name": card["name"]} if (card.get("name") or "").strip() else {}),
            **({"email": card["email"]} if (card.get("email") or "").strip() else {}),
            "address": address,
        }],
        "captcha": {"api_url": "https://api.yescaptcha.com", "api_key": captcha_key},
        "locale": country,
    }
    if proxy_cfg:
        config["proxy"] = proxy_cfg
    so = sms_otp if isinstance(sms_otp, dict) else {}
    poll = (so.get("poll_url") or "").strip()
    manual = bool(so.get("manual_otp"))
    if manual or poll:
        config["sms_otp"] = {
            "phone": (so.get("phone") or "").strip(),
            "poll_url": poll,
            "manual_otp": manual,
        }
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False)
    return config_path


def _run_single_task(task_id, at, card, plan_type, proxy_raw, address, sms_otp=None):
    with _lock:
        task = _tasks[task_id]
        task["status"] = "running"
    q = task["queue"]
    _tls.queue = q

    proxy_cfg = _parse_proxy(proxy_raw)
    captcha_key = _captcha_key()

    try:
        if _is_pay_checkout_input(at):
            pay_input = at.strip()
            config_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), f"_cfg_{task_id}.json"
            )
            _threaded_log(
                "[0/6] 支付链接 / Session ID：跳过账号与建链；无 AT 时不调用 ChatGPT approve …"
            )
            try:
                result = pay.run(
                    pay_input,
                    card_index=0,
                    config_path=config_path,
                    access_token="",
                )
                state = result.get("state", "unknown")
                if state == "succeeded":
                    _threaded_log("  ✓ 支付流程成功!")
                    q.put({"type": "result", "success": True, "message": "支付成功!"})
                    with _lock:
                        task["status"] = "success"
                        task["result"] = "成功"
                else:
                    msg = f"支付未成功 (state={state})"
                    _threaded_log(f"      {msg}")
                    q.put({"type": "result", "success": False, "message": msg})
                    with _lock:
                        task["status"] = "failed"
                        task["result"] = msg[:80]
            except Exception as ex:
                _threaded_log(f"[错误] {ex}")
                q.put({"type": "result", "success": False, "message": str(ex)})
                with _lock:
                    task["status"] = "failed"
                    task["result"] = str(ex)[:80]
            finally:
                try:
                    os.remove(config_path)
                except OSError:
                    pass
            return

        with checkout.session_for_checkout() as ch_sess:
            _threaded_log(f"[0/6] 获取支付链接 (界面选择: {plan_type.upper()}) ...")

            acct_id, plan, has_sub = checkout.get_account_info(
                at, log_cb=_threaded_log, http_session=ch_sess
            )
            if has_sub:
                _threaded_log("      该账号已有活跃订阅, 跳过")
                q.put({"type": "result", "success": True, "message": "已有订阅, 无需开通"})
                with _lock:
                    task["status"] = "skipped"
                    task["result"] = "已有订阅"
                return

            # 建链统一用 SG 地区，与 checkout 内置代理一致
            checkout_country = "SG"
            # 账单地址国家仍取用户填写的值
            billing_country = address.get("country", "US")

            # 策略: PRO 预拒卡 × 2 → TEAM 正式开通
            # 前两轮 PRO ($200) 预期被拒（触发 Stripe 风控缓存），
            # 第三轮 TEAM 使用用户代理+账单地址完成支付。
            chain = ["pro", "pro", "team"]
            PRO_WARMUP_COUNT = 2  # 前 N 轮为 PRO 预拒卡阶段

            config_path = _write_task_config(
                task_id, card, address, captcha_key, proxy_cfg, sms_otp
            )

            last_err = ""
            any_pay_attempt = False
            try:
                for step_idx, p in enumerate(chain):
                    is_warmup = (p == "pro" and step_idx < PRO_WARMUP_COUNT)

                    if is_warmup:
                        _threaded_log(
                            f"====== 第 {step_idx + 1}/{len(chain)} 轮 · "
                            f"PRO 预拒卡 ({step_idx + 1}/{PRO_WARMUP_COUNT}) ======"
                        )
                    else:
                        _threaded_log(
                            f"====== 第 {step_idx + 1}/{len(chain)} 轮 · "
                            f"{p.upper()} 正式开通 ======"
                        )

                    # TEAM: 通过 aimizy 外部 API 获取支付链接
                    # PRO: 通过 ChatGPT backend-api 自行建链
                    if p == "team":
                        pay_url, cs_id, err = checkout.generate_team_payment_link_via_aimizy(
                            at, country=checkout_country,
                            log_cb=_threaded_log,
                        )
                        if not cs_id:
                            last_err = err or ""
                            _threaded_log(f"      TEAM 未拿到会话: {last_err[:180]}")
                            continue
                        pay_input = pay_url if pay_url else cs_id
                        _threaded_log(f"      TEAM 会话: {cs_id[:44]}…")
                    else:
                        cs_id, err = checkout.get_checkout_session(
                            at, plan_type=p, country=checkout_country,
                            log_cb=_threaded_log, account_id=acct_id,
                            quiet=True, http_session=ch_sess,
                        )
                        if not cs_id:
                            last_err = err or ""
                            _threaded_log(f"      {p.upper()} 未拿到会话: {last_err[:180]}")
                            continue

                        long_pay, _ = checkout.pay_openai_url_with_stripe_fragment(
                            cs_id, http_session=ch_sess
                        )
                        pay_input = long_pay if long_pay else cs_id
                        _threaded_log(f"      {p.upper()} 会话: {cs_id[:44]}…")

                    # 代理策略：
                    # - PRO 预拒卡: 使用内置 SG 代理
                    # - TEAM 正式开通: 使用用户填写的代理（匹配卡地址）
                    if is_warmup:
                        run_proxy_cfg = _parse_proxy(checkout.effective_checkout_proxy() or "")
                        _threaded_log("      PRO 预拒卡代理: 内置 SG 线路")
                    else:
                        run_proxy_cfg = proxy_cfg
                        if run_proxy_cfg:
                            _threaded_log("      TEAM 支付代理: 使用网页填写代理")
                        else:
                            _threaded_log("      TEAM 支付代理: 未填写，走直连")

                    _write_task_config(
                        task_id, card, address, captcha_key, run_proxy_cfg, sms_otp
                    )

                    any_pay_attempt = True
                    try:
                        result = pay.run(pay_input, card_index=0,
                                         config_path=config_path, access_token=at)
                    except Exception as ex:
                        last_err = str(ex)
                        if is_warmup:
                            _threaded_log(f"      PRO 预拒卡结果: {ex}")
                        else:
                            _threaded_log(f"      [错误] {ex}")
                        if step_idx < len(chain) - 1:
                            if is_warmup:
                                wait_sec = 3
                                _threaded_log(f"      预拒卡完成, 等待 {wait_sec}s 后继续 …")
                                time.sleep(wait_sec)
                            else:
                                _threaded_log("      本轮未成功, 换下一档位 …")
                        continue

                    state = result.get("state", "unknown")
                    if state == "succeeded":
                        _threaded_log(f"  ✓ {p.upper()} 开通成功!")
                        q.put({"type": "result", "success": True,
                               "message": f"{p.upper()} 开通成功!"})
                        with _lock:
                            task["status"] = "success"
                            task["result"] = "成功"
                        return

                    last_err = f"支付未成功 (state={state})"
                    if is_warmup:
                        _threaded_log(f"      PRO 预拒卡结果: {last_err} (符合预期)")
                        if step_idx < len(chain) - 1:
                            wait_sec = 3
                            _threaded_log(f"      等待 {wait_sec}s 后继续 …")
                            time.sleep(wait_sec)
                    else:
                        _threaded_log(f"      {last_err}")
                        if step_idx < len(chain) - 1:
                            _threaded_log("      换下一档位 …")

                if not any_pay_attempt:
                    _threaded_log("      所有档位均未拿到结账会话")
                    q.put({"type": "result", "success": False,
                           "message": "无法创建支付会话, 请稍后或更换账号重试"})
                    with _lock:
                        task["status"] = "failed"
                        task["result"] = "结账受限"
                    return

                q.put({"type": "result", "success": False,
                       "message": last_err or "支付未成功"})
                with _lock:
                    task["status"] = "failed"
                    task["result"] = (last_err or "失败")[:80]
            finally:
                try:
                    os.remove(config_path)
                except Exception:
                    pass

    except Exception as e:
        _threaded_log(f"[错误] {e}")
        q.put({"type": "result", "success": False, "message": str(e)})
        with _lock:
            task["status"] = "failed"
            task["result"] = str(e)[:80]
    finally:
        _tls.queue = None
        q.put(None)


@app.route("/api/start", methods=["POST"])
def start_single():
    """兼容单任务模式"""
    if not _captcha_key():
        return jsonify({
            "error": "未配置 YesCaptcha：在同目录 config.json 的 captcha.api_key、或 app_settings.json 的 yescaptcha_api_key、或环境变量 YESCAPTCHA_API_KEY",
        }), 503
    data = request.json
    task_id = uuid.uuid4().hex[:10]
    q = queue.Queue()
    with _lock:
        _tasks[task_id] = {"queue": q, "status": "running", "at_mask": "", "card_mask": "", "result": None}

    def run():
        _tls.queue = q
        proxy_cfg = _parse_proxy(data.get("proxy", ""))
        captcha_key = _captcha_key()
        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"_cfg_{task_id}.json")
        card0 = {
            "number": data["card_number"].replace(" ", ""),
            "cvc": data["cvc"],
            "exp_month": data["exp_month"],
            "exp_year": data["exp_year"],
            "address": {
                "country": data.get("country", "US"),
                "line1": data.get("line1", ""),
                "city": data.get("city", ""),
                "state": data.get("state", ""),
                "postal_code": data.get("postal_code", ""),
            },
        }
        _cn = (data.get("cardholder_name") or data.get("name") or "").strip()
        if _cn:
            card0["name"] = _cn
        config = {
            "cards": [card0],
            "captcha": {"api_url": "https://api.yescaptcha.com", "api_key": captcha_key},
            "locale": data.get("country", "US"),
        }
        if proxy_cfg:
            config["proxy"] = proxy_cfg
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False)
        try:
            result = pay.run(data["checkout_url"], card_index=0, config_path=config_path)
            state = result.get("state", "unknown")
            if state == "succeeded":
                q.put({"type": "result", "success": True, "message": "支付成功!"})
            else:
                q.put({"type": "result", "success": False, "message": f"支付未成功 (state={state})"})
        except Exception as e:
            q.put({"type": "result", "success": False, "message": str(e)})
        finally:
            _tls.queue = None
            q.put(None)
            try:
                os.remove(config_path)
            except Exception:
                pass

    threading.Thread(target=run, daemon=True).start()
    return jsonify({"task_id": task_id})


if __name__ == "__main__":
    print("\n  黑金控台 · Codex × ChatGPT — PLUS/TEAM (批量版)")
    print("  http://localhost:5080\n")
    app.run(host="0.0.0.0", port=5080, threaded=True)
