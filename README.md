# 🤖 RionAgent

> GBPAUD 전문 AI 트레이딩 멀티에이전트 시스템 — **목표: 실제 수익**

[AlgoTradingBot](https://github.com/rubayi/AlgoTradingBot)의 AI 의사결정을 두뇌/근육 멀티에이전트 구조로 고도화합니다.

## 구조

```
패턴 감지 → Agent1(Haiku 필터) → Agent2(Opus 판단) → Agent3(Haiku 리스크) → MT5 주문
                                                   ↑
                                    Agent4(Haiku 자기학습) ← trades.db
```

## 상세 설계

👉 [DESIGN.md](./DESIGN.md) 참조

## 구현 현황

- [x] Phase 0: 설계 문서 + 프로젝트 구조
- [ ] Phase 1: Agent 4 (성과 분석 + 일일 Telegram 리포트)
- [ ] Phase 2: Agent 1 (시장 분석 필터)
- [ ] Phase 3: Agent 2 (Opus 두뇌)
- [ ] Phase 4: Agent 3 + 역프롬프팅
- [ ] Phase 5: 자기학습 루프 완성
