"""
developer_agent.py — Agent 5: 자율 개발자

역할: Agent 4 성과 분석 리포트 기반 → params.json 튜닝 제안 → Telegram 승인 → 자동 적용
모델: Claude Opus 4.6 (코드/파라미터 수정은 정확성 최우선)
실행: run_agent4() 완료 후 자동 트리거

플로우:
  1. trades.db 통계 + 현재 params.json 읽기
  2. Opus: 변경 제안 생성 (params.json 범위만)
  3. 변경안 있으면 pending_params.json 저장 + Telegram 전송
  4. /approve_params → 적용 + git commit + 알림
  5. /reject_params  → 폐기 + 알림

안전장치:
  - params.json 변경만 (코드 변경 없음)
  - 1회에 최대 2개 파라미터 변경 제한
  - 허용 범위 하드 클램프 (AI 환각 방지)
  - 승인 없이는 절대 적용 안 됨
  - 6시간 내 무응답 시 자동 만료
"""

import os
import json
import logging
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import anthropic

from bridge.trade_db_reader import TradeDBReader

logger = logging.getLogger(__name__)

OPUS_MODEL = "claude-opus-4-6"

# params 허용 변경 범위 (AI 환각 방지 하드 클램프)
PARAM_LIMITS = {
    "stop_loss_pips":              (8.0,  30.0),
    "take_profit_pips":            (15.0, 60.0),
    "sl_buffer_pips":              (3.0,  15.0),
    "trailing_activation_pips":    (5.0,  25.0),
    "trailing_stop_pips":          (3.0,  15.0),
    "breakeven_activation_pips":   (3.0,  20.0),
    "breakeven_lock_pips":         (0.5,  5.0),
    "partial_close_trigger_pips":  (5.0,  20.0),
    "partial_close_ratio":         (0.3,  0.7),
    "max_spread_pips":             (1.5,  5.0),
    "cooldown_minutes":            (10,   60),
    "lot_size":                    (0.1,  1.0),
    "max_lot_size":                (0.5,  2.0),
}

# 절대 변경 금지 파라미터 (핵심 리스크 지표)
FORBIDDEN_PARAMS = {
    "target_rr_ratio", "min_rr_ratio", "symbol", "bot_name",
    "magic_number", "max_positions",
}

# 한 번에 최대 변경 가능 파라미터 수
MAX_CHANGES_PER_RUN = 2

# 승인 만료 시간 (시간)
APPROVAL_EXPIRE_HOURS = 6

SYSTEM_PROMPT = """당신은 OchestraForRion의 Agent 5 (자율 개발자)입니다.
GBPAUD SELL 봇의 거래 통계를 분석하여 params.json 파라미터 개선안을 제안합니다.

━━━ 핵심 분석 방향 ━━━

이 봇의 핵심 문제는 "질 때 손실이 너무 크다"는 것입니다.
→ 모든 제안은 "손실을 얼마나 작게 끊는가"에 초점을 맞추세요.
→ 주요 파라미터: stop_loss_pips, sl_buffer_pips, trailing_activation_pips,
  breakeven_activation_pips, breakeven_lock_pips, partial_close_trigger_pips

━━━ 제안 원칙 ━━━

1. 데이터 기반: 최소 5건 이상 거래 데이터가 있는 패턴만 분석
2. 보수적 조정: 한 번에 최대 2개 파라미터, 현재값에서 ±30% 이내
3. 명확한 근거: 통계 수치를 직접 인용하여 "왜" 바꾸는지 설명
4. 활성화/비활성화: 승률 < 35% AND 평균손실 > 15pips 패턴만 비활성화 제안
   → boolean 파라미터 (xxx_enabled)는 매우 신중하게만 제안
5. 변경 불필요 시: 솔직하게 "현재 파라미터 적정" 반환

━━━ 절대 변경 금지 ━━━
target_rr_ratio, min_rr_ratio, symbol, bot_name, magic_number, max_positions

━━━ 응답 형식 (JSON만, 다른 텍스트 없음) ━━━

변경 제안 있는 경우:
{
  "changes": [
    {
      "param": "stop_loss_pips",
      "from": 13,
      "to": 11,
      "reason": "bamboo 패턴 평균 SL 도달 거리 10.8pips — 2pips 여유도 충분"
    }
  ],
  "summary": "bamboo SL 타이트 조정으로 평균 손실 -2~3pips 개선 예상",
  "expected_impact": "최근 bamboo 손실 거래 4건 기준 약 8pips × 4 = 32pips 절감"
}

변경 불필요한 경우:
{"changes": [], "summary": "현재 파라미터 데이터 기준 최적", "expected_impact": ""}"""


