"""Текстовая рассылка: предпросмотр, подтверждение и доставка без живого Telegram."""
import builtins
import logging
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

_import = builtins.__import__


def _without_aggregator(name, *args, **kwargs):
    return Mock() if name == 'aggregator' else _import(name, *args, **kwargs)


with patch.dict(os.environ, {'LOGS_DIR': '/private/tmp', 'ADMIN_USER_ID': '123'}), \
        patch.object(builtins, '__import__', side_effect=_without_aggregator), \
        patch.object(logging, 'FileHandler', return_value=logging.NullHandler()):
    import telegram_bot as bot


def make_query(user_id=123, message_id=10):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        message=SimpleNamespace(message_id=message_id, reply_text=AsyncMock()),
        edit_message_text=AsyncMock(), answer=AsyncMock(),
    )


class BroadcastTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.admin = patch.object(bot, 'ADMIN_USER_ID', '123')
        self.admin.start()
        self.addCleanup(self.admin.stop)
        self.context = SimpleNamespace(bot_data={}, bot=SimpleNamespace(
            send_message=AsyncMock(), send_photo=AsyncMock(),
        ))

    async def test_preview_is_text_without_link_image_and_does_not_send_to_users(self):
        message = SimpleNamespace(reply_text=AsyncMock(side_effect=[
            SimpleNamespace(message_id=9), SimpleNamespace(message_id=10),
        ]), reply_photo=AsyncMock())
        update = SimpleNamespace(effective_user=SimpleNamespace(id=123), message=message)
        with patch.object(bot, 'get_broadcast_user_ids', return_value=[1, 2]):
            await bot.broadcast_command(update, self.context)
        preview = message.reply_text.call_args_list[0]
        self.assertEqual(preview.args[0], bot.BROADCAST_TEXT)
        self.assertTrue(preview.kwargs['link_preview_options'].is_disabled)
        message.reply_photo.assert_not_called()
        self.context.bot.send_message.assert_not_called()
        self.assertEqual(self.context.bot_data['broadcast_confirmation_id'], 10)

    async def test_non_admin_cannot_preview_or_send(self):
        update = SimpleNamespace(effective_user=SimpleNamespace(id=999), message=SimpleNamespace(reply_text=AsyncMock()))
        await bot.broadcast_command(update, self.context)
        update.message.reply_text.assert_not_called()
        self.context.bot_data['broadcast_confirmation_id'] = 10
        await bot.run_broadcast(make_query(user_id=999), self.context)
        self.context.bot.send_message.assert_not_called()
        self.assertIn('broadcast_confirmation_id', self.context.bot_data)

    async def test_confirmation_is_consumed_and_cannot_send_twice(self):
        query = make_query()
        self.context.bot_data['broadcast_confirmation_id'] = 10
        with patch.object(bot, 'get_broadcast_user_ids', return_value=[1]), \
                patch.object(bot.asyncio, 'sleep', new_callable=AsyncMock):
            await bot.run_broadcast(query, self.context)
            await bot.run_broadcast(query, self.context)
        self.context.bot.send_message.assert_awaited_once()
        self.context.bot.send_photo.assert_not_called()
        args = self.context.bot.send_message.call_args
        self.assertEqual(args.args, (1, bot.BROADCAST_TEXT))
        self.assertTrue(args.kwargs['link_preview_options'].is_disabled)
        self.assertNotIn('broadcast_confirmation_id', self.context.bot_data)

    async def test_old_preview_and_cancelled_preview_cannot_start_delivery(self):
        self.context.bot_data['broadcast_confirmation_id'] = 11
        await bot.run_broadcast(make_query(message_id=10), self.context)
        self.context.bot.send_message.assert_not_called()
        query = make_query(message_id=11)
        query.data = 'broadcast_cancel'
        await bot.button_handler(SimpleNamespace(callback_query=query), self.context)
        await bot.run_broadcast(query, self.context)
        self.context.bot.send_message.assert_not_called()

    async def test_retry_and_delivery_report_remain_text_only(self):
        query = make_query()
        self.context.bot_data['broadcast_confirmation_id'] = 10
        self.context.bot.send_message.side_effect = [
            bot.telegram.error.RetryAfter(2), None,
            bot.telegram.error.Forbidden('Blocked'), RuntimeError('Unavailable'), None,
        ]
        with patch.object(bot, 'get_broadcast_user_ids', return_value=[1, 2, 3, 4]), \
                patch.object(bot, 'mark_bot_blocked') as mark, \
                patch.object(bot.asyncio, 'sleep', new_callable=AsyncMock):
            await bot.run_broadcast(query, self.context)
        mark.assert_called_once_with(2)
        self.assertEqual([c.args[0] for c in self.context.bot.send_message.call_args_list], [1, 1, 2, 3, 4])
        for call in self.context.bot.send_message.call_args_list:
            self.assertTrue(call.kwargs['link_preview_options'].is_disabled)
        self.assertIn('Доставлено: 2\nЗаблокировали бота: 1\nОшибок: 1', query.message.reply_text.call_args.args[0])


if __name__ == '__main__':
    unittest.main()
