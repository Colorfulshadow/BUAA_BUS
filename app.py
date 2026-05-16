"""
BUAA Bus Monitor — Flask Web Application
"""

import json
import logging
import threading
import time
import urllib.parse
from datetime import date
from pathlib import Path

import requests
from flask import Flask, jsonify, redirect, render_template, request, session

from buaa_auth import BusSession, login

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = "buaa-bus-monitor-2024-xK9mP3qR"

DATA_FILE = Path("./users_data.json")
# Public base URL for Bark notification links (set to your server's address)
PUBLIC_BASE_URL = "http://localhost:5000"

# In-memory monitor registry  {student_id: {task_id: MonitorTask}}
_monitors: dict[str, dict[str, "MonitorTask"]] = {}
_monitor_lock = threading.Lock()


# ── Persistent user store ─────────────────────────────────────

def _load() -> dict:
    if DATA_FILE.exists():
        try:
            return json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _dump(data: dict):
    DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def get_user(sid: str) -> dict:
    return _load().get(sid, {})


def patch_user(sid: str, updates: dict):
    data = _load()
    data.setdefault(sid, {}).update(updates)
    _dump(data)


# ── Monitor task ──────────────────────────────────────────────

BUS_TICKET_PAGE = "http://zhihuixiaoche.buaa.edu.cn/wechat/ticketInfoPage"


class MonitorTask:
    def __init__(self, task_id: str, student_id: str, schedule: dict,
                 bark_url: str, bus_session: BusSession, auto_buy: bool = False):
        self.task_id = task_id
        self.student_id = student_id
        self.schedule = schedule
        self.bark_url = bark_url
        self.bus_session = bus_session
        self.auto_buy = auto_buy
        self.active = True
        self.status = "monitoring"   # monitoring | found | error | stopped
        self.last_check: dict | None = None
        self._thread: threading.Thread | None = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self.active = False
        self.status = "stopped"

    def _run(self):
        origin = self.schedule.get("up_origin_name", "")
        dest = self.schedule.get("up_terminal_name", "")
        depart = self.schedule.get("depart_time", "")
        date_str = self.schedule.get("shifts_date", date.today().strftime("%Y-%m-%d"))
        consecutive_errors = 0

        while self.active:
            try:
                result = self.bus_session.get_schedules(origin, dest, date_str)
                if result.get("success"):
                    consecutive_errors = 0
                    target = next(
                        (s for s in result.get("list", []) if s.get("depart_time") == depart),
                        None,
                    )
                    if target:
                        avail = (
                            target.get("open_seat_num", 0)
                            - target.get("student_num", 0)
                            - target.get("teacher_num", 0)
                        )
                        self.last_check = {
                            "time": time.strftime("%H:%M:%S"),
                            "available": avail,
                            "total": target.get("open_seat_num", 0),
                            "students": target.get("student_num", 0),
                            "teachers": target.get("teacher_num", 0),
                        }
                        if avail > 0:
                            pay_url = None
                            if self.auto_buy:
                                pay_url = (
                                    f"{PUBLIC_BASE_URL}/buy"
                                    f"/{urllib.parse.quote(self.student_id)}"
                                    f"/{urllib.parse.quote(self.task_id)}"
                                )
                            self._bark(
                                "校车有空位啦！",
                                f"{depart} 班次 ({origin}→{dest}) 有 {avail} 个空位！"
                                + (" 点击立即购票！" if pay_url else ""),
                                url=pay_url,
                            )
                            self.status = "found"
                            self.active = False
                            break
                else:
                    consecutive_errors += 1
                    if consecutive_errors >= 6:
                        self.status = "error"
                        self._bark("校车监控警告", f"{depart} 班次连续 {consecutive_errors} 次检查失败")
                        consecutive_errors = 0
            except Exception as e:
                logger.error("Monitor %s error: %s", self.task_id, e)
                consecutive_errors += 1

            time.sleep(10)

    def _bark(self, title: str, body: str, url: str | None = None):
        if not self.bark_url:
            return
        base = self.bark_url.rstrip("/")
        if not base.startswith("http"):
            base = f"https://api.day.app/{base}"
        notify_url = f"{base}/{urllib.parse.quote(title)}/{urllib.parse.quote(body)}"
        if url:
            notify_url += f"?url={urllib.parse.quote(url, safe='')}"
        try:
            requests.post(notify_url, timeout=10)
        except Exception as e:
            logger.warning("Bark notification failed: %s", e)

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "schedule": self.schedule,
            "active": self.active,
            "status": self.status,
            "last_check": self.last_check,
            "auto_buy": self.auto_buy,
        }


# ── Auth helpers ──────────────────────────────────────────────

def _require_login():
    if "student_id" not in session:
        return jsonify({"success": False, "message": "未登录", "code": 401}), 401
    return None


def _get_bus_session() -> BusSession:
    return BusSession.from_dict(session["bus_session"])


