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
