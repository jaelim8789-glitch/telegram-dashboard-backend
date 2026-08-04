"""
NOWPayments  
"""

import json
import pytest
from decimal import Decimal
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
    # Mock NOWPayments API — /v1/invoice response shape (see nowpayments.py's
    # create_payment: switched from /v1/payment, which has no hosted
    # checkout page, to /v1/invoice, which returns invoice_url).
    mock_response = {
        "id": "test_invoice_123",
        "price_amount": 99.99,
        "price_currency": "usd",
        "pay_currency": "usdt",
        "order_id": "tenant_test_plan_pro_1234567890",
        "invoice_url": "https://nowpayments.io/payment/?iid=test_invoice_123",
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
        
        assert result["payment_id"] == "test_invoice_123"
        assert result["checkout_url"] == "https://nowpayments.io/payment/?iid=test_invoice_123"
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


def _make_txn(**overrides):
    """Build a minimal NowPaymentsTransaction-like object."""
    defaults = {
        "payment_id": "test_payment_123",
        "tenant_id": "testtenant123",
        "plan_id": "pro",
        "payment_status": "waiting",
        "paid_amount": None,
        "pay_currency": "usdt",
        "order_id": "tenant_testtenant123_plan_pro_1234567890",
        "fulfilled": False,
        "fulfilled_at": None,
        "note": None,
    }
    defaults.update(overrides)
    txn = MagicMock()
    for k, v in defaults.items():
        setattr(txn, k, v)
    return txn


def _mock_claim_success():
    """A mock execute result whose rowcount==1 (the atomic claim succeeds)."""
    r = MagicMock()
    r.rowcount = 1
    return r


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
    mock_transaction = _make_txn()

    # execute() is called for: select txn, then claim UPDATE. First returns the
    # transaction; the claim UPDATE must return rowcount 1.
    mock_execute_result = MagicMock()
    mock_execute_result.scalar_one_or_none.return_value = mock_transaction

    mock_tenant = MagicMock()
    mock_plan = {"prices_usdt": {"monthly": 99.99}}

    api_status = {
        "payment_status": "finished",
        "actually_paid": 99.99,
        "pay_currency": "usdt",
    }

    with patch.object(mock_db, 'execute', side_effect=[mock_execute_result, _mock_claim_success(), _mock_claim_success()]), \
         patch.object(mock_db, 'commit'), \
         patch.object(mock_db, 'get', return_value=mock_tenant), \
         patch('app.services.nowpayments.get_plan', return_value=mock_plan), \
         patch.object(NOWPaymentsService, 'get_payment_status', new=AsyncMock(return_value=api_status)), \
         patch('app.services.nowpayments.activate_tenant_plan', new=AsyncMock(return_value={"success": True, "api_key": "sk-test"})) as mock_activate_plan, \
         patch('app.services.nowpayments.create_commission', new=AsyncMock()) as mock_commission:

        service = NOWPaymentsService()
        await service.process_webhook(webhook_data, mock_db)

        #     -  ( , )
        mock_activate_plan.assert_awaited_once()
        mock_commission.assert_awaited_once()
        assert mock_transaction.fulfilled is True
        assert mock_transaction.fulfilled_at is not None


@pytest.mark.asyncio
async def test_process_webhook_falls_back_to_order_id_when_payment_id_unmatched():
    """create_payment() now creates via /v1/invoice: the stored payment_id is
    the invoice id, but the webhook's payment_id is the real NOWPayments
    payment id assigned once the customer pays -- these never match. The
    webhook must still find the transaction via order_id (which we set at
    creation and NOWPayments echoes back unchanged), and re-verification
    must use the webhook's real payment_id, not the stored invoice id."""
    webhook_data = {
        "payment_id": "real_payment_id_999",  # different from stored invoice id
        "payment_status": "finished",
        "paid_amount": 99.99,
        "pay_currency": "usdt",
        "order_id": "tenant_testtenant123_plan_pro_1234567890",
    }

    mock_db = AsyncMock(spec=AsyncSession)
    mock_transaction = _make_txn(payment_id="stored_invoice_id_123")

    # First execute() (lookup by payment_id) finds nothing; second (lookup
    # by order_id) finds the transaction; third is the fulfillment claim UPDATE.
    no_match_result = MagicMock()
    no_match_result.scalar_one_or_none.return_value = None
    found_via_order_id = MagicMock()
    found_via_order_id.scalar_one_or_none.return_value = mock_transaction

    mock_tenant = MagicMock()
    mock_plan = {"prices_usdt": {"monthly": 99.99}}
    api_status = {"payment_status": "finished", "actually_paid": 99.99, "pay_currency": "usdt"}

    with patch.object(mock_db, 'execute', side_effect=[no_match_result, found_via_order_id, _mock_claim_success()]), \
         patch.object(mock_db, 'commit'), \
         patch.object(mock_db, 'get', return_value=mock_tenant), \
         patch('app.services.nowpayments.get_plan', return_value=mock_plan), \
         patch.object(NOWPaymentsService, 'get_payment_status', new=AsyncMock(return_value=api_status)) as mock_verify, \
         patch('app.services.nowpayments.activate_tenant_plan', new=AsyncMock(return_value={"success": True, "api_key": "sk-test"})), \
         patch('app.services.nowpayments.create_commission', new=AsyncMock()):

        service = NOWPaymentsService()
        await service.process_webhook(webhook_data, mock_db)

        # Re-verification must use the REAL webhook payment_id, not the
        # stored invoice id, since that's the only one NOWPayments'
        # GET /v1/payment/{id} status endpoint can actually resolve.
        mock_verify.assert_awaited_once_with("real_payment_id_999")
        assert mock_transaction.fulfilled is True


@pytest.mark.asyncio
async def test_process_webhook_success_rejects_when_verify_fails():
    """Server-side re-verification failure must NOT fulfill the payment."""
    webhook_data = {
        "payment_id": "test_payment_123",
        "payment_status": "finished",
        "paid_amount": 99.99,
        "pay_currency": "usdt",
        "order_id": "tenant_testtenant123_plan_pro_1234567890"
    }

    mock_db = AsyncMock(spec=AsyncSession)
    mock_transaction = _make_txn()
    mock_execute_result = MagicMock()
    mock_execute_result.scalar_one_or_none.return_value = mock_transaction

    with patch.object(mock_db, 'execute', return_value=mock_execute_result), \
         patch.object(mock_db, 'commit'), \
         patch.object(NOWPaymentsService, 'get_payment_status', new=AsyncMock(return_value=None)), \
         patch('app.services.nowpayments.activate_tenant_plan', new=AsyncMock()) as mock_activate_plan:

        service = NOWPaymentsService()
        await service.process_webhook(webhook_data, mock_db)

        mock_activate_plan.assert_not_called()
        assert mock_transaction.fulfilled is not True


@pytest.mark.asyncio
async def test_process_webhook_success_rejects_when_api_status_not_finished():
    """Re-verification status mismatch must NOT fulfill the payment."""
    webhook_data = {
        "payment_id": "test_payment_123",
        "payment_status": "finished",
        "paid_amount": 99.99,
        "pay_currency": "usdt",
        "order_id": "tenant_testtenant123_plan_pro_1234567890"
    }

    mock_db = AsyncMock(spec=AsyncSession)
    mock_transaction = _make_txn()
    mock_execute_result = MagicMock()
    mock_execute_result.scalar_one_or_none.return_value = mock_transaction

    api_status = {"payment_status": "waiting", "actually_paid": 99.99}

    with patch.object(mock_db, 'execute', return_value=mock_execute_result), \
         patch.object(mock_db, 'commit'), \
         patch.object(NOWPaymentsService, 'get_payment_status', new=AsyncMock(return_value=api_status)), \
         patch('app.services.nowpayments.activate_tenant_plan', new=AsyncMock()) as mock_activate_plan:

        service = NOWPaymentsService()
        await service.process_webhook(webhook_data, mock_db)

        mock_activate_plan.assert_not_called()


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

    #    ( fulfilled=True )
    mock_transaction = _make_txn(fulfilled=True, payment_status="finished")

    mock_execute_result = MagicMock()
    mock_execute_result.scalar_one_or_none.return_value = mock_transaction

    with patch.object(mock_db, 'execute', return_value=mock_execute_result), \
         patch('app.services.nowpayments.activate_tenant_plan', new=AsyncMock()) as mock_activate_plan:

        service = NOWPaymentsService()
        await service.process_webhook(webhook_data, mock_db)

        #       ->   (  )
        mock_activate_plan.assert_not_called()


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
    mock_transaction = _make_txn()

    mock_execute_result = MagicMock()
    mock_execute_result.scalar_one_or_none.return_value = mock_transaction

    mock_plan = {"prices_usdt": {"monthly": 99.99}}

    api_status = {
        "payment_status": "finished",
        "actually_paid": 80.00,
        "pay_currency": "usdt",
    }

    with patch.object(mock_db, 'execute', return_value=mock_execute_result), \
         patch.object(mock_db, 'commit'), \
         patch('app.services.nowpayments.get_plan', return_value=mock_plan), \
         patch.object(NOWPaymentsService, 'get_payment_status', new=AsyncMock(return_value=api_status)), \
         patch('app.services.nowpayments.activate_tenant_plan', new=AsyncMock()) as mock_activate_plan:

        service = NOWPaymentsService()
        await service.process_webhook(webhook_data, mock_db)

        #     
        assert mock_transaction.note is not None
        assert "Amount mismatch" in mock_transaction.note
        #       ( )
        assert mock_transaction.fulfilled is not True
        mock_activate_plan.assert_not_called()


@pytest.mark.asyncio
async def test_process_webhook_concurrent_claim_lost():
    """A second concurrent webhook (claim UPDATE rowcount 0) must not re-fulfill."""
    webhook_data = {
        "payment_id": "race_payment_1",
        "payment_status": "finished",
        "paid_amount": 99.99,
        "pay_currency": "usdt",
        "order_id": "tenant_testtenant123_plan_pro_1234567890"
    }

    mock_db = AsyncMock(spec=AsyncSession)
    # ORM-level check passes (fulfilled=False), but the atomic claim UPDATE
    # returns rowcount 0 because another concurrent webhook claimed it first.
    mock_transaction = _make_txn(payment_id="race_payment_1")
    mock_execute_result = MagicMock()
    mock_execute_result.scalar_one_or_none.return_value = mock_transaction

    mock_tenant = MagicMock()
    mock_plan = {"prices_usdt": {"monthly": 99.99}}
    api_status = {"payment_status": "finished", "actually_paid": 99.99, "pay_currency": "usdt"}

    claim_lost = MagicMock()
    claim_lost.rowcount = 0

    with patch.object(mock_db, 'execute', side_effect=[mock_execute_result, claim_lost]), \
         patch.object(mock_db, 'commit'), \
         patch.object(mock_db, 'get', return_value=mock_tenant), \
         patch('app.services.nowpayments.get_plan', return_value=mock_plan), \
         patch.object(NOWPaymentsService, 'get_payment_status', new=AsyncMock(return_value=api_status)), \
         patch('app.services.nowpayments.activate_tenant_plan', new=AsyncMock()) as mock_activate_plan, \
         patch('app.services.nowpayments.create_commission', new=AsyncMock()) as mock_commission:

        service = NOWPaymentsService()
        await service.process_webhook(webhook_data, mock_db)

        # The losing webhook must NOT issue a second API key / commission.
        mock_activate_plan.assert_not_called()
        mock_commission.assert_not_called()


@pytest.mark.asyncio
async def test_process_webhook_non_usdt_currency_uses_rate():
    """BTC (non-1:1) payment should be validated via the USD exchange rate."""
    webhook_data = {
        "payment_id": "btc_payment_1",
        "payment_status": "finished",
        "paid_amount": 0.0015,  # BTC
        "pay_currency": "btc",
        "order_id": "tenant_testtenant123_plan_pro_1234567890"
    }

    mock_db = AsyncMock(spec=AsyncSession)
    mock_transaction = _make_txn(payment_id="btc_payment_1", pay_currency="btc")

    mock_execute_result = MagicMock()
    mock_execute_result.scalar_one_or_none.return_value = mock_transaction

    mock_tenant = MagicMock()
    mock_plan = {"prices_usdt": {"monthly": 99.99}}

    api_status = {
        "payment_status": "finished",
        "actually_paid": 0.0015,
        "pay_currency": "btc",
    }

    # 0.0015 BTC at 66660 USD/BTC == 99.99 USD
    with patch.object(mock_db, 'execute', side_effect=[mock_execute_result, _mock_claim_success(), _mock_claim_success()]), \
         patch.object(mock_db, 'commit'), \
         patch.object(mock_db, 'get', return_value=mock_tenant), \
         patch('app.services.nowpayments.get_plan', return_value=mock_plan), \
         patch.object(NOWPaymentsService, 'get_payment_status', new=AsyncMock(return_value=api_status)), \
         patch.object(NOWPaymentsService, '_get_usd_rate', new=AsyncMock(return_value=Decimal("66660.00"))), \
         patch('app.services.nowpayments.activate_tenant_plan', new=AsyncMock(return_value={"success": True, "api_key": "sk-test"})) as mock_activate_plan, \
         patch('app.services.nowpayments.create_commission', new=AsyncMock()):

        service = NOWPaymentsService()
        await service.process_webhook(webhook_data, mock_db)

        mock_activate_plan.assert_awaited_once()
        assert mock_transaction.fulfilled is True


@pytest.mark.asyncio
async def test_process_webhook_parse_order_id_with_underscores():
    """order_id parsing must survive tenant ids that contain underscores."""
    service = NOWPaymentsService()
    tenant, plan = service._parse_order_id("tenant_tenant_abc_123_plan_pro_98765")
    assert tenant == "tenant_abc_123"
    assert plan == "pro"


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