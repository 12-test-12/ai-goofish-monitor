from copy import deepcopy
from datetime import date
import json

import pytest

from scripts.ci.check_npm_audit import BRACES_ADVISORY, check_report, main


def report_fixture():
    report = {
        "auditReportVersion": 2,
        "metadata": {"vulnerabilities": {"high": 1}},
        "vulnerabilities": {
            "braces": {
                "nodes": ["node_modules/braces"],
                "via": [{"name": "braces", "severity": "high", "url": BRACES_ADVISORY}],
            },
            "tailwindcss": {"nodes": ["node_modules/tailwindcss"], "via": ["braces"]},
        },
    }
    return report, {"packages": {"node_modules/braces": {"dev": True}}}


def test_npm_gate_accepts_clean_scan():
    report, lock = report_fixture()
    report["vulnerabilities"] = {}
    report["metadata"]["vulnerabilities"]["high"] = 0
    assert check_report(report, lock) == 0


def test_npm_gate_accepts_only_reviewed_build_advisory():
    report, lock = report_fixture()
    assert check_report(report, lock, today=date(2026, 10, 22)) == 0


@pytest.mark.parametrize("change", ["expired", "runtime", "unknown", "critical", "mixed_nodes"])
def test_npm_gate_blocks_expired_runtime_and_other_advisories(change):
    report, lock = report_fixture()
    today = date(2026, 10, 22)
    if change == "expired":
        today = date(2026, 11, 23)
    elif change == "runtime":
        lock["packages"]["node_modules/braces"]["dev"] = False
    elif change == "unknown":
        report["vulnerabilities"]["braces"]["via"][0]["url"] = "https://github.com/advisories/GHSA-new"
    elif change == "critical":
        report["vulnerabilities"]["braces"]["via"][0]["severity"] = "critical"
    else:
        report["vulnerabilities"]["braces"]["nodes"].append("node_modules/runtime/braces")
        lock["packages"]["node_modules/runtime/braces"] = {}
    assert check_report(report, lock, today=today) == 1


def test_npm_gate_checks_other_advisories_on_excepted_package():
    report, lock = report_fixture()
    other = deepcopy(report["vulnerabilities"]["braces"]["via"][0])
    other["url"] = "https://github.com/advisories/GHSA-other"
    report["vulnerabilities"]["braces"]["via"].append(other)
    assert check_report(report, lock, today=date(2026, 10, 22)) == 1


@pytest.mark.parametrize("change", ["cycle", "missing_details", "unknown_severity"])
def test_npm_gate_rejects_invalid_advisory_graphs(change):
    report, lock = report_fixture()
    if change == "cycle":
        report["vulnerabilities"]["braces"]["via"] = ["tailwindcss"]
    elif change == "missing_details":
        report["vulnerabilities"] = {}
    else:
        report["vulnerabilities"]["braces"]["via"][0]["severity"] = "unknown"
    with pytest.raises(ValueError):
        check_report(report, lock)


@pytest.mark.parametrize("report", [{}, {"error": {"code": "ENOTFOUND"}}, {"auditReportVersion": 2, "vulnerabilities": {}}])
def test_npm_gate_rejects_incomplete_reports(tmp_path, report):
    report_file = tmp_path / "report.json"
    lock_file = tmp_path / "lock.json"
    report_file.write_text(json.dumps(report))
    lock_file.write_text('{"packages": {}}')
    assert main([str(report_file), str(lock_file)]) == 2
