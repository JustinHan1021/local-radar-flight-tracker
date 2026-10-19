"""按客户端 IP 的滑窗限流，以及客户端 IP 的取值口径。

这两件事必须放在一起：限流的效果完全取决于"你以为的客户端是谁"。
把 :func:`client_ip` 的口径写错（无条件采信 ``X-Forwarded-For``），
攻击者只要每次请求换一个伪造 IP，限流和登录锁定就同时失效。
"""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

from aiohttp import web


def client_ip(request: web.Request, trust_proxy: bool) -> str:
    """取客户端 IP。

    **只有在明确配置了信任上游反代（``FRT_TRUST_PROXY=1``）时**才读转发头。
    本服务默认直接监听，此时转发头完全由客户端控制，采信它等于让出限流的钥匙。
    """
    if trust_proxy:
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            # 反代按 "client, proxy1, proxy2" 追加，取最左才是原始客户端
            return forwarded.split(",")[0].strip()
        real = request.headers.get("X-Real-IP")
        if real:
            return real.strip()

    transport = request.transport
    peer = transport.get_extra_info("peername") if transport is not None else None
    if isinstance(peer, (tuple, list)) and peer:
        return str(peer[0])
    return "unknown"


def is_https(request: web.Request, trust_proxy: bool) -> bool:
    """判断当前连接是否为 HTTPS。

    决定会话 Cookie 要不要带 ``Secure``。走反代时 TLS 在反代终止，
    必须靠 ``X-Forwarded-Proto`` 才能知道；直连时看 transport。
    """
    if trust_proxy:
        proto = request.headers.get("X-Forwarded-Proto", "")
        if proto:
            return proto.split(",")[0].strip().lower() == "https"
    transport = request.transport
    return bool(transport is not None and transport.get_extra_info("sslcontext"))


class SlidingWindowLimiter:
    """每键一个时间戳队列的滑窗限流。

    滑窗而不是固定窗口：固定窗口允许在边界处两倍突发（窗口末尾一批、
    下个窗口开头再来一批），对"防滥用"这种目的来说形同虚设。
    """

    def __init__(self, max_events: int, window: float) -> None:
        self._max = max(1, max_events)
        self._window = float(window)
        self._events: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()
        self._last_prune = 0.0

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        cutoff = now - self._window
        with self._lock:
            bucket = self._events[key]
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= self._max:
                return False
            bucket.append(now)
            self._maybe_prune_locked(now, cutoff)
            return True

    def retry_after(self, key: str) -> float:
        """还需等待多久才有额度（秒）。"""
        now = time.monotonic()
        with self._lock:
            bucket = self._events.get(key)
            if not bucket:
                return 0.0
            return max(0.0, bucket[0] + self._window - now)

    def _maybe_prune_locked(self, now: float, cutoff: float) -> None:
        """定期清空空桶。

        不限流的服务也应当允许自己被长期运行：按 IP 建桶的字典
        会被扫描流量慢慢撑大，而空桶留着没有任何意义。
        """
        if now - self._last_prune < 60.0:
            return
        self._last_prune = now
        for key in [k for k, bucket in self._events.items() if not bucket or bucket[-1] < cutoff]:
            del self._events[key]
