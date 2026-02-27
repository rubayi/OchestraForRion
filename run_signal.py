"""
run_signal.py — Agent 1 + Agent 2 + Agent 3 파이프라인 진입점

Usage:
    python run_signal.py --input PATH --output PATH [--dry-run]
    python run_signal.py --input PATH --output PATH [--db PATH] [--params PATH]

출력: signal.json (AlgoTradingBot 연동용)
비용 가드: 일일 Opus 호출 5회 캡 (.opus_daily_count.json)

signal.json 형식:
{
  "timestamp": "2026-02-25T15:00:00",
  "symbol": "GBPAUD",
  "agent1": {"pass": true, "context": "...", "strength": 72},
  "agent2": {"decision": "ENTER", "confidence": 78, "reason": "..."},
  "agent3": {"sl_pips": 13.0, "tp_pips": 26.0, "lot_size": 0.5, "rr_ratio": 2.0,
             "mode": "normal", "reason": "..."},
  "final_decision": "ENTER"
}
"""

import argparse
import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

# 패키지 루트를 sys.path에 추가 (직접 실행 시)
sys.path.insert(0, str(Path(__file__).parent))

# .env 파일 자동 로드 (ANTHROPIC_API_KEY 등)
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass  # python-dotenv 미설치 시 환경변수 직접 설정 필요

from agents.market_analyst import analyze as agent1_analyze
from agents.trade_decision import decide as agent2_decide
from agents.risk_manager import manage as agent3_manage

# ── 기본 경로 (환경변수 또는 하드코딩 fallback) ──────────────────────────────
_ALGO_DIR      = os.environ.get("ALGOTRADINGBOT_DIR", r"C:\Users\rubay\Documents\projects\AlgoTradingBot")
DEFAULT_DB     = os.path.join(_ALGO_DIR, "data", "trades.db")
DEFAULT_PARAMS = os.path.join(_ALGO_DIR, "params.json")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("run_signal")

# 일일 Opus 호출 캡
OPUS_DAILY_LIMIT = 5
OPUS_COUNT_FILE = Path(__file__).parent / ".opus_daily_count.json"


