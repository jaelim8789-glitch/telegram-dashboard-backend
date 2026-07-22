"""
AI 기반 지능형 챗봇 서비스
사용자의 질문을 분석하여 가장 적절한 답변을 자동으로 생성하거나 
기존 규칙 중에서 최적의 응답을 선택해주는 AI 기반 응답 시스템
"""
import asyncio
import logging
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass

from app.core.logging import get_logger
from app.services.auto_reply_service import get_matching_rules
from app.crud import account as account_crud
from app.database import async_session_maker

logger = get_logger(__name__)

@dataclass
class AIResponse:
    """AI 응답 결과 데이터 클래스"""
    response_text: str
    confidence: float  # 0.0 ~ 1.0 사이의 신뢰도 점수
    matched_rule_id: Optional[str] = None  # 매칭된 규칙 ID (있는 경우)
    is_generated: bool = False  # AI가 생성한 응답인지 여부


class AIChatbotService:
    """AI 기반 챗봇 서비스 클래스"""
    
    def __init__(self):
        self.logger = get_logger(__name__)
        
    async def process_message(self, account_id: str, message: str) -> AIResponse:
        """
        사용자의 메시지를 처리하고 AI 기반 응답을 생성
        
        Args:
            account_id: 계정 ID
            message: 사용자 메시지
            
        Returns:
            AIResponse: 처리된 응답 객체
        """
        # 1. 기존 자동 응답 규칙과 매칭 시도
        try:
            matching_response = await self._find_matching_rule_response(account_id, message)
            if matching_response:
                return matching_response
        except Exception as e:
            self.logger.error(f"규칙 매칭 중 오류 발생: {e}")
        
        # 2. 규칙 매칭이 없을 경우 간단한 키워드 기반 응답
        keyword_response = self._process_keyword_response(message)
        if keyword_response:
            return AIResponse(response_text=keyword_response, confidence=0.7, is_generated=False)
        
        # 3. 기본 응답 제공
        return AIResponse(
            response_text=self._get_default_response(),
            confidence=0.3,
            is_generated=False
        )
    
    async def _find_matching_rule_response(self, account_id: str, message: str) -> Optional[AIResponse]:
        """기존 자동 응답 규칙과 메시지를 비교하여 매칭되는 응답을 찾음"""
        async with async_session_maker() as db:
            # 계정 정보 가져오기
            account = await account_crud.get_account(db, account_id)
            if not account:
                return None
            
            # 자동 응답 규칙 가져오기
            rules = await account_crud.get_auto_reply_rules(db, account_id)
            
            # 가장 높은 점수를 받은 규칙 찾기
            best_match = None
            best_score = 0
            
            for rule in rules:
                if not rule.is_active:
                    continue
                    
                score = self._calculate_similarity(message.lower(), rule.match_value.lower())
                
                if score > best_score and score >= 0.6:  # 60% 이상 유사도면 매칭
                    best_score = score
                    best_match = rule
            
            if best_match:
                return AIResponse(
                    response_text=best_match.reply_content,
                    confidence=best_score,
                    matched_rule_id=best_match.id,
                    is_generated=False
                )
        
        return None
    
    def _calculate_similarity(self, text1: str, text2: str) -> float:
        """
        두 텍스트 간의 유사도를 계산 (간단한 알고리즘)
        """
        # 간단한 유사도 계산 - 실제 구현에서는 더 정교한 알고리즘 사용 가능
        words1 = set(text1.split())
        words2 = set(text2.split())
        
        if not words1 and not words2:
            return 1.0
        if not words1 or not words2:
            return 0.0
        
        intersection = words1.intersection(words2)
        union = words1.union(words2)
        
        return len(intersection) / len(union)
    
    def _process_keyword_response(self, message: str) -> Optional[str]:
        """키워드 기반 간단한 응답 처리"""
        message_lower = message.lower()
        
        # 자주 묻는 질문에 대한 간단한 응답
        if any(keyword in message_lower for keyword in ['안녕', '헬로', 'hello', 'hi']):
            return "안녕하세요! 무엇을 도와드릴까요?"
        elif any(keyword in message_lower for keyword in ['감사', '고마워', 'thank']):
            return "도움이 되어 정말 기쁩니다! 😊"
        elif any(keyword in message_lower for keyword in ['도움', 'help', '도움이', '도움주세요']):
            return ("저는 TeleMon 봇입니다. 아래 메뉴에서 원하시는 기능을 선택해주세요!\n"
                   "• /start - 메인 메뉴\n"
                   "• /help - 도움말\n"
                   "• /status - 상태 확인")
        elif any(keyword in message_lower for keyword in ['뭐해', '뭐하니', 'what are you doing']):
            return "사용자님의 메시지를 기다리고 있어요! 무엇을 도와드릴까요?"
        elif any(keyword in message_lower for keyword in ['이름', '너의 이름', 'what is your name']):
            return "저는 TeleMon 자동 응답 봇입니다!"
        
        return None
    
    def _get_default_response(self) -> str:
        """기본 응답 메시지"""
        return ("죄송하지만 이해할 수 없는 메시지입니다. \n"
               "자세한 도움이 필요하시면 /help 명령어를 사용하거나 "
               "메인 메뉴에서 원하시는 기능을 선택해주세요.")


# 전역 인스턴스 생성
ai_chatbot_service = AIChatbotService()


async def process_user_message(account_id: str, message: str) -> AIResponse:
    """
    외부에서 사용할 수 있는 메시지 처리 함수
    
    Args:
        account_id: 계정 ID
        message: 사용자 메시지
        
    Returns:
        AIResponse: 처리된 응답 객체
    """
    return await ai_chatbot_service.process_message(account_id, message)