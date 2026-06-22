"""M3 — MCP tools (FastMCP). Protocol is the SDK's job; we test registration + tool logic offline."""
import asyncio
from pathlib import Path

from scripts import mcp_server


def test_all_tools_registered_with_schema():
    tools = asyncio.run(mcp_server.mcp.list_tools())
    names = {t.name for t in tools}
    assert {"introspect", "generate_tests", "triage_failures", "run_metrics", "accept_run"} <= names
    intro = next(t for t in tools if t.name == "introspect")
    assert "target" in intro.inputSchema["properties"]      # schema derived from the signature


def test_introspect_tool_runs(tmp_path: Path):
    target = tmp_path / "m.py"
    target.write_text("def add(a: int, b: int = 1) -> int:\n    return a + b\n", encoding="utf-8")
    text = mcp_server.introspect(str(target), selector="add")
    assert "add(" in text


def test_run_metrics_tool_runs():
    assert "Ledger" in mcp_server.run_metrics()


def test_accept_run_missing_id():
    assert "not found" in mcp_server.accept_run("no_such_run", 1)
