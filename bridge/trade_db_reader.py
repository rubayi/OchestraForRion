"""
trade_db_reader.py — AlgoTradingBot trades.db 읽기 + 패턴별 통계 계산

trades.db 스키마 주요 컬럼:
  pattern_type TEXT  — "bamboo", "manual", "ma_convergence", "ma_box" 등
  result TEXT        — "WIN" | "LOSS"
  status TEXT        — "CLOSED" | "OPEN"
  profit_pips REAL
  actual_rr REAL
  timestamp_close TEXT
  exit_reason TEXT   — "SL", "MANUAL", "REVERSAL", "PARTIAL_CLOSE", "LOG_SYNC"
"""
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional


@dataclass
class PatternStats:
    pattern: str
    total: int
    wins: int
    losses: int
    win_rate: float          # 0~100
    avg_profit_pips: float
    avg_win_pips: float      # 이긴 거래 평균
    avg_loss_pips: float     # 진 거래 평균 (음수)
    avg_rr: float
    recommendation: str      # "continue" | "review" | "disable"

    def emoji(self) -> str:
        if self.recommendation == "disable":
            return "🔴"
        if self.recommendation == "review":
            return "⚠️"
        return "✅"


@dataclass
class TradeReport:
    generated_at: str
    period_days: int
    total_trades: int          # OPEN 포함 전체
    total_closed: int          # CLOSED만
    total_wins: int
    total_losses: int
    overall_win_rate: float    # 0~100
    total_profit_pips: float
    pattern_stats: list        # List[PatternStats]
    recent_consecutive_losses: int
    exit_reason_counts: dict   # {"SL": 10, "MANUAL": 5, ...}
    best_pattern: Optional[str]
    worst_pattern: Optional[str]


class TradeDBReader:
    def __init__(self, db_path: str):
        if not os.path.exists(db_path):
            raise FileNotFoundError(f"trades.db를 찾을 수 없습니다: {db_path}")
        self.db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def get_stats(self, days: int = 30) -> TradeReport:
        """최근 N일 거래 통계를 계산해 TradeReport로 반환"""
        since = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")

        conn = self._connect()
        try:
            # ── 전체 거래 수 ───────────────────────────────────────────────
            total = conn.execute(
                "SELECT COUNT(*) FROM trades WHERE created_at >= ?", (since,)
            ).fetchone()[0]

            # ── CLOSED 거래 집계 ──────────────────────────────────────────
            closed_row = conn.execute(
                """SELECT
                    COUNT(*) as closed,
                    SUM(CASE WHEN result='WIN' THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN result='LOSS' THEN 1 ELSE 0 END) as losses,
                    COALESCE(SUM(profit_pips), 0) as total_pips
                FROM trades
                WHERE status='CLOSED' AND created_at >= ?""",
                (since,),
            ).fetchone()

            closed = closed_row["closed"] or 0
            wins = closed_row["wins"] or 0
            losses = closed_row["losses"] or 0
            total_pips = closed_row["total_pips"] or 0.0
            overall_win_rate = (wins / closed * 100) if closed > 0 else 0.0

            # ── 패턴별 통계 ───────────────────────────────────────────────
            rows = conn.execute(
                """SELECT
                    COALESCE(pattern_type, 'unknown') as pattern_type,
                    COUNT(*) as total,
                    SUM(CASE WHEN result='WIN' THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN result='LOSS' THEN 1 ELSE 0 END) as losses,
                    AVG(profit_pips) as avg_pips,
                    AVG(CASE WHEN result='WIN' THEN profit_pips END) as avg_win_pips,
                    AVG(CASE WHEN result='LOSS' THEN profit_pips END) as avg_loss_pips,
                    AVG(actual_rr) as avg_rr
                FROM trades
                WHERE status='CLOSED' AND created_at >= ?
                GROUP BY COALESCE(pattern_type, 'unknown')
                ORDER BY total DESC""",
                (since,),
            ).fetchall()

            pattern_stats = []
            for r in rows:
                wr = (r["wins"] / r["total"] * 100) if r["total"] > 0 else 0.0
                avg_pips = r["avg_pips"] or 0.0

                # 권장사항 결정 로직
                if avg_pips < -10 and wr < 40:
                    rec = "disable"
                elif avg_pips < 0 or wr < 50:
                    rec = "review"
                else:
                    rec = "continue"

                pattern_stats.append(
                    PatternStats(
                        pattern=r["pattern_type"],
                        total=r["total"],
                        wins=r["wins"],
                        losses=r["losses"],
                        win_rate=round(wr, 1),
                        avg_profit_pips=round(avg_pips, 1),
                        avg_win_pips=round(r["avg_win_pips"] or 0.0, 1),
                        avg_loss_pips=round(r["avg_loss_pips"] or 0.0, 1),
                        avg_rr=round(r["avg_rr"] or 0.0, 2),
                        recommendation=rec,
                    )
                )

            # ── 최근 연속 손실 ─────────────────────────────────────────────
            recent = conn.execute(
                """SELECT result FROM trades
                WHERE status='CLOSED'
                ORDER BY timestamp_close DESC
                LIMIT 10"""
            ).fetchall()
            consecutive_losses = 0
            for r in recent:
                if r["result"] == "LOSS":
                    consecutive_losses += 1
                else:
                    break

            # ── 청산 사유별 집계 ──────────────────────────────────────────
            exit_rows = conn.execute(
                """SELECT COALESCE(exit_reason, 'UNKNOWN') as reason, COUNT(*) as cnt
                FROM trades
                WHERE status='CLOSED' AND created_at >= ?
                GROUP BY reason
                ORDER BY cnt DESC""",
                (since,),
            ).fetchall()
            exit_reason_counts = {r["reason"]: r["cnt"] for r in exit_rows}

            # ── 최고/최악 패턴 ─────────────────────────────────────────────
            best = (
                max(pattern_stats, key=lambda x: x.avg_profit_pips)
                if pattern_stats else None
            )
            worst = (
                min(pattern_stats, key=lambda x: x.avg_profit_pips)
                if pattern_stats else None
            )

            return TradeReport(
                generated_at=datetime.now().strftime("%Y-%m-%d %H:%M"),
                period_days=days,
                total_trades=total,
                total_closed=closed,
                total_wins=wins,
                total_losses=losses,
                overall_win_rate=round(overall_win_rate, 1),
                total_profit_pips=round(total_pips, 1),
                pattern_stats=pattern_stats,
                recent_consecutive_losses=consecutive_losses,
                exit_reason_counts=exit_reason_counts,
                best_pattern=best.pattern if best else None,
                worst_pattern=worst.pattern if worst else None,
            )

        finally:
            conn.close()
