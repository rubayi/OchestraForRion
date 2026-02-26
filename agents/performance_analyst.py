"""
performance_analyst.py — Agent 4: 성과 분석가

역할: trades.db 통계 → Claude Haiku 4.5 분석 → Telegram 리포트 전송
모델: claude-haiku-4-5-20251001 (빠름, 저렴 ~$0.001/회)
"""
import os
import logging
import requests
import anthropic

from bridge.trade_db_reader import TradeDBReader, TradeReport, PatternStats

logger = logging.getLogger(__name__)

HAIKU_MODEL = "claude-haiku-4-5-20251001"

SYSTEM_PROMPT = """당신은 OchestraForRion 프로젝트의 Agent 4 (성과 분석가)입니다.
RionFX GBPAUD 트레이딩 봇의 거래 통계를 분석하여, 대표님(Ruba)께 Telegram 일일 보고서를 작성합니다.

보고서 작성 규칙:
1. 첫 줄: "📊 [AlohaCTO 일일 성과 보고] YYYY-MM-DD"
2. 전체 요약: 청산 건수, 승률, 총 손익
3. 패턴별 분석 (패턴마다 한 섹션):
   이모지 + 패턴명 + 건수/승률/평균pips + 한 줄 평가
   - ✅ 계속 유지 / ⚠️ 개선 권장 / 🔴 즉시 비활성화 권장
4. 청산 사유 분석 (MANUAL 비중 높으면 반드시 경고)
5. 연속 손실 3회 이상이면 ⚠️ 경고 및 대응 제안
6. 💡 구체적 개선 제안 1~3개 (params.json 수정 예시 포함)
7. 마지막: 종합 평가 한 문장 + "대표님의 현명한 판단을 기다립니다."

언어 및 형식:
- 반드시 존댓말(격식체)로 작성하세요. 대표님께 보고하는 형식입니다.
- 이모지를 적극 활용하되 내용은 빠짐없이 작성하세요.
- 숫자는 소수점 1자리로 통일하세요.
- 내용을 임의로 생략하거나 "..." 으로 줄이지 마세요.
- 한국어로만 작성하세요."""


def _build_stats_summary(report: TradeReport) -> str:
    """TradeReport → AI 입력용 통계 요약 텍스트"""
    lines = [
        f"분석 기간: 최근 {report.period_days}일 ({report.generated_at} 기준)",
        f"전체 거래: {report.total_trades}건 | 청산: {report.total_closed}건",
        f"전체 승률: {report.overall_win_rate}% ({report.total_wins}승 {report.total_losses}패)",
        f"총 수익: {report.total_profit_pips:+.1f} pips",
        f"최근 연속 손실: {report.recent_consecutive_losses}회",
        "",
        "패턴별 상세 통계:",
    ]

    for p in report.pattern_stats:
        lines.append(
            f"  [{p.pattern}] {p.total}건 | "
            f"승률={p.win_rate}% | "
            f"평균={p.avg_profit_pips:+.1f}pips | "
            f"이길때={p.avg_win_pips:+.1f} / 질때={p.avg_loss_pips:+.1f} | "
            f"RR={p.avg_rr} | "
            f"권장={p.recommendation}"
        )

    if report.exit_reason_counts:
        lines.append("")
        lines.append("청산 사유:")
        for reason, cnt in report.exit_reason_counts.items():
            pct = round(cnt / report.total_closed * 100, 1) if report.total_closed > 0 else 0
            lines.append(f"  {reason}: {cnt}건 ({pct}%)")

    return "\n".join(lines)


