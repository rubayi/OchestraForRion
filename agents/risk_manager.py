"""
risk_manager.py — Agent 3: 리스크 관리

역할: Agent 2 ENTER 결정 후 최적 SL/TP/랏 계산
모델: claude-haiku-4-5-20251001 (빠름, 저렴)
입력: Agent 2 결과 + 시장 데이터 + 파라미터 + 최근 거래 이력
출력: {"sl_pips": N, "tp_pips": N, "lot_size": N, "rr_ratio": N,
       "mode": "normal"|"conservative", "reason": str}

보수적 모드: 최근 3거래 연속 손실 시 lot_size × 0.5
"""

import os
import json
import logging
import re

import anthropic

from bridge import mt5_reader, trade_db_reader

logger = logging.getLogger(__name__)

HAIKU_MODEL = "claude-haiku-4-5-20251001"

# 출력값 허용 범위 (하드 클램프 — AI 환각 방지)
SL_MIN_PIPS = 5.0
SL_MAX_PIPS = 30.0
TP_MIN_PIPS = 8.0
TP_MAX_PIPS = 80.0
LOT_MIN     = 0.01
RR_MIN      = 1.0
RR_MAX      = 4.0

SYSTEM_PROMPT = """당신은 GBPAUD SELL 전용 트레이딩봇의 리스크 관리 에이전트입니다.
Agent 2가 ENTER를 결정한 상황에서 최적의 SL/TP/랏 크기를 제안합니다.

계산 원칙:
1. SL (Stop Loss, pips):
   - bamboo/saboten 패턴: sl_buffer_pips 기준 (최소 8p, 최대 25p)
   - 기타 패턴: ATR_추정 × 1.5 (최소 8p, 최대 20p)
   - 보수적 모드: sl_pips × 0.9 (타이트하게)

2. TP (Take Profit, pips):
   - 기본: sl_pips × target_rr (기본 2.0)
   - 하단 라운드까지 여유가 tp보다 작으면 tp = lower_dist_pips × 0.85
   - 최소 8pips 보장

3. 랏 사이즈:
   - 기본: params의 lot_size
   - 보수적 모드: lot_size × 0.5
   - 상한: max_lot_size 준수

4. 보수적 모드: 최근 연속 손실 3회 이상 시 반드시 "conservative" 반환

반드시 JSON 형식으로만 응답하세요. 다른 텍스트 없이 JSON만:
{"sl_pips": N, "tp_pips": N, "lot_size": N, "rr_ratio": N, "mode": "normal" or "conservative", "reason": "계산 근거 1-2문장 (한국어)"}

sl_pips, tp_pips: 소수점 1자리 / lot_size: 소수점 2자리 / rr_ratio: tp_pips / sl_pips"""


def _load_params(params_path: str) -> dict:
    """AlgoTradingBot params.json 로드"""
    try:
        with open(params_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"[Agent3] params.json 로드 실패: {e} → 기본값 사용")
        return {}


def _estimate_atr_pips(candles: list, pip_mult: float = 10000.0) -> float:
    """최근 캔들 high/low 로 평균 True Range 추정 (pips)"""
    tr_list = []
    for c in candles:
        high = c.get("high", 0)
        low  = c.get("low", 0)
        if high and low:
            tr_list.append((high - low) * pip_mult)
    return round(sum(tr_list) / len(tr_list), 1) if tr_list else 8.0


def _build_context(
    agent2_result: dict,
    data: dict,
    params: dict,
    consecutive_losses: int,
    atr_pips: float,
) -> str:
    """Haiku에게 넘길 컨텍스트 문자열 빌드"""
    key_levels = data.get("key_levels", {})
    lower_dist = key_levels.get("lower_dist_pips", 30)
    upper_dist = key_levels.get("upper_dist_pips", 30)

    patterns = data.get("patterns", {})
    detected = [
        name for name, v in patterns.items()
        if isinstance(v, dict) and v.get("detected")
    ]

    base_lot    = params.get("lot_size", 0.5)
    max_lot     = params.get("max_lot_size", 1.0)
    sl_buffer   = params.get("sl_buffer_pips", 8)
    target_rr   = params.get("target_rr_ratio", 2.0)
    base_sl     = params.get("stop_loss_pips", 13)
    dynamic_lot = params.get("dynamic_lot_enabled", False)
    risk_pct    = params.get("risk_per_trade_pct", 0.02) * 100

    lines = [
        "[Agent 2 진입 결정]",
        f"  결정: {agent2_result.get('decision')} | confidence: {agent2_result.get('confidence')}%",
        f"  근거: {agent2_result.get('reason', '')}",
        "",
        "[시장 컨텍스트]",
        f"  심볼: {data.get('symbol', 'GBPAUD')} | 현재가: {data.get('current_price', 0):.5f}",
        f"  세션: {data.get('market_session', 'UNKNOWN')}",
        f"  감지 패턴: {', '.join(detected) if detected else '없음'}",
        f"  하단 라운드까지: {lower_dist}pips | 상단 라운드까지: {upper_dist}pips",
        f"  ATR 추정 (M5 평균 변동폭): {atr_pips}pips",
        "",
        "[리스크 파라미터 (params.json)]",
        f"  기본 SL: {base_sl}pips | SL 버퍼: {sl_buffer}pips | 목표 RR: {target_rr}",
        f"  기본 랏: {base_lot} | 최대 랏: {max_lot}",
        f"  동적 랏: {'ON' if dynamic_lot else 'OFF'} | 위험 비율: {risk_pct:.1f}%",
        "",
        "[최근 거래 현황]",
        f"  연속 손실: {consecutive_losses}회",
        f"  {'⚠️ 보수적 모드 발동 조건 충족' if consecutive_losses >= 3 else '정상 모드'}",
    ]
    return "\n".join(lines)


