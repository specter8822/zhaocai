"""带健康状态的 API Key 轮询调度器。

原版 hajimi 的轮询是「随机洗牌 + 栈弹出」：
- 一个 key 被 429 之后还会被继续抽到，直到整批请求全部失败；
- 失效的 key 不会下线，每次都要重新撞一次 400；
- 没有并发感知，所有 key 被均匀当消耗品用。

这里换成健康感知轮询：
1. 按 (在途请求数, 连续失败次数, 最久未使用) 选 key —— 尽量少并发、少失败、最久没用的；
2. 按错误类型区分处理：
   - key 无效(401/400 invalid key) → 永久下线（重启或手动恢复才回来）
   - 每日配额耗尽(429 quota) → 冷却到太平洋时间零点
   - 分钟级限流(429) → 指数退避冷却，遵守 Retry-After
   - 5xx / 网络错误 → 短暂冷却
   - schema 之类的 400 → 不惩罚 key（是请求的问题）
3. 所有 key 都在冷却时 fail-open，挑最早恢复的那个继续试，避免整站不可用；
4. 提供 snapshot() 给 /healthz 和 /v1/keys 观察状态。
"""
import asyncio
import math
import random
import time
from datetime import datetime, timedelta

try:  # python:3.12-slim 一般带 tzdata，缺了也不影响主流程
    from zoneinfo import ZoneInfo

    _PACIFIC = ZoneInfo("America/Los_Angeles")
except Exception:  # pragma: no cover
    _PACIFIC = None

ACTIVE = "active"
COOLING = "cooling"
DISABLED = "disabled"

AUTH = "auth"
QUOTA = "quota"
RATE = "rate"
SERVER = "server"
NETWORK = "network"
BAD_REQUEST = "bad_request"
UNKNOWN = "unknown"

_QUOTA_HINTS = ("quota", "resource_exhausted", "rate limit exceeded", "free tier")
_AUTH_HINTS = ("api key not valid", "api_key_invalid", "invalid api key", "permission")


