import pytest
from unittest.mock import AsyncMock, patch, MagicMock
import asyncio

from app.services.sms_service import send_verification_sms, SmsSendError


@pytest.mark.asyncio
async def test_send_verification_sms_console_provider():
    """Test sending SMS with console provider"""
    with patch('app.services.sms_service.settings', sms_provider="console"):
        with patch('app.services.sms_service.logger') as mock_logger:
            # Should not raise any exception and just log
            await send_verification_sms("+1234567890", "123456")
            
            # Verify logging was called
            mock_logger.info.assert_called_once()


@pytest.mark.asyncio
async def test_send_verification_sms_unknown_provider():
    """Test that unknown SMS provider raises exception"""
    with patch('app.services.sms_service.settings', sms_provider="unknown"):
        with pytest.raises(SmsSendError) as exc_info:
            await send_verification_sms("+1234567890", "123456")
        
        assert "알 수 없는 SMS_PROVIDER 설정입니다" in str(exc_info.value)


@pytest.mark.asyncio
async def test_send_verification_sms_twilio_provider():
    """Test sending SMS with Twilio provider"""
    with patch('app.services.sms_service.settings', 
               sms_provider="twilio",
               twilio_account_sid="test_sid",
               twilio_auth_token="test_token",
               twilio_phone_number="+1234567890"):
        
        # Mock the twilio client - need to patch where the function imports Client
        mock_twilio_client = MagicMock()
        mock_twilio_messages = MagicMock()
        mock_twilio_client.messages = mock_twilio_messages
        
        # Patch the Client class inside _send_via_twilio function
        with patch('app.services.sms_service._send_via_twilio') as mock_send_via_twilio:
            await send_verification_sms("+0987654321", "123456")
            
            # Verify _send_via_twilio was called
            mock_send_via_twilio.assert_called_once()


@pytest.mark.asyncio
async def test_send_verification_sms_twilio_missing_config():
    """Test that Twilio provider raises exception when config is missing"""
    with patch('app.services.sms_service.settings',
               sms_provider="twilio",
               twilio_account_sid=None,
               twilio_auth_token=None,
               twilio_phone_number=None):
        
        with pytest.raises(SmsSendError) as exc_info:
            await send_verification_sms("+1234567890", "123456")
        
        assert "TWILIO_ACCOUNT_SID / TWILIO_AUTH_TOKEN / TWILIO_PHONE_NUMBER가 설정되지 않았습니다" in str(exc_info.value)


@pytest.mark.asyncio
async def test_send_verification_sms_coolsms_provider():
    """Test sending SMS with Coolsms provider"""
    with patch('app.services.sms_service.settings',
               sms_provider="coolsms",
               coolsms_api_key="test_key",
               coolsms_api_secret="test_secret",
               coolsms_phone_number="+1234567890"):
        
        # Mock httpx.AsyncClient and its response
        mock_response = AsyncMock()
        mock_response.status_code = 200
        
        mock_async_client = AsyncMock()
        mock_async_client.__aenter__ = AsyncMock(return_value=mock_async_client)
        mock_async_client.__aexit__ = AsyncMock(return_value=None)
        mock_async_client.post = AsyncMock(return_value=mock_response)
        
        with patch('app.services.sms_service.httpx.AsyncClient', return_value=mock_async_client):
            # Should not raise any exception
            await send_verification_sms("+0987654321", "123456")


@pytest.mark.asyncio
async def test_send_verification_sms_coolsms_missing_config():
    """Test that Coolsms provider raises exception when config is missing"""
    with patch('app.services.sms_service.settings',
               sms_provider="coolsms",
               coolsms_api_key=None,
               coolsms_api_secret=None,
               coolsms_phone_number=None):
        
        with pytest.raises(SmsSendError) as exc_info:
            await send_verification_sms("+1234567890", "123456")
        
        assert "COOLSMS_API_KEY / COOLSMS_API_SECRET / COOLSMS_PHONE_NUMBER가 설정되지 않았습니다" in str(exc_info.value)