def manage(
    agent2_result: dict,
    json_path: str,
    db_path: str,
    params_path: str,
) -> dict:
    """
    Agent 3: 최적 SL/TP/랏 계산

    Args:
        agent2_result: Agent 2 결과 {"decision": "ENTER", "confidence": int, "reason": str}
        json_path:    rion_data_now.json 경로
        db_path:      trades.db 경로
        params_path:  AlgoTradingBot params.json 경로

    Returns:
        {"sl_pips": float, "tp_pips": float, "lot_size": float,
         "rr_ratio": float, "mode": str, "reason": str}
    """
    params = _load_params(params_path)

    _FALLBACK = {
        "sl_pips":  float(params.get("stop_loss_pips", 13)),
        "tp_pips":  float(params.get("take_profit_pips", 26)),
        "lot_size": float(params.get("lot_size", 0.5)),
        "rr_ratio": float(params.get("target_rr_ratio", 2.0)),
        "mode":     "normal",
        "reason":   "fallback — params.json 기본값 사용",
    }

    # ── 1. 시장 데이터 로드 ────────────────────────────────────────────────────
    try:
        data = mt5_reader.load(json_path)
    except Exception as e:
        logger.error(f"[Agent3] 시장 데이터 로드 실패: {e}")
        return _FALLBACK

    # ── 2. 최근 연속 손실 조회 ─────────────────────────────────────────────────
    consecutive_losses = 0
    try:
        reader = trade_db_reader.TradeDBReader(db_path)
        report = reader.get_stats(days=7)
        consecutive_losses = report.recent_consecutive_losses
        logger.info(f"[Agent3] 최근 연속 손실: {consecutive_losses}회")
    except Exception as e:
        logger.warning(f"[Agent3] DB 조회 실패: {e} → 연속 손실 0으로 처리")

    # ── 3. ATR 추정 ───────────────────────────────────────────────────────────
    m5_candles = data.get("timeframes", {}).get("m5", {}).get("candles_recent", [])
    atr_pips   = _estimate_atr_pips(m5_candles)

    # ── 4. Haiku 호출 ─────────────────────────────────────────────────────────
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY 환경변수가 설정되지 않았습니다.")

    client  = anthropic.Anthropic(api_key=api_key)
    context = _build_context(agent2_result, data, params, consecutive_losses, atr_pips)

    logger.info(
        f"[Agent3] Haiku 리스크 계산 요청 — "
        f"ATR={atr_pips}pips, 연속손실={consecutive_losses}회, "
        f"패턴={[n for n, v in data.get('patterns', {}).items() if isinstance(v, dict) and v.get('detected')]}"
    )

    try:
        response = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=200,
            system=SYSTEM_PROMPT,
            messages=[
                {
                    "role": "user",
                    "content": (
                        "다음 상황에서 GBPAUD SELL 진입 시 최적의 SL/TP/랏을 계산하세요.\n\n"
                        f"{context}"
                    ),
                }
            ],
        )
    except Exception as e:
        logger.error(f"[Agent3] Haiku API 호출 실패: {e}")
        return _FALLBACK

    raw = response.content[0].text.strip()
    logger.info(
        f"[Agent3] 응답 수신 — "
        f"입력 {response.usage.input_tokens}토큰, "
        f"출력 {response.usage.output_tokens}토큰"
    )
    logger.debug(f"[Agent3] 원본 응답: {raw}")

    # ── 5. JSON 파싱 ──────────────────────────────────────────────────────────
    try:
        match  = re.search(r"\{.*\}", raw, re.DOTALL)
        result = json.loads(match.group() if match else raw)

        sl     = float(result.get("sl_pips",  _FALLBACK["sl_pips"]))
        tp     = float(result.get("tp_pips",  _FALLBACK["tp_pips"]))
        lot    = float(result.get("lot_size", _FALLBACK["lot_size"]))
        mode   = str(result.get("mode", "normal")).lower()
        reason = str(result.get("reason", ""))

    except (json.JSONDecodeError, KeyError, ValueError, AttributeError) as e:
        logger.error(f"[Agent3] JSON 파싱 실패: {e} | 원본: {raw}")
        return _FALLBACK

    # ── 6. 하드 클램프 (AI 환각으로 비정상 값 방지) ───────────────────────────
    sl  = max(SL_MIN_PIPS, min(SL_MAX_PIPS, round(sl,  1)))
    tp  = max(TP_MIN_PIPS, min(TP_MAX_PIPS, round(tp,  1)))
    lot = max(LOT_MIN, min(params.get("max_lot_size", 1.0), round(lot, 2)))
    rr  = max(RR_MIN, min(RR_MAX, round(tp / sl, 2))) if sl > 0 else 2.0

    # ── 7. 보수적 모드 강제 적용 (AI가 normal이라 해도 조건 충족 시 덮어씀) ───
    if consecutive_losses >= 3 and mode != "conservative":
        logger.info(f"[Agent3] 연속 손실 {consecutive_losses}회 → 보수적 모드 강제 적용")
        mode = "conservative"

    if mode == "conservative":
        lot    = max(LOT_MIN, round(lot * 0.5, 2))
        reason += f" [보수적 모드: 연속 손실 {consecutive_losses}회, 랏 50% 축소]"

    final = {
        "sl_pips":  sl,
        "tp_pips":  tp,
        "lot_size": lot,
        "rr_ratio": rr,
        "mode":     mode,
        "reason":   reason,
    }

    logger.info(
        f"[Agent3] 최종 결과: SL={sl}p | TP={tp}p | 랏={lot} | RR={rr} | 모드={mode}"
    )
    return final
