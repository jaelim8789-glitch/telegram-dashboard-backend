"""AI Vision Service — 이미지/동영상 분석.

Ollama의 비전 모델(qwen2.5vl)을 사용해 첨부된 이미지/동영상을 분석합니다.
- 이미지: 파일을 읽어 base64로 변환 → Ollama OpenAI 호환 /v1/chat/completions 로 전송
- 동영상: ffmpeg로 프레임 추출 → 대표 프레임을 이미지로 분석

설정:
- AI_VISION_MODEL (기본: qwen2.5vl:7b)
- AI_VISION_API_BASE (기본: DEEPSEEK_API_BASE, 같은 Ollama 박스)
"""

from __future__ import annotations

import base64
import os
import subprocess
from pathlib import Path

import httpx

from app.config import settings
from app.core.logging import get_logger

logger = get_logger(__name__)

_DEFAULT_VISION_MODEL = os.environ.get("AI_VISION_MODEL", "qwen2.5vl:7b")
_VISION_API_BASE = os.environ.get("AI_VISION_API_BASE", settings.deepseek_api_base or "http://localhost:11434/v1")

# ffmpeg이 있는 경우에만 동영상 프레임 추출 시도
_FFMPEG = os.environ.get("AI_FFMPEG_PATH", "ffmpeg")


def _to_base64(filepath: str, mime_type: str) -> str:
    """Read a file and return a base64 data URL."""
    with open(filepath, "rb") as f:
        data = f.read()
    return f"data:{mime_type};base64,{base64.b64encode(data).decode('utf-8')}"


def _ffmpeg_available() -> bool:
    try:
        result = subprocess.run(
            [_FFMPEG, "-version"],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def _extract_video_frames(filepath: str, num_frames: int = 4) -> list[str]:
    """Extract representative frames from a video using ffmpeg.

    Returns a list of temporary image file paths.
    """
    out_dir = Path(filepath).parent / f"frames_{Path(filepath).stem}"
    out_dir.mkdir(parents=True, exist_ok=True)

    # Use ffmpeg to extract evenly-spaced frames
    frames: list[str] = []
    try:
        # First get duration
        probe = subprocess.run(
            [_FFMPEG, "-i", filepath],
            capture_output=True,
            timeout=15,
        )
        # Extract frames with thumbnail filter
        result = subprocess.run(
            [
                _FFMPEG, "-i", filepath,
                "-vf", f"thumbnail={num_frames * 5},scale=640:-1",
                "-frames:v", str(num_frames),
                "-q:v", "5",
                str(out_dir / "frame_%d.jpg"),
            ],
            capture_output=True,
            timeout=60,
        )
        if result.returncode == 0:
            for p in sorted(out_dir.glob("frame_*.jpg")):
                frames.append(str(p))
    except Exception as exc:
        logger.warning("video_frame_extract_failed", error=str(exc))

    return frames


async def analyze_image(filepath: str, mime_type: str, question: str = "") -> str:
    """Analyze a single image with the vision model."""
    try:
        data_url = _to_base64(filepath, mime_type)
        text_prompt = (
            f"{question}\n\n"
            "위 이미지를 상세히 분석해주세요. 이미지에 보이는 내용, 텍스트, "
            "의미, 특징을 구체적으로 설명해주세요. 한국어로 답변해주세요."
        ).strip()

        payload = {
            "model": _DEFAULT_VISION_MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": text_prompt},
                        {"type": "image_url", "image_url": {"url": data_url}},
                    ],
                }
            ],
            "max_tokens": 2000,
        }

        async with httpx.AsyncClient(timeout=120) as client:
            response = await client.post(
                f"{_VISION_API_BASE}/chat/completions",
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
            content = data["choices"][0]["message"]["content"] or ""
            return content.strip()
    except httpx.TimeoutException:
        logger.error("ai_vision_timeout")
        return "이미지 분석에 시간이 초과되었습니다."
    except httpx.HTTPStatusError as exc:
        logger.error("ai_vision_http_error", status=exc.response.status_code, body=exc.response.text[:500])
        return f"이미지 분석 중 오류가 발생했습니다 (HTTP {exc.response.status_code})."
    except Exception as exc:
        logger.error("ai_vision_failed", error=str(exc))
        return "이미지 분석에 실패했습니다."


async def analyze_video(filepath: str, mime_type: str, question: str = "") -> str:
    """Analyze a video by extracting frames and analyzing them."""
    if not _ffmpeg_available():
        logger.warning("ffmpeg_not_available_for_video_analysis")
        return "동영상 분석에는 ffmpeg가 필요합니다. 서버에 ffmpeg가 설치되어 있지 않습니다."

    frames = _extract_video_frames(filepath)
    if not frames:
        return "동영상에서 프레임을 추출하지 못했습니다."

    try:
        text_prompt = (
            f"{question}\n\n"
            "위 프레임들은 동영상에서 추출한 대표 장면들입니다. "
            "각 프레임의 내용을 종합하여 동영상의 전체 내용을 분석해주세요. "
            "한국어로 답변해주세요."
        ).strip()

        content_parts: list[dict] = [{"type": "text", "text": text_prompt}]
        for frame in frames:
            data_url = _to_base64(frame, "image/jpeg")
            content_parts.append({"type": "image_url", "image_url": {"url": data_url}})

        payload = {
            "model": _DEFAULT_VISION_MODEL,
            "messages": [{"role": "user", "content": content_parts}],
            "max_tokens": 2000,
        }

        async with httpx.AsyncClient(timeout=180) as client:
            response = await client.post(
                f"{_VISION_API_BASE}/chat/completions",
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
            content = data["choices"][0]["message"]["content"] or ""
            return content.strip()
    except httpx.TimeoutException:
        logger.error("ai_vision_video_timeout")
        return "동영상 분석에 시간이 초과되었습니다."
    except Exception as exc:
        logger.error("ai_vision_video_failed", error=str(exc))
        return "동영상 분석에 실패했습니다."
    finally:
        # Cleanup temp frame files
        import shutil
        for frame in frames:
            try:
                parent = Path(frame).parent
                if parent.exists() and parent.name.startswith("frames_"):
                    shutil.rmtree(parent, ignore_errors=True)
            except Exception:
                pass
