"""Playbook internals. Public imports stay on ``abcxauto.lab_playbook``.

- ``schema`` — card/type shape, ``retire_if``, fill assumption, ``clamp_update``
- ``persist`` — lab/live json, ``load_lab`` / ``save_lab``
- ``promote`` — graduation + live snapshot (sample + one numeric kill;
  ``conservative_pnl``). Paper mids cannot graduate.
- ``live_cards`` — live-card notes and scan constraints (prose is not a send gate)
"""