def _load_opus_counter() -> dict:
    """일일 Opus 호출 카운터 로드. 날짜 바뀌면 자동 리셋."""
    today = datetime.now().strftime("%Y-%m-%d")
    if OPUS_COUNT_FILE.exists():
        try:
            with open(OPUS_COUNT_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if data.get("date") == today:
                return data
        except (json.JSONDecodeError, KeyError):
            pass
    return {"date": today, "count": 0}


def _save_opus_counter(counter: dict) -> None:
    """일일 Opus 호출 카운터 저장."""
    with open(OPUS_COUNT_FILE, "w", encoding="utf-8") as f:
        json.dump(counter, f)


def _check_opus_limit() -> tuple:
    """
    Opus 일일 한도 확인.

    Returns:
        (allowed: bool, counter: dict)
    """
    counter = _load_opus_counter()
    return counter["count"] < OPUS_DAILY_LIMIT, counter


def _increment_opus_counter(counter: dict) -> None:
    """Opus 호출 카운터 증가 + 저장."""
    counter["count"] += 1
    _save_opus_counter(counter)


def _get_symbol(json_path: str) -> str:
    """JSON에서 심볼 추출 (실패 시 GBPAUD 기본값)."""
    try:
        with open(json_path, "r", encoding="utf-8") as f:
            return json.load(f).get("symbol", "GBPAUD")
    except Exception:
        return "GBPAUD"


def _write_signal(signal: dict, output_path: str) -> None:
    """signal.json 쓰기 (디렉토리 없으면 자동 생성)."""
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(signal, f, ensure_ascii=False, indent=2)
    logger.info(f"signal.json 저장: {output_path}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="RionAgent Signal Pipeline — Agent 1 (Haiku) + Agent 2 (Opus)"
    )
    parser.add_argument("--input", required=True, help="rion_data_now.json 경로")
    parser.add_argument("--output", required=True, help="signal.json 출력 경로")
    parser.add_argument("--db", default=DEFAULT_DB, help=f"trades.db 경로 (기본값: {DEFAULT_DB})")
    parser.add_argument("--params", default=DEFAULT_PARAMS, help=f"params.json 경로 (기본값: {DEFAULT_PARAMS})")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="실제 API 미호출 — 파이프라인 구조만 검증",
    )
    args = parser.parse_args()

    timestamp = datetime.now().isoformat(timespec="seconds")
    logger.info(f"=== RionAgent Signal Pipeline 시작 ({timestamp}) ===")
    logger.info(f"입력: {args.input}")
    logger.info(f"출력: {args.output}")

    # ── DRY-RUN 모드 ───────────────────────────────────────────────
    if args.dry_run:
        logger.info("[DRY-RUN] API 호출 없이 구조만 검증합니다")
        signal = {
            "timestamp": timestamp,
            "symbol": "GBPAUD",
            "agent1": {"pass": True, "context": "[DRY-RUN] 테스트 컨텍스트", "strength": 70},
            "agent2": {
                "decision": "SKIP",
                "confidence": 0,
                "reason": "[DRY-RUN] 실제 API 미호출",
            },
            "agent3": None,
            "final_decision": "SKIP",
            "dry_run": True,
        }
        _write_signal(signal, args.output)
        logger.info("[DRY-RUN] 완료 — signal.json 작성 성공")
        return 0

    symbol = _get_symbol(args.input)

    # ── Agent 1 (Haiku 시장 필터) ───────────────────────────────────
    logger.info("--- Agent 1 (Haiku 시장 필터) 실행 중 ---")
    agent1_result = agent1_analyze(args.input)
    logger.info(
        f"Agent 1 결과: pass={agent1_result['pass']}, "
        f"strength={agent1_result['strength']}"
    )

    # Agent 1 미통과 → 즉시 SKIP (Opus 미호출)
    if not agent1_result["pass"]:
        logger.info("Agent 1 필터 미통과 → SKIP (Opus 미호출, 비용 절약)")
        signal = {
            "timestamp": timestamp,
            "symbol": symbol,
            "agent1": agent1_result,
            "agent2": None,
            "final_decision": "SKIP",
            "skip_reason": "agent1_filter",
        }
        _write_signal(signal, args.output)
        return 0

    # ── Opus 일일 한도 확인 ─────────────────────────────────────────
    allowed, counter = _check_opus_limit()
    if not allowed:
        logger.warning(
            f"일일 Opus 한도 초과: {counter['count']}/{OPUS_DAILY_LIMIT}회 "
            f"({counter['date']}) → Agent 2 스킵"
        )
        signal = {
            "timestamp": timestamp,
            "symbol": symbol,
            "agent1": agent1_result,
            "agent2": {
                "decision": "SKIP",
                "confidence": 0,
                "reason": (
                    f"일일 Opus 한도 초과 "
                    f"({counter['count']}/{OPUS_DAILY_LIMIT}회)"
                ),
            },
            "final_decision": "SKIP",
            "skip_reason": "opus_daily_limit",
        }
        _write_signal(signal, args.output)
        return 0

    # ── Agent 2 (Opus 매매 결정) ────────────────────────────────────
    logger.info(
        f"--- Agent 2 (Opus 매매 결정) 실행 중 "
        f"[오늘 {counter['count']}/{OPUS_DAILY_LIMIT}회 사용] ---"
    )
    agent2_result = agent2_decide(agent1_result, args.input)

    # Opus 카운터 증가
    _increment_opus_counter(counter)
    logger.info(
        f"Opus 카운터 업데이트: {counter['count']}/{OPUS_DAILY_LIMIT}회"
    )

    final_decision = agent2_result["decision"]
    logger.info(
        f"Agent 2 결정: {final_decision} "
        f"(confidence={agent2_result['confidence']})"
    )

    # ── Agent 3 (Haiku 리스크 계산) — ENTER 결정 시에만 실행 ─────────────────
    agent3_result = None
    if final_decision == "ENTER":
        logger.info(
            f"--- Agent 3 (Haiku 리스크 계산) 실행 중 "
            f"[DB={args.db}] [params={args.params}] ---"
        )
        try:
            agent3_result = agent3_manage(
                agent2_result=agent2_result,
                json_path=args.input,
                db_path=args.db,
                params_path=args.params,
            )
            logger.info(
                f"Agent 3 결과: SL={agent3_result['sl_pips']}p | "
                f"TP={agent3_result['tp_pips']}p | "
                f"랏={agent3_result['lot_size']} | "
                f"RR={agent3_result['rr_ratio']} | "
                f"모드={agent3_result['mode']}"
            )
        except Exception as e:
            logger.error(f"Agent 3 오류: {e}", exc_info=True)
            # Agent 3 실패해도 파이프라인 계속 (ENTER 결정은 유지)

    # ── signal.json 출력 ────────────────────────────────────────────
    signal = {
        "timestamp": timestamp,
        "symbol": symbol,
        "agent1": agent1_result,
        "agent2": agent2_result,
        "agent3": agent3_result,
        "final_decision": final_decision,
    }
    _write_signal(signal, args.output)

    logger.info(f"=== 완료: final_decision={final_decision} ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
