"""Fail the release gate on high-risk CodeQL results or incomplete SARIF reports."""

import argparse
import json
import math
import os
import sys
from pathlib import Path


def check_reports(directory: Path, min_score: float = 7.0) -> tuple[int, int]:
    if not math.isfinite(min_score) or not 0 <= min_score <= 10:
        raise ValueError("Security severity threshold must be between 0 and 10")
    reports = sorted(directory.glob("*.sarif"))
    if not reports:
        raise ValueError(f"No SARIF reports found in {directory}")

    total = blocked = 0
    for report in reports:
        payload = json.loads(report.read_text(encoding="utf-8"))
        runs = payload.get("runs")
        if payload.get("version") != "2.1.0" or not isinstance(runs, list) or not runs:
            raise ValueError(f"Invalid or empty SARIF report: {report}")
        for run in runs:
            for invocation in run.get("invocations", []):
                if invocation.get("executionSuccessful") is False:
                    raise ValueError(f"CodeQL analysis did not complete: {report}")
                if any(note.get("level") == "error" for note in invocation.get("toolExecutionNotifications", [])):
                    raise ValueError(f"CodeQL reported an execution error: {report}")
            driver = run.get("tool", {}).get("driver", {})
            rules = driver.get("rules", [])
            rules_by_id = {rule["id"]: rule for rule in rules if isinstance(rule, dict) and "id" in rule}
            for extension in run.get("tool", {}).get("extensions", []):
                if isinstance(extension, dict):
                    for rule in extension.get("rules", []):
                        if isinstance(rule, dict) and "id" in rule and rule["id"] not in rules_by_id:
                            rules_by_id[rule["id"]] = rule
            results = run.get("results")
            if not isinstance(results, list):
                raise ValueError(f"SARIF results are missing: {report}")
            for result in results:
                # Skip findings that have been explicitly suppressed in source code or baseline
                suppressions = result.get("suppressions")
                if suppressions and all(isinstance(s, dict) and s.get("status") in (None, "accepted") for s in suppressions):
                    continue
                rule_id = result.get("ruleId")
                if rule_id is not None:
                    if rule_id not in rules_by_id:
                        raise KeyError(f"Rule ID '{rule_id}' not found in SARIF rules or extensions: {report}")
                    rule = rules_by_id[rule_id]
                else:
                    index = result["ruleIndex"]
                    if not isinstance(index, int) or not 0 <= index < len(rules):
                        raise ValueError(f"Invalid SARIF rule index: {report}")
                    rule = rules[index]
                total += 1
                score = rule.get("properties", {}).get("security-severity")
                if score is not None:
                    score = float(score)
                    if not math.isfinite(score) or not 0 <= score <= 10:
                        raise ValueError(f"Invalid security severity: {report}")
                    blocking = score >= min_score
                else:
                    level = result.get("level", rule.get("defaultConfiguration", {}).get("level", "warning"))
                    blocking = level == "error"
                if blocking:
                    blocked += 1
                    # Do not print source snippets, which may contain credentials.
                    print(f"Blocking CodeQL finding: {rule['id']} (security severity: {score})")
    return total, blocked


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--min-score", type=float, default=7.0)
    args = parser.parse_args(argv)
    try:
        total, blocked = check_reports(args.directory, args.min_score)
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        print(f"CodeQL gate failed: {exc}", file=sys.stderr)
        return 2

    message = f"CodeQL: {total} findings, {blocked} blocking (threshold {args.min_score:g})."
    print(message)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as summary:
            summary.write(message + "\n\nSee the CodeQL artifact and Security → Code scanning for details.\n")
    return 1 if blocked else 0


if __name__ == "__main__":
    sys.exit(main())
