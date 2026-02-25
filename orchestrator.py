"""
orchestrator.py — RionAgent 오케스트레이터

사용법:
  python orchestrator.py           # 스케줄러 모드 (매일 03:00 KST)
  python orchestrator.py --now     # Agent 4 즉시 실행 (테스트용)
  python orchestrator.py --days 7  # 최근 7일만 분석

환경변수 (.env):
  ANTHROPIC_API_KEY=...
  TELEGRAM_BOT_TOKEN=...
  TELEGRAM_CHAT_ID=...
  ALGOTRADINGBOT_DIR=C:\\Users\\rubay\\Documents\\projects\\AlgoTradingBot
"""
import os
import sys
import logging
import argparse
import schedule
import time

from dotenv import load_dotenv

load_dotenv()

# ── 로깅 설정 ────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── 경로 설정 ─────────────────────────────────────────────────────────────────
ALGOTRADINGBOT_DIR = os.environ.get(
    "ALGOTRADINGBOT_DIR",
    r"C:\Users\rubay\Documents\projects\AlgoTradingBot",
)
DB_PATH = os.path.join(ALGOTRADINGBOT_DIR, "data", "trades.db")

# ── Agent 4 실행 ──────────────────────────────────────────────────────────────

def run_agent4(days: int = 30) -> None:
    """Agent 4 (성과 분석) 실행"""
    from agents.performance_analyst import run as agent4_run

    logger.info("=" * 50)
    logger.info(f"[Orchestrator] Agent 4 실행 시작 (최근 {days}일)")
    try:
        success = agent4_run(db_path=DB_PATH, days=days)
        if success:
            logger.info("[Orchestrator] Agent 4 완료 ✅")
        else:
            logger.warning("[Orchestrator] Agent 4 완료 (Telegram 전송 실패)")
    except FileNotFoundError as e:
        logger.error(f"[Orchestrator] DB 파일 없음: {e}")
    except Exception as e:
        logger.error(f"[Orchestrator] Agent 4 오류: {e}", exc_info=True)
    logger.info("=" * 50)


# ── 진입점 ────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="RionAgent Orchestrator")
    parser.add_argument(
        "--now", action="store_true", help="Agent 4 즉시 실행 (스케줄 없이)"
    )
    parser.add_argument(
        "--days", type=int, default=30, help="분석 기간 (일수, 기본값=30)"
    )
    parser.add_argument(
        "--time", default="03:00", help="KST 실행 시각 (HH:MM, 기본값=03:00)"
    )
    args = parser.parse_args()

    # DB 존재 여부 사전 확인
    if not os.path.exists(DB_PATH):
        logger.error(f"trades.db를 찾을 수 없습니다: {DB_PATH}")
        logger.error("ALGOTRADINGBOT_DIR 환경변수를 확인하거나 .env 파일을 설정하세요.")
        sys.exit(1)

    if args.now:
        # ── 즉시 실행 모드 ────────────────────────────────────────────────
        logger.info(f"[Orchestrator] 즉시 실행 모드 (기간: {args.days}일)")
        run_agent4(days=args.days)
        return

    # ── 스케줄러 모드 ─────────────────────────────────────────────────────────
    logger.info(f"[Orchestrator] 스케줄러 시작 — 매일 {args.time} KST 실행")
    logger.info(f"  DB 경로: {DB_PATH}")
    logger.info(f"  분석 기간: {args.days}일")

    schedule.every().day.at(args.time).do(run_agent4, days=args.days)

    # 스케줄러 시작 시 즉시 한 번 실행할지 확인
    next_run = schedule.next_run()
    logger.info(f"  다음 실행: {next_run}")

    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