def _load_params(params_path: str) -> dict:
    try:
        with open(params_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning(f"[Agent5] params.json 로드 실패: {e}")
        return {}


def _build_context(stats_text: str, params: dict) -> str:
    """Opus에게 넘길 컨텍스트 빌드"""
    key_params = {
        k: params.get(k)
        for k in PARAM_LIMITS
        if k in params
    }
    # boolean 파라미터도 포함
    for k in list(params.keys()):
        if k.endswith("_enabled"):
            key_params[k] = params[k]

    return (
        f"{stats_text}\n\n"
        f"[현재 params.json (조정 가능 파라미터)]\n"
        + "\n".join(f"  {k}: {v}" for k, v in key_params.items())
    )


def _validate_changes(changes: list, params: dict) -> list:
    """변경안 유효성 검사 + 하드 클램프"""
    valid = []
    for c in changes:
        param = c.get("param", "")
        to_val = c.get("to")
        from_val = c.get("from")

        # 금지 파라미터 차단
        if param in FORBIDDEN_PARAMS:
            logger.warning(f"[Agent5] 금지 파라미터 변경 시도 차단: {param}")
            continue

        # params.json에 없는 파라미터 차단
        if param not in params and not param.endswith("_enabled"):
            logger.warning(f"[Agent5] 존재하지 않는 파라미터 차단: {param}")
            continue

        # boolean 파라미터 (xxx_enabled) 검사
        if param.endswith("_enabled"):
            if not isinstance(to_val, bool):
                logger.warning(f"[Agent5] boolean 타입 불일치 차단: {param}={to_val}")
                continue
            valid.append({
                "param": param,
                "from": params.get(param),
                "to": to_val,
                "reason": c.get("reason", ""),
            })
            continue

        # 숫자 파라미터 범위 클램프
        if param in PARAM_LIMITS:
            lo, hi = PARAM_LIMITS[param]
            try:
                to_val = float(to_val)
                to_val = max(lo, min(hi, to_val))
                # 정수형 파라미터는 int로
                if isinstance(params.get(param), int):
                    to_val = int(round(to_val))
                elif isinstance(params.get(param), float):
                    to_val = round(to_val, 1)
            except (TypeError, ValueError):
                logger.warning(f"[Agent5] 타입 변환 실패 차단: {param}={to_val}")
                continue

            if str(to_val) == str(from_val) or to_val == params.get(param):
                logger.info(f"[Agent5] 변경값 동일 — 스킵: {param} {from_val}→{to_val}")
                continue

        valid.append({
            "param": param,
            "from": params.get(param, from_val),
            "to": to_val,
            "reason": c.get("reason", ""),
        })

    # 최대 변경 수 제한
    return valid[:MAX_CHANGES_PER_RUN]


def analyze(db_path: str, params_path: str, days: int = 30) -> Optional[dict]:
    """
    Agent 5 핵심 함수: 성과 통계 + params 분석 → 변경 제안 생성

    Args:
        db_path:     trades.db 경로
        params_path: params.json 경로
        days:        분석 기간

    Returns:
        변경 제안 dict {"changes": [...], "summary": ..., "expected_impact": ...}
        변경 없으면 {"changes": [], ...}
        오류 시 None
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY 환경변수가 설정되지 않았습니다.")

    params = _load_params(params_path)
    if not params:
        logger.error("[Agent5] params.json 로드 실패 — 분석 중단")
        return None

    # DB 통계 조회
    try:
        reader = TradeDBReader(db_path)
        report = reader.get_stats(days=days)
    except Exception as e:
        logger.error(f"[Agent5] DB 통계 조회 실패: {e}")
        return None

    if report.total_closed < 5:
        logger.info(f"[Agent5] 청산 거래 {report.total_closed}건 — 분석 최소 건수(5) 미달, 스킵")
        return {"changes": [], "summary": f"청산 건수 부족 ({report.total_closed}건, 최소 5건)", "expected_impact": ""}

    # 통계 텍스트 빌드 (Agent 4와 동일 소스)
    from agents.performance_analyst import _build_stats_summary
    stats_text = _build_stats_summary(report)
    context = _build_context(stats_text, params)

    logger.info(
        f"[Agent5] Opus 분석 요청 — "
        f"청산 {report.total_closed}건, 패턴 {len(report.pattern_stats)}개"
    )

    client = anthropic.Anthropic(api_key=api_key)

    try:
        response = client.messages.create(
            model=OPUS_MODEL,
            max_tokens=600,
            system=SYSTEM_PROMPT,
            messages=[{
                "role": "user",
                "content": (
                    "다음 거래 통계와 현재 파라미터를 분석하여 "
                    "params.json 개선안을 제안해주세요.\n\n"
                    f"{context}"
                ),
            }],
        )
    except Exception as e:
        logger.error(f"[Agent5] Opus API 호출 실패: {e}")
        return None

    raw = response.content[0].text.strip()
    logger.info(
        f"[Agent5] 응답 수신 — "
        f"입력 {response.usage.input_tokens}토큰, "
        f"출력 {response.usage.output_tokens}토큰"
    )
    logger.debug(f"[Agent5] 원본 응답: {raw}")

    # JSON 파싱
    try:
        match = re.search(r"\{.*\}", raw, re.DOTALL)
        result = json.loads(match.group() if match else raw)
    except (json.JSONDecodeError, AttributeError) as e:
        logger.error(f"[Agent5] JSON 파싱 실패: {e} | 원본: {raw[:200]}")
        return None

    changes = result.get("changes", [])

    # 유효성 검사 + 클램프
    validated = _validate_changes(changes, params)

    return {
        "changes":         validated,
        "summary":         result.get("summary", ""),
        "expected_impact": result.get("expected_impact", ""),
    }


def save_pending(proposal: dict, pending_path: str) -> None:
    """변경 제안을 pending_params.json으로 저장"""
    data = {
        "timestamp":       datetime.now().isoformat(),
        "status":          "pending",
        "changes":         proposal["changes"],
        "summary":         proposal["summary"],
        "expected_impact": proposal["expected_impact"],
        "expires_at":      (datetime.now() + timedelta(hours=APPROVAL_EXPIRE_HOURS)).isoformat(),
        "message_id":      None,
    }
    Path(pending_path).parent.mkdir(parents=True, exist_ok=True)
    with open(pending_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    logger.info(f"[Agent5] pending_params.json 저장: {pending_path}")


def load_pending(pending_path: str) -> Optional[dict]:
    """pending_params.json 로드. 없거나 만료/처리됨이면 None."""
    try:
        with open(pending_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None

    if data.get("status") != "pending":
        return None

    # 만료 확인
    expires_at = data.get("expires_at")
    if expires_at:
        try:
            if datetime.fromisoformat(expires_at) < datetime.now():
                logger.info("[Agent5] pending_params 만료됨")
                data["status"] = "expired"
                with open(pending_path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                return None
        except ValueError:
            pass

    return data


def apply_changes(pending: dict, params_path: str, algo_dir: str) -> tuple:
    """
    승인된 변경사항 params.json에 적용 + git commit

    Returns:
        (success: bool, message: str)
    """
    changes = pending.get("changes", [])
    if not changes:
        return False, "변경 사항 없음"

    # params.json 로드
    params = _load_params(params_path)
    if not params:
        return False, "params.json 로드 실패"

    # 변경 전 백업 (git에서 복구 가능하므로 파일 백업은 생략, git commit으로 대체)
    applied = []
    for c in changes:
        param = c["param"]
        to_val = c["to"]
        old_val = params.get(param)
        params[param] = to_val
        applied.append(f"  {param}: {old_val} → {to_val}")
        logger.info(f"[Agent5] 파라미터 적용: {param} {old_val} → {to_val}")

    # params.json 저장
    try:
        with open(params_path, "w", encoding="utf-8") as f:
            json.dump(params, f, ensure_ascii=False, indent=2)
    except Exception as e:
        return False, f"params.json 저장 실패: {e}"

    logger.info(f"[Agent5] params.json 저장 완료: {params_path}")

    # git commit
    summary = pending.get("summary", "파라미터 튜닝")
    commit_msg = (
        f"tune: Agent5 자율 파라미터 튜닝 — {summary}\n\n"
        + "\n".join(applied)
        + "\n\nCo-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>"
    )
    try:
        subprocess.run(
            ["git", "add", "params.json"],
            cwd=algo_dir, check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", commit_msg],
            cwd=algo_dir, check=True, capture_output=True,
        )
        logger.info("[Agent5] git commit 완료")
    except subprocess.CalledProcessError as e:
        logger.warning(f"[Agent5] git commit 실패 (적용은 됨): {e.stderr.decode()[:100]}")
        # 파일은 이미 저장됨 — git 실패해도 계속 진행

    return True, "\n".join(applied)


def format_proposal_message(proposal: dict) -> str:
    """Telegram 전송용 제안 메시지 포맷"""
    changes = proposal.get("changes", [])
    if not changes:
        return ""

    lines = [
        "🔧 *Agent 5 파라미터 튜닝 제안*",
        "",
        f"📋 *요약*: {proposal.get('summary', '')}",
        f"📈 *예상 효과*: {proposal.get('expected_impact', '')}",
        "",
        "*변경 내용:*",
    ]
    for c in changes:
        lines.append(f"  • `{c['param']}`: {c['from']} → *{c['to']}*")
        lines.append(f"    _{c.get('reason', '')}_")

    lines += [
        "",
        "✅ 승인: `/approve_params`",
        "❌ 거절: `/reject_params`",
        f"⏰ 만료: {APPROVAL_EXPIRE_HOURS}시간 후 자동 폐기",
    ]
    return "\n".join(lines)
