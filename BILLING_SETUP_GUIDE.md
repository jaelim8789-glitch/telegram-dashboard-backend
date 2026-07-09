# 💳 결제 시스템 설정 가이드 (USDT + Telegram Stars)

> **Stripe/PortOne 필요없음!**  
> USDT(암호화폐) + Telegram Stars만 받으면 됩니다.  
> 수수료 0원, 즉시 정산, 익명성 보장!

---

## 1. USDT 결제 (월 구독료)

### 📌 준비물
```
1. 암호화폐 지갑 (바이낸스/업비트/빗썸 등)
   → USDT(TRC20) 입금 가능한 지갑이면 됨

2. USDT 입금받을 내 지갑 주소
   → 거래소에서 "입금" → USDT → TRC20 네트워크 선택
   → 내 지갑 주소 복사
```

### ⚙️ 설정 방법

```env
# .env 파일에 내 USDT 지갑 주소 입력
USDT_WALLET_ADDRESS=TXx8xxxxx... (내 트론(TRC20) 지갑 주소)
USDT_NETWORK=TRC20
```

### 🔄 결제流程
```
1. 사용자가 요금제 선택 (Basic $15/월, Pro $38/월)
2. 시스템이 인보이스 생성 → 지갑 주소 + 금액 표시
3. 사용자가 USDT 송금 (메모에 고유코드 입력)
4. 관리자가 입금 확인 → 요금제 활성화 (수동/자동)
```

### 💰 요금표 (USDT)

| 플랜 | 월간 | 연간 (20% 할인) |
|------|------|----------------|
| **Free** | 무료 | 무료 |
| **Basic** | $15 | $144 |
| **Pro** | $38 | $365 |
| **Enterprise** | $150 | $1,440 |

---

## 2. Telegram Stars 결제 (애드온)

### 📌 준비물
```
1. BotFather에서 봇 생성 (이미 되어있음)
2. 봇 설정에서 "Payments" 활성화
   → @BotFather → /mybots → [봇 선택] → Payments
   → Telegram Stars 선택
```

### 🎁 제공할 애드온 목록

| 아이템 | Stars | 설명 |
|--------|-------|------|
| ⚡ **긴급 발송 부스터** | 100 | 1회 긴급 발송 |
| 🤖 **AI 댓글 10회권** | 50 | GPT 댓글 10회 |
| 📊 **분석 리포트** | 200 | PDF 리포트 |
| 🎨 **프리미엄 템플릿** | 30 | 템플릿 5종 |
| 🔌 **추가 계정 슬롯** | 150 | 계정 +1 (월) |

### 🔄 Stars 결제流程
```
1. 사용자가 대시보드에서 "Stars로 구매" 버튼 클릭
2. 봇이 sendInvoice 메시지 전송
3. 사용자가 Telegram 안에서 1초 결제
4. successful_payment 웹훅 → 즉시 기능 활성화

💡 Stars는 Fragment에서 TON/USDT로 출금 가능!
```

---

## 3. USDT 지갑 만들기 (초보자용)

### 방법 1: 바이낸스 (추천)
```
1. https://binance.com 회원가입
2. 입금 → 암호화폐 → USDT 선택
3. 네트워크: TRC20 (트론) 선택
4. 내 입금 주소 복사 → .env에 입력
```

### 방법 2: 업비트 (한국)
```
1. https://upbit.com 회원가입 (카카오톡 가능)
2. 입금 → USDT → TRC20
3. 내 지갑 주소 확인
```

### 방법 3: 개인 지갑 (Trust Wallet)
```
1. Trust Wallet 앱 설치
2. USDT 토큰 추가 (트론 네트워크)
3. 받기 → USDT → TRC20 → 주소 복사
```

> **💡 TRC20(트론) 네트워크 추천 이유**: 수수료 가장 낮음 ($0.5~1), 속도 빠름

---

## 4. API 엔드포인트

```http
### 요금제 정보
GET /api/billing/plans

### USDT 인보이스 생성
POST /api/billing/usdt/invoice?tenant_id=xxx&plan=basic&billing=monthly

### USDT 입금 확인 (관리자용)
POST /api/billing/usdt/confirm?tenant_id=xxx&tx_hash=0x...

### Stars 애드온 정보
GET /api/billing/stars/invoice/broadcast_booster

### Stars 충전
POST /api/billing/stars/add?tenant_id=xxx&stars_amount=100

### Stars 사용
POST /api/billing/stars/spend?tenant_id=xxx&item=broadcast_booster

### 구독 상태 조회
GET /api/billing/subscription/{tenant_id}

### 구독 취소
POST /api/billing/subscription/{tenant_id}/cancel
```

---

## 5. 요약: 첫 결제까지 10분

```
1. 바이낸스/업비트에서 USDT 지갑 주소 생성 (3분)
2. .env에 USDT_WALLET_ADDRESS 입력 (1분)
3. 서버 재시작 (1분)
4. 테스트: Free로 가입 → 요금제 확인 (5분)
5. 오픈! 🚀
```

> **USDT 수수료 = 거의 0원**  
> **Stars 수수료 = Telegram이 가져가지만 전환율 높음**  
> **두 가지면 충분합니다! Stripe/PortOne 불필요 ✅**