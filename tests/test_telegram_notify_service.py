import pytest
from unittest.mock import AsyncMock, patch, MagicMock

from app.services.telegram_notify import send_telegram_message


@pytest.mark.asyncio
async def test_send_telegram_message_with_valid_token():
    """Test sending a message with a valid token"""
    # Mock the Bot instance and its send_message method
    mock_bot = AsyncMock()
    mock_bot.send_message = AsyncMock(return_value=True)
    
    with patch('app.services.telegram_notify.Bot', return_value=mock_bot):
        with patch('app.services.telegram_notify.settings', telegram_bot_token='fake_token'):
            result = await send_telegram_message(12345, "Test message")
            
            # Assert the message was sent
            mock_bot.send_message.assert_called_once_with(
                chat_id=12345, 
                text="Test message", 
                parse_mode=None
            )
            assert result is True


@pytest.mark.asyncio
async def test_send_telegram_message_no_token():
    """Test that no message is sent when there's no token"""
    with patch('app.services.telegram_notify.settings', telegram_bot_token=None):
        result = await send_telegram_message(12345, "Test message")
        
        # Should return False without attempting to send
        assert result is False


@pytest.mark.asyncio
async def test_send_telegram_message_telegram_error():
    """Test handling of TelegramError"""
    from telegram.error import TelegramError
    
    mock_bot = AsyncMock()
    mock_bot.send_message.side_effect = TelegramError("Forbidden: bot was blocked by the user")
    
    with patch('app.services.telegram_notify.Bot', return_value=mock_bot):
        with patch('app.services.telegram_notify.settings', telegram_bot_token='fake_token'):
            result = await send_telegram_message(12345, "Test message")
            
            # Should return False when TelegramError occurs
            assert result is False


@pytest.mark.asyncio
async def test_send_telegram_message_general_exception():
    """Test handling of general exceptions"""
    mock_bot = AsyncMock()
    mock_bot.send_message.side_effect = Exception("Network error")
    
    with patch('app.services.telegram_notify.Bot', return_value=mock_bot):
        with patch('app.services.telegram_notify.settings', telegram_bot_token='fake_token'):
            result = await send_telegram_message(12345, "Test message")
            
            # Should return False when any exception occurs
            assert result is False


@pytest.mark.asyncio
async def test_send_telegram_message_with_parse_mode():
    """Test sending a message with parse_mode"""
    mock_bot = AsyncMock()
    mock_bot.send_message = AsyncMock(return_value=True)
    
    with patch('app.services.telegram_notify.Bot', return_value=mock_bot):
        with patch('app.services.telegram_notify.settings', telegram_bot_token='fake_token'):
            result = await send_telegram_message(12345, "Test message", parse_mode="HTML")
            
            # Assert the message was sent with parse_mode
            mock_bot.send_message.assert_called_once_with(
                chat_id=12345, 
                text="Test message", 
                parse_mode="HTML"
            )
            assert result is True