# ── Routes ────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/me")
def api_me():
    sid = session.get("student_id")
    if not sid:
        return jsonify({"logged_in": False})
    user = get_user(sid)
    return jsonify({"logged_in": True, "student_id": sid, "bark_url": user.get("bark_url", "")})


@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.json or {}
    sid = data.get("student_id", "").strip()
    pwd = data.get("password", "").strip()

    if not sid or not pwd:
        return jsonify({"success": False, "message": "请输入学号和密码"})

    ok, msg, bus_session = login(sid, pwd)
    if not ok:
        return jsonify({"success": False, "message": msg})

    session["student_id"] = sid
    session["bus_session"] = bus_session.to_dict()

    user = get_user(sid)
    return jsonify({"success": True, "message": msg, "bark_url": user.get("bark_url", "")})


@app.route("/api/logout", methods=["POST"])
def api_logout():
    # Monitors keep running after logout; they hold their own BusSession copy
    session.clear()
    return jsonify({"success": True})


@app.route("/api/schedules")
def api_schedules():
    err = _require_login()
    if err:
        return err

    origin = request.args.get("origin", "学院路")
    destination = request.args.get("destination", "沙河")
    shifts_date = request.args.get("date", date.today().strftime("%Y-%m-%d"))

    result = _get_bus_session().get_schedules(origin, destination, shifts_date)

    # Surface expired-session errors
    if not result.get("success") and result.get("code") in (401, 403):
        return jsonify({"success": False, "message": "会话已过期，请重新登录", "code": 401}), 401

    return jsonify(result)


@app.route("/api/bark_url", methods=["POST"])
def api_bark_url():
    err = _require_login()
    if err:
        return err

    bark_url = (request.json or {}).get("bark_url", "").strip()
    patch_user(session["student_id"], {"bark_url": bark_url})
    return jsonify({"success": True, "message": "Bark URL 已保存"})


@app.route("/api/monitor/start", methods=["POST"])
def api_monitor_start():
    err = _require_login()
    if err:
        return err

    data = request.json or {}
    schedule = data.get("schedule", {})
    bark_url = data.get("bark_url", "").strip() or get_user(session["student_id"]).get("bark_url", "")
    auto_buy = bool(data.get("auto_buy", False))
    sid = session["student_id"]

    if not bark_url:
        return jsonify({"success": False, "message": "请先在设置中配置 Bark 通知 URL"})

    task_id = "{shifts_number}_{shifts_date}_{depart_time}".format(**{
        "shifts_number": schedule.get("shifts_number", ""),
        "shifts_date": schedule.get("shifts_date", ""),
        "depart_time": schedule.get("depart_time", ""),
    })

    with _monitor_lock:
        user_tasks = _monitors.setdefault(sid, {})
        existing = user_tasks.get(task_id)
        if existing and existing.active:
            return jsonify({"success": False, "message": "该班次已在监控中"})

        task = MonitorTask(task_id, sid, schedule, bark_url, _get_bus_session(), auto_buy=auto_buy)
        task.start()
        user_tasks[task_id] = task

    suffix = "（发现空位将推送购票链接）" if auto_buy else ""
    return jsonify({
        "success": True,
        "task_id": task_id,
        "message": (
            f"开始监控 {schedule.get('depart_time')} 班次 "
            f"({schedule.get('up_origin_name')}→{schedule.get('up_terminal_name')}){suffix}"
        ),
    })


@app.route("/api/monitor/stop", methods=["POST"])
def api_monitor_stop():
    err = _require_login()
    if err:
        return err

    task_id = (request.json or {}).get("task_id", "")
    sid = session["student_id"]

    with _monitor_lock:
        task = _monitors.get(sid, {}).get(task_id)
        if task:
            task.stop()
            return jsonify({"success": True})

    return jsonify({"success": False, "message": "监控任务不存在"})


@app.route("/api/monitor/list")
def api_monitor_list():
    err = _require_login()
    if err:
        return err

    sid = session["student_id"]
    with _monitor_lock:
        tasks = [t.to_dict() for t in _monitors.get(sid, {}).values()]

    return jsonify({"success": True, "tasks": tasks})


@app.route("/buy/<student_id>/<task_id>")
def buy_redirect(student_id: str, task_id: str):
    """
    Redirect link embedded in Bark notifications.
    Opens ticketInfoPage in whatever browser the user taps from (WeChat, Safari, etc.).
    The link carries no session — it lands on the BUAA bus site which the user
    must already be logged into via WeChat/browser, or they log in there.
    """
    with _monitor_lock:
        task = _monitors.get(student_id, {}).get(task_id)

    if not task:
        return "监控任务不存在或已结束", 404

    shifts_number = task.schedule.get("shifts_number", "")
    shifts_date = task.schedule.get("shifts_date", "")
    target = (
        f"{BUS_TICKET_PAGE}"
        f"?shifts_number={urllib.parse.quote(str(shifts_number))}"
        f"&shifts_date={urllib.parse.quote(shifts_date)}"
    )
    return redirect(target)


if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=5000, threaded=True)
