"""
orchestrator.py — RionAgent 오케스트레이터

사용법:
  python orchestrator.py           # 데몬 모드 (Telegram 폴링 + 매일 03:00 KST 보고)
  python orchestrator.py --now     # Agent 4 즉시 실행 (Task Scheduler 호환)
  python orchestrator.py --days 7  # 최근 7일만 분석

환경변수 (.env):
  ANTHROPIC_API_KEY=...
  TELEGRAM_BOT_TOKEN=...
  TELEGRAM_CHAT_ID=...
  ALGOTRADINGBOT_DIR=C:\\Users\\rubay\\Documents\\projects\\AlgoTradingBot
"""
import os
import sys
import json
import logging
import argparse
import re
import schedule
import time
import threading
import subprocess
import requests

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
DB_PATH          = os.path.join(ALGOTRADINGBOT_DIR, "data", "trades.db")
CONTROL_BOT_PATH = os.path.join(ALGOTRADINGBOT_DIR, "rion_control_bot.py")
LAST_REPORT_FILE = os.path.join(os.path.dirname(__file__), "last_report.json")

# ── Telegram 설정 ─────────────────────────────────────────────────────────────
BOT_TOKEN     = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID       = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"

# ── Telegram 유틸 ─────────────────────────────────────────────────────────────

def _tg_get_updates(offset=None):
    try:
        resp = requests.get(
            f"{TELEGRAM_BASE}/getUpdates",
            params={"timeout": 30, "offset": offset},
            timeout=35,
        )
        return resp.json()
    except Exception as e:
        logger.error(f"[TG] getUpdates 오류: {e}")
        return None


def _tg_send(text: str) -> None:
    if not BOT_TOKEN or not CHAT_ID:
        return
    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]
    url = f"{TELEGRAM_BASE}/sendMessage"
    for chunk in chunks:
        try:
            resp = requests.post(
                url,
                json={"chat_id": CHAT_ID, "text": chunk},
                timeout=10,
            )
            if not resp.ok:
                logger.error(f"[TG] 전송 실패: {resp.status_code}")
            if len(chunks) > 1:
                time.sleep(0.3)
        except Exception as e:
            logger.error(f"[TG] 전송 오류: {e}")


def _load_last_report() -> dict:
    try:
        with open(LAST_REPORT_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


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


# ── 피드백 / 자유질문 처리 ────────────────────────────────────────────────────

def _handle_feedback(feedback_text: str) -> None:
    """보고서 Reply → Haiku 피드백 응답"""
    from bridge.trade_db_reader import TradeDBReader
    from agents.performance_analyst import analyze_feedback

    logger.info(f"[Orchestrator] 피드백 수신: {feedback_text[:60]}")
    try:
        report = TradeDBReader(DB_PATH).get_stats(days=30)
        response = analyze_feedback(report, feedback_text)
        _tg_send(response)
    except Exception as e:
        logger.error(f"[Orchestrator] 피드백 처리 오류: {e}", exc_info=True)
        _tg_send(f"❌ 피드백 처리 중 오류가 발생했습니다: {e}")


def _handle_free_text(text: str) -> None:
    """자유 텍스트 질문 → Claude Haiku 응답"""
    import anthropic
    from bridge.trade_db_reader import TradeDBReader
    from agents.performance_analyst import _build_stats_summary, HAIKU_MODEL

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        _tg_send("❌ ANTHROPIC_API_KEY가 설정되지 않았습니다.")
        return

    try:
        report = TradeDBReader(DB_PATH).get_stats(days=30)
        stats = _build_stats_summary(report)
    except Exception as e:
        stats = f"(통계 로드 실패: {e})"

    client = anthropic.Anthropic(api_key=api_key)
    try:
        resp = client.messages.create(
            model=HAIKU_MODEL,
            max_tokens=1000,
            system=(
                "당신은 AlohaCTO입니다. RionFX GBPAUD 트레이딩봇의 성과 분석을 담당하며 "
                "대표님(Ruba)의 질문에 존댓말(격식체)로 성실하게 답변합니다."
            ),
            messages=[{
                "role": "user",
                "content": f"[최근 30일 거래 통계]\n{stats}\n\n[질문]\n{text}",
            }],
        )
        answer = resp.content[0].text
        _tg_send(f"🤖 AlohaCTO\n\n{answer}")
    except Exception as e:
        logger.error(f"[Orchestrator] 자유질문 처리 오류: {e}")
        _tg_send(f"❌ 분석 중 오류: {e}")


# ── MT5 명령 subprocess 호출 ──────────────────────────────────────────────────

def _run_mt5_command(cmd: str, **kwargs) -> str:
    """MT5 명령 → rion_control_bot.py subprocess 호출"""
    if not os.path.exists(CONTROL_BOT_PATH):
        return f"❌ rion_control_bot.py를 찾을 수 없습니다:\n{CONTROL_BOT_PATH}"

    args = [sys.executable, CONTROL_BOT_PATH, "--cmd", cmd]
    if kwargs.get("ticket"):
        args += ["--ticket", str(kwargs["ticket"])]

    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=30,
            cwd=ALGOTRADINGBOT_DIR,
            encoding="utf-8",
            errors="replace",
        )
        output = result.stdout.strip() or result.stderr.strip() or "✅ 완료"
        return output[:3500]
    except subprocess.TimeoutExpired:
        return "⏱️ 명령 실행 시간 초과 (30초)"
    except Exception as e:
        return f"❌ 오류: {e}"


