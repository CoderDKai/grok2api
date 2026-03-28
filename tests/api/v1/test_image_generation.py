import unittest
from unittest.mock import AsyncMock, Mock, patch

import orjson

from app.services.grok.services.image import ImageGenerationService
from app.services.grok.services.image_edit import ImageCollectProcessor
from app.services.grok.services.model import ModelService
from app.services.reverse.app_chat import AppChatReverse


def _stream_lines(*payloads):
    async def _gen():
        for payload in payloads:
            yield payload

    return _gen()


class AppChatReverseTests(unittest.TestCase):
    def test_build_payload_uses_mode_id_when_model_absent(self):
        payload = AppChatReverse.build_payload(
            message="draw a cat",
            model=None,
            mode=None,
            request_overrides={"modeId": "auto", "imageGenerationCount": 1},
        )

        self.assertEqual(payload["modeId"], "auto")
        self.assertEqual(payload["imageGenerationCount"], 1)
        self.assertNotIn("modelName", payload)
        self.assertNotIn("modelMode", payload)


class ImageGenerationServiceTests(unittest.IsolatedAsyncioTestCase):
    @patch("app.services.grok.services.image_edit.get_config", return_value=0)
    @patch("app.services.grok.services.image.pick_token", new_callable=AsyncMock)
    @patch("app.services.grok.services.chat.GrokChatService.chat", new_callable=AsyncMock)
    @patch(
        "app.services.grok.services.image_edit.BaseProcessor.process_url",
        new_callable=AsyncMock,
    )
    async def test_generate_collects_images_from_app_chat_card_attachment(
        self,
        mock_process_url,
        mock_chat,
        mock_pick_token,
        _mock_image_edit_config,
    ):
        mock_pick_token.return_value = "token-1"
        mock_process_url.return_value = "https://cdn.example.com/generated.png"
        mock_chat.return_value = _stream_lines(
            orjson.dumps(
                {
                    "result": {
                        "response": {
                            "cardAttachment": {
                                "jsonData": orjson.dumps(
                                    {
                                        "type": "render_generated_image",
                                        "image_chunk": {
                                            "progress": 100,
                                            "imageUrl": "generated/path/image.png",
                                        },
                                    }
                                ).decode()
                            }
                        }
                    }
                }
            )
        )

        token_mgr = Mock()
        token_mgr.consume = AsyncMock()
        model_info = ModelService.get("grok-imagine-1.0")

        result = await ImageGenerationService().generate(
            token_mgr=token_mgr,
            token="token-1",
            model_info=model_info,
            prompt="draw a cat",
            n=1,
            response_format="url",
            size="1024x1024",
            aspect_ratio="1:1",
            stream=False,
        )

        self.assertEqual(result.data, ["https://cdn.example.com/generated.png"])
        mock_chat.assert_awaited_once()
        kwargs = mock_chat.await_args.kwargs
        self.assertIsNone(kwargs["model"])
        self.assertIsNone(kwargs["mode"])
        self.assertEqual(kwargs["request_overrides"]["modeId"], "auto")


class ImageCollectProcessorTests(unittest.IsolatedAsyncioTestCase):
    @patch("app.services.grok.services.image_edit.get_config", return_value=0)
    @patch(
        "app.services.grok.services.image_edit.BaseProcessor.process_url",
        new_callable=AsyncMock,
    )
    async def test_collect_processor_accepts_render_searched_image(
        self,
        mock_process_url,
        _mock_image_edit_config,
    ):
        mock_process_url.return_value = "https://cdn.example.com/searched.png"
        processor = ImageCollectProcessor(
            "grok-imagine-1.0",
            "token-1",
            response_format="url",
        )

        images = await processor.process(
            _stream_lines(
                orjson.dumps(
                    {
                        "result": {
                            "response": {
                                "cardAttachment": {
                                    "jsonData": orjson.dumps(
                                        {
                                            "type": "render_searched_image",
                                            "image": {
                                                "original": "https://images.example.com/result.png"
                                            },
                                        }
                                    ).decode()
                                }
                            }
                        }
                    }
                )
            )
        )

        self.assertEqual(images, ["https://cdn.example.com/searched.png"])
        mock_process_url.assert_awaited_once_with(
            "https://images.example.com/result.png", "image"
        )
