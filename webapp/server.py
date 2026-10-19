"""aiohttp 应用：路由、中间件、静态文件。

安全设计见 ``docs/WEB.md``。这里只强调三处**容易写错且后果严重**的地方：

1. **静态资源必须公开**（否则登录页自己都加载不出来），但 ``/api/*`` 全部要会话。
2. **写操作一律要求 CSRF 头**，登录也不例外。Cookie 鉴权天然带 CSRF 面，
   而跨站表单无法自定义请求头，所以这个头就是有效的门槛。
3. **客户端 IP 只在配置了信任反代时才读转发头**（见 :mod:`webapp.limits`）。
   默认直连部署下，转发头完全由客户端控制。

启动::

    python -m webapp.server
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import sys
from pathlib import Path

from aiohttp import web

from app.config import DEFAULTS, PROJECT_ROOT, Config
from app.config import validate as validate_config
from app.data.pipeline import UpstreamGuard
from app.data.sources import available_sources

from webapp import serialize
from webapp.auth import LoginThrottle, SessionStore, verify_password
from webapp.frames import FrameService
from webapp.limits import SlidingWindowLimiter, client_ip, is_https
from webapp.settings import (
    BASEMAP_LAYERS,
    CSRF_HEADER,
    CSRF_VALUE,
    SESSION_COOKIE,
    ServerSettings,
    env_flag,
)

STATIC_DIR = Path(__file__).resolve().parent / "static"

#: 请求体上限。本服务只收很小的 JSON；不设限等于把内存交给对方。
MAX_BODY_BYTES = 16 * 1024

#: 口令长度上限。scrypt 对超长输入是不对称的开销，不该让它变成放大器。
MAX_PASSWORD_CHARS = 256

RATE_WINDOW_SECONDS = 60.0

#: 无需会话即可访问的 API 路径。登录接口本身当然要能调。
PUBLIC_API_PATHS = frozenset({"/api/session"})

#: 配置接口允许写入的键。
ALLOWED_CONFIG_KEYS = frozenset(DEFAULTS) | {"fr24_api_key", "location"}

CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; "
    "script-src 'self'; connect-src 'self'; base-uri 'none'; form-action 'none'"
)

log = logging.getLogger("webapp")


# ----------------------------------------------------------------------
# 中间件
# ----------------------------------------------------------------------


def _apply_security_headers(request: web.Request, response: web.StreamResponse) -> None:
    response.headers["Content-Security-Policy"] = CSP
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    settings: ServerSettings = request.app["settings"]
    if is_https(request, settings.trust_proxy):
        response.headers["Strict-Transport-Security"] = "max-age=31536000"


@web.middleware
async def security_headers_middleware(request: web.Request, handler):
    """把安全头挂到**所有**响应上，包括错误响应。

    单独一层（最外层）而不是在每个 handler 里加：错误路径最容易漏，
    而漏掉的恰好是 401/403/429 这些最需要防嗅探的响应。
    """
    try:
        response = await handler(request)
    except web.HTTPException as exc:
        payload = {
            "error": getattr(exc, "text", None) or exc.reason or "请求被拒绝",
            "status": exc.status,
        }
        response = web.json_response(payload, status=exc.status)
        for name, value in (exc.headers or {}).items():
            response.headers[name] = value
    _apply_security_headers(request, response)
    return response


@web.middleware
async def rate_limit_middleware(request: web.Request, handler):
    settings: ServerSettings = request.app["settings"]
    limiter: SlidingWindowLimiter = request.app["limiter"]
    ip = client_ip(request, settings.trust_proxy)
    if not limiter.allow(ip):
        retry = limiter.retry_after(ip)
        raise web.HTTPTooManyRequests(
            text="请求过于频繁，请稍后再试",
            headers={"Retry-After": str(int(retry) + 1)},
        )
    return await handler(request)


@web.middleware
async def auth_middleware(request: web.Request, handler):
    path = request.path
    if not path.startswith("/api/"):
        return await handler(request)

    # 先查 CSRF：写操作不管是否已登录都要求这个头
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        if request.headers.get(CSRF_HEADER) != CSRF_VALUE:
            raise web.HTTPForbidden(text="缺少 CSRF 请求头")

    if path in PUBLIC_API_PATHS:
        return await handler(request)

    sessions: SessionStore = request.app["sessions"]
    if not sessions.valid(request.cookies.get(SESSION_COOKIE)):
        raise web.HTTPUnauthorized(text="请先登录")
    return await handler(request)


# ----------------------------------------------------------------------
# 参数校验
# ----------------------------------------------------------------------


def _query_float(
    request: web.Request,
    name: str,
    *,
    minimum: float,
    maximum: float,
    default: float | None = None,
) -> float:
    """取一个有界浮点参数。

    必须显式查 ``isfinite``：``nan`` 与任何数比较都是 False，
    所以 ``nan < minimum`` 和 ``nan > maximum`` 同时为假，它会**穿过**区间检查，
    再被一路带进投影计算。
    """
    raw = request.query.get(name)
    if raw is None or raw == "":
        if default is None:
            raise web.HTTPBadRequest(text=f"缺少参数 {name}")
        return float(default)
    try:
        value = float(raw)
    except ValueError:
        raise web.HTTPBadRequest(text=f"参数 {name} 不是合法数字")
    if not math.isfinite(value) or not (minimum <= value <= maximum):
        raise web.HTTPBadRequest(
            text=f"参数 {name} 超出允许范围 [{minimum}, {maximum}]"
        )
    return value


async def _json_body(request: web.Request) -> dict:
    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        raise web.HTTPBadRequest(text="请求体不是合法 JSON")
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text="请求体必须是 JSON 对象")
    return body


# ----------------------------------------------------------------------
# 会话
# ----------------------------------------------------------------------


async def handle_login(request: web.Request) -> web.Response:
    settings: ServerSettings = request.app["settings"]
    throttle: LoginThrottle = request.app["login_throttle"]
    ip = client_ip(request, settings.trust_proxy)

    locked = throttle.locked_for(ip)
    if locked > 0:
        raise web.HTTPTooManyRequests(
            text="登录尝试过多，请稍后再试",
            headers={"Retry-After": str(int(locked) + 1)},
        )

    body = await _json_body(request)
    password = body.get("password")
    if not isinstance(password, str) or len(password) > MAX_PASSWORD_CHARS:
        throttle.note_failure(ip)
        raise web.HTTPUnauthorized(text="口令不正确")

    if not verify_password(password, settings.password_hash):
        throttle.note_failure(ip)
        raise web.HTTPUnauthorized(text="口令不正确")

    throttle.note_success(ip)
    token = request.app["sessions"].create()
    response = web.Response(status=204)
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.session_ttl,
        httponly=True,
        samesite="Strict",
        secure=is_https(request, settings.trust_proxy),
        path="/",
    )
    return response


async def handle_logout(request: web.Request) -> web.Response:
    request.app["sessions"].revoke(request.cookies.get(SESSION_COOKIE))
    response = web.Response(status=204)
    response.del_cookie(SESSION_COOKIE, path="/")
    return response


# ----------------------------------------------------------------------
# 状态与配置
# ----------------------------------------------------------------------


async def handle_state(request: web.Request) -> web.Response:
    config: Config = request.app["config"]
    frames: FrameService = request.app["frames"]
    manual = config.manual_location()
    return web.json_response(
        {
            "config": serialize.config_to_dict(config),
            "location": {
                "mode": config.location_mode,
                "manual": list(manual) if manual else None,
            },
            "cooling": frames.cooling(),
            "sources": list(available_sources()),
        }
    )


async def handle_config_put(request: web.Request) -> web.Response:
    config: Config = request.app["config"]
    body = await _json_body(request)

    unknown = sorted(set(body) - ALLOWED_CONFIG_KEYS)
    if unknown:
        raise web.HTTPBadRequest(text="不认识的配置项：" + "、".join(unknown))

    merged = dict(config.as_dict())
    merged.update(body)
    # validate 会把越界值夹回合法范围，并补齐缺失键
    config.update(**validate_config(merged))
    config.save()
    return web.json_response({"config": serialize.config_to_dict(config)})


# ----------------------------------------------------------------------
# 航班数据
# ----------------------------------------------------------------------


async def handle_flights(request: web.Request) -> web.Response:
    config: Config = request.app["config"]
    lat = _query_float(request, "lat", minimum=-90.0, maximum=90.0)
    lon = _query_float(request, "lon", minimum=-180.0, maximum=180.0)
    radius = _query_float(
        request,
        "radius_km",
        minimum=2.0,
        maximum=460.0,
        default=float(config.radius_km),
    )
    frames: FrameService = request.app["frames"]
    return web.json_response(await frames.frame(lat, lon, radius))


async def handle_flight_detail(request: web.Request) -> web.Response:
    icao24 = (request.match_info.get("icao24") or "").strip().lower()
    if not icao24 or len(icao24) > 16 or not icao24.isalnum():
        raise web.HTTPBadRequest(text="icao24 不合法")

    config: Config = request.app["config"]
    lat = _query_float(request, "lat", minimum=-90.0, maximum=90.0)
    lon = _query_float(request, "lon", minimum=-180.0, maximum=180.0)
    radius = _query_float(
        request,
        "radius_km",
        minimum=2.0,
        maximum=460.0,
        default=float(config.radius_km),
    )

    frames: FrameService = request.app["frames"]
    frame = await frames.frame(lat, lon, radius)
    for item in frame["flights"]:
        if item["icao24"].lower() == icao24:
            return web.json_response({"flight": item, "stale": frame["stale"]})
    # 不在当前帧里是常态（航班飞出了范围），不是错误
    return web.json_response({"flight": None, "reason": "该航班已不在当前雷达范围内"})


def _load_airports():
    """惰性加载机场库。

    不在启动时加载：``AirportDatabase.load()`` 可能触发数据下载（约 16 MB），
    那会阻塞事件循环让服务迟迟不可用。放到首次用到时、且在工作线程里做。
    """
    from app.airports import AirportDatabase

    database = AirportDatabase()
    return database if database.load() else None


def _search_airports(database, query: str, limit: int) -> list[dict]:
    needle = query.lower()
    matches: list[dict] = []
    for airport in database.with_runways():
        haystack = " ".join(
            str(part)
            for part in (
                airport.icao,
                airport.iata,
                airport.name,
                airport.city,
                airport.country,
            )
            if part
        ).lower()
        if needle in haystack:
            matches.append(
                {
                    "icao": airport.icao,
                    "iata": airport.iata,
                    "name": airport.name,
                    "city": airport.city,
                    "country": airport.country,
                    "lat": airport.lat,
                    "lon": airport.lon,
                }
            )
            if len(matches) >= limit:
                break
    return matches


async def handle_airports(request: web.Request) -> web.Response:
    query = (request.query.get("q") or "").strip()
    limit = int(_query_float(request, "limit", minimum=1, maximum=200, default=30))
    if not query:
        raise web.HTTPBadRequest(text="缺少参数 q")

    database = request.app.get("airports")
    if database is None:
        try:
            database = await asyncio.to_thread(_load_airports)
        except Exception as exc:  # noqa: BLE001
            log.warning("机场数据加载失败：%s", exc)
            database = None
        request.app["airports"] = database

    if database is None:
        raise web.HTTPServiceUnavailable(text="机场数据尚未就绪")

    matches = await asyncio.to_thread(_search_airports, database, query, limit)
    return web.json_response({"airports": matches})


async def handle_basemap(request: web.Request) -> web.Response:
    lat = _query_float(request, "lat", minimum=-90.0, maximum=90.0)
    lon = _query_float(request, "lon", minimum=-180.0, maximum=180.0)
    radius = _query_float(request, "radius_km", minimum=2.0, maximum=460.0)
    size = int(_query_float(request, "size", minimum=64, maximum=2048, default=768))

    # 参数校验必须排在依赖检查之前：渲染模块缺席是**服务端**的问题，
    # 不该让它把客户端本应得到的 400 盖成 503，否则排查时会找错方向。
    raw_layers = request.query.get("layers", "")
    requested = {part.strip() for part in raw_layers.split(",") if part.strip()}
    unknown = sorted(requested - set(BASEMAP_LAYERS))
    if unknown:
        raise web.HTTPBadRequest(text="不认识的图层：" + "、".join(unknown))

    try:
        from webapp.render import render_basemap
    except ImportError:
        raise web.HTTPServiceUnavailable(text="底图渲染模块不可用")

    data = await asyncio.to_thread(
        render_basemap, lat, lon, radius, size, frozenset(requested)
    )
    return web.Response(
        body=data,
        content_type="image/png",
        # 同一视野的底图短时间内不会变，让浏览器也缓存一份
        headers={"Cache-Control": "private, max-age=300"},
    )


async def handle_locate(request: web.Request) -> web.Response:
    from app.locate import locate

    try:
        result = await asyncio.to_thread(locate, prefer_windows=False)
    except Exception as exc:  # noqa: BLE001 - 定位失败只是降级，不该 500
        return web.json_response({"location": None, "error": str(exc)})
    return web.json_response(
        {
            "location": {"lat": result.lat, "lon": result.lon, "source": result.source},
            "error": "",
        }
    )


# ----------------------------------------------------------------------
# 静态资源
# ----------------------------------------------------------------------


async def handle_index(request: web.Request) -> web.Response:
    index = STATIC_DIR / "index.html"
    if not index.is_file():
        # 前端还没构建出来时给一条明确的提示，而不是让 FileResponse 抛成 500
        raise web.HTTPServiceUnavailable(text="前端资源缺失：webapp/static/index.html")
    return web.FileResponse(index)


# ----------------------------------------------------------------------
# 应用装配
# ----------------------------------------------------------------------


async def _on_startup(app: web.Application) -> None:
    log.info("服务已就绪，数据根目录：%s", PROJECT_ROOT)


async def _on_cleanup(app: web.Application) -> None:
    app["frames"].close()


def create_app(settings: ServerSettings | None = None) -> web.Application:
    settings = settings or ServerSettings.load()

    app = web.Application(
        middlewares=[
            security_headers_middleware,
            rate_limit_middleware,
            auth_middleware,
        ],
        client_max_size=MAX_BODY_BYTES,
    )

    app["settings"] = settings
    app["config"] = Config()
    app["sessions"] = SessionStore(settings.session_ttl)
    app["login_throttle"] = LoginThrottle(settings.login_max_failures, settings.login_lockout)
    app["limiter"] = SlidingWindowLimiter(
        settings.rate_limit_per_min, RATE_WINDOW_SECONDS
    )
    app["frames"] = FrameService(app["config"], UpstreamGuard())
    app["airports"] = None

    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)

    app.router.add_get("/", handle_index)
    if STATIC_DIR.is_dir():
        # add_static 自带路径包含检查，不会越出 STATIC_DIR
        app.router.add_static("/static/", STATIC_DIR, name="static")

    app.router.add_post("/api/session", handle_login)
    app.router.add_delete("/api/session", handle_logout)
    app.router.add_get("/api/state", handle_state)
    app.router.add_put("/api/config", handle_config_put)
    app.router.add_get("/api/flights", handle_flights)
    app.router.add_get("/api/flight/{icao24}", handle_flight_detail)
    app.router.add_get("/api/airports", handle_airports)
    app.router.add_get("/api/basemap", handle_basemap)
    app.router.add_get("/api/locate", handle_locate)

    return app


def main() -> None:
    # Windows 下把输出重定向到文件时，控制台默认编码是 GBK，
    # 中文日志会变成乱码（实测 ``服务已就绪`` → ``鏈嶅姟宸插氨缁``）。
    # 显式切到 UTF-8，errors="replace" 保证任何环境都不会因为编码中断启动。
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    settings = ServerSettings.load()

    note = settings.describe_bootstrap()
    if note:
        print("=" * 68)
        print(note)
        print("=" * 68)

    if not (settings.trust_proxy or env_flag("FRT_ALLOW_PUBLIC_BIND")):
        if settings.host not in ("127.0.0.1", "localhost", "::1"):
            print(
                "警告：正在监听非回环地址，但没有设置 FRT_TRUST_PROXY=1。\n"
                "      若前面有反向代理终止 TLS，请设置它，否则会话 Cookie 不会带 Secure；\n"
                "      若是直接暴露，请停在代理后面 —— 明文 HTTP 下口令会以明文过网。"
            )

    web.run_app(create_app(settings), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
