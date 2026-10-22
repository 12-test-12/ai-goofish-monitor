import json

import pytest

from scripts.ci.check_codeql import check_reports, main


def write_report(tmp_path, *, score="7.0", results=None, invocation=None):
    rule = {"id": "py/test-rule", "properties": {"security-severity": score}}
    if score is None:
        rule["properties"] = {}
        rule["defaultConfiguration"] = {"level": "error"}
    run = {
        "tool": {"driver": {"rules": [rule]}},
        "results": [{"ruleId": rule["id"]}] if results is None else results,
    }
    if invocation is not None:
        run["invocations"] = [invocation]
    path = tmp_path / "python.sarif"
    path.write_text(json.dumps({"version": "2.1.0", "runs": [run]}), encoding="utf-8")
    return path


@pytest.mark.parametrize("score,blocked", [("6.9", 0), ("7.0", 1), ("9.8", 1), (None, 1)])
def test_codeql_gate_enforces_security_severity(tmp_path, score, blocked):
    write_report(tmp_path, score=score)
    assert check_reports(tmp_path) == (1, blocked)
    assert main([str(tmp_path)]) == blocked


def test_codeql_gate_accepts_clean_report(tmp_path):
    write_report(tmp_path, results=[])
    assert check_reports(tmp_path) == (0, 0)


def test_codeql_gate_resolves_rule_index(tmp_path):
    write_report(tmp_path, results=[{"ruleIndex": 0}])
    assert check_reports(tmp_path) == (1, 1)


def test_codeql_gate_checks_every_report_and_run(tmp_path):
    first = write_report(tmp_path, score="1.0")
    first.rename(tmp_path / "low.sarif")
    second = write_report(tmp_path, score="8.0")
    payload = json.loads(second.read_text(encoding="utf-8"))
    payload["runs"] *= 2
    second.write_text(json.dumps(payload), encoding="utf-8")
    assert check_reports(tmp_path) == (3, 2)


@pytest.mark.parametrize("problem", ["missing", "json", "empty_runs", "missing_results", "failed_scan", "tool_error", "bad_score", "unknown_rule"])
def test_codeql_gate_fails_closed_on_incomplete_or_invalid_reports(tmp_path, problem):
    if problem != "missing":
        path = write_report(tmp_path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        run = payload["runs"][0]
        if problem == "empty_runs":
            payload["runs"] = []
        elif problem == "missing_results":
            del run["results"]
        elif problem == "failed_scan":
            run["invocations"] = [{"executionSuccessful": False}]
        elif problem == "tool_error":
            run["invocations"] = [{"toolExecutionNotifications": [{"level": "error"}]}]
        elif problem == "bad_score":
            run["tool"]["driver"]["rules"][0]["properties"]["security-severity"] = "NaN"
        elif problem == "unknown_rule":
            run["results"] = [{"ruleId": "unknown"}]
        path.write_text("not-json" if problem == "json" else json.dumps(payload), encoding="utf-8")
    assert main([str(tmp_path)]) == 2


@pytest.mark.parametrize("score", [-1, 11, float("nan"), float("inf")])
def test_codeql_gate_rejects_invalid_threshold(tmp_path, score):
    write_report(tmp_path)
    with pytest.raises(ValueError, match="threshold"):
        check_reports(tmp_path, min_score=score)
