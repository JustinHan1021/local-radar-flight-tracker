"""Web 服务端配置。

与桌面版的 :mod:`app.config` 刻意分开：那边是**用户偏好**（半径、数据源、显示开关、
定位），这边是**服务端运行参数**（监听地址、口令哈希、代理信任、限流阈值）。
两者落在同一个数据根目录下，但分文件存放——一份配置里混着"界面偏好"和
"监听地址 + 口令哈希"迟早会出问题（比如把整份配置回给前端时把密钥带出去）。
"""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass

from app.config import PROJECT_ROOT

#: 服务端配置文件，与桌面版 ``config.json`` 并列。
SERVER_CONFIG_PATH = PROJECT_ROOT / "webapp_config.json"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080
DEFAULT_SESSION_TTL = 30 * 24 * 3600
DEFAULT_RATE_LIMIT_PER_MIN = 240
DEFAULT_LOGIN_MAX_FAILURES = 5
DEFAULT_LOGIN_LOCKOUT = 30.0

#: 会话 Cookie 名。前缀 ``__Host-`` 要求 Secure + Path=/ + 无 Domain，
#: 但那在纯 HTTP（本地调试）下会被浏览器拒绝，故只用普通名字 + 显式属性。
SESSION_COOKIE = "frt_session"

#: 写操作必须带的自定义头。Cookie 鉴权天然有 CSRF 风险，
#: 而跨站表单无法自定义请求头，所以要求它即可挡住（再叠加 SameSite=Strict）。
CSRF_HEADER = "X-Requested-With"
CSRF_VALUE = "local-radar"

#: 底图允许请求的叠加图层。放在这里而不是 ``webapp/render.py``：
#: 参数白名单属于 HTTP 层的职责，而且**必须能在渲染模块缺席时照常工作**——
#: 否则后端没就绪时，畸形请求会拿到 503 而不是它应得的 400。
BASEMAP_LAYERS = ("terrain", "roads", "clouds", "places", "airports")


def env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class ServerSettings:
    """服务端运行参数。"""

    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    #: 是否采信 X-Forwarded-For / X-Forwarded-Proto。
    #: 前面没有可信反代时必须为 False，否则客户端可以伪造 IP 绕过限流与登录锁定。
    trust_proxy: bool = False
    session_ttl: int = DEFAULT_SESSION_TTL
    rate_limit_per_min: int = DEFAULT_RATE_LIMIT_PER_MIN
    login_max_failures: int = DEFAULT_LOGIN_MAX_FAILURES
    login_lockout: float = DEFAULT_LOGIN_LOCKOUT
    password_hash: str = ""
    #: 首次启动自动生成的口令。仅用于在控制台打印一次，不写盘、不回前端。
    bootstrap_password: str | None = None

    @classmethod
    def load(cls) -> "ServerSettings":
        raw: dict = {}
        if SERVER_CONFIG_PATH.exists():
            try:
                loaded = json.loads(SERVER_CONFIG_PATH.read_text("utf-8"))
                if isinstance(loaded, dict):
                    raw = loaded
            except (OSError, ValueError):
                # 配置损坏不该让服务起不来：退回默认值并重建
                raw = {}

        settings = cls(
            host=os.environ.get("FRT_HOST", str(raw.get("host", DEFAULT_HOST))),
            port=int(os.environ.get("FRT_PORT", raw.get("port", DEFAULT_PORT))),
            trust_proxy=env_flag("FRT_TRUST_PROXY", bool(raw.get("trust_proxy", False))),
            session_ttl=int(raw.get("session_ttl", DEFAULT_SESSION_TTL)),
            rate_limit_per_min=int(
                raw.get("rate_limit_per_min", DEFAULT_RATE_LIMIT_PER_MIN)
            ),
            login_max_failures=int(
                raw.get("login_max_failures", DEFAULT_LOGIN_MAX_FAILURES)
            ),
            login_lockout=float(raw.get("login_lockout", DEFAULT_LOGIN_LOCKOUT)),
            password_hash=str(raw.get("password_hash", "")),
        )
        settings._resolve_password()
        return settings

    def _resolve_password(self) -> None:
        """确定生效的口令哈希，优先级：环境变量 > 配置文件 > 现场生成。

        环境变量优先是为了让部署时**不必把口令写进磁盘**——
        进程环境只在内存里，配置文件会被备份、被同步、被误提交。
        """
        from webapp.auth import hash_password

        env_password = os.environ.get("FRT_PASSWORD")
        if env_password:
            self.password_hash = hash_password(env_password)
            return

        if self.password_hash:
            return

        self.bootstrap_password = secrets.token_urlsafe(12)
        self.password_hash = hash_password(self.bootstrap_password)
        self.save()

    def save(self) -> None:
        """原子写盘，避免中途崩溃留下半截 JSON。"""
        payload = {
            "host": self.host,
            "port": self.port,
            "trust_proxy": self.trust_proxy,
            "session_ttl": self.session_ttl,
            "rate_limit_per_min": self.rate_limit_per_min,
            "login_max_failures": self.login_max_failures,
            "login_lockout": self.login_lockout,
            "password_hash": self.password_hash,
        }
        tmp = SERVER_CONFIG_PATH.with_name(SERVER_CONFIG_PATH.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")
        tmp.replace(SERVER_CONFIG_PATH)

    def describe_bootstrap(self) -> str | None:
        """首次生成口令时给出可打印的提示（含一次性明文）。"""
        if not self.bootstrap_password:
            return None
        return (
            "已生成本服务的登录口令（只显示这一次）：\n"
            f"    {self.bootstrap_password}\n"
            "它已哈希后存入 webapp_config.json。"
            "若想改用自定口令，设置环境变量 FRT_PASSWORD 后重启。"
        )
