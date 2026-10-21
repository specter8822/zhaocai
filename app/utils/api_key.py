import re
import os
import logging
import asyncio
from app.utils.logging import format_log_message
from app.utils.key_scheduler import key_scheduler
import app.config.settings as settings

logger = logging.getLogger("my_logger")


class APIKeyManager:
    def __init__(self):
        self.api_keys = re.findall(r"AIzaSy[a-zA-Z0-9_-]{33}", settings.GEMINI_API_KEYS)
        # 加载更多 GEMINI_API_KEYS
        for i in range(1, 99):
            if keys := os.environ.get(f"GEMINI_API_KEYS_{i}", ""):
                self.api_keys += re.findall(r"AIzaSy[a-zA-Z0-9_-]{33}", keys)
            else:
                break

        self.key_stack = []  # 兼容旧代码/面板，实际调度由 key_scheduler 负责
        self.lock = asyncio.Lock()
        self._sync_scheduler()

    def _sync_scheduler(self):
        key_scheduler.cooldown_base = float(getattr(settings, "KEY_COOLDOWN_BASE", 15))
        key_scheduler.cooldown_max = float(getattr(settings, "KEY_COOLDOWN_MAX", 300))
        key_scheduler.set_keys(self.api_keys)

    def _reset_key_stack(self):
        """兼容旧调用点：重置轮转游标并同步 key 列表。"""
        key_scheduler.reset_cursor()
        self._sync_scheduler()

    async def get_available_key(self):
        """按健康状态挑一个 key。

        - 优先在途少、连续失败少、最久未被使用的 key；
        - 冷却中的 key 不会被选中（除非全部都在冷却，此时 fail-open 选最早恢复的）；
        - 已判定失效的 key 永久跳过。
        """
        async with self.lock:
            self._sync_scheduler()
            key = await key_scheduler.select()
            if key:
                return key

            if not self.api_keys:
                log_msg = format_log_message("ERROR", "没有配置任何 API 密钥！")
                logger.error(log_msg)
            log_msg = format_log_message("ERROR", "没有可用的API密钥！")
            logger.error(log_msg)
            return None

    async def release_key(self, key):
        await key_scheduler.release(key)

    async def report_success(self, key):
        await key_scheduler.report_success(key)

    async def report_failure(self, key, error):
        return await key_scheduler.report_failure(key, error)

    def show_all_keys(self):
        snapshot = key_scheduler.snapshot()
        log_msg = format_log_message(
            "INFO",
            f"当前可用API key个数: {snapshot['total']} "
            f"(active={snapshot['counts']['active']}, cooling={snapshot['counts']['cooling']}, "
            f"disabled={snapshot['counts']['disabled']})",
        )
        logger.info(log_msg)
        for item in snapshot["keys"]:
            logger.info(
                format_log_message(
                    "INFO",
                    f"API Key {item['key']}: {item['status']} "
                    f"in_flight={item['in_flight']} failures={item['failures']} "
                    f"cooldown={item['cooldown_remaining']}s {item['last_error_kind']}",
                )
            )


async def test_api_key(api_key: str) -> bool:
    """
    测试 API 密钥是否有效。
    """
    try:
        import httpx

        url = "https://generativelanguage.googleapis.com/v1beta/models?key={}".format(
            api_key
        )
        async with httpx.AsyncClient() as client:
            response = await client.get(url)
            response.raise_for_status()
            return True
    except Exception:
        return False
