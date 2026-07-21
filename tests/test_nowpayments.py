"""
NOWPayments 서비스 테스트
"""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession
from app.main import app
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
    """인보이스 생성 테스트"""
    # Mock NOWPayments API 응답
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
    
    # Mock the httpx client
    with patch('app.services.nowpayments.httpx.AsyncClient') as mock_client_class:
        mock_client_instance = AsyncMock()
        mock_client_class.return_value.__aenter__.return_value = mock_client_instance
        mock_response_obj = MagicMock()
        mock_response_obj.status_code = 200
        mock_response_obj.json.return_value = mock_response
        mock_client_instance.post.return_value = mock_response_obj
        
        # Mock the database transaction creation
        with patch('app.services.nowpayments.NowPaymentsTransaction') as mock_transaction_class:
            mock_transaction = MagicMock()
            mock_transaction_class.return_value = mock_transaction
            
            # Mock settings
            with patch('app.services.nowpayments.settings') as mock_settings:
                mock_settings.NOWPAYMENTS_API_KEY = 'test_api_key'
                mock_settings.NOWPAYMENTS_PUBLIC_KEY = 'test_public_key'
                mock_settings.NOWPAYMENTS_IPN_SECRET = 'test_secret'
                mock_settings.base_url = 'https://api.nowpayments.io/v1'
                mock_settings.frontend_url = 'https://example.com'
                
                service = NOWPaymentsService()

                # Call the method (inside the settings patch, so base_url/
                # frontend_url lookups during create_payment hit the mock)
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
    """웹훅 서명 검증 테스트"""
    service = NOWPaymentsService()
    
    # Set the IPN secret directly for testing
    service.ipn_secret = "test_secret_123"
    
    payload = b'{"payment_id": "test_payment_123", "status": "finished"}'
    
    # 실제 서명 생성
    import hmac
    import hashlib
    expected_signature = hmac.new(
        service.ipn_secret.encode('utf-8'),
        payload,
        hashlib.sha512
    ).hexdigest()
    
    # 올바른 서명 테스트
    assert service.verify_webhook_signature(payload, expected_signature) == True
    
    # 잘못된 서명 테스트
    assert service.verify_webhook_signature(payload, "invalid_signature") == False


@pytest.mark.asyncio
async def test_process_webhook_success():
    """웹훅 처리 성공 테스트"""
    webhook_data = {
        "payment_id": "test_payment_123",
        "payment_status": "finished",
        "paid_amount": 99.99,
        "pay_currency": "usdt",
        "order_id": "tenant_test_tenant_123_plan_pro_1234567890"
    }
    
    # Mock 데이터베이스 세션
    mock_db = AsyncMock(spec=AsyncSession)
    
    # 기존 거래 조회를 위한 모킹
    mock_transaction = MagicMock()
    mock_transaction.payment_status = "waiting"
    mock_transaction.paid_amount = None
    mock_transaction.pay_currency = "usdt"
    
    # Mock select 쿼리 결과
    mock_select_result = MagicMock()
    mock_select_result.scalar_one_or_none.return_value = mock_transaction
    
    # Mock tenant lookup
    mock_tenant = MagicMock()
    mock_tenant_result = MagicMock()
    mock_tenant_result.scalar_one_or_none.return_value = mock_tenant

    # Mock plan lookup
    mock_plan = MagicMock()
    mock_plan.price_usd = 99.99

    with patch('app.services.nowpayments.select') as mock_select_func, \
         patch.object(mock_db, 'execute', side_effect=[mock_select_result, mock_tenant_result]), \
         patch('app.services.nowpayments.get_plan', return_value=mock_plan), \
         patch('app.services.nowpayments.activate_tenant_plan') as mock_activate_plan, \
         patch('app.services.nowpayments.Tenant') as mock_tenant_class:

        service = NOWPaymentsService()
        await service.process_webhook(webhook_data, mock_db)

        # 플랜 적용이 호출되었는지 확인
        mock_activate_plan.assert_called_once()


@pytest.mark.asyncio
async def test_process_webhook_duplicate():
    """웹훅 중복 처리 테스트"""
    webhook_data = {
        "payment_id": "existing_payment_123",
        "payment_status": "finished",
        "paid_amount": 99.99,
        "pay_currency": "usdt",
        "order_id": "tenant_test_tenant_123_plan_pro_1234567890"
    }
    
    # Mock 데이터베이스 세션
    mock_db = AsyncMock(spec=AsyncSession)
    
    # 이미 완료된 거래
    mock_transaction = MagicMock()
    mock_transaction.payment_status = "finished"
    mock_transaction.paid_amount = 99.99
    mock_transaction.pay_currency = "usdt"
    
    # select 쿼리 결과 모킹
    mock_select_result = MagicMock()
    mock_select_result.scalar_one_or_none.return_value = mock_transaction

    with patch('app.services.nowpayments.select'), \
         patch.object(mock_db, 'execute', side_effect=[mock_select_result]):

        service = NOWPaymentsService()
        await service.process_webhook(webhook_data, mock_db)

        # 중복 결제이므로 추가 작업(commit 등)이 수행되지 않아야 함
        mock_db.commit.assert_not_called()


@pytest.mark.asyncio
async def test_process_webhook_amount_mismatch():
    """웹훅 금액 불일치 테스트"""
    webhook_data = {
        "payment_id": "test_payment_123",
        "payment_status": "finished",
        "paid_amount": 80.00,  # 기대 금액과 다름
        "pay_currency": "usdt",
        "order_id": "tenant_test_tenant_123_plan_pro_1234567890"
    }
    
    # Mock 데이터베이스 세션
    mock_db = AsyncMock(spec=AsyncSession)
    
    # 기존 거래 조회를 위한 모킹
    mock_transaction = MagicMock()
    mock_transaction.payment_status = "waiting"
    mock_transaction.paid_amount = None
    mock_transaction.pay_currency = "usdt"
    
    # select 쿼리 결과 모킹
    mock_transaction_result = MagicMock()
    mock_transaction_result.scalar_one_or_none.return_value = mock_transaction

    # Mock plan lookup
    mock_plan = MagicMock()
    mock_plan.price_usd = 99.99  # Expected amount

    with patch('app.services.nowpayments.select') as mock_select_func, \
         patch.object(mock_db, 'execute', side_effect=[mock_transaction_result]), \
         patch('app.services.nowpayments.get_plan', return_value=mock_plan), \
         patch('app.services.nowpayments.Tenant'):

        service = NOWPaymentsService()
        await service.process_webhook(webhook_data, mock_db)
        
        # 노트에 금액 불일치가 기록되었는지 확인
        assert mock_transaction.note is not None
        assert "Amount mismatch" in mock_transaction.note


def test_nowpayments_service_initialization():
    """NOWPayments 서비스 초기화 테스트 - settings 객체 직접 패치"""
    from app.config import settings
    
    # Mock the settings object attributes
    with patch.object(settings, 'NOWPAYMENTS_API_KEY', 'test_api_key'), \
         patch.object(settings, 'NOWPAYMENTS_PUBLIC_KEY', 'test_public_key'), \
         patch.object(settings, 'NOWPAYMENTS_IPN_SECRET', 'test_secret'):
        
        service = NOWPaymentsService()
        
        assert service.api_key == 'test_api_key'
        assert service.public_key == 'test_public_key'
        assert service.ipn_secret == 'test_secret'
        assert service.base_url == 'https://api.nowpayments.io/v1'