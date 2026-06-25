"""Feishu card sender — calls lark-oapi SDK directly.

Why not use gateway/platforms/feishu.py's send()?
- AGENTS.md explicitly forbids plugins from touching core files
- The existing adapter only supports text/post types, not interactive
- This module is self-contained and reads config.yaml directly

For v0.2: this is a working sender. For v0.3, we can add a feature request
to Hermes to expose a public send_interactive_card() in the platform ABC.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)


class FeishuCardClient:
    """Sends/edits interactive cards via lark-oapi SDK.

    Lazy init: client is created on first use, not at import time.
    This avoids loading lark_oapi at plugin import (heavy).
    """

    def __init__(self, app_id: str, app_secret: str, domain: str = "feishu"):
        self.app_id = app_id
        self.app_secret = app_secret
        # lark-oapi expects full URL like https://open.feishu.cn
        # "feishu" / "lark" are shorthand tokens used by Hermes core adapter
        self._domain_token = domain.lower()
        self._client = None

    def _get_client(self):
        if self._client is None:
            try:
                import lark_oapi as lark
                # Use the SDK's domain constants directly so we get the
                # right base URL for both Feishu and Lark (overseas) tenants.
                if self._domain_token == "lark":
                    from lark_oapi.core.const import LARK_DOMAIN as _domain
                else:
                    from lark_oapi.core.const import FEISHU_DOMAIN as _domain
                self._client = lark.Client.builder() \
                    .app_id(self.app_id) \
                    .app_secret(self.app_secret) \
                    .domain(_domain) \
                    .build()
                logger.debug("[feishu-interactive-cards] lark client initialized (domain=%s)", _domain)
            except ImportError as e:
                logger.error("[feishu-interactive-cards] lark_oapi not available: %s", e)
                raise
        return self._client

    async def send_card(
        self,
        receive_id: str,
        receive_id_type: str,
        card_payload: Dict[str, Any],
    ) -> Tuple[bool, str]:
        """Send an interactive card.

        Args:
            receive_id: chat_id (oc_xxx) or open_id (ou_xxx) or user_id
            receive_id_type: "chat_id" | "open_id" | "user_id"
            card_payload: the full card JSON (will be JSON-serialized as content)

        Returns:
            (success, message_id_or_error)
        """
        try:
            from lark_oapi.api.im.v1 import (
                CreateMessageRequest,
                CreateMessageRequestBody,
            )

            content_json = json.dumps(card_payload, ensure_ascii=False)
            body = CreateMessageRequestBody.builder() \
                .receive_id(receive_id) \
                .msg_type("interactive") \
                .content(content_json) \
                .build()

            request = CreateMessageRequest.builder() \
                .receive_id_type(receive_id_type) \
                .request_body(body) \
                .build()

            client = self._get_client()
            response = await asyncio.to_thread(
                client.im.v1.message.create, request
            )

            if response.success():
                message_id = response.data.message_id if response.data else "unknown"
                logger.info("[feishu-interactive-cards] Card sent: %s", message_id)
                return True, message_id
            else:
                error_msg = response.msg or "unknown error"
                logger.error(
                    "[feishu-interactive-cards] Send failed: code=%s msg=%s",
                    response.code, error_msg,
                )
                return False, f"{response.code}: {error_msg}"

        except Exception as exc:
            logger.exception("[feishu-interactive-cards] Send exception: %s", exc)
            return False, str(exc)

    async def edit_card(
        self,
        message_id: str,
        card_payload: Dict[str, Any],
    ) -> Tuple[bool, str]:
        """Edit/update a previously sent interactive card.

        Uses the SDK's `Message.patch()` method which wraps the dedicated
        "更新消息卡片" API (PATCH /open-apis/im/v1/messages/{message_id}).

        Note: `message.update()` only accepts text/post msg_types and will
        reject `interactive` cards. Always use `Message.patch()` for
        editing sent cards.

        API ref: https://open.feishu.cn/document/server-docs/im-v1/message-card/patch
        """
        try:
            from lark_oapi.api.im.v1 import (
                PatchMessageRequest,
                PatchMessageRequestBody,
            )

            card_json = json.dumps(card_payload, ensure_ascii=False)
            body = PatchMessageRequestBody.builder() \
                .content(card_json) \
                .build()

            request = PatchMessageRequest.builder() \
                .message_id(message_id) \
                .request_body(body) \
                .build()
            # NOTE: lark-oapi SDK methods are SYNC (verified via
            # `inspect.iscoroutinefunction`). Wrap with `asyncio.to_thread`
            # so we don't block the event loop when the lark SDK makes
            # its blocking HTTP call. This matches `send_card`'s pattern.
            response = await asyncio.to_thread(
                self._get_client().im.v1.message.patch, request
            )

            if response.success():
                logger.info("[feishu-interactive-cards] Card updated: %s", message_id)
                return True, "ok"
            else:
                err_code = getattr(response, "code", -1)
                err_msg = getattr(response, "msg", "unknown")
                logger.error(
                    "[feishu-interactive-cards] Edit failed: code=%s msg=%s",
                    err_code, err_msg,
                )
                return False, f"{err_code}: {err_msg}"

        except Exception as exc:
            logger.exception("[feishu-interactive-cards] Edit exception: %s", exc)
            return False, str(exc)

    async def delete_card(self, message_id: str) -> Tuple[bool, str]:
        """Permanently delete a message via the SDK's `Message.delete()` method.

        Wraps the "撤回消息" API (DELETE /open-apis/im/v1/messages/{message_id}).
        Uses TENANT access token (no user context needed for self-sent messages).

        Bot-sent messages can be recalled within 24h of being sent. After
        24h the API returns code 230020 ("message can't be recalled after
        24 hours") and we treat that as a non-fatal soft failure.

        Note: lark-oapi SDK's message namespace methods are SYNC
        (verified via `inspect.iscoroutinefunction`). Wrap with
        `asyncio.to_thread` to avoid blocking the event loop. Same
        applies to `patch`/`create`/`update`/`get`.

        Returns:
            (success, "ok" or error_string)
        """
        try:
            from lark_oapi.api.im.v1 import DeleteMessageRequest
        except ImportError as exc:
            logger.error("[feishu-interactive-cards] SDK import error: %s", exc)
            return False, f"SDK import error: {exc}"

        try:
            request = DeleteMessageRequest.builder() \
                .message_id(message_id) \
                .build()
            # NOTE: lark-oapi SDK methods are SYNC. Wrap with
            # `asyncio.to_thread` to avoid blocking the event loop.
            # This matches `send_card` and `edit_card` patterns.
            response = await asyncio.to_thread(
                self._get_client().im.v1.message.delete, request
            )

            if response.success():
                logger.info("[feishu-interactive-cards] Card deleted: %s", message_id)
                return True, "ok"
            else:
                err_code = getattr(response, "code", -1)
                err_msg = getattr(response, "msg", "unknown")
                logger.error(
                    "[feishu-interactive-cards] Delete failed: code=%s msg=%s",
                    err_code, err_msg,
                )
                return False, f"{err_code}: {err_msg}"

        except Exception as exc:
            logger.exception("[feishu-interactive-cards] Delete exception: %s", exc)
            return False, str(exc)


# ===========================================================================
# Singleton with config.yaml lookup
# ===========================================================================

_client_singleton: Optional[FeishuCardClient] = None


def get_feishu_client() -> Optional[FeishuCardClient]:
    """Get or create the singleton FeishuCardClient.

    Reads credentials from config.yaml on first call.
    Returns None if config is missing.
    """
    global _client_singleton
    if _client_singleton is not None:
        return _client_singleton

    try:
        import yaml
        from hermes_constants import get_hermes_home
    except ImportError:
        # Try without hermes_constants (for standalone testing)
        from pathlib import Path
        config_path = Path.home() / ".hermes" / "config.yaml"
    else:
        config_path = get_hermes_home() / "config.yaml"

    if not config_path.exists():
        logger.error("[feishu-interactive-cards] config.yaml not found")
        return None

    try:
        import yaml
        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        feishu_cfg = cfg.get("feishu", {})
        app_id = feishu_cfg.get("app_id")
        app_secret = feishu_cfg.get("app_secret")

        if not app_id or not app_secret:
            logger.error(
                "[feishu-interactive-cards] feishu.app_id or app_secret missing"
            )
            return None

        domain = "lark" if "lark" in str(feishu_cfg).lower() else "feishu"
        _client_singleton = FeishuCardClient(app_id, app_secret, domain)
        return _client_singleton

    except Exception as exc:
        logger.exception("[feishu-interactive-cards] Failed to load config: %s", exc)
        return None