def _detect_close_intent(text: str):
    """텍스트에서 수동 청산 의도 + 티켓번호 추출"""
    keywords = ['청산', '종료', '닫기', '그만', 'close', 'exit', 'stop']
    has_keyword = any(kw in text.lower() for kw in keywords)
    ticket_match = re.search(r'(\d{8,11})', text)
    if has_keyword and ticket_match:
        return int(ticket_match.group(1))
    return None


# ── Telegram 폴링 데몬 ────────────────────────────────────────────────────────

HELP_TEXT = (
    "📋 RionAgent 명령어\n\n"
    "/report    — AlohaCTO 성과 분석 즉시 실행\n"
    "/status    — 봇 실행 상태\n"
    "/log       — 최근 로그 30줄\n"
    "/positions — 포지션 정보\n"
    "/params    — 현재 파라미터\n"
    "/restart   — 봇 재시작\n"
    "/help      — 이 도움말\n\n"
    "그 외 텍스트 → AlohaCTO AI 분석\n"
    "보고서 메시지에 Reply → 피드백 응답"
)


def run_telegram_daemon() -> None:
    """Telegram 폴링 데몬"""
    if not BOT_TOKEN or not CHAT_ID:
        logger.warning("[TG] BOT_TOKEN 또는 CHAT_ID 미설정 — 폴링 비활성화")
        return

    logger.info("[TG] 폴링 데몬 시작")
    offset = None
    retry_delay = 5

    while True:
        try:
            updates = _tg_get_updates(offset)
            if not updates or not updates.get("ok"):
                time.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 60)
                continue

            retry_delay = 5

            for update in updates.get("result", []):
                offset = update["update_id"] + 1

                msg     = update.get("message", {})
                chat_id = str(msg.get("chat", {}).get("id", ""))
                text    = msg.get("text", "").strip()

                if chat_id != str(CHAT_ID) or not text:
                    continue

                logger.info(f"[TG] 수신: {text[:80]}")

                # ── 보고서 Reply 감지 ────────────────────────────────────────
                reply_to = msg.get("reply_to_message", {})
                if reply_to:
                    last = _load_last_report()
                    if last and reply_to.get("message_id") == last.get("message_id"):
                        threading.Thread(
                            target=_handle_feedback, args=(text,), daemon=True
                        ).start()
                        continue

                # ── 명령어 처리 ──────────────────────────────────────────────
                cmd = text.split("@")[0]  # @봇이름 제거

                if cmd == "/report":
                    _tg_send("📊 AlohaCTO Agent 4 분석 중... (30초 내외 소요)")
                    threading.Thread(target=run_agent4, daemon=True).start()

                elif cmd == "/help" or cmd == "/start":
                    _tg_send(HELP_TEXT)

                elif cmd == "/status":
                    _tg_send("⏳ 조회 중...")
                    threading.Thread(
                        target=lambda: _tg_send(_run_mt5_command("status")),
                        daemon=True,
                    ).start()

                elif cmd == "/log":
                    threading.Thread(
                        target=lambda: _tg_send(_run_mt5_command("log")),
                        daemon=True,
                    ).start()

                elif cmd == "/positions":
                    threading.Thread(
                        target=lambda: _tg_send(_run_mt5_command("positions")),
                        daemon=True,
                    ).start()

                elif cmd == "/params":
                    threading.Thread(
                        target=lambda: _tg_send(_run_mt5_command("params")),
                        daemon=True,
                    ).start()

                elif cmd == "/restart":
                    _tg_send("⏳ 재시작 중...")
                    threading.Thread(
                        target=lambda: _tg_send(_run_mt5_command("restart")),
                        daemon=True,
                    ).start()

                elif text.startswith("/"):
                    _tg_send(f"❓ 알 수 없는 명령어: {text}\n/help 로 확인하세요.")

                else:
                    # ── 수동 청산 감지 ────────────────────────────────────
                    ticket = _detect_close_intent(text)
                    if ticket:
                        _tg_send(f"⏳ 티켓 {ticket} 청산 시도 중...")
                        threading.Thread(
                            target=lambda t=ticket: _tg_send(
                                _run_mt5_command("close", ticket=t)
                            ),
                            daemon=True,
                        ).start()
                    else:
                        # ── 자유 텍스트 → AlohaCTO AI ────────────────────
                        _tg_send("🔍 분석 중...")
                        threading.Thread(
                            target=_handle_free_text, args=(text,), daemon=True
                        ).start()

        except KeyboardInterrupt:
            logger.info("[TG] 폴링 데몬 종료")
            break
        except Exception as e:
            logger.error(f"[TG] 폴링 루프 오류: {e}")
            time.sleep(10)


