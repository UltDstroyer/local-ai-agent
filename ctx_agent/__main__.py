"""Dry-run policy checker. No commands or filesystem mutations are executed."""

import argparse
import json
import sys
from pathlib import Path

from .policy import Policy, PolicyError


def main() -> int:
    parser = argparse.ArgumentParser(description="ctx-agent policy dry run")
    parser.add_argument(
        "--policy", type=Path,
        default=Path(__file__).resolve().parent.parent / "config/policy.example.json",
    )
    parser.add_argument("--session", choices=("background", "interactive"),
                        default="background")
    parser.add_argument("operation")
    parser.add_argument("paths", nargs="*")
    args = parser.parse_args()
    try:
        policy = Policy.from_file(args.policy)
        decision = policy.evaluate(args.operation, args.paths, args.session)
    except PolicyError as exc:
        print(json.dumps({"effect": "deny", "reason": str(exc)}))
        return 2
    print(json.dumps({"effect": decision.effect, "reason": decision.reason}))
    return 0 if decision.permitted else 3


if __name__ == "__main__":
    sys.exit(main())
