"""口令校验、会话令牌、登录节流。

公网可访问意味着这三件都是**必需品**，不是加固项：

* 口令用 scrypt + 每实例随机盐，只存哈希；比对走 :func:`hmac.compare_digest`，
  避免逐字节比较的时序侧信道。
* 会话令牌由 :mod:`secrets` 生成，服务端内存表持有并带过期时间。
  不用 JWT：无状态令牌无法在"怀疑泄露时立刻失效"，而这个服务只有一个用户，
  内存表的开销可以忽略。
* 登录失败按 IP 指数退避，挡住在线暴力破解（口令是唯一防线）。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time

#: scrypt 参数。``n=2**14`` 在交互式登录下约几十毫秒，内存约 16 MB，
#: 对在线爆破是不可接受的成本，对本人登录无感。
SCRYPT_N = 2 ** 14
SCRYPT_R = 8
SCRYPT_P = 1
SALT_BYTES = 16
KEY_BYTES = 32


def hash_password(password: str) -> str:
    """把口令编码成 ``scrypt$n$r$p$salt$key`` 形式。"""
    salt = secrets.token_bytes(SALT_BYTES)
    key = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=SCRYPT_N,
        r=SCRYPT_R,
        p=SCRYPT_P,
        dklen=KEY_BYTES,
    )
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${key.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """常数时间比对口令。参数被篡改或格式不符时返回 False，不抛异常。"""
    if not password or not stored:
        return False
    try:
        scheme, n_raw, r_raw, p_raw, salt_hex, key_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(key_hex)
        key = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=int(n_raw),
            r=int(r_raw),
            p=int(p_raw),
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(key, expected)


class SessionStore:
    """内存会话表。令牌 → 过期时刻（epoch 秒）。"""

    def __init__(self, ttl: int) -> None:
        self._ttl = ttl
        self._sessions: dict[str, float] = {}
        self._lock = threading.Lock()

    def create(self) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._sessions[token] = time.time() + self._ttl
        return token

    def valid(self, token: str | None) -> bool:
        if not token:
            return False
        now = time.time()
        with self._lock:
            deadline = self._sessions.get(token)
            if deadline is None:
                return False
            if deadline <= now:
                del self._sessions[token]
                return False
            return True

    def revoke(self, token: str | None) -> None:
        if not token:
            return
        with self._lock:
            self._sessions.pop(token, None)

    def revoke_all(self) -> None:
        with self._lock:
            self._sessions.clear()

    def purge_expired(self) -> None:
        now = time.time()
        with self._lock:
            for token in [t for t, deadline in self._sessions.items() if deadline <= now]:
                del self._sessions[token]

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)


class LoginThrottle:
    """登录失败按 IP 指数退避。

    ``max_failures`` 次失败之后开始锁定，每多失败一次锁定时长翻倍，直到封顶。
    成功登录立即清零——否则本人打错几次之后会被自己锁在门外很久。
    """

    def __init__(
        self,
        max_failures: int = 5,
        base_lockout: float = 30.0,
        cap_lockout: float = 3600.0,
    ) -> None:
        self._max_failures = max_failures
        self._base = base_lockout
        self._cap = cap_lockout
        self._state: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()

    def locked_for(self, key: str) -> float:
        """剩余锁定秒数；未锁定返回 0。"""
        with self._lock:
            _, locked_until = self._state.get(key, (0, 0.0))
            return max(0.0, locked_until - time.monotonic())

    def note_failure(self, key: str) -> None:
        with self._lock:
            failures, _ = self._state.get(key, (0, 0.0))
            failures += 1
            locked_until = 0.0
            if failures >= self._max_failures:
                over = failures - self._max_failures
                locked_until = time.monotonic() + min(
                    self._base * (2 ** over), self._cap
                )
            self._state[key] = (failures, locked_until)

    def note_success(self, key: str) -> None:
        with self._lock:
            self._state.pop(key, None)

    def prune(self) -> None:
        """清掉已解锁且无失败记录的条目，防止按 IP 无限增长。"""
        now = time.monotonic()
        with self._lock:
            for key in [
                k for k, (failures, until) in self._state.items()
                if until <= now and failures == 0
            ]:
                del self._state[key]
