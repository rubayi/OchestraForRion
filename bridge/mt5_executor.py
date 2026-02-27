"""
mt5_executor.py — MT5 주문 실행 브릿지

OchestraForRion 신규 봇 전용.
rion_watcher.py의 복잡한 로직 없이,
signal.json의 SL/TP/랏을 그대로 MT5에 전달.
"""

import json
import logging
from pathlib import Path
from typing import Optional

try:
    import MetaTrader5 as mt5
    _MT5_AVAILABLE = True
except ImportError:
    _MT5_AVAILABLE = False

logger = logging.getLogger(__name__)

DEVIATION = 10  # 가격 허용 편차 (points)


def _require_mt5():
    if not _MT5_AVAILABLE:
        raise ImportError("MetaTrader5 패키지가 설치되지 않았습니다: pip install MetaTrader5")


def connect(config_path: str) -> bool:
    """MT5 초기화 + 계정 연결

    Args:
        config_path: mt5_config_rionbot.json 경로

    Returns:
        True = 연결 성공
    """
    _require_mt5()

    cfg_file = Path(config_path)
    if not cfg_file.exists():
        logger.error(f"MT5 설정 파일 없음: {config_path}")
        logger.error("mt5_config_rionbot.json.example 을 복사하여 계정 정보를 입력하세요.")
        return False

    try:
        with open(cfg_file, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception as e:
        logger.error(f"MT5 설정 파일 읽기 실패: {e}")
        return False

    login  = cfg.get("login")
    passwd = cfg.get("password")
    server = cfg.get("server")
    path   = cfg.get("path", "")

    kwargs = dict(login=int(login), password=str(passwd), server=str(server))
    if path:
        kwargs["path"] = path

    if not mt5.initialize(**kwargs):
        logger.error(f"MT5 초기화 실패: {mt5.last_error()}")
        return False

    info = mt5.account_info()
    if info is None:
        logger.error(f"MT5 계정 정보 조회 실패: {mt5.last_error()}")
        mt5.shutdown()
        return False

    logger.info(
        f"MT5 연결 성공: 계정={info.login} | 서버={info.server} | "
        f"잔고={info.balance:.2f} {info.currency}"
    )
    return True


def place_sell(
    symbol: str,
    lot: float,
    sl_pips: float,
    tp_pips: float,
    magic: int,
    comment: str = "OchestraForRion",
) -> dict:
    """SELL 시장가 주문 실행

    Args:
        symbol:   거래 심볼 (예: "GBPAUD")
        lot:      랏 크기
        sl_pips:  손절 거리 (pips)
        tp_pips:  목표 거리 (pips)
        magic:    매직 넘버
        comment:  주문 코멘트

    Returns:
        {"success": bool, "ticket": int, "price": float, ...}
    """
    _require_mt5()

    sym_info = mt5.symbol_info(symbol)
    if sym_info is None:
        return {"success": False, "error": f"심볼 정보 없음: {symbol}"}

    if not sym_info.visible:
        if not mt5.symbol_select(symbol, True):
            return {"success": False, "error": f"심볼 활성화 실패: {symbol}"}

    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return {"success": False, "error": f"틱 데이터 없음: {symbol}"}

    # 1 pip = point × 10 (5자리 브로커 기준)
    pip_val = sym_info.point * 10
    bid = tick.bid
    digits = sym_info.digits

    sl_price = round(bid + sl_pips * pip_val, digits)
    tp_price = round(bid - tp_pips * pip_val, digits)

    request = {
        "action":      mt5.TRADE_ACTION_DEAL,
        "symbol":      symbol,
        "volume":      float(lot),
        "type":        mt5.ORDER_TYPE_SELL,
        "price":       bid,
        "sl":          sl_price,
        "tp":          tp_price,
        "deviation":   DEVIATION,
        "magic":       magic,
        "comment":     comment[:31],           # MT5 코멘트 최대 31자
        "type_time":   mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    result = mt5.order_send(request)

    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        retcode = result.retcode if result else -1
        comment_err = result.comment if result else "unknown"
        logger.error(f"주문 실패: retcode={retcode} | {comment_err}")
        return {
            "success": False,
            "error":   f"retcode={retcode} — {comment_err}",
        }

    logger.info(
        f"주문 체결: Ticket={result.order} | "
        f"가격={result.price:.5f} | SL={sl_price:.5f} | TP={tp_price:.5f} | "
        f"랏={lot}"
    )
    return {
        "success": True,
        "ticket":  result.order,
        "price":   result.price,
        "sl":      sl_price,
        "tp":      tp_price,
        "lot":     lot,
    }


def close_position(ticket: int) -> dict:
    """포지션 청산 (시장가)

    Args:
        ticket: MT5 티켓 번호

    Returns:
        {"success": bool, "ticket": int, ...}
    """
    _require_mt5()

    positions = mt5.positions_get(ticket=ticket)
    if not positions:
        return {"success": False, "error": f"포지션 없음: {ticket}"}

    pos  = positions[0]
    tick = mt5.symbol_info_tick(pos.symbol)
    if tick is None:
        return {"success": False, "error": f"틱 데이터 없음: {pos.symbol}"}

    # SELL 포지션 청산 = BUY 주문
    close_type = mt5.ORDER_TYPE_BUY if pos.type == 1 else mt5.ORDER_TYPE_SELL
    price      = tick.ask if close_type == mt5.ORDER_TYPE_BUY else tick.bid

    request = {
        "action":      mt5.TRADE_ACTION_DEAL,
        "symbol":      pos.symbol,
        "volume":      pos.volume,
        "type":        close_type,
        "position":    ticket,
        "price":       price,
        "deviation":   DEVIATION,
        "magic":       pos.magic,
        "comment":     "OchestraForRion close",
        "type_time":   mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    result = mt5.order_send(request)
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        retcode = result.retcode if result else -1
        msg = result.comment if result else "unknown"
        return {"success": False, "error": f"청산 실패: retcode={retcode} — {msg}"}

    logger.info(f"청산 완료: Ticket={ticket} | 가격={result.price:.5f}")
    return {"success": True, "ticket": ticket, "price": result.price}


def get_positions(magic: Optional[int] = None) -> list:
    """현재 포지션 조회

    Args:
        magic: 특정 magic number만 필터 (None이면 전체)

    Returns:
        MT5 PositionInfo 리스트
    """
    _require_mt5()

    positions = mt5.positions_get()
    if positions is None:
        return []
    if magic is not None:
        positions = [p for p in positions if p.magic == magic]
    return list(positions)


def get_account_info() -> Optional[dict]:
    """계정 정보 조회"""
    _require_mt5()
    info = mt5.account_info()
    if info is None:
        return None
    return {
        "login":   info.login,
        "server":  info.server,
        "balance": info.balance,
        "equity":  info.equity,
        "margin":  info.margin,
        "currency": info.currency,
    }


def disconnect() -> None:
    """MT5 연결 해제"""
    if _MT5_AVAILABLE:
        mt5.shutdown()
        logger.info("MT5 연결 해제")
