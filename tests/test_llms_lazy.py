"""LLM 客户端懒加载回归测试。

锁死一件事：**导入期不得构造模型客户端**。
`ChatOpenAI` 构造时会连带建立 HTTP 客户端，而该客户端要解析宿主环境的
`NO_PROXY`；宿主代理配置不合法时，原实现会让整条导入链（`import app.main`、
pytest 收集）直接失败。详见 `docs/change2026-11-17.md`。

不依赖数据库 / Redis / 网络。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from langchain_core.language_models.base import BaseLanguageModel

import app.agents.llms as llms

ROOT = Path(__file__).resolve().parents[1]

# 真实踩过的写法：IPv6 回环加上了 URL 才需要的方括号，httpx 会把它解析成端口 ':1]'
HOSTILE_NO_PROXY = "localhost,.localhost,127.0.0.1,127.0.0.0/8,[::1],::1"


def test_module_namespace_holds_no_client_instances() -> None:
    """模块顶层不再存放已构造的模型实例。"""
    eager = [
        name
        for name, value in vars(llms).items()
        if isinstance(value, BaseLanguageModel)
    ]
    assert eager == []


def test_models_are_exposed_as_factories_only() -> None:
    """对外只剩工厂函数，旧的模块级单例已移除。"""
    assert callable(llms.get_deepseek_llm)
    assert callable(llms.get_qwen_llm)
    assert not hasattr(llms, "deepseek_llm")
    assert not hasattr(llms, "qwen_llm")


def test_import_app_main_survives_hostile_no_proxy() -> None:
    """启动链在宿主机代理配置不合法时也必须导入成功。"""
    proc = subprocess.run(
        [sys.executable, "-c", "import app.main"],
        cwd=str(ROOT),
        env={**os.environ, "NO_PROXY": HOSTILE_NO_PROXY},
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr[-800:]
