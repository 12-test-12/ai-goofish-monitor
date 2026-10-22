"""Check npm audit JSON; narrowly accept an unpatched build-only advisory."""

import argparse
from datetime import date
import json
from pathlib import Path
import sys


# Tailwind 3 uses braces for repository-controlled content globs. There is no
# patched braces release. Never accept this advisory in runtime dependencies.
BRACES_ADVISORY = "https://github.com/advisories/GHSA-vfj7-8cjw-p6xm"
BRACES_REVIEW_DATE = date(2026, 11, 22)


def check_report(report: dict, lock: dict, *, today: date | None = None) -> int:
    today = today or date.today()
    if report.get("error") or report.get("auditReportVersion") != 2:
        raise ValueError("npm audit failed or returned an unsupported report")
    vulnerabilities = report.get("vulnerabilities")
    if not isinstance(vulnerabilities, dict) or not isinstance(report.get("metadata", {}).get("vulnerabilities"), dict):
        raise ValueError("npm audit report is incomplete")
    counts = report["metadata"]["vulnerabilities"]
    if not vulnerabilities and (counts.get("high", 0) or counts.get("critical", 0)):
        raise ValueError("npm audit report is missing high/critical advisory details")
    packages = lock["packages"]

    def validate_chain(name, ancestors):
        if name in ancestors or name not in vulnerabilities:
            raise ValueError(f"Invalid transitive advisory chain: {name}")
        via = vulnerabilities[name]["via"]
        if not isinstance(via, list) or not via:
            raise ValueError(f"Missing advisory details for {name}")
        for advisory in via:
            if isinstance(advisory, str):
                validate_chain(advisory, ancestors | {name})
            elif advisory["severity"] not in {"info", "low", "moderate", "high", "critical"}:
                raise ValueError(f"Invalid advisory severity for {name}")

    for name in vulnerabilities:
        validate_chain(name, set())
    blocking = 0
    for name, finding in vulnerabilities.items():
        via = finding["via"]
        if not isinstance(via, list) or not via:
            raise ValueError(f"Missing advisory details for {name}")
        # npm repeats transitive findings on every affected parent. Check the
        # actual advisories, rather than counting those parent entries again.
        for advisory in via:
            if isinstance(advisory, str):
                if advisory not in vulnerabilities:
                    raise ValueError(f"Missing transitive advisory: {advisory}")
                continue
            if advisory["severity"] not in {"high", "critical"}:
                continue
            nodes = finding["nodes"]
            build_only_braces = (
                name == "braces" and advisory.get("name") == "braces"
                and advisory.get("url") == BRACES_ADVISORY
                and advisory["severity"] == "high"
                and today <= BRACES_REVIEW_DATE and bool(nodes)
                and all(packages[node].get("dev") is True for node in nodes)
            )
            if build_only_braces:
                print(f"Temporary build-only exception: braces; review by {BRACES_REVIEW_DATE}")
            else:
                blocking += 1
                print(f"Blocking npm advisory: {name}: {advisory.get('url', 'unknown')}")
    return blocking


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("lock", type=Path)
    args = parser.parse_args(argv)
    try:
        blocked = check_report(
            json.loads(args.report.read_text(encoding="utf-8")),
            json.loads(args.lock.read_text(encoding="utf-8")),
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        print(f"npm audit gate failed: {exc}", file=sys.stderr)
        return 2
    print(f"npm audit: {blocked} blocking advisories")
    return 1 if blocked else 0


if __name__ == "__main__":
    sys.exit(main())
