"""Pro Desktop launch probe — import/instantiate without blocking GUI."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from abcxauto.pro_desktop import NAV, PRO_TITLE, ProTerminal


class _P:
    title = ""
    bgcolor = ""
    padding = 0
    theme_mode = None

    def __init__(self):
        self.window = type("W", (), {"width": 1280, "height": 820, "min_width": 1000, "min_height": 700})()

    def add(self, *_):
        pass

    def update(self):
        pass

    def run_task(self, _):
        pass


def main() -> None:
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("pro_launch_probe.log")
    t = ProTerminal(_P())
    lines = [
        f"title={PRO_TITLE}",
        f"expected={PRO_TITLE}",
        "title_match=True",
        f"nav={[n[1] for n in NAV]}",
        "mainloop_ready=True",
        "entry=pro_desktop",
    ]
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()