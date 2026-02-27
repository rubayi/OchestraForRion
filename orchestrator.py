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
from datetime import datetime

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
PARAMS_PATH      = os.path.join(ALGOTRADINGBOT_DIR, "params.json")
JSON_PATH        = os.path.join(ALGOTRADINGBOT_DIR, "rion_data", "rion_data_now.json")
SIGNAL_OUT_PATH  = os.path.join(os.path.dirname(__file__), "signal.json")
CONTROL_BOT_PATH = os.path.join(ALGOTRADINGBOT_DIR, "rion_control_bot.py")
LAST_REPORT_FILE    = os.path.join(os.path.dirname(__file__), "last_report.json")
PENDING_PARAMS_FILE = os.path.join(os.path.dirname(__file__), "pending_params.json")

# ── TastyFX 브로커 설정 ────────────────────────────────────────────────────────
TASTYFX_ALGOTRADINGDIR = os.environ.get(
    "TASTYFX_ALGOTRADINGDIR",
    r"C:\Users\rubay\Documents\projects\AlgoTradingBot_tasty",
)
TASTYFX_DB_PATH   = os.environ.get(
    "TASTYFX_DB_PATH",
    r"C:\Users\rubay\Documents\projects\AlgoTradingBot_tasty\data\trades.db",
)
TASTYFX_BOT_TOKEN    = os.environ.get("TASTYFX_BOT_TOKEN", "")
TASTYFX_CHAT_ID      = os.environ.get("TASTYFX_CHAT_ID", "")
TASTYFX_TELEGRAM_BASE = f"https://api.telegram.org/bot{TASTYFX_BOT_TOKEN}" if TASTYFX_BOT_TOKEN else ""
TASTYFX_CONTROL_BOT  = os.path.join(TASTYFX_ALGOTRADINGDIR, "rion_control_bot.py")
TASTYFX_PENDING_CLOSE = os.path.join(TASTYFX_ALGOTRADINGDIR, "rion_data", "pending_close.json")

# 청산 확인 설정
TASTYFX_CLOSE_RETRY_INTERVAL = 180   # 3분 간격 재요청
TASTYFX_CLOSE_MAX_RETRIES    = 3     # 최대 3회 재시도 후 자동청산

# ── 손실 모니터 설정 ───────────────────────────────────────────────────────────
LOSS_WARN_PIPS        = float(os.environ.get("LOSS_WARN_PIPS",       "-5.0"))  # 1차 경고
LOSS_URGENT_PIPS      = float(os.environ.get("LOSS_URGENT_PIPS",    "-10.0"))  # 긴급 경고
LOSS_ESCALATION_PIPS  = float(os.environ.get("LOSS_ESCALATION_PIPS",  "3.0"))  # 60초 내 악화 기준
LOSS_DATA_MAX_AGE_SEC = 300  # rion_data_now.json 최대 허용 오래됨 (5분)

# ── Telegram 설정 ─────────────────────────────────────────────────────────────
BOT_TOKEN     = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID       = os.environ.get("TELEGRAM_CHAT_ID", "")
TELEGRAM_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"

# ── Telegram 유틸 ─────────────────────────────────────────────────────────────

# ── TastyFX Telegram 헬퍼 ─────────────────────────────────────────────────────

def _tg_tastyfx_send(text: str) -> None:
    """TastyFX 봇(@TastyFxofRubaBot)으로 메시지 전송"""
    if not TASTYFX_BOT_TOKEN or not TASTYFX_CHAT_ID:
        return
    url = f"{TASTYFX_TELEGRAM_BASE}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={"chat_id": TASTYFX_CHAT_ID, "text": text},
            timeout=10,
        )
        if not resp.ok:
            logger.error(f"[TastyFX TG] 전송 실패: {resp.status_code}")
    except Exception as e:
        logger.error(f"[TastyFX TG] 전송 오류: {e}")


