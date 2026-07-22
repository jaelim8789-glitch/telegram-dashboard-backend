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
    level: int | None = None


class ReferralDashboardResponse(BaseModel):
    my_code: str | None = None
    referral_code_id: str | None = None
    referred_users: list[ReferralReferredUser] = []
    pending_commission_total: int = 0
    paid_commission_total: int = 0
    tier_label: str = "기본"
    tier_rate: float = 0.50
    distributor_level: int = 1
    badges: list[str] = []
    weekly_referrals: int = 0
    conversion_stats: dict = {}


class AdminPendingCommissionItem(BaseModel):
    id: str
    referrer_id: str
    referrer_phone: str
    referred_user_phone: str
    source_type: str
    amount: int
    commission_rate: float
    commission_amount: int
    level: int = 1
    created_at: datetime


class AdminPendingCommissionResponse(BaseModel):
    items: list[AdminPendingCommissionItem] = []
    total_count: int = 0


class PayoutRecord(BaseModel):
    id: str
    referrer_id: str
    referrer_phone: str
    amount: int
    fee: int = 0
    payout_type: str = "standard"
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
    level: int = 1


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


class SetPayoutMethodRequest(BaseModel):
    method: Literal["wallet", "stars", "credit"]
    wallet_address: str | None = None


class ChangeCodeRequest(BaseModel):
    new_code: str = Field(min_length=3, max_length=20, pattern=r"^[A-Za-z0-9가-힣]+$")


class CommissionItem(BaseModel):
    id: str
    referred_user_phone: str
    source_type: str
    amount: int
    commission_rate: float
    commission_amount: int
    level: int = 1
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


class RegisterDistributorResponse(BaseModel):
    success: bool
    message: str
    is_distributor: bool


class DistributorStatusResponse(BaseModel):
    is_distributor: bool


class DistributorListItem(BaseModel):
    tenant_id: str
    phone: str
    plan: str
    referral_code: str
    referral_count: int
    total_revenue: int
    total_commission: int
    total_payout: int
    commission_rate_override: float | None = None
    status: str
    level: int = 1
    created_at: datetime | None = None


class DistributorListResponse(BaseModel):
    items: list[DistributorListItem] = []
    total_count: int = 0


class SetDistributorRateRequest(BaseModel):
    rate: float = Field(ge=0.0, le=1.0)


class SuspendDistributorRequest(BaseModel):
    reason: str = Field(max_length=500)
    suspended: bool = True


class RejectPayoutRequest(BaseModel):
    reason: str = Field(max_length=500)


class SettlementAuditItem(BaseModel):
    id: str
    action: str
    actor_id: str | None = None
    target_id: str | None = None
    details: str
    created_at: datetime


class SettlementAuditResponse(BaseModel):
    items: list[SettlementAuditItem] = []


class InstantCashoutRequest(BaseModel):
    amount: int | None = Field(default=None, ge=1000)


class InstantCashoutResponse(BaseModel):
    success: bool
    payout_id: str | None = None
    amount: int = 0
    fee: int = 0
    net_amount: int = 0
    message: str


class BadgeInfo(BaseModel):
    badge_key: str
    earned_at: datetime | None = None


class BadgesResponse(BaseModel):
    badges: list[BadgeInfo] = []
    all_badges: list[dict] = []


class WeeklyMission(BaseModel):
    key: str
    label: str
    current: int
    target: int
    reward: str
    completed: bool


class WeeklyMissionsResponse(BaseModel):
    missions: list[WeeklyMission] = []


class SetDistributorMemoRequest(BaseModel):
    memo: str = Field(max_length=500)


class GetDistributorMemoResponse(BaseModel):
    memo: str


class PendingPayoutCountResponse(BaseModel):
    count: int


class SetWebhookRequest(BaseModel):
    url: str = Field(max_length=500)


class ConversionAnalytics(BaseModel):
    total_clicks: int = 0
    total_signups: int = 0
    conversion_rate: float = 0.0
    daily_clicks: list[dict] = []
