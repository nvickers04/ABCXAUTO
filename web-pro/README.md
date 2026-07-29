# ABCXAUTO Web Pro (desktop UI)

X Lights Out operator shell. Used as:

1. **Desktop app** — `python -m abcxauto --desktop` (native window via pywebview)
2. **Browser** — `cd web-pro && npm run dev`

Paper-sim UI until wired to live ProEngine API. Risk core stays Python.

## Build

```bash
cd web-pro
npm install
npm run build
```

## Desktop icon

From repo root:

```bash
pip install pywebview   # native window (optional but recommended)
python scripts/install_desktop_icon.py
```

Double-click **ABCXAUTO Pro** on your Desktop.


## Live IBKR

The desktop shell serves this UI **and** a local API:

| Route | Source |
|-------|--------|
| `GET /api/book` | IBKR positions + orders + account |
| `GET /api/bars/{SYM}` | IBKR historical (preferred) or MDA |
| `POST /api/connect` | Connect TWS paper (default 7497) |

```bash
# TWS paper running with API enabled
python -m abcxauto --desktop
# Click Connect in the UI → book + Focus levels go live
```

Without TWS/API the UI keeps the demo book.
