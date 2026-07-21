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
    
    with patch('httpx.AsyncClient.post') as mock_post:
        mock_post.return_value = AsyncMock()
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = mock_response
        
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
    """웹훅 서명 검증 테스트"""
    service = NOWPaymentsService()
    
    # 실제 시크릿이 없을 경우 테스트
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
    
    # select 쿼리 결과 모킹
    mock_execute_result = MagicMock()
    mock_execute_result.scalar_one_or_none.return_value = mock_transaction
    
    with patch.object(mock_db, 'execute', return_value=mock_execute_result), \
         patch.object(mock_db, 'commit'), \
         patch('app.services.nowpayments.apply_plan_to_tenant') as mock_apply_plan:
        
        service = NOWPaymentsService()
        await service.process_webhook(webhook_data, mock_db)
        
        # 데이터베이스 커밋이 호출되었는지 확인
        mock_db.commit.assert_called_once()
        
        # 플랜 적용이 호출되었는지 확인
        mock_apply_plan.assert_called_once()


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
    mock_execute_result = MagicMock()
    mock_execute_result.scalar_one_or_none.return_value = mock_transaction
    
    with patch.object(mock_db, 'execute', return_value=mock_execute_result):
        service = NOWPaymentsService()
        await service.process_webhook(webhook_data, mock_db)
        
        # 이미 완료된 상태이므로 추가 작업이 수행되지 않아야 함


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
    mock_execute_result = MagicMock()
    mock_execute_result.scalar_one_or_none.return_value = mock_transaction
    
    with patch.object(mock_db, 'execute', return_value=mock_execute_result), \
         patch.object(mock_db, 'commit'):
        
        service = NOWPaymentsService()
        await service.process_webhook(webhook_data, mock_db)
        
        # 노트에 금액 불일치가 기록되었는지 확인
        assert mock_transaction.note is not None
        assert "Amount mismatch" in mock_transaction.note


def test_nowpayments_service_initialization():
    """NOWPayments 서비스 초기화 테스트"""
    import os
    os.environ["NOWPAYMENTS_API_KEY"] = "test_api_key"
    os.environ["NOWPAYMENTS_PUBLIC_KEY"] = "test_public_key"
    os.environ["NOWPAYMENTS_IPN_SECRET"] = "test_secret"
    
    service = NOWPaymentsService()
    
    assert service.api_key == "test_api_key"
    assert service.public_key == "test_public_key"
    assert service.ipn_secret == "test_secret"
    assert service.base_url == "https://api.nowpayments.io/v1"