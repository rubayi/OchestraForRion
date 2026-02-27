"""
rion_bot.py — OchestraForRion 독립 트레이딩봇

ICMarkets 신규 데모 계정에서 Claude 에이전트 파이프라인으로 독립 거래.
기존 rion_watcher.py와 완전 분리 — 공유 자원 없음.

플로우 (5분마다):
  M5 캔들 마감
    → 시장 데이터 수집 (collect_pattern_data_advanced.py)
    → 포지션 없으면: Agent 1 (Haiku 필터) → Agent 2 (Opus 결정) → Agent 3 (Haiku SL/TP)
    → ENTER → MT5 SELL 주문 실행
    → 포지션 있으면: 단순 모니터링 (SL/TP는 MT5가 자동 처리)

사용법:
  python rion_bot.py              # 실거래 모드
  python rion_bot.py --dry-run    # 주문 미실행 (구조/파이프라인 검증)

환경변수 (.env):
  ANTHROPIC_API_KEY=...
  TELEGRAM_BOT_TOKEN=...
  TELEGRAM_CHAT_ID=...
  ALGOTRADINGBOT_DIR=C:\\...\\AlgoTradingBot
  RIONBOT_MT5_CONFIG=C:\\...\\mt5_config_rionbot.json   (기본: ./mt5_config_rionbot.json)
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

# ── 패키지 루트 sys.path 추가 ────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))

# ── .env 자동 로드 ────────────────────────────────────────────────────────────
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent / ".env")
except ImportError:
    pass

import requests

# ── 로깅 설정 ─────────────────────────────────────────────────────────────────
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

# Windows cp949 터미널에서 유니코드(이모지) 출력 가능하도록 UTF-8 강제
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_DIR / "rion_bot.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("rion_bot")

# ── 경로 설정 ─────────────────────────────────────────────────────────────────
BASE_DIR       = Path(__file__).parent
ALGO_DIR       = Path(os.environ.get("ALGOTRADINGBOT_DIR", r"C:\Users\rubay\Documents\projects\AlgoTradingBot"))
COLLECTOR_PATH = ALGO_DIR / "collect_pattern_data_advanced.py"
JSON_PATH      = ALGO_DIR / "rion_data" / "rion_data_now.json"    # 기존 봇과 시장 데이터 공유
DB_PATH        = ALGO_DIR / "data" / "trades.db"
PARAMS_PATH    = BASE_DIR / "rionbot_params.json"
CONFIG_PATH    = Path(os.environ.get("RIONBOT_MT5_CONFIG", str(BASE_DIR / "mt5_config_rionbot.json")))
LOCK_FILE      = BASE_DIR / ".rion_bot.lock"

# ── 봇 설정 ───────────────────────────────────────────────────────────────────
SYMBOL           = "GBPAUD"
MAGIC            = 20261001        # OchestraForRion 전용 magic number
M5_SECONDS       = 300             # 5분 캔들
SIGNAL_COOLDOWN  = 900             # 15분 이내 재진입 차단 (초)
JSON_MAX_AGE     = 180             # rion_data_now.json 최대 허용 오래됨 (초)
OPUS_DAILY_LIMIT = 5               # run_signal.py와 공유

# ── Telegram ──────────────────────────────────────────────────────────────────
# RIONBOT_TELEGRAM_TOKEN: OchestraRionBot 전용 (@OchestraRionBot)
# 없으면 공용 TELEGRAM_BOT_TOKEN으로 폴백
BOT_TOKEN     = os.environ.get("RIONBOT_TELEGRAM_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID       = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"


def _tg_send(text: str) -> None:
    """Telegram 메시지 전송 (실패해도 봇 중단 없음)"""
    if not BOT_TOKEN or not CHAT_ID:
        return
    try:
        requests.post(
            f"{TELEGRAM_BASE}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"},
            timeout=10,
        )
    except Exception as e:
        logger.warning(f"Telegram 전송 실패: {e}")


# ── 단일 인스턴스 보장 ────────────────────────────────────────────────────────

def _acquire_lock() -> bool:
    """다른 rion_bot.py 인스턴스가 있으면 False"""
    if LOCK_FILE.exists():
        try:
            pid = int(LOCK_FILE.read_text().strip())
            # 프로세스가 실제로 살아있는지 확인
            import psutil
            if psutil.pid_exists(pid):
                logger.error(f"이미 실행 중인 rion_bot.py PID {pid}")
                return False
        except Exception:
            pass
    LOCK_FILE.write_text(str(os.getpid()))
    return True


def _release_lock() -> None:
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except Exception:
        pass


# ── 시장 데이터 수집 ──────────────────────────────────────────────────────────

def collect_market_data() -> bool:
    """collect_pattern_data_advanced.py 실행 → rion_data_now.json 갱신

    Returns:
        True = 신선한 JSON 갱신 확인
    """
    if not COLLECTOR_PATH.exists():
        logger.error(f"수집 스크립트 없음: {COLLECTOR_PATH}")
        return False

    for attempt in range(3):
        if attempt > 0:
            time.sleep(5)
        try:
            result = subprocess.run(
                [sys.executable, str(COLLECTOR_PATH)],
                capture_output=True,
                text=True,
                timeout=60,
                encoding="utf-8",
            )
            if result.returncode != 0:
                logger.warning(f"데이터 수집 실패 (시도 {attempt+1}/3): {result.stderr[:150]}")
                continue

            # JSON 신선도 확인
            if JSON_PATH.exists():
                age = time.time() - JSON_PATH.stat().st_mtime
                if age <= JSON_MAX_AGE:
                    logger.info(f"시장 데이터 갱신 완료 (age={age:.0f}초)")
                    return True
                logger.warning(f"JSON 갱신됐으나 오래됨: {age:.0f}초 (최대 {JSON_MAX_AGE}초)")
            else:
                logger.warning("rion_data_now.json 파일 없음")

        except subprocess.TimeoutExpired:
            logger.warning(f"데이터 수집 타임아웃 (시도 {attempt+1}/3)")

    return False


# ── 신호 파이프라인 ────────────────────────────────────────────────────────────

def run_pipeline(dry_run: bool = False) -> Optional[dict]:
    """Agent 1 → Agent 2 → Agent 3 파이프라인 실행

    Returns:
        ENTER 결정 시: {"agent1": ..., "agent2": ..., "agent3": ...}
        SKIP 또는 오류: None
    """
    from agents.market_analyst import analyze as agent1_analyze
    from agents.trade_decision import decide as agent2_decide
    from agents.risk_manager import manage as agent3_manage

    json_path = str(JSON_PATH)

    # ── Agent 1 (Haiku 시장 필터) ──────────────────────────────────────────
    try:
        a1 = agent1_analyze(json_path)
    except Exception as e:
        logger.error(f"Agent 1 오류: {e}")
        return None

    logger.info(f"Agent 1: pass={a1.get('pass')} | strength={a1.get('strength')}/100")

    if not a1.get("pass"):
        logger.info(f"Agent 1 필터 미통과 — SKIP (Opus 미호출)")
        return None

    # ── Opus 일일 한도 확인 ────────────────────────────────────────────────
    # run_signal.py의 카운터 파일을 공유 사용
    opus_counter_file = BASE_DIR / ".opus_daily_count.json"
    today = datetime.now().strftime("%Y-%m-%d")
    try:
        data = json.loads(opus_counter_file.read_text()) if opus_counter_file.exists() else {}
        count = data.get("count", 0) if data.get("date") == today else 0
    except Exception:
        count = 0

    if count >= OPUS_DAILY_LIMIT:
        logger.warning(f"Opus 일일 한도 초과 ({count}/{OPUS_DAILY_LIMIT}) — Agent 2 스킵")
        _tg_send(f"⚠️ OchestraForRion: Opus 일일 한도 초과 ({count}/{OPUS_DAILY_LIMIT}회)")
        return None

    # ── Agent 2 (Opus 매매 결정) ───────────────────────────────────────────
    if dry_run:
        logger.info("[DRY-RUN] Agent 2 (Opus) 미호출")
        return {
            "agent1": a1,
            "agent2": {"decision": "ENTER", "confidence": 0, "reason": "[DRY-RUN]"},
            "agent3": {"sl_pips": 13.0, "tp_pips": 26.0, "lot_size": 0.1,
                       "rr_ratio": 2.0, "mode": "normal", "reason": "[DRY-RUN]"},
        }

    try:
        a2 = agent2_decide(a1, json_path)
    except Exception as e:
        logger.error(f"Agent 2 오류: {e}")
        return None

    # Opus 카운터 증가
    try:
        opus_counter_file.write_text(json.dumps({"date": today, "count": count + 1}))
    except Exception:
        pass

    logger.info(f"Agent 2: {a2.get('decision')} | confidence={a2.get('confidence')}%")

    if a2.get("decision") != "ENTER":
        logger.info(f"Agent 2 SKIP — {a2.get('reason', '')[:80]}")
        return None

    # ── Agent 3 (Haiku SL/TP/랏) ──────────────────────────────────────────
    try:
        a3 = agent3_manage(
            agent2_result=a2,
            json_path=json_path,
            db_path=str(DB_PATH),
            params_path=str(PARAMS_PATH),
        )
    except Exception as e:
        logger.error(f"Agent 3 오류: {e} — params.json 기본값 사용")
        # fallback
        params = json.loads(PARAMS_PATH.read_text(encoding="utf-8")) if PARAMS_PATH.exists() else {}
        a3 = {
            "sl_pips":  float(params.get("stop_loss_pips", 13)),
            "tp_pips":  float(params.get("take_profit_pips", 26)),
            "lot_size": float(params.get("lot_size", 0.5)),
            "rr_ratio": 2.0,
            "mode":     "normal",
            "reason":   "fallback",
        }

    logger.info(
        f"Agent 3: SL={a3['sl_pips']}p | TP={a3['tp_pips']}p | "
        f"랏={a3['lot_size']} | RR={a3['rr_ratio']} | 모드={a3['mode']}"
    )

    return {"agent1": a1, "agent2": a2, "agent3": a3}


# ── 메인 루프 ─────────────────────────────────────────────────────────────────

def _next_m5_close_in(buffer: float = 5.0) -> float:
    """다음 M5 캔들 마감까지 남은 초 (buffer초 전에 깨어남)"""
    now = time.time()
    remainder = now % M5_SECONDS
    secs_to_close = M5_SECONDS - remainder
    return max(10.0, secs_to_close - buffer)


def main_loop(dry_run: bool = False) -> None:
    """메인 트레이딩 루프"""
    from bridge.mt5_executor import connect, get_positions, place_sell, disconnect, get_account_info

    # MT5 연결
    if not connect(str(CONFIG_PATH)):
        logger.error("MT5 연결 실패 — 종료")
        _tg_send("❌ OchestraForRion: MT5 연결 실패 — 봇 종료")
        return

    acc = get_account_info()
    start_msg = (
        f"🚀 *OchestraForRion 봇 시작*\n"
        f"계정: {acc['login']} @ {acc['server']}\n"
        f"잔고: {acc['balance']:.2f} {acc['currency']}\n"
        f"{'⚠️ DRY-RUN 모드 (주문 미실행)' if dry_run else '✅ 실거래 모드'}"
    )
    logger.info(start_msg.replace("*", ""))
    _tg_send(start_msg)

    last_signal_time = 0.0

    try:
        while True:
            # ── M5 마감 대기 ────────────────────────────────────────────────
            wait_secs = _next_m5_close_in()
            logger.info(f"다음 M5 마감까지 {wait_secs:.0f}초 대기...")
            time.sleep(wait_secs)

            logger.info("=" * 50)
            logger.info(f"스캔 시작: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

            # ── 시장 데이터 수집 ────────────────────────────────────────────
            if not collect_market_data():
                logger.warning("데이터 수집 실패 — 이번 캔들 스킵")
                time.sleep(60)
                continue

            # ── 현재 포지션 확인 ────────────────────────────────────────────
            positions = get_positions(magic=MAGIC)

            if positions:
                for p in positions:
                    pnl_pips = (p.price_open - p.price_current) * 10000
                    logger.info(
                        f"포지션 유지 중: Ticket={p.ticket} | "
                        f"진입={p.price_open:.5f} | 현재={p.price_current:.5f} | "
                        f"손익={pnl_pips:+.1f}pips"
                    )
                logger.info("포지션 보유 중 → 신호 파이프라인 스킵 (SL/TP는 MT5 자동 처리)")
                continue

            # ── 쿨다운 체크 ─────────────────────────────────────────────────
            elapsed = time.time() - last_signal_time
            if elapsed < SIGNAL_COOLDOWN:
                remaining = SIGNAL_COOLDOWN - elapsed
                logger.info(f"쿨다운 중 ({remaining:.0f}초 남음) — 스킵")
                continue

            # ── 신호 파이프라인 실행 ────────────────────────────────────────
            logger.info("신호 파이프라인 실행 (Agent 1→2→3)...")
            result = run_pipeline(dry_run=dry_run)

            if result is None:
                logger.info("파이프라인 결과: SKIP")
                continue

            # ENTER 결정
            a2 = result["agent2"]
            a3 = result["agent3"]
            sl   = a3["sl_pips"]
            tp   = a3["tp_pips"]
            lot  = a3["lot_size"]
            conf = a2.get("confidence", 0)
            reason = a2.get("reason", "")[:120]

            logger.info(f"ENTER 결정 — SL={sl}p | TP={tp}p | 랏={lot} | confidence={conf}%")

            if dry_run:
                logger.info(f"[DRY-RUN] 주문 미실행: SELL {SYMBOL} {lot}랏 SL={sl}p TP={tp}p")
                _tg_send(
                    f"🧪 *OchestraForRion DRY-RUN*\n"
                    f"SELL {SYMBOL} | 랏={lot} | SL={sl}p | TP={tp}p\n"
                    f"confidence={conf}% | {reason}"
                )
                last_signal_time = time.time()
                continue

            # ── MT5 주문 실행 ───────────────────────────────────────────────
            order = place_sell(
                symbol=SYMBOL,
                lot=lot,
                sl_pips=sl,
                tp_pips=tp,
                magic=MAGIC,
                comment="OrchestraRion",
            )

            last_signal_time = time.time()

            if order["success"]:
                ticket = order["ticket"]
                price  = order["price"]
                logger.info(f"진입 성공: Ticket={ticket} | 가격={price:.5f}")
                _tg_send(
                    f"✅ *OchestraForRion 진입*\n"
                    f"Ticket: `{ticket}` | SELL {SYMBOL}\n"
                    f"가격: {price:.5f} | SL: {sl}p | TP: {tp}p | 랏: {lot}\n"
                    f"confidence: {conf}% | 모드: {a3['mode']}\n"
                    f"근거: {reason}"
                )
            else:
                logger.error(f"진입 실패: {order['error']}")
                _tg_send(f"❌ *OchestraForRion 진입 실패*\n{order['error']}")

    except KeyboardInterrupt:
        logger.info("사용자 중단 (KeyboardInterrupt)")
    except Exception as e:
        logger.exception(f"치명적 오류: {e}")
        _tg_send(f"🚨 *OchestraForRion 봇 오류*\n{e}")
    finally:
        disconnect()
        _release_lock()
        logger.info("봇 종료")
        _tg_send("🛑 OchestraForRion 봇 종료")


# ── 진입점 ────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="OchestraForRion 독립 트레이딩봇")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="MT5 주문 미실행 — Agent 파이프라인 구조만 검증",
    )
    args = parser.parse_args()

    # 설정 파일 존재 확인
    if not CONFIG_PATH.exists() and not args.dry_run:
        logger.error(f"MT5 설정 파일 없음: {CONFIG_PATH}")
        logger.error("mt5_config_rionbot.json.example 을 복사 후 계정 정보를 입력하세요.")
        sys.exit(1)

    if not PARAMS_PATH.exists():
        logger.error(f"봇 파라미터 파일 없음: {PARAMS_PATH}")
        logger.error("rionbot_params.json이 필요합니다.")
        sys.exit(1)

    # 단일 인스턴스 확인
    if not _acquire_lock():
        logger.error("이미 실행 중입니다. .rion_bot.lock 파일 확인하세요.")
        sys.exit(1)

    main_loop(dry_run=args.dry_run)


if __name__ == "__main__":
    main()
