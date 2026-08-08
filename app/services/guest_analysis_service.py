"""Service for analyzing guest chat messages and assigning categories."""
from typing import Dict, Tuple
from app.services.ai_core_service import call_ollama # Assuming this is the right function

# Define the categories
CATEGORIES = {
    "Technical Inquiry": ["기능", "사용법", "연결", "설정", "오류", "bug", "api"],
    "Sales/Price": ["가격", "비용", "결제", "요금", "크레딧"],
    "Complaint/Satisfaction": ["불만", "문제", "개선", "싫어요"],
    "Security/Abuse": ["해킹", "탈취", "사기", "비밀번호"],
    "Off-topic": []
}

async def classify_message(message: str) -> Tuple[str, str, float]:
    """
    Attempts to classify a guest message into a primary and secondary category.
    Uses a simple keyword approach as a placeholder. A more sophisticated method would involve calling the AI.
    """
    lower_msg = message.lower()
    primary_cat = "Off-topic" # Default
    secondary_cat = ""
    confidence = 0.5 # Default confidence

    # Simple keyword matching for demonstration
    for cat, keywords in CATEGORIES.items():
        if any(keyword in lower_msg for keyword in keywords):
            primary_cat = cat
            break

    # For a more robust solution, call the AI model here
    # prompt = f"Classify the following Korean query into one of these categories: {list(CATEGORIES.keys())}. Query: {message}"
    # ai_response, _, _ = await call_ollama([{"role": "user", "content": prompt}], json_mode=True)
    # if ai_response:
    #     try:
    #         parsed_response = json.loads(ai_response)
    #         primary_cat = parsed_response.get('primary_category', primary_cat)
    #         secondary_cat = parsed_response.get('secondary_category', secondary_cat)
    #         confidence = parsed_response.get('confidence', confidence)
    #     except json.JSONDecodeError:
    #         pass

    return primary_cat, secondary_cat, confidence