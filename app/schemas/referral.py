from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class GenerateReferralCodeResponse(BaseModel):
    code: str
    referral_code_id: str


class ReferralReferredUser(BaseModel):
    tenant_id: str
    phone: str
    plan: str
    has_paid: bool
    joined_at: datetime | None = None


class ReferralDashboardResponse(BaseModel):
    my_code: str | None = None
    referral_code_id: str | None = None
    referred_users: list[ReferralReferredUser] = []
    pending_commission_total: int = 0
    paid_commission_total: int = 0


class AdminPendingCommissionItem(BaseModel):
    id: str
    referrer_id: str
    referrer_phone: str
    referred_user_phone: str
    source_type: str
    amount: int
    commission_rate: float
    commission_amount: int
    created_at: datetime


class AdminPendingCommissionResponse(BaseModel):
    items: list[AdminPendingCommissionItem] = []
    total_count: int = 0


class PayoutRecord(BaseModel):
    id: str
    referrer_id: str
    referrer_phone: str
    amount: int
    status: str
    paid_at: datetime | None = None
    created_at: datetime


class ProcessPayoutResponse(BaseModel):
    success: bool
    payouts_created: int
    total_amount: int
    message: str


class LeaderboardEntry(BaseModel):
    rank: int
    referrer_id: str
    phone: str
    referral_count: int
    total_commission_earned: int
    tier: str


class LeaderboardResponse(BaseModel):
    items: list[LeaderboardEntry] = []


class DailyStatsItem(BaseModel):
    date: str
    signups: int = 0
    commissions: int = 0


class ReferralStatsResponse(BaseModel):
    total_referrers: int = 0
    total_referred: int = 0
    total_commissions_pending: int = 0
    total_commissions_paid: int = 0
    total_commission_amount_pending: int = 0
    total_commission_amount_paid: int = 0
    daily: list[DailyStatsItem] = []


class SetChatIdRequest(BaseModel):
    chat_id: str


class SetWalletRequest(BaseModel):
    wallet_address: str = Field(min_length=10, max_length=100)


class ChangeCodeRequest(BaseModel):
    new_code: str = Field(min_length=3, max_length=20, pattern=r"^[A-Za-z0-9가-힣]+$")


class CommissionItem(BaseModel):
    id: str
    referred_user_phone: str
    source_type: str
    amount: int
    commission_rate: float
    commission_amount: int
    status: str
    created_at: datetime


class MyCommissionsResponse(BaseModel):
    items: list[CommissionItem] = []
    total_count: int = 0


class AdminSettingItem(BaseModel):
    key: str
    value: str


class AdminSettingsResponse(BaseModel):
    settings: list[AdminSettingItem] = []


class UpdateSettingsRequest(BaseModel):
    settings: list[AdminSettingItem]


class AdminCodeStatsItem(BaseModel):
    code: str
    owner_phone: str
    used_count: int
    expires_at: datetime | None = None
    created_at: datetime


class AdminCodeStatsResponse(BaseModel):
    items: list[AdminCodeStatsItem] = []
