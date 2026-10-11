"""Run one Brew & Co. task against the local server and configured Gemini API."""

import argparse
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent.loop import create_live_agent_loop
from env.server import DEFAULT_DATABASE, DEFAULT_EVENT_LOG


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task", help='Natural-language task, e.g. "We are out of Assam tea — sort it out."')
    parser.add_argument("--base-url", default="http://127.0.0.1:5000")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--events", type=Path, default=DEFAULT_EVENT_LOG)
    args = parser.parse_args()

    loop, executor = create_live_agent_loop(args.database, args.events, args.base_url)
    try:
        state = loop.start(args.task)
        while state.status in {"waiting_for_user", "waiting_for_approval"}:
            if state.status == "waiting_for_user":
                answer = input(f"Agent asks: {state.question}\nYour answer: ")
                state = loop.resume(state, answer)
            else:
                print(f"Approval needed for draft {state.draft_id}. Review it at {args.base_url}/")
                input("After approving or rejecting it on the dashboard, press Enter here to resume: ")
                state = loop.resume(state)
        print(f"Status: {state.status}")
        print(state.report or "No report returned.")
        if state.verification:
            print(f"Verification: {state.verification['status']}")
            for name, check in state.verification.get("checks", {}).items():
                print(f"  {'PASS' if check['passed'] else 'FAIL'} {name}: {check['detail']}")
        print(f"Event log: {args.events}")
    finally:
        executor.close()


if __name__ == "__main__":
    main()