def _tg_tastyfx_get_updates(offset=None):
    """TastyFX 봇 업데이트 수신"""
    if not TASTYFX_BOT_TOKEN:
        return None
    try:
        resp = requests.get(
            f"{TASTYFX_TELEGRAM_BASE}/getUpdates",
            params={"timeout": 5, "offset": offset},
            timeout=10,
        )
        return resp.json()
    except Exception as e:
        logger.error(f"[TastyFX TG] getUpdates 오류: {e}")
        return None


def _run_tastyfx_close(ticket: int) -> str:
    """TastyFX rion_control_bot.py --cmd close --ticket XXXXX 실행"""
    try:
        result = subprocess.run(
            [sys.executable, TASTYFX_CONTROL_BOT, "--cmd", "close", "--ticket", str(ticket)],
            capture_output=True, text=True, timeout=30, encoding="utf-8",
        )
        output = (result.stdout + result.stderr).strip()
        return output if output else f"(exit {result.returncode})"
    except subprocess.TimeoutExpired:
        return "❌ 청산 명령 타임아웃 (30초)"
    except Exception as e:
        return f"❌ 청산 실패: {e}"


def _check_tastyfx_pending_close() -> None:
    """pending_close.json 확인 → 재시도 / 자동청산 처리

    - retry_count < MAX_RETRIES: 3분 간격으로 재요청
    - retry_count >= MAX_RETRIES: 자동청산 실행
    - status != 'pending': 처리 완료(무시)
    """
    if not os.path.exists(TASTYFX_PENDING_CLOSE):
        return

    try:
        with open(TASTYFX_PENDING_CLOSE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return

    if data.get("status") != "pending":
        return

    ticket      = data.get("ticket")
    retry_count = data.get("retry_count", 0)
    last_sent   = data.get("last_sent_at", "")
    profit_pips = data.get("profit_pips", 0.0)
    confidence  = data.get("confidence", 0)
    reason      = data.get("reason", "")

    # 마지막 전송 이후 경과 시간 계산
    try:
        last_dt = datetime.fromisoformat(last_sent)
        elapsed = (datetime.now() - last_dt).total_seconds()
    except Exception:
        elapsed = TASTYFX_CLOSE_RETRY_INTERVAL + 1  # 파싱 실패 시 즉시 처리

    if elapsed < TASTYFX_CLOSE_RETRY_INTERVAL:
        return  # 아직 3분 안 됨

    if retry_count >= TASTYFX_CLOSE_MAX_RETRIES:
        # 자동 청산
        logger.warning(f"[TastyFX] 응답 없음 {TASTYFX_CLOSE_MAX_RETRIES}회 → 자동 청산: Ticket {ticket}")
        result = _run_tastyfx_close(ticket)
        _tg_tastyfx_send(
            f"⏱️ 응답 없어 자동 청산 실행: Ticket {ticket}\n결과: {result}"
        )
        # pending_close.json 삭제
        try:
            os.remove(TASTYFX_PENDING_CLOSE)
        except Exception:
            pass
    else:
        # 재요청
        retry_count += 1
        pnl_str = f"{profit_pips:+.1f}"
        _tg_tastyfx_send(
            f"🔔 청산 재확인 요청 ({retry_count}/{TASTYFX_CLOSE_MAX_RETRIES})\n"
            f"Ticket: {ticket}\n"
            f"현재 손익: {pnl_str} pips (AI {confidence}%)\n"
            f"사유: {reason}\n\n"
            f"청산하시겠습니까?\n"
            f"'예' → 즉시 청산  |  '아니오' → 유지\n"
            f"({TASTYFX_CLOSE_MAX_RETRIES - retry_count}회 후 무응답 시 자동청산)"
        )
        data["retry_count"]  = retry_count
        data["last_sent_at"] = datetime.now().isoformat()
        try:
            with open(TASTYFX_PENDING_CLOSE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[TastyFX] pending_close.json 업데이트 실패: {e}")


def run_tastyfx_telegram_daemon() -> None:
    """TastyFX 봇(@TastyFxofRubaBot) 폴링 데몬

    '예' → pending_close.json 의 ticket 청산
    '아니오' → pending_close.json 삭제 (유지)
    30초마다 _check_tastyfx_pending_close() 호출 (재시도/자동청산)
    """
    if not TASTYFX_BOT_TOKEN or not TASTYFX_CHAT_ID:
        logger.warning("[TastyFX TG] BOT_TOKEN 또는 CHAT_ID 미설정 — TastyFX 폴링 비활성화")
        return

    logger.info("[TastyFX TG] 폴링 데몬 시작")
    offset      = None
    retry_delay = 5
    last_check  = 0.0  # _check_tastyfx_pending_close() 마지막 호출 시각

    while True:
        try:
            # 30초마다 retry/auto-close 체크
            now = time.time()
            if now - last_check >= 30:
                _check_tastyfx_pending_close()
                last_check = now

            updates = _tg_tastyfx_get_updates(offset)
            if not updates or not updates.get("ok"):
                time.sleep(retry_delay)
                retry_delay = min(retry_delay * 2, 60)
                continue

            retry_delay = 5

            for update in updates.get("result", []):
                offset  = update["update_id"] + 1
                msg     = update.get("message", {})
                chat_id = str(msg.get("chat", {}).get("id", ""))
                text    = msg.get("text", "").strip()

                if chat_id != str(TASTYFX_CHAT_ID) or not text:
                    continue

                logger.info(f"[TastyFX TG] 수신: {text[:80]}")

                # pending_close.json 없으면 일반 메시지 무시
                if not os.path.exists(TASTYFX_PENDING_CLOSE):
                    continue

                try:
                    with open(TASTYFX_PENDING_CLOSE, "r", encoding="utf-8") as f:
                        pending = json.load(f)
                except Exception:
                    continue

                if pending.get("status") != "pending":
                    continue

                ticket = pending.get("ticket")

                if text in ("예", "네", "yes", "YES", "Yes"):
                    _tg_tastyfx_send(f"⏳ Ticket {ticket} 청산 중...")
                    result = _run_tastyfx_close(ticket)
                    _tg_tastyfx_send(f"✅ 청산 완료: Ticket {ticket}\n{result}")
                    try:
                        os.remove(TASTYFX_PENDING_CLOSE)
                    except Exception:
                        pass

                elif text in ("아니오", "아니", "no", "NO", "No"):
                    _tg_tastyfx_send(f"↩️ Ticket {ticket} 청산 취소 — 포지션 유지")
                    try:
                        os.remove(TASTYFX_PENDING_CLOSE)
                    except Exception:
                        pass

        except Exception as e:
            logger.error(f"[TastyFX TG] 폴링 루프 오류: {e}")
            time.sleep(10)


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


# ── 손실 확대 모니터 ──────────────────────────────────────────────────────────

_loss_pnl_history: dict = {}   # {ticket: [pnl, pnl, ...]}  최근 2개만 유지
_loss_alert_sent:  dict = {}   # {ticket: {"warn": bool, "urgent": bool}}


def _check_loss_escalation() -> None:
    """rion_data_now.json 포지션 PnL → 손실 확대 감지 → Telegram 알림"""
    if not os.path.exists(JSON_PATH):
        return

    # 데이터 신선도 확인
    age = time.time() - os.path.getmtime(JSON_PATH)
    if age > LOSS_DATA_MAX_AGE_SEC:
        return  # 5분 이상 오래된 데이터는 무시

    try:
        with open(JSON_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return

    positions = data.get("positions", [])

    # 포지션 없으면 히스토리·알림 상태 초기화
    if not positions:
        _loss_pnl_history.clear()
        _loss_alert_sent.clear()
        return

    active_tickets = set()

    for pos in positions:
        ticket  = str(pos.get("ticket", ""))
        pnl     = float(pos.get("pnl_pips", 0))
        entry   = pos.get("entry", 0)
        current = pos.get("current", 0)

        if not ticket:
            continue

        active_tickets.add(ticket)

        # 히스토리 초기화
        if ticket not in _loss_pnl_history:
            _loss_pnl_history[ticket] = []
            _loss_alert_sent[ticket]  = {"warn": False, "urgent": False}

        history = _loss_pnl_history[ticket]
        prev_pnl = history[-1] if history else None
        history.append(pnl)
        if len(history) > 3:
            history.pop(0)

        state  = _loss_alert_sent[ticket]
        alerts = []

        # ① 1차 경고: 처음 LOSS_WARN_PIPS 돌파
        if pnl <= LOSS_WARN_PIPS and not state["warn"]:
            alerts.append(f"⚠️ 손실 {pnl:+.1f} pips")
            state["warn"] = True

        # ② 긴급 경고: 처음 LOSS_URGENT_PIPS 돌파
        if pnl <= LOSS_URGENT_PIPS and not state["urgent"]:
            alerts.append(f"🚨 손실 {pnl:+.1f} pips — 즉시 확인 필요")
            state["urgent"] = True

        # ③ 손실 급확대: 60초 내 LOSS_ESCALATION_PIPS 이상 악화
        if prev_pnl is not None and pnl < 0:
            deterioration = prev_pnl - pnl  # 양수 = 더 악화됨
            if deterioration >= LOSS_ESCALATION_PIPS:
                alerts.append(
                    f"📉 급확대: {prev_pnl:+.1f} → {pnl:+.1f} pips "
                    f"({deterioration:.1f}pips 악화 / 60초)"
                )

        if alerts:
            symbol = data.get("symbol", "GBPAUD")
            msg = (
                f"{'🚨' if pnl <= LOSS_URGENT_PIPS else '⚠️'} [손실 알림] {symbol} SELL\n"
                f"티켓: {ticket}\n"
                f"진입가: {entry:.5f} | 현재가: {current:.5f}\n"
                f"현재 손익: {pnl:+.1f} pips\n\n"
                + "\n".join(alerts)
            )
            logger.warning(f"[LossMonitor] 알림 발송 — 티켓 {ticket}, {pnl:+.1f} pips")
            _tg_send(msg)

    # 청산된 포지션 상태 정리
    for closed_ticket in list(_loss_pnl_history.keys()):
        if closed_ticket not in active_tickets:
            _loss_pnl_history.pop(closed_ticket, None)
            _loss_alert_sent.pop(closed_ticket, None)


def run_loss_monitor() -> None:
    """손실 확대 모니터 스레드 — 60초마다 포지션 PnL 감시"""
    logger.info(
        f"[LossMonitor] 시작 — "
        f"경고={LOSS_WARN_PIPS}pips / 긴급={LOSS_URGENT_PIPS}pips / "
        f"급확대={LOSS_ESCALATION_PIPS}pips/60초"
    )
    while True:
        _check_loss_escalation()
        time.sleep(60)


# ── Agent 4 실행 ──────────────────────────────────────────────────────────────

def run_agent4(days: int = 30) -> None:
    """Agent 4 (성과 분석) 실행 — ICMarkets + TastyFX 각각 분석"""
    from agents.performance_analyst import run as agent4_run

    brokers = [
        {
            "label":     "ICMarkets",
            "db_path":   DB_PATH,
            "bot_token": None,          # 환경변수 TELEGRAM_BOT_TOKEN 사용
            "chat_id":   None,          # 환경변수 TELEGRAM_CHAT_ID 사용
        },
        {
            "label":     "TastyFX",
            "db_path":   TASTYFX_DB_PATH,
            "bot_token": TASTYFX_BOT_TOKEN or None,
            "chat_id":   TASTYFX_CHAT_ID or None,
        },
    ]

    for broker in brokers:
        label = broker["label"]
        db    = broker["db_path"]

        logger.info("=" * 50)
        logger.info(f"[Orchestrator] Agent 4 실행 시작 [{label}] (최근 {days}일)")

        # TastyFX DB가 아직 없으면 건너뜀
        if not os.path.exists(db):
            logger.info(f"[Orchestrator] [{label}] DB 없음 — 스킵: {db}")
            continue

        try:
            success = agent4_run(
                db_path=db,
                days=days,
                bot_token=broker["bot_token"],
                chat_id=broker["chat_id"],
                broker_label=label,
            )
            if success:
                logger.info(f"[Orchestrator] Agent 4 [{label}] 완료 ✅")
            else:
                logger.warning(f"[Orchestrator] Agent 4 [{label}] 완료 (Telegram 전송 실패)")
        except FileNotFoundError as e:
            logger.error(f"[Orchestrator] [{label}] DB 파일 없음: {e}")
        except Exception as e:
            logger.error(f"[Orchestrator] Agent 4 [{label}] 오류: {e}", exc_info=True)

    logger.info("=" * 50)

    # Agent 4 완료 후 Agent 5 트리거 (ICMarkets만, 충분한 데이터 있을 때)
    if os.path.exists(DB_PATH):
        run_agent5(db_path=DB_PATH, days=days)


# ── Agent 5 실행 ──────────────────────────────────────────────────────────────

def run_agent5(db_path: str = None, days: int = 30) -> None:
    """Agent 5 (자율 개발자) 실행 — 파라미터 튜닝 제안 생성 + Telegram 전송"""
    from agents.developer_agent import analyze as agent5_analyze
    from agents.developer_agent import save_pending, format_proposal_message, load_pending

    db_path = db_path or DB_PATH

    # 이미 pending 제안이 있으면 새 분석 스킵
    existing = load_pending(PENDING_PARAMS_FILE)
    if existing:
        logger.info("[Agent5] 이미 pending 제안이 존재합니다 — 새 분석 스킵")
        return

    logger.info("[Orchestrator] Agent 5 (파라미터 튜닝 분석) 시작")
    try:
        proposal = agent5_analyze(
            db_path=db_path,
            params_path=PARAMS_PATH,
            days=days,
        )
    except Exception as e:
        logger.error(f"[Orchestrator] Agent 5 오류: {e}", exc_info=True)
        return

    if proposal is None:
        logger.warning("[Agent5] 분석 실패 (None 반환)")
        return

    changes = proposal.get("changes", [])
    if not changes:
        logger.info(f"[Agent5] 변경 제안 없음 — {proposal.get('summary', '')}")
        _tg_send(
            f"🔧 *Agent 5 분석 완료*\n"
            f"📋 {proposal.get('summary', '현재 파라미터 최적')}\n"
            f"_(변경 제안 없음)_"
        )
        return

    # 제안 저장 + Telegram 전송
    save_pending(proposal, PENDING_PARAMS_FILE)
    msg = format_proposal_message(proposal)
    _tg_send(msg)
    logger.info(f"[Agent5] 제안 {len(changes)}건 Telegram 전송 완료")


# ── Agent 5 승인/거절 핸들러 ──────────────────────────────────────────────────

def _handle_approve_params() -> None:
    """Telegram /approve_params → pending_params 적용 + git commit"""
    from agents.developer_agent import load_pending, apply_changes

    pending = load_pending(PENDING_PARAMS_FILE)
    if not pending:
        _tg_send("⚠️ 승인할 파라미터 제안이 없습니다 (없거나 만료됨)")
        return

    changes = pending.get("changes", [])
    if not changes:
        _tg_send("⚠️ 변경 내용이 없는 제안입니다")
        return

    _tg_send("⏳ 파라미터 적용 중...")

    success, detail = apply_changes(pending, PARAMS_PATH, ALGOTRADINGBOT_DIR)

    # pending 상태 업데이트
    try:
        pending["status"] = "applied" if success else "failed"
        with open(PENDING_PARAMS_FILE, "w", encoding="utf-8") as f:
            json.dump(pending, f, ensure_ascii=False, indent=2)
    except Exception:
        pass

    if success:
        _tg_send(
            f"✅ *파라미터 적용 완료*\n\n"
            f"{detail}\n\n"
            f"📌 AlgoTradingBot을 재시작해야 적용됩니다.\n"
            f"봇 재시작: /restart"
        )
        logger.info(f"[Agent5] 파라미터 적용 완료:\n{detail}")
    else:
        _tg_send(f"❌ *적용 실패*: {detail}")
        logger.error(f"[Agent5] 파라미터 적용 실패: {detail}")


def _handle_reject_params() -> None:
    """Telegram /reject_params → pending_params 폐기"""
    try:
        if not os.path.exists(PENDING_PARAMS_FILE):
            _tg_send("⚠️ 거절할 파라미터 제안이 없습니다")
            return
        with open(PENDING_PARAMS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("status") != "pending":
            _tg_send("⚠️ 이미 처리된 제안입니다")
            return
        data["status"] = "rejected"
        with open(PENDING_PARAMS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        _tg_send("🚫 파라미터 제안이 거절되었습니다. 현재 설정을 유지합니다.")
        logger.info("[Agent5] 파라미터 제안 거절됨")
    except Exception as e:
        logger.error(f"[Agent5] 거절 처리 오류: {e}")
        _tg_send(f"❌ 거절 처리 중 오류: {e}")


# ── Agent 1→2→3 파이프라인 실행 ──────────────────────────────────────────────

def run_signal_pipeline() -> None:
    """Agent 1 (Haiku 필터) → Agent 2 (Opus 결정) → Agent 3 (Haiku 리스크) 파이프라인 실행
    결과를 Telegram으로 전송."""
    import json as _json
    from pathlib import Path

    logger.info("=" * 50)
    logger.info("[Orchestrator] 시그널 파이프라인 실행 시작")

    # rion_data_now.json 존재 여부 확인
    if not os.path.exists(JSON_PATH):
        msg = f"❌ rion_data_now.json 없음\n경로: {JSON_PATH}\n\n봇이 실행 중인지 확인하세요."
        logger.error(f"[Orchestrator] {msg}")
        _tg_send(msg)
        return

    # run_signal.py 경로
    run_signal_py = os.path.join(os.path.dirname(__file__), "run_signal.py")
    if not os.path.exists(run_signal_py):
        _tg_send(f"❌ run_signal.py를 찾을 수 없습니다:\n{run_signal_py}")
        return

    try:
        result = subprocess.run(
            [
                sys.executable, run_signal_py,
                "--input",  JSON_PATH,
                "--output", SIGNAL_OUT_PATH,
                "--db",     DB_PATH,
                "--params", PARAMS_PATH,
            ],
            capture_output=True,
            text=True,
            timeout=120,
            cwd=os.path.dirname(__file__),
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        _tg_send("⏱️ 시그널 파이프라인 타임아웃 (120초)")
        logger.error("[Orchestrator] 시그널 파이프라인 타임아웃")
        return
    except Exception as e:
        _tg_send(f"❌ 파이프라인 실행 오류: {e}")
        logger.error(f"[Orchestrator] 파이프라인 오류: {e}")
        return

    if result.returncode != 0:
        err = (result.stderr or result.stdout or "알 수 없는 오류")[:500]
        _tg_send(f"❌ 파이프라인 오류 (코드 {result.returncode})\n{err}")
        logger.error(f"[Orchestrator] 파이프라인 종료코드 {result.returncode}: {err}")
        return

    # signal.json 읽기
    try:
        with open(SIGNAL_OUT_PATH, "r", encoding="utf-8") as f:
            sig = _json.load(f)
    except Exception as e:
        _tg_send(f"❌ signal.json 읽기 실패: {e}")
        logger.error(f"[Orchestrator] signal.json 읽기 실패: {e}")
        return

    # Telegram 보고서 작성
    a1 = sig.get("agent1") or {}
    a2 = sig.get("agent2") or {}
    a3 = sig.get("agent3") or {}
    final = sig.get("final_decision", "SKIP")
    ts = sig.get("timestamp", "")

    if final == "ENTER":
        icon = "🟢"
    else:
        icon = "🔴"

    lines = [
        f"{icon} *RionAgent 시그널* — {ts}",
        "",
        f"*Agent 1* (필터): pass={a1.get('pass')} | strength={a1.get('strength')}/100",
        f"  {a1.get('context', '')}",
    ]

    if a2:
        lines += [
            "",
            f"*Agent 2* (결정): {a2.get('decision')} | confidence={a2.get('confidence')}%",
            f"  {a2.get('reason', '')}",
        ]
    else:
        lines.append("\nAgent 2: 미호출 (Agent 1 필터 미통과)")

    if a3:
        mode_icon = "⚠️ 보수적" if a3.get("mode") == "conservative" else "✅ 정상"
        lines += [
            "",
            f"*Agent 3* (리스크): {mode_icon}",
            f"  SL={a3.get('sl_pips')}p | TP={a3.get('tp_pips')}p | "
            f"랏={a3.get('lot_size')} | RR={a3.get('rr_ratio')}",
            f"  {a3.get('reason', '')}",
        ]

    lines += [
        "",
        f"━━━ *최종: {final}* ━━━",
    ]

    _tg_send("\n".join(lines))
    logger.info(f"[Orchestrator] 파이프라인 완료 — final_decision={final}")
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
            max_tokens=2000,
            system=(
                "당신은 AlohaCTO입니다. RionFX GBPAUD 트레이딩봇의 성과 분석을 담당하며 "
                "대표님(Ruba)의 질문에 존댓말(격식체)로 성실하게 답변합니다. "
                "내용을 임의로 생략하거나 '...'으로 줄이지 마세요."
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
    if kwargs.get("date"):
        args += ["--date", str(kwargs["date"])]

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
    "/signal        — Agent1→2→3 시그널 파이프라인 즉시 실행\n"
    "/report        — AlohaCTO 성과 분석 즉시 실행\n"
    "/tune          — Agent5 파라미터 튜닝 분석 즉시 실행\n"
    "/approve_params — Agent5 파라미터 제안 승인 + 적용\n"
    "/reject_params  — Agent5 파라미터 제안 거절\n"
    "/status        — 봇 실행 상태\n"
    "/log           — 최근 로그 30줄\n"
    "/positions     — 포지션 정보\n"
    "/params        — 현재 파라미터\n"
    "/history       — 오늘 거래 내역\n"
    "/history YYYYMMDD — 특정일 거래 내역\n"
    "/restart       — 봇 재시작\n"
    "/help          — 이 도움말\n\n"
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

                if cmd == "/signal":
                    _tg_send("🔍 시그널 파이프라인 실행 중... (Agent1→2→3, 60초 내외 소요)")
                    threading.Thread(target=run_signal_pipeline, daemon=True).start()

                elif cmd == "/report":
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

                elif cmd.startswith("/history"):
                    parts = text.split()
                    date_arg = parts[1] if len(parts) > 1 else None
                    _tg_send("📊 거래 내역 조회 중...")
                    threading.Thread(
                        target=lambda d=date_arg: _tg_send(
                            _run_mt5_command("history", date=d)
                        ),
                        daemon=True,
                    ).start()

                elif cmd == "/tune":
                    _tg_send("🔧 Agent 5 파라미터 튜닝 분석 시작...")
                    threading.Thread(
                        target=run_agent5, daemon=True
                    ).start()

                elif cmd == "/approve_params":
                    threading.Thread(
                        target=_handle_approve_params, daemon=True
                    ).start()

                elif cmd == "/reject_params":
                    threading.Thread(
                        target=_handle_reject_params, daemon=True
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

    # 손실 확대 모니터 스레드 시작
    threading.Thread(target=run_loss_monitor, daemon=True, name="loss_monitor").start()

    # TastyFX 청산 확인 폴링 데몬 스레드 시작
    threading.Thread(target=run_tastyfx_telegram_daemon, daemon=True, name="tastyfx_close_daemon").start()
    logger.info(f"  TastyFX 청산 확인 폴링: {'활성화' if TASTYFX_BOT_TOKEN and TASTYFX_CHAT_ID else '비활성화 (토큰 없음)'}")

    # Telegram 폴링을 메인 스레드에서 실행
    run_telegram_daemon()


if __name__ == "__main__":
    main()
