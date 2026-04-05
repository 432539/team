"""
黑金控台 · Codex × ChatGPT — PLUS/TEAM Web UI (批量版)
"""
import json
import os
import queue
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from flask import Flask, Response, jsonify, render_template, request

import checkout
import pay

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True


def _captcha_key() -> str:
    """从环境变量读取，勿在代码中写死密钥（便于开源与部署）。"""
    return (os.environ.get("YESCAPTCHA_API_KEY") or "").strip()


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
        return jsonify({"error": "未配置环境变量 YESCAPTCHA_API_KEY"}), 503
    data = request.json
    ats = [a.strip() for a in (data.get("ats") or "").strip().splitlines() if a.strip()]
    cards_raw = [c.strip() for c in (data.get("cards") or "").strip().splitlines() if c.strip()]

    if not ats:
        return jsonify({"error": "请输入至少一个 Access Token"}), 400
    if not cards_raw:
        return jsonify({"error": "请输入至少一条银行卡信息"}), 400

    cards = []
    for i, raw in enumerate(cards_raw):
        parts = re.split(r"[|\t,]", raw)
        if len(parts) < 4:
            return jsonify({"error": f"银行卡第 {i+1} 行格式错误, 需要: 卡号|月|年|CVC"}), 400
        cards.append({
            "number": parts[0].replace(" ", ""),
            "exp_month": parts[1].strip(),
            "exp_year": parts[2].strip(),
            "cvc": parts[3].strip(),
        })

    batch_id = uuid.uuid4().hex[:10]
    plan_type = data.get("plan_type", "plus").lower()
    proxy = data.get("proxy", "").strip()
    address = {
        "country": data.get("country", "KR"),
        "state": data.get("state", ""),
        "city": data.get("city", ""),
        "line1": data.get("line1", ""),
        "postal_code": data.get("postal_code", ""),
    }
    max_threads = min(int(data.get("threads", 3)), 10)

    task_ids = []
    for i, at in enumerate(ats):
        card = cards[i % len(cards)]
        task_id = f"{batch_id}_{i}"
        q = queue.Queue()
        at_mask = at[:8] + "..." + at[-6:] if len(at) > 20 else at[:10] + "..."
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
                card = cards[i % len(cards)]
                tid = f"{batch_id}_{i}"
                futures.append(pool.submit(
                    _run_single_task, tid, at, card, plan_type, proxy, address
                ))
            for f in futures:
                f.result()
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


def _build_proxy_url(raw: str) -> str:
    raw = raw.strip()
    if not raw:
        return ""
    if raw.startswith("http"):
        return raw
    return f"http://{raw}"


def _run_single_task(task_id, at, card, plan_type, proxy_raw, address):
    with _lock:
        task = _tasks[task_id]
        task["status"] = "running"
    q = task["queue"]
    _tls.queue = q

    proxy_url = _build_proxy_url(proxy_raw)
    proxy_cfg = _parse_proxy(proxy_raw)
    captcha_key = _captcha_key()

    try:
        _threaded_log(f"[0/6] 获取支付链接 ({plan_type.upper()}) ...")

        acct_id, plan, has_sub = checkout.get_account_info(at, proxy_url, log_cb=_threaded_log)
        if has_sub:
            _threaded_log("      该账号已有活跃订阅, 跳过")
            q.put({"type": "result", "success": True, "message": "已有订阅, 无需开通"})
            with _lock:
                task["status"] = "skipped"
                task["result"] = "已有订阅"
            return

        cs_id, err = checkout.get_checkout_session(
            at, proxy_url=proxy_url, plan_type=plan_type,
            country=address.get("country", "KR"), log_cb=_threaded_log,
        )
        if not cs_id:
            raise RuntimeError(f"获取支付链接失败: {err}")

        _threaded_log(f"      支付链接: {cs_id[:40]}...")

        config_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)), f"_cfg_{task_id}.json"
        )
        config = {
            "cards": [{
                "number": card["number"],
                "cvc": card["cvc"],
                "exp_month": card["exp_month"],
                "exp_year": card["exp_year"],
                "address": address,
            }],
            "captcha": {"api_url": "https://api.yescaptcha.com", "api_key": captcha_key},
            "locale": address.get("country", "KR"),
        }
        if proxy_cfg:
            config["proxy"] = proxy_cfg

        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f, ensure_ascii=False)

        try:
            result = pay.run(cs_id, card_index=0, config_path=config_path)
            state = result.get("state", "unknown")
            if state == "succeeded":
                q.put({"type": "result", "success": True, "message": f"{plan_type.upper()} 开通成功!"})
                with _lock:
                    task["status"] = "success"
                    task["result"] = "成功"
            else:
                q.put({"type": "result", "success": False, "message": f"支付未成功 (state={state})"})
                with _lock:
                    task["status"] = "failed"
                    task["result"] = f"state={state}"
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
        return jsonify({"error": "未配置环境变量 YESCAPTCHA_API_KEY"}), 503
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
        config = {
            "cards": [{
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
            }],
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
