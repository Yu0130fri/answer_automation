import argparse
import subprocess
from pathlib import Path

_CURRENT_DIR = Path(__file__).absolute().parent
_CLI = _CURRENT_DIR / "main.py"

parser = argparse.ArgumentParser(description="Answer Automation")
parser.add_argument("--email", type=str, help="your email")
parser.add_argument("--password", type=str, help="your password")

if __name__ == "__main__":
    args = parser.parse_args()
    print("======アンケート回答開始しました======")
    subprocess.run(
        ["python3", str(_CLI), "--email", args.email, "--password", args.password]
    )
    print("======アンケートの回答実行を終了しました======")
