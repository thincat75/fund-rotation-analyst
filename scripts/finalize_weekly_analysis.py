#!/usr/bin/env python3
"""Validate and merge evidence-bound LLM synthesis into a schema-v2 weekly analysis."""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Any

from data_access import load_json, write_json


ALLOWED_ACTION_TERMS = {"观察", "等待确认", "控制同质化", "减配去重", "保留", "不追高"}
FORBIDDEN_MARGIN_CLAIMS = {"低杠杆所以有很大上涨空间", "高杠杆所以马上见顶", "融资余额下降所以必然见底", "融资余额增长所以一定看多"}
SECTOR_FIELDS = {"persistent_leaders", "emerging_sectors", "fading_sectors"}
REQUIRED_FIELDS = {
    "market_regime", "rotation_path", "persistent_leaders", "emerging_sectors", "fading_sectors",
    "portfolio_implications", "action_explanations", "uncertainties", "confidence", "evidence_refs",
    "model", "prompt_version", "generated_at", "evidence_hash",
}


def validate_synthesis(synthesis: dict[str, Any], evidence_bundle: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    missing = sorted(REQUIRED_FIELDS - set(synthesis))
    if missing:
        errors.append(f"缺少字段：{'、'.join(missing)}")
    evidence = evidence_bundle.get("evidence") or {}
    if synthesis.get("evidence_hash") != evidence_bundle.get("evidence_hash"):
        errors.append("证据哈希不匹配")
    refs = synthesis.get("evidence_refs") or []
    unknown_refs = [ref for ref in refs if ref not in evidence]
    if unknown_refs:
        errors.append(f"引用不存在：{'、'.join(unknown_refs[:5])}")
    referenced_periods = {evidence[ref].get("period") for ref in refs if ref in evidence}
    if refs and len(referenced_periods - {None}) < 2:
        errors.append("主结论没有引用至少两个周期")
    known_entities = {str(row.get("entity_name")) for row in evidence.values()}
    for field in SECTOR_FIELDS:
        unknown = [str(name) for name in synthesis.get(field) or [] if str(name) not in known_entities]
        if unknown:
            errors.append(f"{field}包含证据外实体：{'、'.join(unknown)}")
    for text in synthesis.get("action_explanations") or []:
        if re.search(r"\d+(?:\.\d+)?\s*%", str(text)):
            errors.append("LLM动作解释不得自行生成比例")
        if any(term in str(text) for term in ("买入", "清仓", "满仓")):
            errors.append("LLM动作解释越过程序门控")
        if not any(term in str(text) for term in ALLOWED_ACTION_TERMS):
            errors.append(f"动作解释不在允许语义内：{text}")
    if synthesis.get("confidence") not in {"高", "中", "低"}:
        errors.append("confidence必须为高、中或低")
    synthesis_text = str(synthesis)
    if any(claim in synthesis_text for claim in FORBIDDEN_MARGIN_CLAIMS):
        errors.append("LLM两融解释包含确定性因果推论")
    return list(dict.fromkeys(errors))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--analysis", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--synthesis", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    analysis = load_json(args.analysis)
    evidence = load_json(args.evidence)
    synthesis = load_json(args.synthesis)
    errors = validate_synthesis(synthesis, evidence)
    if errors:
        raise SystemExit("LLM SYNTHESIS REJECTED: " + "；".join(errors))
    analysis["llm_synthesis"] = {**synthesis, "status": "validated"}
    analysis["llm_evidence_hash"] = evidence.get("evidence_hash")
    write_json(args.output, analysis)


if __name__ == "__main__":
    main()
