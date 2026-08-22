import argparse
import os
from pathlib import Path

from selenium_moppy import AnswerQuestionnaire


def _load_dotenv(path: Path) -> dict:
    env = {}
    if not path.exists():
        return env
    try:
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                env[key] = val
    except Exception:
        return {}
    return env


def set_args():
    parser = argparse.ArgumentParser(description="get email and password")
    parser.add_argument("--email", type=str, help="your email", required=False)
    parser.add_argument("--password", type=str, help="your password", required=False)
    parser.add_argument(
        "--threads",
        type=int,
        default=3,
        help="parallel Chrome workers to run in background (default: 3)",
    )
    parser.add_argument(
        "--reset-state",
        action="store_true",
        help="discard saved in-progress state and start fresh",
    )

    args = parser.parse_args()

    # Load .env if present and fill missing args
    env_path = Path(".env")
    env = _load_dotenv(env_path)

    if not args.email and env.get("EMAIL"):
        args.email = env.get("EMAIL")
    if not args.password and env.get("PASSWORD"):
        args.password = env.get("PASSWORD")

    # If still missing, fail
    if not args.email or not args.password:
        parser.error("email and password must be provided via --email/--password or a .env file with EMAIL and PASSWORD entries")

    return args


def main():
    args = set_args()
    questionnaires = AnswerQuestionnaire(email=args.email, password=args.password)
    if args.reset_state:
        questionnaires.clear_resume_state()

    try:
        if args.threads > 1:
            questionnaires.run_parallel(worker_count=args.threads)
            return
        questionnaires.run()
    except KeyboardInterrupt:
        print("中断しました。次回実行時に自動で再開されます。", flush=True)
        raise SystemExit(130)


if __name__ == "__main__":
    main()
