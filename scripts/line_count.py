"""Count desktop glue lines for verification evidence."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = [ROOT / "abcxauto" / "desktop.py", ROOT / "abcxauto" / "__main__.py"]
SCRATCH = Path(r"C:\Users\nvick\AppData\Local\Temp\grok-goal-eafc232c6c32\implementer")

def main() -> None:
    lines = []
    total = 0
    for f in FILES:
        n = sum(1 for _ in f.open(encoding="utf-8"))
        total += n
        lines.append(f"{n} {f.name}")
    lines.append(f"total_glue={total}")
    SCRATCH.mkdir(parents=True, exist_ok=True)
    SCRATCH.joinpath("line_count.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))

if __name__ == "__main__":
    main()