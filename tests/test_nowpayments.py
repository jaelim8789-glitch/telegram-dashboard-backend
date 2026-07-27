"""
NOWPayments  
"""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession
from app.main import app
from app.config import settings
from app.services.nowpayments import NOWPaymentsService
from app.models.tenant import Tenant
from app.models.nowpayments import NowPaymentsTransaction
from app.core.plans import PLAN_CATALOG


@pytest.fixture
def client():
    """Test client fixture"""
    with TestClient(app) as c:
        yield c


@pytest.mark.asyncio
async def test_create_invoice():
    """  """
    # Mock NOWPayments API 
    mock_response = {
        "payment_id": "test_payment_123",
        "price_amount": 99.99,
        "price_currency": "usd",
        "pay_currency": "usdt",
        "order_id": "tenant_test_plan_pro_1234567890",
        "payment_status": "waiting",
        "pay_address": "test_address_123",
        "created_at": "2023-01-01T00:00:00Z"
    }
    
    mock_response_obj = MagicMock()
    mock_response_obj.status_code = 200
    mock_response_obj.json.return_value = mock_response

    mock_db_session = AsyncMock()
    mock_session_maker = MagicMock()
    mock_session_maker.return_value.__aenter__.return_value = mock_db_session
    mock_session_maker.return_value.__aexit__.return_value = None

    with patch('app.services.nowpayments.httpx.AsyncClient') as mock_client_class, \
         patch('app.database.async_session_maker', mock_session_maker):
        mock_client_instance = AsyncMock()
        mock_client_class.return_value.__aenter__.return_value = mock_client_instance
        mock_client_instance.post.return_value = mock_response_obj

        service = NOWPaymentsService()
        result = await service.create_payment(
            amount=99.99,
            currency="usdt",
            plan_id="pro",
            tenant_id="test_tenant_123",
            order_description="Test Pro Subscription"
        )
        
        assert result["payment_id"] == "test_payment_123"
        assert result["price_amount"] == 99.99
        assert result["pay_currency"] == "usdt"


def test_verify_webhook_signature():
    """   """
    service = NOWPaymentsService()
    
    #     
    service.ipn_secret = "test_secret_123"
    
    payload = b'{"payment_id": "test_payment_123", "status": "finished"}'
    
    #   
    import hmac
    import hashlib
    expected_signature = hmac.new(
        service.ipn_secret.encode('utf-8'),
        payload,
        hashlib.sha512
    ).hexdigest()
    
    #   
    assert service.verify_webhook_signature(payload, expected_signature) == True
    
    #   
    assert service.verify_webhook_signature(payload, "invalid_signature") == False


@pytest.mark.asyncio
async def test_process_webhook_success():
    """   """
    webhook_data = {
        "payment_id": "test_payment_123",
        "payment_status": "finished",
        "paid_amount": 99.99,
        "pay_currency": "usdt",
        "order_id": "tenant_testtenant123_plan_pro_1234567890"
    }
    
    # Mock  
    mock_db = AsyncMock(spec=AsyncSession)
    
    #     
    mock_transaction = MagicMock()
    mock_transaction.payment_status = "waiting"
    mock_transaction.paid_amount = None
    mock_transaction.pay_currency = "usdt"
    
    # select   
    mock_execute_result = MagicMock()
    mock_execute_result.scalar_one_or_none.return_value = mock_transaction
    
    mock_tenant_result = MagicMock()
    mock_tenant_result.scalar_one_or_none.return_value = MagicMock()
    mock_plan = {"prices_usdt": {"monthly": 99.99}}

    with patch.object(mock_db, 'execute', side_effect=[mock_execute_result, mock_tenant_result]), \
         patch.object(mock_db, 'commit'), \
         patch('app.services.nowpayments.get_plan', return_value=mock_plan), \
         patch('app.services.nowpayments.activate_tenant_plan') as mock_activate_plan:

        service = NOWPaymentsService()
        await service.process_webhook(webhook_data, mock_db)

        #    
        mock_db.commit.assert_called_once()

        #    
        mock_activate_plan.assert_called_once()


@pytest.mark.asyncio
async def test_process_webhook_duplicate():
    """   """
    webhook_data = {
        "payment_id": "existing_payment_123",
        "payment_status": "finished",
        "paid_amount": 99.99,
        "pay_currency": "usdt",
        "order_id": "tenant_testtenant123_plan_pro_1234567890"
    }
    
    # Mock  
    mock_db = AsyncMock(spec=AsyncSession)
    
    #   
    mock_transaction = MagicMock()
    mock_transaction.payment_status = "finished"
    mock_transaction.paid_amount = 99.99
    mock_transaction.pay_currency = "usdt"
    
    # select   
    mock_execute_result = MagicMock()
    mock_execute_result.scalar_one_or_none.return_value = mock_transaction
    
    with patch.object(mock_db, 'execute', return_value=mock_execute_result):
        service = NOWPaymentsService()
        await service.process_webhook(webhook_data, mock_db)
        
        #        


@pytest.mark.asyncio
async def test_process_webhook_amount_mismatch():
    """   """
    webhook_data = {
        "payment_id": "test_payment_123",
        "payment_status": "finished",
        "paid_amount": 80.00,  #   
        "pay_currency": "usdt",
        "order_id": "tenant_testtenant123_plan_pro_1234567890"
    }
    
    # Mock  
    mock_db = AsyncMock(spec=AsyncSession)
    
    #     
    mock_transaction = MagicMock()
    mock_transaction.payment_status = "waiting"
    mock_transaction.paid_amount = None
    mock_transaction.pay_currency = "usdt"
    
    # select   
    mock_execute_result = MagicMock()
    mock_execute_result.scalar_one_or_none.return_value = mock_transaction

    mock_plan = {"prices_usdt": {"monthly": 99.99}}

    with patch.object(mock_db, 'execute', return_value=mock_execute_result), \
         patch.object(mock_db, 'commit'), \
         patch('app.services.nowpayments.get_plan', return_value=mock_plan):

        service = NOWPaymentsService()
        await service.process_webhook(webhook_data, mock_db)

        #     
        assert mock_transaction.note is not None
        assert "Amount mismatch" in mock_transaction.note


def test_nowpayments_service_initialization():
    """NOWPayments     settings   """
    with patch.object(settings, 'NOWPAYMENTS_API_KEY', 'test_api_key'), \
         patch.object(settings, 'NOWPAYMENTS_PUBLIC_KEY', 'test_public_key'), \
         patch.object(settings, 'NOWPAYMENTS_IPN_SECRET', 'test_secret'):

        service = NOWPaymentsService()

        assert service.api_key == 'test_api_key'
        assert service.public_key == 'test_public_key'
        assert service.ipn_secret == 'test_secret'
        assert service.base_url == "https://api.nowpayments.io/v1"