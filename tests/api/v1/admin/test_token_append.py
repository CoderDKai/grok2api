import unittest
from unittest.mock import AsyncMock, Mock, patch

from app.api.v1.admin import token as token_api


class _Lock:
    async def __aenter__(self):
        return None

    async def __aexit__(self, exc_type, exc, tb):
        return False


class TokenAppendApiTests(unittest.IsolatedAsyncioTestCase):
    @patch("app.api.v1.admin.token.get_token_manager", new_callable=AsyncMock)
    async def test_append_adds_new_tokens_and_skips_existing(self, mock_get_manager):
        mgr = Mock()
        mgr.get_pool_name_for_token.side_effect = (
            lambda token: "legacy" if token == "exists" else None
        )
        mgr.add = AsyncMock(return_value=True)
        mock_get_manager.return_value = mgr

        payload = await token_api.append_tokens(
            {"ssoBasic": ["new-token", "exists", "  ", "new-token"]}
        )

        self.assertEqual(payload["summary"], {"added": 1, "skipped": 2, "invalid": 1})
        mgr.add.assert_awaited_once_with("new-token", "ssoBasic")

    @patch("app.api.v1.admin.token.get_token_manager", new_callable=AsyncMock)
    async def test_append_ignores_extra_fields_in_object_items(self, mock_get_manager):
        mgr = Mock()
        mgr.get_pool_name_for_token.return_value = None
        mgr.add = AsyncMock(return_value=True)
        mock_get_manager.return_value = mgr

        payload = await token_api.append_tokens(
            {"ssoBasic": [{"token": "abc", "note": "ignored", "tags": ["x"]}]}
        )

        self.assertEqual(payload["summary"], {"added": 1, "skipped": 0, "invalid": 0})
        mgr.add.assert_awaited_once_with("abc", "ssoBasic")

    @patch("app.api.v1.admin.token.get_storage")
    @patch("app.api.v1.admin.token.get_token_manager", new_callable=AsyncMock)
    async def test_update_tokens_keeps_replace_semantics(
        self, mock_get_manager, mock_get_storage
    ):
        storage = Mock()
        storage.acquire_lock.return_value = _Lock()
        storage.load_tokens = AsyncMock(
            return_value={"ssoBasic": [{"token": "old-token"}]}
        )
        storage.save_tokens = AsyncMock()
        mock_get_storage.return_value = storage

        mgr = Mock()
        mgr.reload = AsyncMock()
        mock_get_manager.return_value = mgr

        payload = await token_api.update_tokens({"ssoBasic": ["new-token"]})

        self.assertEqual(payload["status"], "success")
        storage.save_tokens.assert_awaited_once()
        saved_payload = storage.save_tokens.await_args.args[0]
        self.assertEqual(list(saved_payload.keys()), ["ssoBasic"])
        self.assertEqual(len(saved_payload["ssoBasic"]), 1)
        self.assertEqual(saved_payload["ssoBasic"][0]["token"], "new-token")
        self.assertEqual(saved_payload["ssoBasic"][0]["tags"], [])
