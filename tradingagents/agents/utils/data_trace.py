"""Data source tracing utility for analyst reports."""


def build_data_trace(agent_name: str, entries: list) -> str:
    """Build a standardized data source trace section.
    
    Args:
        agent_name: Display name of the analyst
        entries: List of (tool_name, data_source, summary) tuples
    
    Returns:
        Markdown string for the trace section
    """
    lines = ["\n\n---\n### 📊 数据来源追溯\n"]
    lines.append("| 工具 | 数据源 | 返回内容 |")
    lines.append("|------|--------|----------|")
    for tool, source, summary in entries:
        lines.append(f"| {tool} | {source} | {summary} |")
    lines.append(f"\n*分析师: {agent_name}*")
    return "\n".join(lines)


def summarize_data(data, max_len: int = 50) -> str:
    """Summarize any data type for trace display."""
    if data is None:
        return "无数据"
    # Handle numeric values (e.g. float from DataCollector pool)
    if isinstance(data, (int, float)):
        return f"✅ {data}"
    if not isinstance(data, str):
        data = str(data)
    if not data:
        return "无数据"
    # Only treat as failure if it's a real error/failure, not informational messages
    if "调用失败" in data:
        return "❌ 获取失败"
    if "无数据" in data or "缺失" in data:
        return "⚠ 数据缺失"
    if "no data" in data.lower() and len(data) < 80:
        return "⚠ 数据缺失"
    lines = [l for l in data.split("\n") if l.strip() and not l.startswith("#")]
    if len(lines) > 3:
        return f"✅ {len(lines)} 行数据"
    return f"✅ {data[:max_len]}..."
