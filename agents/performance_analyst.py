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

SYSTEM_PROMPT = """당신은 RionFX GBPAUD 트레이딩 봇의 전문 성과 분석가입니다.
거래 통계를 분석해 트레이더에게 Telegram 리포트를 작성합니다.

리포트 작성 규칙:
1. 첫 줄: "📊 [RionAgent 일일 성과 리포트] YYYY-MM-DD"
2. 패턴별 한 줄씩: 이모지 + 패턴명 + 핵심 지표 + 한 줄 평가
   - ✅ 계속 사용 / ⚠️ 개선 필요 / 🔴 비활성화 권장
3. 청산 사유 분석 (MANUAL 비중이 높으면 경고)
4. 연속 손실이 3회 이상이면 경고
5. 💡 구체적 params.json 개선안 1~3개
   - 예시: "bamboo_min_candles 3→5", "stop_loss_pips 18→15"
6. 마지막 줄: 한 문장 종합 평가

출력은 Telegram 메시지 형식 (이모지 사용, 500자 이내로 간결하게).
숫자는 반드시 소수점 1자리로 통일.
한국어로만 작성."""


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
        max_tokens=800,
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
    try:
        resp = requests.post(
            url,
            json={"chat_id": chat_id, "text": message},
            timeout=15,
        )
        if resp.ok:
            logger.info("[Agent4] Telegram 전송 성공")
            return True
        else:
            logger.error(f"[Agent4] Telegram 전송 실패: {resp.status_code} {resp.text}")
            return False
    except requests.RequestException as e:
        logger.error(f"[Agent4] Telegram 요청 오류: {e}")
        return False


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
