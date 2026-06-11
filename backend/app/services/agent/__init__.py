"""AI-агент: ops/диагностика поверх детерминированных примитивов.

Фаза 1 (AI_AGENT_ROADMAP.md) — read-only диагностический триаж. Несущие
принципы: scoped-токен (не мастер-ключ — здесь LLM-ключ ANTHROPIC_API_KEY),
все действия атрибутируются actor=agent в audit_logs, kill switch
AGENT_ENABLED, и НИКАКИХ мутаций инфры на этой фазе (tools только read).
"""
