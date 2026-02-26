"""
mt5_reader.py — MT5 시장 데이터 파서

rion_data_now.json을 로드하고 AI 입력용 텍스트로 변환.
"""

import json
import os
import logging
from datetime import datetime

logger = logging.getLogger(__name__)

MAX_AGE_SECONDS = 180  # 3분 이상이면 stale 경고


def load(json_path: str) -> dict:
    """
    rion_data_now.json 로드 + 신선도 검사.

    Returns:
        파싱된 dict

    Raises:
        FileNotFoundError: 파일 없음
        json.JSONDecodeError: JSON 파싱 실패
    """
    if not os.path.exists(json_path):
        raise FileNotFoundError(f"rion_data_now.json 없음: {json_path}")

    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # 신선도 검사
    ts_str = data.get("timestamp")
    if ts_str:
        try:
            ts = datetime.fromisoformat(ts_str)
            age_seconds = (datetime.now() - ts).total_seconds()
            if age_seconds > MAX_AGE_SECONDS:
                logger.warning(
                    f"[mt5_reader] 데이터가 {age_seconds:.0f}초 전 데이터 "
                    f"(기준: {MAX_AGE_SECONDS}초) — AI 분석이 부정확할 수 있음"
                )
            else:
                logger.debug(f"[mt5_reader] 신선도 OK ({age_seconds:.0f}초 전)")
        except ValueError:
            logger.warning(f"[mt5_reader] timestamp 파싱 실패: {ts_str}")

    return data


def build_haiku_summary(data: dict) -> str:
    """
    Haiku용 간결 요약 (토큰 절약 — 핵심 지표만).

    포함 항목:
    - symbol, current_price, market_session
    - H4/H1 trend_strength, MA 배열 하락 정렬 여부
    - 감지된 패턴 목록
    - key_levels (라운드 레벨 거리)
    - 현재 포지션 수
    """
    symbol = data.get("symbol", "UNKNOWN")
    price = data.get("current_price", 0)
    session = data.get("market_session", "UNKNOWN")
    positions = data.get("positions", [])

    tf = data.get("timeframes", {})
    h4 = tf.get("h4", {})
    h1 = tf.get("h1", {})

    # MA 배열 확인: 200 > 75 > 20 → 하락 정렬
    def ma_aligned_bear(tf_data: dict) -> bool:
        ma20 = tf_data.get("ma20", 0)
        ma75 = tf_data.get("ma75", 0)
        ma200 = tf_data.get("ma200", 0)
        return bool(ma20 and ma75 and ma200 and ma200 > ma75 > ma20)

    h4_aligned = ma_aligned_bear(h4)
    h1_aligned = ma_aligned_bear(h1)

    # 감지된 패턴
    patterns = data.get("patterns", {})
    detected = [
        name for name, v in patterns.items()
        if isinstance(v, dict) and v.get("detected")
    ]

    key_levels = data.get("key_levels", {})
    upper_round = key_levels.get("upper_round", "?")
    upper_dist = key_levels.get("upper_dist_pips", "?")
    lower_round = key_levels.get("lower_round", "?")
    lower_dist = key_levels.get("lower_dist_pips", "?")

    lines = [
        f"[시장 요약] {symbol} | 현재가: {price:.5f} | 세션: {session}",
        "",
        "[추세 분석]",
        (
            f"  H4: trend_strength={h4.get('trend_strength', '?')} | "
            f"MA 하락정렬={'O' if h4_aligned else 'X'} | "
            f"ma75_slope={h4.get('ma75_slope_pips', '?')}pips"
        ),
        (
            f"  H1: trend_strength={h1.get('trend_strength', '?')} | "
            f"MA 하락정렬={'O' if h1_aligned else 'X'} | "
            f"ma75_slope={h1.get('ma75_slope_pips', '?')}pips"
        ),
        "",
        f"[감지된 패턴] {', '.join(detected) if detected else '없음'}",
        "",
        "[핵심 레벨]",
        f"  상단 라운드: {upper_round} (현재가에서 {upper_dist}pips 위)",
        f"  하단 라운드: {lower_round} (현재가에서 {lower_dist}pips 아래)",
        "",
        f"[포지션] {len(positions)}개 보유 중",
    ]

    return "\n".join(lines)


def build_opus_context(data: dict) -> str:
    """
    Opus용 전체 컨텍스트 (build_haiku_summary + 상세 항목).

    추가 항목:
    - M5 최근 캔들 5개 (shape, body_pips)
    - 타임프레임별 ma75_slope_pips, vs_ma75, vs_ma200, 지지/저항
    - middles (H1/H4/D1/W1)
    - 패턴 상세 (info 포함)
    """
    base = build_haiku_summary(data)

    tf = data.get("timeframes", {})
    m5 = tf.get("m5", {})

    # M5 최근 캔들 5개
    m5_candles = m5.get("candles_recent", [])[-5:]
    candle_lines = [
        f"    {c.get('time', '?')} | {c.get('shape', '?')} | body={c.get('body_pips', '?')}pips"
        for c in m5_candles
    ]

    # 타임프레임 상세
    def tf_detail(name: str, tf_data: dict) -> str:
        return (
            f"  {name.upper()}: "
            f"ma75_slope={tf_data.get('ma75_slope_pips', '?')}pips | "
            f"vs_ma75={tf_data.get('price_vs_ma75_pips', '?')}pips | "
            f"vs_ma200={tf_data.get('price_vs_ma200_pips', '?')}pips | "
            f"저항={tf_data.get('resistance', '?')} | "
            f"지지={tf_data.get('support', '?')}"
        )

    # Middles
    middles = data.get("middles", {})
    mid_keys = ["h1", "h4", "d1", "w1"]
    if all(isinstance(middles.get(k), (int, float)) for k in mid_keys):
        mid_line = (
            f"  H1={middles['h1']:.5f} | H4={middles['h4']:.5f} | "
            f"D1={middles['d1']:.5f} | W1={middles['w1']:.5f}"
        )
    else:
        mid_line = f"  {middles}"

    # 패턴 상세
    pattern_lines = []
    for name, v in data.get("patterns", {}).items():
        if isinstance(v, dict):
            if v.get("detected"):
                info = v.get("info") or v.get("distance_pips", "")
                pattern_lines.append(f"  {name}: 감지됨 ({info})")
            else:
                pattern_lines.append(f"  {name}: 미감지")

    lines = (
        [base, "", "[M5 최근 캔들 5개]"]
        + candle_lines
        + [
            "",
            "[타임프레임 상세]",
            tf_detail("m5", m5),
            tf_detail("h1", tf.get("h1", {})),
            tf_detail("h4", tf.get("h4", {})),
            tf_detail("d1", tf.get("d1", {})),
            "",
            "[중간선 (Middles)]",
            mid_line,
            "",
            "[패턴 상세]",
        ]
        + pattern_lines
    )

    return "\n".join(lines)
