"""
market_analyst.py — Agent 1: 시장 분석 필터

역할: Haiku로 빠른 시장 컨텍스트 필터 → Opus 호출 여부 결정
모델: claude-haiku-4-5-20251001 (빠름, 저렴 ~$0.0003/회)
출력: {"pass": bool, "context": str, "strength": 0-100}

pass=True 기준: strength >= 55
"""

import os
import json
import logging
import re

import anthropic

from bridge import mt5_reader

logger = logging.getLogger(__name__)

HAIKU_MODEL = "claude-haiku-4-5-20251001"

SYSTEM_PROMPT = """당신은 GBPAUD SELL 전용 트레이딩봇의 시장 분석 필터입니다.
주어진 MT5 시장 데이터를 분석하여 현재 SELL 진입에 적합한 시장 컨텍스트인지 판단합니다.

판단 기준:
1. H4 + H1 하락 추세 정렬 (trend_strength 음수 또는 MA 배열 200>75>20)
2. 주요 저항선 근접 또는 하회
3. 라운드 레벨 상단까지 충분한 여유 (upper_dist_pips > 20)
4. 시장 세션 (Tokyo는 약세, London/NY 진입 우선)
5. 기존 포지션 없음 또는 소수

반드시 JSON 형식으로만 응답하세요. 다른 텍스트 없이 JSON만:
{"pass": true/false, "context": "시장 상황 2-3문장 요약 (한국어)", "strength": 0-100}

strength: SELL 유리도 점수 (0=완전 불리, 100=매우 유리)
pass=true 기준: strength >= 55"""


def analyze(json_path: str) -> dict:
    """
    Agent 1: MT5 데이터 → Haiku 시장 필터 분석

    Args:
        json_path: rion_data_now.json 경로

    Returns:
        {"pass": bool, "context": str, "strength": int}
        실패 시: {"pass": False, "context": "오류 설명", "strength": 0}
    """
    # 1. 데이터 로드 + 신선도 확인
    try:
        data = mt5_reader.load(json_path)
    except FileNotFoundError as e:
        logger.error(f"[Agent1] 데이터 파일 없음: {e}")
        return {"pass": False, "context": f"데이터 파일 없음: {e}", "strength": 0}
    except Exception as e:
        logger.error(f"[Agent1] 데이터 로드 실패: {e}")
        return {"pass": False, "context": "데이터 로드 오류", "strength": 0}

    # 2. Haiku 요약 빌드
    summary = mt5_reader.build_haiku_summary(data)

    # 3. API 키 확인
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY 환경변수가 설정되지 않았습니다.")

    client = anthropic.Anthropic(api_key=api_key)

    logger.info(
        f"[Agent1] Haiku 분석 요청 — "
        f"{data.get('symbol')} @ {data.get('current_price')} "
        f"세션={data.get('market_session')}"
    )

    # 4. Haiku 호출
    try:
        response = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=300,
            system=SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": (
                        "다음 GBPAUD 시장 데이터를 분석하고 "
                        "SELL 진입 적합 여부를 판단하세요.\n\n"
                        f"{summary}"
                    ),
                }
            ],
        )
    except Exception as e:
        logger.error(f"[Agent1] Haiku API 호출 실패: {e}")
        return {"pass": False, "context": "API 호출 오류", "strength": 0}

    raw = response.content[0].text.strip()
    logger.info(
        f"[Agent1] 응답 수신 — "
        f"입력 {response.usage.input_tokens}토큰, "
        f"출력 {response.usage.output_tokens}토큰"
    )
    logger.debug(f"[Agent1] 원본 응답: {raw}")

    # 5. JSON 파싱 (마크다운 코드블록 대응)
    try:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        result = json.loads(match.group() if match else raw)

        # 타입 보정
        result["pass"] = bool(result.get("pass", False))
        result["strength"] = int(result.get("strength", 0))
        result["context"] = str(result.get("context", ""))

        logger.info(
            f"[Agent1] 결과: pass={result['pass']}, "
            f"strength={result['strength']}, "
            f"context={result['context'][:60]}..."
        )
        return result

    except (json.JSONDecodeError, KeyError, ValueError, AttributeError) as e:
        logger.error(f"[Agent1] JSON 파싱 실패: {e} | 원본: {raw}")
        return {"pass": False, "context": "분석 오류 (JSON 파싱 실패)", "strength": 0}