# ── 진입점 ────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="RionAgent Orchestrator")
    parser.add_argument(
        "--now", action="store_true", help="Agent 4 즉시 실행 (Task Scheduler 호환)"
    )
    parser.add_argument(
        "--days", type=int, default=30, help="분석 기간 (일수, 기본값=30)"
    )
    parser.add_argument(
        "--time", default="03:00", help="KST 보고 시각 (HH:MM, 기본값=03:00)"
    )
    args = parser.parse_args()

    if not os.path.exists(DB_PATH):
        logger.error(f"trades.db를 찾을 수 없습니다: {DB_PATH}")
        logger.error("ALGOTRADINGBOT_DIR 환경변수를 확인하거나 .env 파일을 설정하세요.")
        sys.exit(1)

    if args.now:
        # ── 즉시 실행 모드 (Task Scheduler 호환) ─────────────────────────────
        logger.info(f"[Orchestrator] 즉시 실행 모드 (기간: {args.days}일)")
        run_agent4(days=args.days)
        return

    # ── 데몬 모드: 스케줄러 + Telegram 폴링 ──────────────────────────────────
    logger.info(f"[Orchestrator] 데몬 시작")
    logger.info(f"  일일 보고: 매일 {args.time} KST")
    logger.info(f"  DB 경로: {DB_PATH}")
    logger.info(f"  Telegram 폴링: {'활성화' if BOT_TOKEN and CHAT_ID else '비활성화 (토큰 없음)'}")

    schedule.every().day.at(args.time).do(run_agent4, days=args.days)
    next_run = schedule.next_run()
    logger.info(f"  다음 보고 예정: {next_run}")

    # 스케줄러를 별도 스레드에서 실행
    def _scheduler_loop():
        while True:
            schedule.run_pending()
            time.sleep(30)

    threading.Thread(target=_scheduler_loop, daemon=True, name="scheduler").start()

    # Telegram 폴링을 메인 스레드에서 실행
    run_telegram_daemon()


if __name__ == "__main__":
    main()
