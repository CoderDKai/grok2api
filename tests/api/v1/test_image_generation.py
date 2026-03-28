import unittest
from unittest.mock import AsyncMock, Mock, patch

import orjson

from app.services.grok.services.image import ImageGenerationService
from app.services.grok.services.image_edit import ImageCollectProcessor, ImageStreamProcessor
from app.services.grok.services.chat import StreamProcessor
from app.services.grok.services.image import ImageWSStreamProcessor
from app.services.grok.services.model import ModelService
from app.services.reverse.app_chat import AppChatReverse
from app.core.exceptions import UpstreamException


def _stream_lines(*payloads):
    async def _gen():
        for payload in payloads:
            yield payload

    return _gen()


def _parse_sse_data(chunk: str):
    prefix = "data: "
    assert chunk.startswith(prefix), chunk
    return orjson.loads(chunk[len(prefix) :].strip())


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


class StreamProcessorTests(unittest.IsolatedAsyncioTestCase):
    @patch("app.services.grok.services.chat.get_config")
    async def test_first_text_chunk_carries_role_and_content(
        self,
        mock_get_config,
    ):
        def config_side_effect(key, default=None):
            if key == "app.filter_tags":
                return []
            if key == "chat.stream_timeout":
                return 1
            return default

        mock_get_config.side_effect = config_side_effect
        processor = StreamProcessor("grok-420", "token-1", show_think=False)

        chunks = []
        async for chunk in processor.process(
            _stream_lines(
                orjson.dumps(
                    {
                        "result": {
                            "response": {
                                "token": "hello",
                                "responseId": "resp-1",
                            }
                        }
                    }
                )
            )
        ):
            chunks.append(chunk)

        self.assertGreaterEqual(len(chunks), 3)
        first = _parse_sse_data(chunks[0])
        self.assertEqual(
            first["choices"][0]["delta"],
            {"role": "assistant", "content": "hello"},
        )

    @patch("app.services.grok.services.chat.get_config")
    async def test_empty_stream_raises_upstream_error(
        self,
        mock_get_config,
    ):
        def config_side_effect(key, default=None):
            if key == "app.filter_tags":
                return []
            if key == "chat.stream_timeout":
                return 1
            return default

        mock_get_config.side_effect = config_side_effect
        processor = StreamProcessor("grok-420", "token-1", show_think=False)

        with self.assertRaises(UpstreamException) as ctx:
            async for _ in processor.process(_stream_lines()):
                pass

        self.assertEqual(
            ctx.exception.details["type"],
            "empty_stream_response",
        )


class ImageStreamCompatibilityTests(unittest.IsolatedAsyncioTestCase):
    async def test_image_ws_chat_stream_reports_error_when_no_image_emitted(self):
        processor = ImageWSStreamProcessor(
            "grok-imagine-1.0",
            "token-1",
            n=1,
            response_format="url",
            size="1024x1024",
            chat_format=True,
        )

        chunks = []
        async for chunk in processor.process(_stream_lines()):
            chunks.append(chunk)

        self.assertEqual(len(chunks), 2)
        self.assertIn("event: error", chunks[0])
        self.assertIn('"code":"empty_image_stream"', chunks[0])
        self.assertEqual(chunks[1], "data: [DONE]\n\n")

    @patch("app.services.grok.services.image_edit.get_config", return_value=1)
    async def test_image_edit_chat_stream_reports_error_when_no_image_emitted(
        self,
        _mock_get_config,
    ):
        processor = ImageStreamProcessor(
            "grok-imagine-1.0-edit",
            "token-1",
            n=1,
            response_format="url",
            chat_format=True,
        )

        chunks = []
        async for chunk in processor.process(_stream_lines()):
            chunks.append(chunk)

        self.assertEqual(len(chunks), 2)
        self.assertIn("event: error", chunks[0])
        self.assertIn('"code":"empty_image_stream"', chunks[0])
        self.assertEqual(chunks[1], "data: [DONE]\n\n")
