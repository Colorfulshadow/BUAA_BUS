"""
BUAA Bus System Authentication
Handles SSO login and authenticated API calls.
"""

import re
import time
import logging
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

SSO_LOGIN_URL = "https://sso.buaa.edu.cn/login"
BUS_CAS_SERVICE_URL = "http://zhihuixiaoche.buaa.edu.cn/wechat/CASLogin"
BUS_INDEX_URL = "http://zhihuixiaoche.buaa.edu.cn/wechat/indexPage"
BUS_SHIFTS_URL = "http://zhihuixiaoche.buaa.edu.cn/wechat/ShiftsSearch"

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 18_7 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Mobile/15E148 MicroMessenger/8.0.65(0x18004130) "
    "NetType/WIFI Language/zh_CN"
)


class BusSession:
    """Authenticated session for the BUAA bus system."""

    def __init__(self, cookies: dict, csrf_token: str):
        self.cookies = cookies
        self.csrf_token = csrf_token

    def _new_session(self) -> requests.Session:
        s = requests.Session()
        s.headers.update({"User-Agent": MOBILE_UA})
        for name, value in self.cookies.items():
            s.cookies.set(name, value)
        return s

    def get_schedules(self, origin: str, destination: str, shifts_date: str) -> dict:
        s = self._new_session()
        try:
            r = s.post(
                BUS_SHIFTS_URL,
                headers={
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                    "X-Requested-With": "XMLHttpRequest",
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                    "Origin": "http://zhihuixiaoche.buaa.edu.cn",
                    "Referer": BUS_INDEX_URL,
                    "X-CSRF-TOKEN": self.csrf_token,
                },
                data={
                    "up_origin_name": origin,
                    "up_terminal_name": destination,
                    "shifts_date": shifts_date,
                    "act": "search",
                },
                timeout=15,
            )
            if r.status_code == 200:
                return r.json()
            return {"success": False, "message": f"HTTP {r.status_code}", "code": r.status_code}
        except requests.exceptions.Timeout:
            return {"success": False, "message": "请求超时"}
        except Exception as e:
            return {"success": False, "message": str(e)}

    def to_dict(self) -> dict:
        return {"cookies": self.cookies, "csrf_token": self.csrf_token}

    @classmethod
    def from_dict(cls, data: dict) -> "BusSession":
        return cls(data["cookies"], data["csrf_token"])


def login(username: str, password: str) -> tuple[bool, str, "BusSession | None"]:
    """
    Log in via BUAA SSO and return an authenticated BusSession.
    Returns (success, message, session_or_none).
    """
    s = requests.Session()
    s.headers.update({"User-Agent": MOBILE_UA})

    try:
        # 1. Fetch SSO login page to get execution token
        r = s.get(
            SSO_LOGIN_URL,
            params={"service": BUS_CAS_SERVICE_URL},
            allow_redirects=True,
            timeout=15,
        )
        soup = BeautifulSoup(r.text, "html.parser")
        exec_input = soup.find("input", {"name": "execution"})
        if not exec_input:
            return False, "无法获取 SSO 登录参数，请稍后重试", None
        execution = exec_input.get("value", "")

        # 2. Submit credentials
        r2 = s.post(
            SSO_LOGIN_URL,
            data={
                "username": username,
                "password": password,
                "submit": "登录",
                "type": "username_password",
                "execution": execution,
                "_eventId": "submit",
            },
            headers={"Referer": r.url},
            allow_redirects=False,
            timeout=15,
        )

        # Handle weak-password warning page (HTTP 401)
        if r2.status_code == 401:
            soup = BeautifulSoup(r2.text, "html.parser")
            form = soup.find("form", {"id": "continueForm"})
            if form:
                exec_v = form.find("input", {"name": "execution"})
                if exec_v:
                    time.sleep(6)
                    r2 = s.post(
                        SSO_LOGIN_URL,
                        data={"execution": exec_v.get("value"), "_eventId": "ignoreAndContinue"},
                        headers={"Referer": SSO_LOGIN_URL},
                        allow_redirects=False,
                        timeout=15,
                    )

        # If still on SSO page (login error)
        if r2.status_code == 200:
            soup = BeautifulSoup(r2.text, "html.parser")
            err = soup.find(class_="error-msg") or soup.find(id="errormsghide")
            msg = err.get_text(strip=True) if err else "用户名或密码错误"
            return False, msg, None

        if r2.status_code not in (301, 302, 303, 307, 308):
            return False, f"登录失败 (HTTP {r2.status_code})", None

        # 3. Follow redirect to bus CASLogin → indexPage
        ticket_url = r2.headers.get("Location", "")
        r3 = s.get(ticket_url, allow_redirects=True, timeout=15)

        if "zhihuixiaoche.buaa.edu.cn" not in r3.url:
            return False, "登录失败，跳转至意外页面", None

        # 4. Extract CSRF token embedded in page JS
        csrf_match = re.search(r"var csrf_token='([^']+)'", r3.text)
        if not csrf_match:
            return False, "无法获取 CSRF Token，请重试", None
        csrf_token = csrf_match.group(1)

        cookies = {c.name: c.value for c in s.cookies}
        return True, "登录成功", BusSession(cookies, csrf_token)

    except requests.exceptions.Timeout:
        return False, "网络超时，请检查网络连接", None
    except requests.exceptions.ConnectionError:
        return False, "网络连接失败，请检查网络", None
    except Exception as e:
        logger.exception("Login error")
        return False, f"登录异常: {e}", None
