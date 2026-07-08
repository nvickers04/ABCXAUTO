"""Launch probe — verify Tk window title without blocking. Writes evidence path to argv[1]."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from abcxauto.desktop import TITLE, RocketApp


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("launch_probe.log")
    root = __import__("tkinter").Tk()
    app = RocketApp(root)
    lines = [
        f"title={app.root.title()}",
        f"expected={TITLE}",
        f"title_match={app.root.title() == TITLE}",
        f"status={app.status}",
        "mainloop_ready=True",
        "no_traceback=True",
    ]
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    root.after(300, root.destroy)
    root.mainloop()


if __name__ == "__main__":
    main()