def analyze(report: TradeReport) -> str:
    """
    Claude Haiku 4.5로 성과 분석 → Telegram 리포트 문자열 반환

    Returns:
        분석 결과 문자열 (Telegram 전송용)
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY 환경변수가 설정되지 않았습니다.")

    client = anthropic.Anthropic(api_key=api_key)
    stats_text = _build_stats_summary(report)

    logger.info(f"[Agent4] Haiku 분석 요청 — 패턴 {len(report.pattern_stats)}개")

    response = client.messages.create(
        model=HAIKU_MODEL,
        max_tokens=2500,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": (
                    f"다음 거래 통계를 분석하고 Telegram 리포트를 작성해주세요.\n\n"
                    f"{stats_text}"
                ),
            }
        ],
    )

    result = response.content[0].text
    logger.info(
        f"[Agent4] 분석 완료 — 입력 {response.usage.input_tokens}토큰, "
        f"출력 {response.usage.output_tokens}토큰"
    )
    return result


def send_telegram(message: str) -> bool:
    """
    Telegram으로 메시지 전송

    Returns:
        True if sent successfully, False otherwise
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        logger.warning("[Agent4] TELEGRAM_BOT_TOKEN 또는 TELEGRAM_CHAT_ID 미설정 — 콘솔 출력")
        print("\n" + "=" * 60)
        print(message)
        print("=" * 60 + "\n")
        return False

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    # Telegram 메시지 최대 4096자 — 초과 시 분할 전송
    chunks = [message[i:i+4000] for i in range(0, len(message), 4000)]
    last_message_id = None
    try:
        for chunk in chunks:
            resp = requests.post(
                url,
                json={"chat_id": chat_id, "text": chunk},
                timeout=15,
            )
            if not resp.ok:
                logger.error(f"[Agent4] Telegram 전송 실패: {resp.status_code} {resp.text}")
                return False
            last_message_id = resp.json().get("result", {}).get("message_id")
        logger.info(f"[Agent4] Telegram 전송 성공 ({len(chunks)}개 메시지)")

        # 마지막 리포트 message_id 저장 → control_bot이 Reply 감지에 사용
        if last_message_id:
            import json as _json
            from datetime import datetime as _dt
            _state_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "last_report.json")
            with open(_state_path, "w", encoding="utf-8") as _f:
                _json.dump({"message_id": last_message_id, "sent_at": _dt.now().isoformat()}, _f)
        return True
    except requests.RequestException as e:
        logger.error(f"[Agent4] Telegram 요청 오류: {e}")
        return False


FEEDBACK_SYSTEM_PROMPT = """당신은 OchestraForRion 프로젝트의 Agent 4 (성과 분석가)입니다.
대표님(Ruba)께서 방금 받으신 성과 보고서에 피드백을 주셨습니다.
거래 통계와 피드백을 참고하여 대표님의 질문/의견에 성실하게 답변해주세요.

응답 규칙:
- 반드시 존댓말(격식체)로 작성하세요.
- 피드백의 핵심을 파악하고 구체적으로 답변하세요.
- 필요하면 통계 수치를 직접 인용하세요.
- 한국어로만 작성하세요.
- 답변은 500자 이내로 간결하게 작성하세요."""


def analyze_feedback(report: TradeReport, feedback_text: str) -> str:
    """
    보고서에 대한 대표님 피드백 → Haiku 응답 생성

    Args:
        report: 현재 거래 통계 (컨텍스트용)
        feedback_text: 대표님이 보낸 피드백 텍스트

    Returns:
        응답 문자열 (Telegram 전송용)
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY 환경변수가 설정되지 않았습니다.")

    client = anthropic.Anthropic(api_key=api_key)
    stats_text = _build_stats_summary(report)

    logger.info(f"[Agent4] 피드백 응답 요청: {feedback_text[:50]}...")

    response = client.messages.create(
        model=HAIKU_MODEL,
        max_tokens=800,
        system=FEEDBACK_SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": (
                    f"[현재 30일 거래 통계]\n{stats_text}\n\n"
                    f"[대표님 피드백]\n{feedback_text}"
                ),
            }
        ],
    )

    result = response.content[0].text
    logger.info(f"[Agent4] 피드백 응답 완료 — {response.usage.output_tokens}토큰")
    return f"💬 AlohaCTO:\n\n{result}"


def run(db_path: str, days: int = 30) -> bool:
    """
    Agent 4 전체 파이프라인 실행:
    trades.db 읽기 → Haiku 분석 → Telegram 전송

    Args:
        db_path: AlgoTradingBot의 data/trades.db 경로
        days: 분석할 최근 일수

    Returns:
        True if Telegram sent successfully
    """
    logger.info(f"[Agent4] 시작 — db={db_path}, 기간={days}일")

    # 1. DB 읽기
    reader = TradeDBReader(db_path)
    report = reader.get_stats(days=days)
    logger.info(
        f"[Agent4] 데이터 로드 완료 — "
        f"청산 {report.total_closed}건 | 승률 {report.overall_win_rate}%"
    )

    if report.total_closed == 0:
        msg = f"📊 [RionAgent] 최근 {days}일 거래 없음 — 리포트 생략"
        logger.info(msg)
        send_telegram(msg)
        return True

    # 2. Haiku 분석
    analysis = analyze(report)

    # 3. Telegram 전송
    return send_telegram(analysis)
