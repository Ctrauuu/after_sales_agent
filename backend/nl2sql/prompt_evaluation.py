"""可由 CI 调用的 NL2SQL Prompt A/B 评估入口。"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from nl2sql.prompt_registry import (
    NL2SQLEvaluator,
    default_registry,
    evaluation_passed,
    load_golden_dataset,
)
from nl2sql.runtime import nl2sql_pipeline


def main() -> int:
    """输入：命令行可选 ``--dataset`` 参数；读取默认 Prompt 注册表和生产 NL2SQL 组件。

    输出：向标准输出写入 JSON 报告；通过返回 ``0``，退化返回 ``1``，持平返回 ``2``。
    功能：比较注册表最新两个版本，更新新版评估指标，并只在准确指标提升且无退化时允许 CI 合并。
    """
    parser = argparse.ArgumentParser(description="评估最新两个 NL2SQL Prompt 版本")
    parser.add_argument("--dataset", type=Path, required=True, help="脱敏 Golden Dataset JSON")
    args = parser.parse_args()

    registry = default_registry()
    versions = registry.list_versions("nl2sql")
    active_version = nl2sql_pipeline.prompt_version
    if not versions or versions[-1] == active_version:
        print("没有高于已部署版本的 NL2SQL Prompt 候选，跳过 A/B 评估")
        return 0

    report = NL2SQLEvaluator(
        nl2sql_pipeline.generator,
        nl2sql_pipeline.validator,
        nl2sql_pipeline.executor,
    ).evaluate(
        registry.load_prompt("nl2sql", active_version),
        registry.load_prompt("nl2sql", versions[-1]),
        load_golden_dataset(args.dataset),
    )
    latest = registry.load_prompt("nl2sql", versions[-1])
    latest.metrics = {
        "sql_syntax_pass_rate": report.sql_syntax_pass_rate,
        "result_match_rate": report.result_match_rate,
        "evaluated_at": datetime.now().isoformat(),
    }
    registry.save_prompt(latest)
    print(json.dumps(asdict(report), ensure_ascii=False, indent=2))
    if evaluation_passed(report):
        return 0
    if report.winner == "tie":
        print("评估结果持平或仅成本改善，需人工审核")
        return 2
    print("评估未通过：新版 SQL 指标退化或未达到提升要求")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