def _mask(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 12:
        return key[:4] + "..." + key[-2:]
    return key[:6] + "..." + key[-4:]


def next_pacific_midnight(now: float) -> float:
    """下一次太平洋时间零点的时间戳；拿不到时区时退化到固定 UTC-7。"""
    if _PACIFIC is None:
        current = datetime.fromtimestamp(now) + timedelta(hours=-7)
        tomorrow = (current + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return tomorrow.timestamp() + 7 * 3600
    local = datetime.fromtimestamp(now, _PACIFIC)
    tomorrow = (local + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return tomorrow.timestamp()


def _error_response(error):
    response = getattr(error, "response", None)
    return response if response is not None else None


def classify_error(error) -> str:
    """把上游异常归一成错误类型。"""
    import httpx
    import requests

    if isinstance(error, (httpx.TimeoutException, requests.exceptions.Timeout)):
        return NETWORK
    if isinstance(error, (httpx.ConnectError, httpx.NetworkError, requests.exceptions.ConnectionError)):
        return NETWORK

    status = None
    body_text = ""
    response = _error_response(error)
    if response is not None:
        status = getattr(response, "status_code", None)
        try:
            body_text = response.text or ""
        except Exception:
            try:
                body_text = str(response.json())
            except Exception:
                body_text = ""
    lowered = body_text.lower()

    if status in (401, 403):
        return AUTH
    if status == 429:
        if any(hint in lowered for hint in _QUOTA_HINTS) and "minute" not in lowered:
            return QUOTA
        return RATE
    if status == 400:
        if "api key" in lowered:
            return AUTH
        return BAD_REQUEST
    if status is not None and 500 <= status < 600:
        return SERVER
    if isinstance(error, (httpx.HTTPStatusError, requests.exceptions.HTTPError)):
        return UNKNOWN
    return UNKNOWN


def _retry_after_seconds(error) -> float:
    response = _error_response(error)
    headers = getattr(response, "headers", None) or {}
    raw = headers.get("retry-after") if hasattr(headers, "get") else None
    if not raw:
        return 0.0
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return 0.0


class KeyState:
    __slots__ = (
        "key",
        "failures",
        "successes",
        "in_flight",
        "last_used",
        "cooldown_until",
        "disabled",
        "last_error",
        "last_error_kind",
    )

    def __init__(self, key: str):
        self.key = key
        self.failures = 0
        self.successes = 0
        self.in_flight = 0
        self.last_used = 0.0
        self.cooldown_until = 0.0
        self.disabled = False
        self.last_error = ""
        self.last_error_kind = ""

    @property
    def status(self) -> str:
        if self.disabled:
            return DISABLED
        if self.cooldown_until > time.time():
            return COOLING
        return ACTIVE


class KeyScheduler:
    def __init__(self, cooldown_base: float = 15.0, cooldown_max: float = 300.0):
        self._states: dict[str, KeyState] = {}
        self._lock = asyncio.Lock()
        self.cooldown_base = cooldown_base
        self.cooldown_max = cooldown_max
        self._cursor = 0
        self.stale_in_flight = 120.0

    # ---------- key 集合 ----------
    def set_keys(self, keys) -> None:
        """同步 key 列表：新增的建状态，删除的丢弃，已有的保留健康数据。"""
        unique = []
        seen = set()
        for key in keys:
            if key and key not in seen:
                seen.add(key)
                unique.append(key)
        for key in unique:
            self._states.setdefault(key, KeyState(key))
        for key in list(self._states):
            if key not in seen:
                del self._states[key]

    def reset_cursor(self) -> None:
        self._cursor = 0

    def reset_key(self, key: str) -> bool:
        state = self._states.get(key)
        if not state:
            return False
        state.disabled = False
        state.cooldown_until = 0.0
        state.failures = 0
        return True

    def reset_all(self) -> int:
        """清空所有 key 的冷却/失效状态，返回受影响的 key 数量。"""
        count = 0
        for state in self._states.values():
            state.disabled = False
            state.cooldown_until = 0.0
            state.failures = 0
            count += 1
        return count

    def reset_by_prefix(self, prefix: str) -> int:
        """按 key 前缀/掩码恢复（方便在精简日志里点名恢复某个 key）。"""
        prefix = (prefix or "").strip()
        if not prefix:
            return 0
        count = 0
        for state in self._states.values():
            if state.key.startswith(prefix) or _mask(state.key) == prefix:
                state.disabled = False
                state.cooldown_until = 0.0
                state.failures = 0
                count += 1
        return count

    def _release_stale(self) -> None:
        now = time.time()
        for state in self._states.values():
            if state.in_flight > 0 and now - state.last_used > self.stale_in_flight:
                state.in_flight = 0

    # ---------- 选择 ----------
    def _available(self, now: float, exclude: set[str]) -> list[KeyState]:
        candidates = []
        for state in self._states.values():
            if state.key in exclude:
                continue
            if state.disabled:
                continue
            if state.cooldown_until > now:
                continue
            candidates.append(state)
        return candidates

    def _pick(self, candidates: list[KeyState]) -> KeyState:
        # 在途少的优先 → 连续失败少的优先 → 最久没被用的优先 → 轮转游标打散
        candidates.sort(key=lambda s: (s.in_flight, s.failures, s.last_used))
        best = candidates[0]
        if best.in_flight == 0 and best.failures == 0:
            self._cursor = (self._cursor + 1) % max(1, len(candidates))
        return best

    async def select(self, exclude=None) -> str | None:
        exclude = set(exclude or ())
        async with self._lock:
            self._release_stale()
            now = time.time()
            candidates = self._available(now, exclude)
            if candidates:
                state = self._pick(candidates)
                state.in_flight += 1
                state.last_used = now
                return state.key

            # 全部冷却中：fail-open，挑最早恢复的 key，避免整站不可用
            cooling = [
                s
                for s in self._states.values()
                if not s.disabled and s.key not in exclude
            ]
            if cooling:
                cooling.sort(key=lambda s: s.cooldown_until)
                state = cooling[0]
                state.in_flight += 1
                state.last_used = now
                return state.key
            return None

    async def release(self, key: str) -> None:
        state = self._states.get(key)
        if not state:
            return
        async with self._lock:
            if state.in_flight > 0:
                state.in_flight -= 1

    # ---------- 结果上报 ----------
    async def report_success(self, key: str) -> None:
        state = self._states.get(key)
        if not state:
            return
        async with self._lock:
            state.successes += 1
            state.failures = 0
            state.cooldown_until = 0.0
            state.last_error = ""
            state.last_error_kind = ""

    async def report_failure(self, key: str, error, failure_since_success: int | None = None) -> str:
        """记录失败并冷却/下线，返回错误类型。"""
        state = self._states.get(key)
        if not state:
            return UNKNOWN
        kind = classify_error(error)
        now = time.time()

        async with self._lock:
            if kind == BAD_REQUEST:
                # 请求本身有问题（比如 schema 不合法），不惩罚 key
                return kind

            state.last_error_kind = kind
            try:
                state.last_error = str(error)[:200]
            except Exception:
                state.last_error = ""

            if kind == AUTH:
                state.disabled = True
                return kind

            state.failures += 1
            attempts = failure_since_success if failure_since_success is not None else state.failures
            retry_after = _retry_after_seconds(error)

            if kind == QUOTA:
                # 反复 429 视作日配额耗尽，冷却到太平洋时间零点
                if state.failures >= 3:
                    state.cooldown_until = max(now, next_pacific_midnight(now))
                else:
                    state.cooldown_until = now + max(retry_after, self._backoff(attempts, 60.0))
            elif kind == RATE:
                state.cooldown_until = now + max(retry_after, self._backoff(attempts, 120.0))
            elif kind == SERVER:
                state.cooldown_until = now + max(retry_after, self._backoff(attempts, 30.0))
            elif kind == NETWORK:
                state.cooldown_until = now + max(retry_after, self._backoff(attempts, 20.0))
            else:
                state.cooldown_until = now + max(retry_after, self._backoff(attempts, 20.0))
            return kind

    def _backoff(self, attempts: int, cap: float) -> float:
        base = self.cooldown_base
        value = base * math.pow(2, max(0, attempts - 1))
        return min(cap, self.cooldown_max, value)

    # ---------- 观测 ----------
    def snapshot(self) -> dict:
        now = time.time()
        keys = []
        for state in self._states.values():
            keys.append(
                {
                    "key": _mask(state.key),
                    "status": state.status,
                    "in_flight": state.in_flight,
                    "failures": state.failures,
                    "successes": state.successes,
                    "cooldown_remaining": max(0, int(state.cooldown_until - now)),
                    "last_error_kind": state.last_error_kind,
                    "last_error": state.last_error,
                }
            )
        counts = {
            ACTIVE: sum(1 for k in keys if k["status"] == ACTIVE),
            COOLING: sum(1 for k in keys if k["status"] == COOLING),
            DISABLED: sum(1 for k in keys if k["status"] == DISABLED),
        }
        return {"total": len(keys), "counts": counts, "keys": keys}


key_scheduler = KeyScheduler()
