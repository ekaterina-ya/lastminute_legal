"""Проверки моделей по этапам, уровней thinking и учёта расхода без вызовов API."""
import unittest
from unittest.mock import AsyncMock, Mock, patch

from google.genai import types

import backend_logic as backend


def make_response():
    return types.GenerateContentResponse(
        candidates=[types.Candidate(
            content=types.Content(parts=[types.Part(text='Готовое заключение')]),
            finish_reason='STOP',
        )],
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=100, candidates_token_count=20,
            cached_content_token_count=10, thoughts_token_count=50,
            total_token_count=170,
        ),
    )


class ModelRoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.sdk = Mock()
        self.sdk.aio.models.generate_content = AsyncMock(return_value=make_response())
        patcher = patch.object(backend.genai, 'Client', return_value=self.sdk)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = backend.GeminiClient(
            api_key='test-key', primary_model='gemini-4-hydrogen',
            fallback_model='gemini-3.1-pro-preview', embedding_model='embedding',
            preprocess_model='gemini-3.1-pro-preview', preprocess_thinking_level='low',
            primary_thinking_level='high', fallback_thinking_level='medium',
        )

    async def test_primary_stages_and_fallback_use_requested_model_and_thinking(self):
        cases = [
            ('preprocess', False, 'gemini-3.1-pro-preview', types.ThinkingLevel.LOW),
            ('final', False, 'gemini-4-hydrogen', types.ThinkingLevel.HIGH),
            ('preprocess', True, 'gemini-3.1-pro-preview', types.ThinkingLevel.MEDIUM),
            ('final', True, 'gemini-3.1-pro-preview', types.ThinkingLevel.MEDIUM),
        ]
        for stage, fallback, model, level in cases:
            with self.subTest(stage=stage, fallback=fallback):
                self.sdk.aio.models.generate_content.reset_mock()
                result = await self.client.generate(['Креатив'], use_fallback=fallback, stage=stage)
                call = self.sdk.aio.models.generate_content.call_args.kwargs
                self.assertEqual(call['model'], model)
                self.assertEqual(call['config'].thinking_config.thinking_level, level)
                self.assertEqual(result, {'status':'SUCCESS', 'text':'Готовое заключение', 'model':model})

    async def test_legacy_configuration_preserves_model_and_api_default_thinking(self):
        client = backend.GeminiClient('test-key', 'old-primary', 'old-fallback', 'embedding')
        for stage in ['preprocess', 'final']:
            await client.generate(['Креатив'], stage=stage)
            call = self.sdk.aio.models.generate_content.call_args.kwargs
            self.assertEqual(call['model'], 'old-primary')
            self.assertIsNone(call['config'].thinking_config)

    async def test_usage_logs_include_thinking_and_keep_existing_parser_prefix(self):
        logger = Mock()
        await self.client.generate(['Креатив'], user_logger=logger, stage='final')
        line = logger.info.call_args_list[0].args[0]
        self.assertRegex(line, r'\[USAGE\] stage=final model=gemini-4-hydrogen input=100 output=20 cached=10 ok=1')
        self.assertIn('thinking=50 level=high', line)
        self.assertRegex(line, r'duration_ms=\d+$')
        self.assertEqual(backend._extract_usage(Mock(usage_metadata=None)), (0, 0, 0, 0))

    async def test_failed_call_retains_selected_stage_model_and_logs_no_known_usage(self):
        self.sdk.aio.models.generate_content.side_effect = RuntimeError('Временный сбой')
        logger = Mock()
        result = await self.client.generate(['Креатив'], user_logger=logger, stage='preprocess')
        self.assertEqual(result['status'], 'ERROR')
        self.assertEqual(result['model'], 'gemini-3.1-pro-preview')
        line = logger.info.call_args.args[0]
        self.assertIn('input=0 output=0 cached=0 ok=0 thinking=0 level=low', line)


if __name__ == '__main__':
    unittest.main()
