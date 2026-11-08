"""多项目 Agent 管理平台 · 领域模块。

独立于被纳管项目的业务库：独立 SQLite + 文件采集适配器 + stdlib HTTP 展示层。
入口见 ``scripts/agents_dashboard.py``。
"""

__all__ = ["service", "sources", "store", "templates"]
