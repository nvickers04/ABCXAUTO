"""Per-tab page builders for the Pro cockpit. Layout only."""
from __future__ import annotations

import flet as ft

from abcxauto.desktop.tokens import (
    BORDER,
    BRAIN_FIELDS,
    LINK_FIELDS,
    MUTED,
    PACING_FIELDS,
    SURFACE,
    TEXT,
)


class PagesMixin:

    def _page_overview(self) -> ft.Control:
        """The watch surface. The stream owns it."""
        return ft.Column(
            [
                self._stream_pane(),
                self._hidden_metrics,
            ],
            spacing=0,
            expand=True,
        )


    def _stream_pane(self) -> ft.Container:
        return ft.Container(
            expand=True,
            bgcolor="#0a0a0a",
            padding=ft.Padding.symmetric(horizontal=16, vertical=12),
            content=ft.Column(
                [
                    ft.Row(
                        [
                            ft.Text(
                                "Grok stream", size=15, weight=ft.FontWeight.BOLD, color=TEXT
                            ),
                            ft.Container(expand=True),
                            self.lbl_stream_status,
                            self.btn_stream_follow,
                            self.btn_copy_stream,
                        ],
                        spacing=8,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    ft.Container(content=self.think_scroll, expand=True),
                ],
                expand=True,
                spacing=8,
            ),
        )


    def _page_positions(self) -> ft.Control:
        heads = {
            "lots": [("lot", None), ("basis", 110), ("mtm", 58)],
            "orders": [
                ("oid", 46),
                ("name", None),
                ("side / type", 104),
                ("qty", 38),
                ("price", 104),
                ("role", 58),
            ],
            "fills": [("name", None), ("side", 56), ("qty", 44), ("price", 76)],
            "log": [("time", 60), ("what", 116), ("result", None)],
        }
        self.tab_heads = {k: self._head_row(v) for k, v in heads.items()}
        bodies: list[ft.Control] = []
        for key in ("lots", "orders", "fills", "log"):
            bodies.append(
                ft.Column(
                    [self.tab_heads[key], self.tab_bodies[key]],
                    spacing=4,
                    expand=True,
                )
            )
        self.tab_panes = dict(zip(("lots", "orders", "fills", "log"), bodies))
        return ft.Column(
            [
                ft.Container(
                    padding=ft.Padding.symmetric(horizontal=16, vertical=12),
                    border=ft.Border(bottom=ft.BorderSide(1, BORDER)),
                    content=ft.Column(
                        [
                            self._section_header("Book", self._refresh_book_tab),
                            ft.Row(
                                [
                                    self.tabs[k]["chip"]
                                    for k in ("lots", "orders", "fills", "log")
                                ],
                                spacing=6,
                                wrap=True,
                            ),
                        ],
                        spacing=10,
                        tight=True,
                    ),
                ),
                ft.Container(
                    expand=True,
                    padding=ft.Padding.symmetric(horizontal=16, vertical=10),
                    content=ft.Column(bodies, spacing=0, expand=True),
                ),
            ],
            spacing=0,
            expand=True,
        )


    def _page_notebook(self) -> ft.Control:
        self.notebook_raw_panel = ft.Container(
            visible=False,
            bgcolor=SURFACE,
            border=ft.Border.all(1, BORDER),
            border_radius=8,
            padding=10,
            content=self.lbl_notebook_body,
        )
        self._sync_notebook_page(force=True)
        return ft.Column(
            [
                self._section_refresh(
                    "Lab playbook",
                    self._refresh_notebook_tab,
                    self.lbl_notebook_head,
                    self.lbl_notebook_meta,
                    self.lbl_notebook_lots,
                    self.lbl_nb_playbook,
                ),
                ft.Container(
                    expand=True,
                    padding=ft.Padding.symmetric(horizontal=16, vertical=12),
                    content=ft.Column(
                        [
                            ft.Text(
                                "Setup cards", size=15, weight=ft.FontWeight.BOLD, color=TEXT
                            ),
                            self.col_notebook_cards,
                            self.notebook_raw_panel,
                            ft.Text(
                                "Order-type coverage",
                                size=15,
                                weight=ft.FontWeight.BOLD,
                                color=TEXT,
                            ),
                            self.lbl_notebook_types,
                            self.col_notebook_types,
                        ],
                        spacing=10,
                        scroll=ft.ScrollMode.AUTO,
                        expand=True,
                    ),
                ),
            ],
            spacing=0,
            expand=True,
        )


    def _page_scorecard(self) -> ft.Control:
        self._sync_scorecard_page(force=True)
        return ft.Column(
            [
                self._section_refresh(
                    "Book vs model",
                    self._refresh_scorecard_tab,
                    ft.Row(
                        [
                            ft.Column(
                                [ft.Text("NetLiq", size=12, color=MUTED), self.lbl_sc_netliq],
                                spacing=2,
                                tight=True,
                            ),
                            ft.Container(width=28),
                            ft.Column(
                                [
                                    ft.Text("Edge", size=12, color=MUTED),
                                    self.lbl_edge,
                                    self.lbl_edge_sub,
                                ],
                                spacing=2,
                                tight=True,
                            ),
                            ft.Container(width=28),
                            ft.Column(
                                [
                                    ft.Text("Fill slip", size=12, color=MUTED),
                                    self.lbl_sc_slip,
                                    self.lbl_sc_slip_sub,
                                ],
                                spacing=2,
                                tight=True,
                            ),
                            ft.Container(width=28),
                            ft.Column(
                                [
                                    ft.Text("Model $", size=12, color=MUTED),
                                    self.lbl_sc_spend,
                                    self.lbl_sc_spend_sub,
                                ],
                                spacing=2,
                                tight=True,
                            ),
                        ],
                        spacing=12,
                        wrap=True,
                        vertical_alignment=ft.CrossAxisAlignment.END,
                    ),
                    self.lbl_sc_verdict,
                    self.lbl_sc_score,
                    self.lbl_sc_session,
                ),
                ft.Container(
                    expand=True,
                    padding=ft.Padding.only(bottom=12),
                    content=ft.Column(
                        [
                            self._section("Windows", self.col_sc_windows),
                            self._section(
                                "Setup card scores",
                                ft.Text(
                                    "Sends tied to the card that called them, "
                                    "with realized P&L from fills.",
                                    size=11,
                                    color=MUTED,
                                ),
                                self.col_sc_cards,
                            ),
                            self._section(
                                "Playbook revisions",
                                ft.Text(
                                    "Edge stamped when the card was written → edge when "
                                    "it was replaced.",
                                    size=11,
                                    color=MUTED,
                                ),
                                self.col_sc_ledger,
                            ),
                            self.lbl_sc_strats,
                        ],
                        spacing=0,
                        scroll=ft.ScrollMode.AUTO,
                        expand=True,
                    ),
                ),
            ],
            spacing=0,
            expand=True,
        )


    def _page_risk(self) -> ft.Control:
        self._sync_risk_page(force=True)

        def _posture(name: str) -> ft.Control:
            return ft.Container(
                content=ft.Text(name, size=12, weight=ft.FontWeight.W_600, color=TEXT),
                padding=ft.Padding.symmetric(horizontal=12, vertical=6),
                border=ft.Border.all(1, BORDER),
                border_radius=999,
                ink=True,
                on_click=lambda _e, n=name: self._set_risk_posture(n),
            )

        return ft.Column(
            [
                self._section_refresh(
                    "Posture",
                    self._refresh_risk_tab,
                    self.lbl_risk_posture,
                    ft.Row(
                        [_posture("defensive"), _posture("balanced"), _posture("aggressive")],
                        spacing=6,
                        wrap=True,
                    ),
                    self.lbl_risk_status,
                ),
                ft.Container(
                    expand=True,
                    padding=ft.Padding.only(bottom=12),
                    content=ft.Column(
                        [
                            self._section(
                                "Floor",
                                ft.Text(
                                    "Editable and persisted to risk_settings.json. Enter or the "
                                    "check applies. Tighten only — a value that would weaken the "
                                    "walk-away floor is clamped and reported.",
                                    size=11,
                                    color=MUTED,
                                ),
                                self.col_risk_knobs,
                            ),
                            self._section(
                                "Gate rejections",
                                ft.Text(
                                    "Refused gates this RTH session and today. "
                                    "Reason, count, latest example.",
                                    size=11,
                                    color=MUTED,
                                ),
                                self.lbl_risk_gates,
                                self.col_risk_gates,
                            ),
                            self._section(
                                "Size floors",
                                self._field_row(
                                    "sizing_floors",
                                    "Percent size floors",
                                    "paper may size freely — live forces on",
                                    control=self.sw_size_floors,
                                ),
                                self.lbl_risk_floors,
                            ),
                            self._section(
                                "Halt",
                                self.lbl_risk_halt_state,
                                self.lbl_risk_halt_math,
                                ft.Text(
                                    "Halt / Resume is the rail button. Flatten All is "
                                    "on this page — confirm, then the engine flattens "
                                    "on the IB loop. Exits always bypass Halt.",
                                    size=11,
                                    color=MUTED,
                                ),
                                self.btn_flatten,
                            ),
                            self._section(
                                "As persisted",
                                ft.Text(
                                    "What the floor accepted after clamping.",
                                    size=11,
                                    color=MUTED,
                                ),
                                self.lbl_risk_glance,
                            ),
                        ],
                        spacing=0,
                        scroll=ft.ScrollMode.AUTO,
                        expand=True,
                    ),
                ),
            ],
            spacing=0,
            expand=True,
        )


    def _page_settings(self) -> ft.Control:
        """Agent settings the operator owns. Mode/port stay on the gated path."""
        self._sync_settings_page(force=True)
        return ft.Column(
            [
                self._section_refresh(
                    "Brain",
                    self._refresh_settings_tab,
                    ft.Text(
                        "Which Grok takes the look. Strategy stays Grok's — this does "
                        "not touch the prompt or the playbook.",
                        size=11,
                        color=MUTED,
                    ),
                    *[self._field_row(k, label, hint) for k, label, hint in BRAIN_FIELDS],
                    self.lbl_settings_brain,
                    self.lbl_settings_status,
                ),
                ft.Container(
                    expand=True,
                    padding=ft.Padding.only(bottom=12),
                    content=ft.Column(
                        [
                            self._section(
                                "Data and pacing",
                                ft.Text(
                                    "How often the background monitor polls, reviews "
                                    "and halts on a dead broker link. Session look/"
                                    "token caps stop a flat grind — hit stays idle, "
                                    "chat kept. Scan depth is self_tune. Stay-up has "
                                    "no sit clock.",
                                    size=11,
                                    color=MUTED,
                                ),
                                self._field_row(
                                    "monitor_enabled",
                                    "Portfolio monitor",
                                    "off stops the background poll loop",
                                    control=self.gates["monitor_enabled"],
                                ),
                                *[
                                    self._field_row(k, label, hint)
                                    for k, label, hint in PACING_FIELDS
                                ],
                                self._field_row(
                                    "monitor_extended_hours",
                                    "Review premarket / postmarket",
                                    "monitor reviews outside RTH",
                                    control=self.gates["monitor_extended_hours"],
                                ),
                            ),
                            self._section(
                                "Connection",
                                ft.Text(
                                    "Paper / live and the port are one decision behind the "
                                    "confirm phrase — they are not fields here.",
                                    size=11,
                                    color=MUTED,
                                ),
                                ft.Row(
                                    [
                                        ft.Text("Mode", size=12, color=TEXT, expand=True),
                                        self.lbl_settings_mode,
                                        self._btn(
                                            "Switch paper / live",
                                            outlined=True,
                                            on_click=self._toggle_trading_mode,
                                        ),
                                    ],
                                    spacing=8,
                                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                                ),
                                self.lbl_settings_link,
                                *[self._field_row(k, label, hint) for k, label, hint in LINK_FIELDS],
                            ),
                            self._section("Persisted to", self.lbl_settings_path),
                        ],
                        spacing=0,
                        scroll=ft.ScrollMode.AUTO,
                        expand=True,
                    ),
                ),
            ],
            spacing=0,
            expand=True,
        )
