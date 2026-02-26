"""
trade_decision.py — Agent 2: 최종 매매 결정

역할: Agent 1 통과 후 Opus로 상세 진입/스킵 판단
모델: claude-opus-4-6
조건: Agent 1 pass=True 일 때만 호출
출력: {"decision": "ENTER" or "SKIP", "confidence": 0-100, "reason": str}

confidence < 67 → 자동 강제 SKIP
"""

import os
import json
import logging
import re

import anthropic

from bridge import mt5_reader

logger = logging.getLogger(__name__)

OPUS_MODEL = "claude-opus-4-6"
CONFIDENCE_THRESHOLD = 67  # params.json AI confidence threshold와 일치

SYSTEM_PROMPT = """당신은 GBPAUD SELL 전용 알고리즘 트레이딩봇의 최종 매매 결정 에이전트입니다.
Agent 1의 시장 컨텍스트 분석과 상세 MT5 데이터를 검토하여 실제 진입 여부를 결정합니다.

진입(ENTER) 조건:
1. M5 패턴 확인: 슈팅스타, 베어 엔걸핑, Saboten, Bamboo 등 SELL 신호 패턴
2. H1 저항선 근방 또는 직전 반등 실패 확인
3. H4 하락 모멘텀 유지 (ma75_slope 음수, trend_strength -1 이하)
4. 라운드 레벨 하단까지 충분한 공간 (lower_dist_pips > 15)
5. 현재 포지션 0개 (최대 1개 룰)
6. D1 추세가 중립 이상 하락 (trend_strength <= 0)

스킵(SKIP) 조건:
- M5 강한 상승 모멘텀 (연속 2개 이상 big_bull)
- 라운드 레벨 하단까지 여유 < 15pips (손익비 부족)
- 포지션 이미 1개 이상 보유
- D1 강한 상승세 (trend_strength > 0)
- Tokyo 세션이며 H4 trend_strength > -1 (충분한 하락 모멘텀 없음)

반드시 JSON 형식으로만 응답하세요. 다른 텍스트 없이 JSON만:
{"decision": "ENTER" or "SKIP", "confidence": 0-100, "reason": "판단 근거 1-2문장 (한국어)"}

confidence: 진입 결정의 확신도 (67 미만이면 자동으로 SKIP 처리됨)"""


def decide(agent1_result: dict, json_path: str) -> dict:
    """
    Agent 2: Opus 최종 진입/스킵 판단

    Args:
        agent1_result: Agent 1 결과 {"pass": True, "context": str, "strength": int}
        json_path: rion_data_now.json 경로

    Returns:
        {"decision": "ENTER" or "SKIP", "confidence": int, "reason": str}
        실패 시: {"decision": "SKIP", "confidence": 0, "reason": "오류 설명"}
    """
    # 1. 전체 컨텍스트 빌드
    try:
        data = mt5_reader.load(json_path)
        full_context = mt5_reader.build_opus_context(data)
    except Exception as e:
        logger.error(f"[Agent2] 데이터 로드 실패: {e}")
        return {"decision": "SKIP", "confidence": 0, "reason": f"데이터 로드 오류: {e}"}

    # 2. API 키 확인
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY 환경변수가 설정되지 않았습니다.")

    client = anthropic.Anthropic(api_key=api_key)

    logger.info(
        f"[Agent2] Opus 분석 요청 — "
        f"{data.get('symbol')} @ {data.get('current_price')} "
        f"(Agent1 strength={agent1_result.get('strength')})"
    )

    user_content = (
        f"[Agent 1 시장 컨텍스트]\n"
        f"분석: {agent1_result.get('context', '')}\n"
        f"SELL 유리도: {agent1_result.get('strength', 0)}/100\n\n"
        f"[상세 MT5 시장 데이터]\n"
        f"{full_context}\n\n"
        f"위 데이터를 종합하여 지금 GBPAUD SELL 진입 여부를 결정해주세요."
    )

    # 3. Opus 호출
    try:
        response = client.messages.create(
            model=OPUS_MODEL,
            max_tokens=400,
            system=SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": user_content,
                }
            ],
        )
    except Exception as e:
        logger.error(f"[Agent2] Opus API 호출 실패: {e}")
        return {"decision": "SKIP", "confidence": 0, "reason": f"API 호출 오류: {e}"}

    raw = response.content[0].text.strip()
    logger.info(
        f"[Agent2] 응답 수신 — "
        f"입력 {response.usage.input_tokens}토큰, "
        f"출력 {response.usage.output_tokens}토큰"
    )
    logger.debug(f"[Agent2] 원본 응답: {raw}")

    # 4. JSON 파싱
    try:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        result = json.loads(match.group() if match else raw)

        # 타입 보정
        result["decision"] = str(result.get("decision", "SKIP")).upper()
        result["confidence"] = int(result.get("confidence", 0))
        result["reason"] = str(result.get("reason", ""))

        # decision 값 검증
        if result["decision"] not in ("ENTER", "SKIP"):
            logger.warning(f"[Agent2] 비정상 decision값: {result['decision']} → SKIP 처리")
            result["decision"] = "SKIP"

        # 5. confidence 임계값 미달 시 강제 SKIP
        if result["confidence"] < CONFIDENCE_THRESHOLD:
            if result["decision"] == "ENTER":
                result["reason"] += (
                    f" (confidence {result['confidence']} < "
                    f"{CONFIDENCE_THRESHOLD} → 강제 SKIP)"
                )
                logger.info(
                    f"[Agent2] confidence 미달로 강제 SKIP: "
                    f"{result['confidence']} < {CONFIDENCE_THRESHOLD}"
                )
            result["decision"] = "SKIP"

        logger.info(
            f"[Agent2] 결정: {result['decision']}, "
            f"confidence={result['confidence']}, "
            f"reason={result['reason'][:60]}..."
        )
        return result

    except (json.JSONDecodeError, KeyError, ValueError, AttributeError) as e:
        logger.error(f"[Agent2] JSON 파싱 실패: {e} | 원본: {raw}")
        return {"decision": "SKIP", "confidence": 0, "reason": "분석 오류 (JSON 파싱 실패)"}
