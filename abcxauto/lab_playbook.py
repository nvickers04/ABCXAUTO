"""One playbook tree. The socket (7497 paper TWS vs 7496 live TWS) is the live switch.

Promote contract: ``abcxauto.playbook.promote`` (``retire_if.sample`` plus one
numeric kill; ``conservative_pnl``; paper mids cannot graduate). Card schema:
``abcxauto.playbook.schema``. Persistence: ``abcxauto.playbook.persist``.
Live-card notes: ``abcxauto.playbook.live_cards``. Public names stay on this module.


One tree, two layers, both written by Grok::

    TYPE market_bracket            <- durable: tool_order, gotchas, review
      |- card: mega-cap earnings-flush bounce   <- thesis, evidence, retire_if
      |- card: opening-range continuation
    TYPE vertical_spread
      |- card: post-earnings IV crush

* the **trunk** is ``types``: one entry per sendable ORDER_EXAMPLES key holding
  what Grok learned about *executing that structure* — the tool sequence that
  works, the execution gotchas, how it reviews the result. Durable, changes
  slowly. The clerk never writes schema here: ``ORDER EXAMPLES`` is already in
  the prompt, and restating it was how ~40% of the old notebook became
  boilerplate. See ``type_schema_echo_keys``.
* the **branches** are that type's ``cards``: disposable hypotheses, each
  carrying its thesis, the evidence that produced it, and the falsification it
  declares for itself (``retire_if``). Numerous, tested, retired.
* **locked OPEN starters** fill a trunk that has no hunting card so the book
  is not three flush cards plus empty slots. Seeded on lab load/save only.
  Live snapshots are never seeded. ``locked`` is clerk seed identity — not a
  hunt floor, not a send stamp, not a freeze. Grok rewrites the same name;
  a named write drops ``locked`` so the upgrade can hunt. Virgin starters stay
  visible as unused type names on the wake and run sheet. Retired stay off.

A card's position in the tree *is* its ticket, so a winning card sits inside
the type entry it is supposed to improve — promoting what it learned is a move
within one stanza, not a join across two lists. Card identity is therefore
``(type, name)``, not a bare name.

The clerk's job is attribution, not authorship: a named ``params.card`` tags
the fill so the card is scored on its own resolved trades. New risk must name
an existing card (scorecard label). Notebook prose is not a send gate — hold,
gap, tape, session, and book sentences cannot invent a refuse. Operator
flattens, panic, and halt exits are tracked but kept out of the graduation
math — an interrupted trade is neither proof nor falsification. A card
graduates only on a conservative fill assumption (not ``paper_mid``), a
computed ``conservative_pnl`` (debit at ask / credit at bid, or fill vs
NBBO), its own ``retire_if.sample``, one numeric kill (``max_losses`` or
``max_loss_usd``), a thesis, and positive conservative edge. Paper TWS
``realized_pnl`` is a fact, not the verdict input. Fees sit in that P&L;
model cost stays on honesty. Graduated cards can still snapshot to
``playbook_live.json`` as an operator live-enable. New risk is new risk on
either socket.

A flat top-level ``cards`` list is still accepted on a write and is still
*projected* on a read for the cockpit and older callers, but the tree is the
only thing stored. See ``_migrate_book`` and ``_flat_card_projection``.

Notebook is not executable, not a standing order. Clerk cadence is
overnight park only — a card does not set a look clock. Clerk validates
writes against gates (floors / live / sleeve) like self_tune.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from abcxauto.path_math import commission_cost, conservative_trade_pnl, quote_bid_ask
from abcxauto.playbook.schema import *  # noqa: F401,F403
from abcxauto.playbook.persist import *  # noqa: F401,F403
from abcxauto.playbook.promote import *  # noqa: F401,F403
from abcxauto.playbook.live_cards import *  # noqa: F401,F403

logger = logging.getLogger(__name__)

def _card_log_path() -> Path:
    import os

    raw = (os.environ.get("ABCXAUTO_CARD_LOG_PATH") or "").strip()
    if raw:
        return Path(raw)
    return _REPO_ROOT / "data" / "state" / "card_sends.jsonl"


def _send_is_new_risk(strategy: str, params: dict[str, Any] | None) -> bool:
    """Reuse the clerk's own predicate so sample counting matches the gate."""
    try:
        from abcxauto.agent_loop import is_new_risk

        return bool(is_new_risk(strategy, params))
    except Exception:
        return False


def card_types_by_name(state: dict[str, Any] | None = None) -> dict[str, list[str]]:
    """``name`` -> the types it branches under. More than one is ambiguous."""
    blob = state if isinstance(state, dict) else load_lab()
    out: dict[str, list[str]] = {}
    for type_name, card in walk_cards(blob):
        key = str(card.get("name") or "").strip().lower()
        if not key:
            continue
        seen = out.setdefault(key, [])
        if type_name and type_name not in seen:
            seen.append(type_name)
    return out


def resolve_card_type(
    card: Any,
    *,
    strategy: str = "",
    state: dict[str, Any] | None = None,
) -> tuple[str, bool]:
    """Which type a card send belongs to. Returns ``(type, ambiguous)``.

    A new-risk send is unambiguous by construction: the gate already proved the
    card lives under the strategy being sent. Anything else â€” a management
    ticket, or a row written before cards were nested â€” is resolved by name,
    and only when exactly one type claims that name.
    """
    name = str(card or "").strip().lower()
    if not name:
        return "", False
    index = card_types_by_name(state)
    types = index.get(name) or []
    want = str(strategy or "").strip().lower()
    if want and want in types:
        return want, False
    if len(types) == 1:
        return types[0], False
    if len(types) > 1:
        return "", True
    return "", False


def record_card_send(
    *,
    card: str,
    strategy: str,
    symbol: str = "",
    result: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> None:
    """Tie a dispatched ticket to the card that called for it.

    Without this the only feedback a card gets is whole-book drift, which is
    the same number for every card. ``new_risk`` separates the entry that opens
    a trade from the management tickets that follow, so a card's declared sample
    counts trades and not keystrokes. ``type`` pins the row to one branch of the
    tree, because a name alone is no longer an identity.
    """
    name = str(card or "").strip()
    if not name:
        return
    from abcxauto.memory.journal import _order_ids_from_result_json

    try:
        oids = sorted(_order_ids_from_result_json(json.dumps(result or {}, default=str)))
    except Exception:
        oids = []
    try:
        card_type, _ambiguous = resolve_card_type(name, strategy=strategy)
    except Exception:
        card_type = ""
    row = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "card": name[:120],
        "type": card_type[:60],
        "strategy": str(strategy or "")[:60],
        "symbol": str(symbol or "").upper()[:12],
        "order_ids": oids[:12],
        "new_risk": _send_is_new_risk(strategy, params),
    }
    path = _card_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except OSError:
        logger.debug("card send log write failed", exc_info=True)


def _card_sends(limit: int = 400) -> list[dict[str, Any]]:
    path = _card_log_path()
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines()[-limit:]:
            line = line.strip()
            if not line:
                continue
            try:
                blob = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(blob, dict) and blob.get("card"):
                rows.append(blob)
    except OSError:
        return []
    return rows


def resolve_send_types(
    sends: list[dict[str, Any]] | None,
    state: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Stamp every send row with its ``(type, name)`` identity.

    Rows written before cards were nested carry only a name. One resolves when
    exactly one type claims that name, or when the row's own strategy is a type
    holding it. When two types hold the name the row is marked ``ambiguous`` and
    attributed to neither: crediting a trade to a card that never asked for it
    is worse than leaving it visibly unattributed.
    """
    index = card_types_by_name(state)
    out: list[dict[str, Any]] = []
    for row in sends or []:
        if not isinstance(row, dict) or not row.get("card"):
            continue
        item = dict(row)
        have = str(item.get("type") or "").strip().lower()
        if have:
            item["type"] = have
            item["ambiguous"] = False
            out.append(item)
            continue
        name = str(item.get("card") or "").strip().lower()
        types = index.get(name) or []
        want = str(item.get("strategy") or "").strip().lower()
        if want and want in types:
            item["type"], item["ambiguous"] = want, False
        elif len(types) == 1:
            item["type"], item["ambiguous"] = types[0], False
        else:
            item["type"], item["ambiguous"] = "", len(types) > 1
        out.append(item)
    return out


def _send_oids(row: dict[str, Any]) -> list[int]:
    out: list[int] = []
    for oid in row.get("order_ids") or []:
        try:
            out.append(int(oid))
        except (TypeError, ValueError):
            continue
    return out


def _ts_num(raw: Any) -> float:
    """Epoch seconds from an ISO stamp. Fills say ``Z``, card sends say ``+00:00``."""
    text = str(raw or "").strip()
    if not text:
        return 0.0
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.timestamp()


_SEND_BID_KEYS = ("bid", "nbbo_bid", "bid_at_send", "send_bid")
_SEND_ASK_KEYS = ("ask", "nbbo_ask", "ask_at_send", "send_ask")
_SEND_LAST_KEYS = ("ibkr_last", "last", "mid")


def _entry_send_oid(send: dict[str, Any]) -> int | None:
    oids = _send_oids(send)
    return oids[0] if oids else None


def _usable_spread(row: dict[str, Any] | None) -> bool:
    bid, ask = quote_bid_ask(row)
    return bid is not None and ask is not None and (ask - bid) > 1e-9


def _copy_quote_side(dst: dict[str, Any], src: dict[str, Any], side: str, keys: tuple[str, ...]) -> None:
    if dst.get(side) is not None:
        return
    for key in keys:
        if src.get(key) is not None:
            dst[side] = src.get(key)
            return


def _overlay_send_quotes(
    fill: dict[str, Any],
    send: dict[str, Any],
    *,
    marks_by_oid: dict[int, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Copy this send's NBBO onto its own entry fill. Closers keep their quotes."""
    row = dict(fill)
    try:
        oid = int(fill.get("order_id"))
    except (TypeError, ValueError):
        return row
    src: dict[str, Any] | None = None
    mark = (marks_by_oid or {}).get(oid)
    if isinstance(mark, dict):
        src = mark
    elif oid == _entry_send_oid(send):
        src = send
    if not isinstance(src, dict):
        return row
    if not _usable_spread(row):
        src_bid = next((src.get(k) for k in _SEND_BID_KEYS if src.get(k) is not None), None)
        src_ask = next((src.get(k) for k in _SEND_ASK_KEYS if src.get(k) is not None), None)
        try:
            src_spread = (
                src_bid is not None
                and src_ask is not None
                and float(src_ask) - float(src_bid) > 1e-9
            )
        except (TypeError, ValueError):
            src_spread = False
        if src_spread:
            row["bid"] = src_bid
            row["ask"] = src_ask
        else:
            _copy_quote_side(row, src, "bid", _SEND_BID_KEYS)
            _copy_quote_side(row, src, "ask", _SEND_ASK_KEYS)
    if row.get("ibkr_last") is None:
        for key in _SEND_LAST_KEYS:
            if src.get(key) is not None:
                row["ibkr_last"] = src.get(key)
                break
    if not row.get("fill_label") and src.get("fill_label"):
        row["fill_label"] = src.get("fill_label")
    return row


def _related_fills(
    send: dict[str, Any],
    closer: dict[str, Any],
    all_fills: list[dict[str, Any]] | None,
    *,
    send_marks: dict[int, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Entry + exit prints for one card trade. Closer is always included."""
    own = set(_send_oids(send))
    try:
        closer_oid = int(closer["order_id"])
    except (TypeError, ValueError, KeyError):
        closer_oid = None
    if closer_oid is not None:
        own.add(closer_oid)
    sym = str(closer.get("symbol") or send.get("symbol") or "").upper()
    opened = _ts_num(send.get("ts"))
    closed = _ts_num(closer.get("ts"))
    out: list[dict[str, Any]] = []
    seen: set[Any] = set()
    for fill in all_fills or []:
        if not isinstance(fill, dict):
            continue
        try:
            oid = int(fill["order_id"])
        except (TypeError, ValueError, KeyError):
            continue
        if oid not in own:
            continue
        if sym and str(fill.get("symbol") or "").upper() != sym:
            continue
        t = _ts_num(fill.get("ts"))
        if opened and t and t < opened - 2.0:
            continue
        if closed and t and t > closed + 2.0:
            continue
        key = fill.get("exec_id") or (oid, str(fill.get("ts") or ""), fill.get("side"))
        if key in seen:
            continue
        seen.add(key)
        out.append(_overlay_send_quotes(fill, send, marks_by_oid=send_marks))
    closer_exec = closer.get("exec_id")
    closer_ts = str(closer.get("ts") or "")
    already = False
    for fill in out:
        if closer_exec and fill.get("exec_id") == closer_exec:
            already = True
            break
        if (
            closer_oid is not None
            and fill.get("order_id") == closer_oid
            and str(fill.get("ts") or "") == closer_ts
        ):
            already = True
            break
    if not already:
        out.append(_overlay_send_quotes(closer, send, marks_by_oid=send_marks))
    return out


def _net_trade_realized(
    closer: dict[str, Any], related: list[dict[str, Any]]
) -> float:
    """Raw IBKR close print minus every commission on the round trip."""
    try:
        gross = float(closer.get("realized_pnl") or 0.0)
    except (TypeError, ValueError):
        gross = 0.0
    fees = 0.0
    seen: set[Any] = set()
    for fill in related or [closer]:
        if not isinstance(fill, dict):
            continue
        key = fill.get("exec_id") or id(fill)
        if key in seen:
            continue
        seen.add(key)
        fees += commission_cost(fill)
    if not seen:
        fees = commission_cost(closer)
    return round(gross - fees, 4)


def classify_card_trades(
    sends: list[dict[str, Any]] | None,
    fills: list[dict[str, Any]] | None,
    dispatched: set | None = None,
    *,
    all_fills: list[dict[str, Any]] | None = None,
    send_marks: dict[int, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """One row per new-risk card send: how that trade actually ended.

    ``protective`` â€” the card's own stop/target filled.
    ``decision``   â€” a dispatched ticket closed it (Grok's call).
    ``operator``   â€” the exit has no dispatch behind it: a manual TWS flatten,
                     another client session, or the panic/halt path. The card's
                     thesis was interrupted, so it is neither proof nor
                     falsification and never counts toward the declared sample.
    ``open``       â€” nothing has closed it yet.
    """
    rows = [r for r in (sends or []) if isinstance(r, dict) and r.get("card")]
    rows.sort(key=lambda r: _ts_num(r.get("ts")))
    placed = set(dispatched or set())
    # Which send produced each order id, so a fill can be traced to its ticket.
    owner: dict[int, dict[str, Any]] = {}
    for row in rows:
        for oid in _send_oids(row):
            owner.setdefault(oid, row)
    closers = [
        f
        for f in (fills or [])
        if isinstance(f, dict) and f.get("order_id") is not None
    ]
    closers.sort(key=lambda f: _ts_num(f.get("ts")))
    used: set[int] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        if row.get("new_risk") is False:
            continue
        if row.get("new_risk") is None and not _send_is_new_risk(
            str(row.get("strategy") or ""), None
        ):
            continue
        sym = str(row.get("symbol") or "").upper()
        ts = str(row.get("ts") or "")
        opened = _ts_num(ts)
        own = set(_send_oids(row))
        trade: dict[str, Any] = {
            "card": str(row.get("card") or ""),
            "type": str(row.get("type") or ""),
            "ambiguous": bool(row.get("ambiguous")),
            "symbol": sym,
            "strategy": str(row.get("strategy") or ""),
            "opened_at": ts,
            "exit": EXIT_OPEN,
            "realized_pnl": None,
            "conservative_pnl": None,
            "exit_order_id": None,
            "exit_at": None,
        }
        for idx, fill in enumerate(closers):
            if idx in used:
                continue
            if sym and str(fill.get("symbol") or "").upper() != sym:
                continue
            if opened and _ts_num(fill.get("ts")) < opened:
                continue
            oid = int(fill["order_id"])
            src = owner.get(oid)
            if oid in own:
                kind = EXIT_PROTECTIVE
            elif src is not None or oid in placed:
                kind = EXIT_DECISION
            else:
                kind = EXIT_OPERATOR
            used.add(idx)
            related = _related_fills(
                row, fill, all_fills, send_marks=send_marks
            )
            cons = conservative_trade_pnl(related)
            trade.update(
                exit=kind,
                realized_pnl=_net_trade_realized(fill, related),
                conservative_pnl=(
                    round(float(cons), 4) if cons is not None else None
                ),
                exit_order_id=oid,
                exit_at=fill.get("ts"),
            )
            break
        out.append(trade)
    return out


def _journal_exit_facts() -> tuple[
    list[dict[str, Any]], set, list[dict[str, Any]], dict[int, dict[str, Any]]
]:
    """Closing fills, dispatched order ids, every fill, and send NBBO by oid."""
    fills: list[dict[str, Any]] = []
    placed: set = set()
    all_fills: list[dict[str, Any]] = []
    send_marks: dict[int, dict[str, Any]] = {}
    try:
        from abcxauto.memory import get_journal

        journal = get_journal()
        fn = getattr(journal, "closing_fills", None)
        if callable(fn):
            fills = [f for f in (fn() or []) if isinstance(f, dict)]
        fn = getattr(journal, "dispatched_order_ids", None)
        if callable(fn):
            placed = set(fn() or set())
        fn = getattr(journal, "listed_fills", None)
        if callable(fn):
            all_fills = [f for f in (fn() or []) if isinstance(f, dict)]
        fn = getattr(journal, "send_marks_by_order_id", None)
        if callable(fn):
            raw = fn() or {}
            if isinstance(raw, dict):
                for key, val in raw.items():
                    try:
                        send_marks[int(key)] = val if isinstance(val, dict) else {}
                    except (TypeError, ValueError):
                        continue
    except Exception:
        logger.debug("journal exit facts unavailable", exc_info=True)
    return fills, placed, all_fills, send_marks


def _empty_score(card: str = "", card_type: str = "") -> dict[str, Any]:
    return {
        "card": card,
        "type": card_type,
        "sends": 0,
        "realized_pnl": 0.0,
        "attributed_fills": 0,
        "last_send": None,
        "symbols": [],
        "trades": 0,
        "resolved": 0,
        "interrupted": 0,
        "open": 0,
        "resolved_pnl": 0.0,
        "conservative_pnl": None,
        "interrupted_pnl": 0.0,
        "resolved_wins": 0,
        "resolved_losses": 0,
        "ambiguous_sends": 0,
        "exits": {k: 0 for k in CARD_EXIT_KINDS},
        "open_trades": [],
    }


def card_scores(cards: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Per-card attribution: what it sent, what resolved, what interrupted it.

    Buckets are keyed by ``(type, name)``, so the same setup name under two
    order types scores as the two different experiments it is. ``realized_pnl``
    is every dollar the card's own order ids booked â€” the book number has to
    reconcile. ``resolved_pnl`` is net of commissions on trades whose exit
    was the card's own protection or a dispatched decision. ``conservative_pnl``
    is the graduation number: debit at ask / credit at bid (or fill vs NBBO),
    also net of fees. Paper TWS realized is not that number.
    """
    raw_sends = _card_sends()
    if not raw_sends:
        return []
    by_key: dict[tuple[str, str], dict[str, Any]] = {}
    by_name: dict[str, list[dict[str, Any]]] = {}
    for c in cards or []:
        if not isinstance(c, dict) or not c.get("name"):
            continue
        name = str(c["name"]).strip().lower()
        by_key[card_key(c.get("type") or c.get("ticket"), name)] = c
        by_name.setdefault(name, []).append(c)
    state: dict[str, Any] | None = None
    if cards is not None:
        # Resolve legacy name-only rows against the book we were handed, not
        # whatever happens to be on disk.
        tree: dict[str, Any] = {}
        for c in cards:
            if not isinstance(c, dict) or not c.get("name"):
                continue
            parent = str(c.get("type") or c.get("ticket") or "").strip().lower()
            if parent:
                tree.setdefault(parent, {}).setdefault("cards", []).append(c)
        state = {"types": tree}
    sends = resolve_send_types(raw_sends, state)
    realized: dict[int, float] = {}
    try:
        from abcxauto.memory import get_journal

        journal = get_journal()
        fn = getattr(journal, "realized_by_order_id", None)
        if callable(fn):
            realized = dict(fn() or {})
    except Exception:
        realized = {}
    fills, placed, all_fills, send_marks = _journal_exit_facts()
    trades = classify_card_trades(
        sends, fills, placed, all_fills=all_fills, send_marks=send_marks
    )
    buckets: dict[tuple[str, str], dict[str, Any]] = {}

    def _bucket(card_type: Any, name: str) -> dict[str, Any]:
        key = card_key(card_type, name)
        return buckets.setdefault(key, _empty_score(name, key[0]))

    cons_sum: dict[tuple[str, str], float] = {}
    cons_missing: set[tuple[str, str]] = set()
    cons_n: dict[tuple[str, str], int] = {}

    for row in sends:
        name = str(row.get("card") or "")
        bucket = _bucket(row.get("type"), name)
        bucket["sends"] += 1
        if row.get("ambiguous"):
            bucket["ambiguous_sends"] += 1
        bucket["last_send"] = row.get("ts") or bucket["last_send"]
        sym = str(row.get("symbol") or "")
        if sym and sym not in bucket["symbols"]:
            bucket["symbols"].append(sym)
        for oid in _send_oids(row):
            if oid in realized:
                bucket["realized_pnl"] += float(realized[oid])
                bucket["attributed_fills"] += 1
    for trade in trades:
        bucket = _bucket(trade.get("type"), str(trade.get("card") or ""))
        kind = str(trade.get("exit") or EXIT_OPEN)
        bucket["trades"] += 1
        bucket["exits"][kind] = bucket["exits"].get(kind, 0) + 1
        pnl = trade.get("realized_pnl")
        pnl_f = float(pnl) if isinstance(pnl, (int, float)) else 0.0
        if kind in RESOLVED_EXITS:
            bucket["resolved"] += 1
            bucket["resolved_pnl"] += pnl_f
            if pnl_f < 0:
                bucket["resolved_losses"] += 1
            elif pnl_f > 0:
                bucket["resolved_wins"] += 1
            key = card_key(trade.get("type"), str(trade.get("card") or ""))
            cons_n[key] = cons_n.get(key, 0) + 1
            cp = trade.get("conservative_pnl")
            if isinstance(cp, (int, float)):
                cons_sum[key] = cons_sum.get(key, 0.0) + float(cp)
            else:
                cons_missing.add(key)
        elif kind == EXIT_OPERATOR:
            bucket["interrupted"] += 1
            bucket["interrupted_pnl"] += pnl_f
        else:
            bucket["open"] += 1
            bucket.setdefault("open_trades", [])
            bucket["open_trades"].append(
                {
                    "opened_at": trade.get("opened_at"),
                    "symbol": trade.get("symbol"),
                }
            )
    out: list[dict[str, Any]] = []
    for (card_type, name), bucket in buckets.items():
        for key in ("realized_pnl", "resolved_pnl", "interrupted_pnl"):
            bucket[key] = round(float(bucket[key]), 4)
        ck = (card_type, name)
        if ck in cons_missing or cons_n.get(ck, 0) == 0:
            bucket["conservative_pnl"] = None
        else:
            bucket["conservative_pnl"] = round(float(cons_sum.get(ck, 0.0)), 4)
        bucket["symbols"] = bucket["symbols"][:8]
        match = by_key.get((card_type, name))
        if match is None:
            same = by_name.get(name) or []
            match = same[0] if len(same) == 1 else None
        if by_key:
            bucket["on_current_book"] = match is not None
        bucket.update(card_verdict(bucket, match))
        out.append(bucket)
    return sorted(
        out,
        key=lambda b: (-int(b.get("sends") or 0), str(b.get("card")), str(b.get("type"))),
    )

def strategy_scores() -> list[dict[str, Any]]:
    """Realized P&L per sendable strategy, from the journal's fills join."""
    try:
        from abcxauto.memory import get_journal

        rows = get_journal().strategy_performance() or []
    except Exception:
        return []
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        out.append(
            {
                "strategy": row.get("strategy"),
                "n_fills": row.get("n_fills"),
                "realized_pnl": round(float(row.get("realized_pnl_sum") or 0.0), 4),
                "commissions": round(float(row.get("commissions_sum") or 0.0), 4),
                "last_fill_ts": row.get("last_fill_ts"),
            }
        )
    return sorted(out, key=lambda r: float(r.get("realized_pnl") or 0.0), reverse=True)

def _reject_note(rejected: dict[str, str]) -> str:
    if "unknown_type" in rejected:
        return rejected["unknown_type"]
    if "hypothesis_cap" in rejected:
        return rejected["hypothesis_cap"]
    if "invented_pct_gate" in rejected and set(rejected) <= {"invented_pct_gate"}:
        return "notebook cannot invent a % gate"
    if "ticker_list" in rejected:
        return rejected["ticker_list"]
    if "diary" in rejected:
        return rejected["diary"]
    if "shape" in rejected:
        return rejected["shape"]
    if "invented_pct_gate" in rejected:
        return "notebook cannot invent a % gate"
    return "notebook cannot loosen gates"


def apply_from_judgment(judgment: dict[str, Any] | None) -> dict[str, Any] | None:
    """Paper: persist Grok's notebook. Live: ignore writes.

    Gate knobs in the payload are rejected (not applied). Notes still save.
    Diary / ticker-list / unknown-type writes do not save.
    """
    if not judgment or not is_paper():
        return None
    raw = judgment.get("lab_playbook") or judgment.get("playbook")
    rejected = dict(gate_rejects(raw))
    rejected.update(book_shape_rejects(raw))
    if _HARD_SHAPE.intersection(rejected):
        return {
            "status": "rejected",
            "rejected": rejected,
            "note": _reject_note(rejected),
        }
    update = clamp_update(raw)
    if not update:
        if rejected:
            note = "notebook cannot loosen gates"
            if "invented_pct_gate" in rejected:
                note = "notebook cannot invent a % gate"
            return {
                "status": "rejected",
                "rejected": rejected,
                "note": _reject_note(rejected),
            }
        return None
    prev = load_lab()
    score = None
    try:
        from abcxauto.scorecard import compute_scorecard

        score = compute_scorecard()
    except Exception:
        score = None
    state = save_lab(
        update,
        scorecard=score,
        persist_instructions="invented_pct_gate" in rejected,
    )
    maybe_promote(scorecard=score)
    out = dict(state)
    facts = card_facts(state)
    if state.get("revision_held"):
        out["revision_held"] = True
        out.setdefault("note", "book unchanged — revision held")
    out["cards"] = _flat_card_projection(state)
    out["graduated_cards"] = [_card_label(r) for r in facts if r.get("graduated")]
    out["tripped_cards"] = [_card_label(r) for r in facts if r.get("tripped")]
    out["needs_declaration"] = [
        _card_label(r) for r in facts if _owes_declaration(r)
    ]
    if rejected:
        out["rejected"] = rejected
        if "invented_pct_gate" in rejected:
            out["note"] = "notes saved; invented % gate lines stripped"
        else:
            out["note"] = "notes saved; gate knobs ignored"
    return out


def playbook_is_stale(
    lab: dict[str, Any] | None = None,
    *,
    now: datetime | None = None,
) -> bool:
    """True when a standing card is older than the lab rewrite cadence."""
    state = lab if isinstance(lab, dict) else load_lab()
    if not _has_book(state):
        return False
    age = playbook_age_hours(state, now=now)
    return age is not None and age >= stale_hours()


def _window_edges(scorecard: dict[str, Any] | None) -> dict[str, Any]:
    wins = (scorecard or {}).get("windows") if isinstance(scorecard, dict) else None
    if not isinstance(wins, dict):
        wins = {}
    out: dict[str, Any] = {}
    for label in _CARD_WINDOWS:
        row = wins.get(label) if isinstance(wins.get(label), dict) else {}
        out[f"win_{label}"] = row.get("edge_usd")
        out[f"win_{label}_beat"] = row.get("beating_model")
    return out


def _since_write_score(
    lab: dict[str, Any],
    scorecard: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Book vs model since this card was written. Inception hole stays on the scorecard."""
    written = str(lab.get("written_at") or "")
    now_nl = (scorecard or {}).get("net_liquidation") if isinstance(scorecard, dict) else None
    empty = {"since_write_edge": None, "since_write_pnl": None, "since_write_cost": None}
    if not written:
        return empty
    try:
        from abcxauto.memory import get_journal

        journal = get_journal()
    except Exception:
        return empty
    start_nl, _start_ts = None, None
    try:
        if hasattr(journal, "nav_at_or_before"):
            start_nl, _start_ts = journal.nav_at_or_before(written)
    except Exception:
        start_nl = None
    usage = {}
    try:
        if hasattr(journal, "model_usage_since"):
            usage = dict(journal.model_usage_since(written) or {})
    except Exception:
        usage = {}
    cost = float(usage.get("cost_usd") or 0.0)
    try:
        now_f = float(now_nl) if now_nl is not None else None
        start_f = float(start_nl) if start_nl is not None else None
    except (TypeError, ValueError):
        return {**empty, "since_write_cost": cost}
    if now_f is None or start_f is None or start_f <= 0:
        return {**empty, "since_write_cost": cost}
    pnl = now_f - start_f
    return {
        "since_write_edge": pnl - cost,
        "since_write_pnl": pnl,
        "since_write_cost": cost,
    }


def _parse_card_clock(raw: Any) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _hypothesis_clock(lab: dict[str, Any] | None) -> str:
    """Newest testing card clock. Lab written_at is a diary stamp after holds.

    First-card-only made a 5-day flush sibling keep ``book_stale`` on after a
    same-day card was written and sent — Grok then wrote the book every look.
    """
    state = lab if isinstance(lab, dict) else {}
    best = ""
    best_dt: datetime | None = None
    for _type_name, card in walk_cards(state):
        if not _is_live_hypothesis(card):
            continue
        clock = str(card.get("written_at") or "").strip()
        dt = _parse_card_clock(clock)
        if dt is None:
            continue
        if best_dt is None or dt > best_dt:
            best_dt = dt
            best = clock
    return best or str(state.get("written_at") or "").strip()


def playbook_age_hours(
    lab: dict[str, Any] | None = None,
    *,
    now: datetime | None = None,
) -> float | None:
    raw = _hypothesis_clock(lab)
    if not raw:
        return None
    try:
        written = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if written.tzinfo is None:
        written = written.replace(tzinfo=timezone.utc)
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=timezone.utc)
    return max(0.0, (clock - written).total_seconds() / 3600.0)

def playbook_run_sheets(
    book: dict[str, Any] | None = None,
    *,
    tool_trace: list[str] | None = None,
    last_look: list[str] | None = None,
    flat: bool | None = None,
    quoted: Any = None,
    news: Any = None,
    session_today: bool | None = None,
    session_range: Any = None,
    positions: Any = None,
) -> list[dict[str, Any]]:
    """Open cards as notes. No next= tool and no hunt send assignment.

    Lock is seed identity — a locked starter stays on the sheet as an unused
    type. Retired stay off. tool_order is notebook, not clerk next=.
    """
    _ = (tool_trace, last_look, quoted, news, session_today, session_range, positions)
    state = book if isinstance(book, dict) else load_lab()
    types = state.get("types") if isinstance(state.get("types"), dict) else {}
    managing = flat is False
    scored = {
        card_key(row.get("type"), row.get("card")): row
        for row in card_facts(state)
    }
    out: list[dict[str, Any]] = []
    for type_name, card in walk_cards(state):
        if not type_name:
            continue
        if not _is_open_notebook_card(card):
            continue
        status = str(card.get("status") or "testing").strip().lower()
        stanza = types.get(type_name) if isinstance(types.get(type_name), dict) else {}
        parent_order = _norm_recipe(
            stanza.get("tool_order") or stanza.get("default_tool_recipe")
        )
        card_order = _norm_recipe(
            card.get("tool_order") or card.get("default_tool_recipe")
        )
        review = str(stanza.get("review") or card.get("review") or "").strip()
        if managing:
            # Review is prose, not a recipe. A line that only names book
            # must not hide fills/quote/candles.
            order = list(_DEFAULT_MANAGE_ORDER)
        else:
            order = card_order or parent_order or list(_DEFAULT_HUNT_ORDER)
        score = scored.get(card_key(type_name, card.get("name"))) or {}
        row: dict[str, Any] = {
            "type": type_name,
            "card": card.get("name"),
            "status": status,
            "tool_order": order,
            "sends": int(score.get("sends") or 0),
            "resolved": int(score.get("resolved") or 0),
        }
        if card.get("locked") is True:
            row["locked"] = True
        if score.get("sample_left") is not None:
            row["sample_left"] = score.get("sample_left")
        if managing and review:
            row["review"] = review[:240]
        out.append(row)
        if len(out) >= 24:
            break
    return out


def playbook_glance(scorecard: dict[str, Any] | None = None) -> dict[str, Any]:
    """Score since the last write. Not the notebook text â€” Grok asks playbook() for that."""
    facts = playbook_facts(scorecard)
    out = {
        "revision": facts.get("revision"),
        "age_h": facts.get("age_h"),
        "since_write_edge": facts.get("since_write_edge"),
        "now_edge": facts.get("now_edge"),
        "now_edge_pct_of_nl": facts.get("now_edge_pct_of_nl"),
        "now_beating": facts.get("now_beating"),
        "win_4h": facts.get("win_4h"),
        "lots_at_write": list(facts.get("lots_at_write") or [])[:16],
        "stale": playbook_is_stale(),
    }
    try:
        from abcxauto.mode_size import mode_size_band

        out["mode"] = playbook_mode()
        out["mode_size"] = mode_size_band()
    except Exception:
        pass
    return out


def _MANAGEMENT_TRUNKS() -> frozenset[str]:
    """Trunks that adjust or close existing risk rather than open it."""
    from abcxauto.proposals import MANAGEMENT_STRATEGIES

    return MANAGEMENT_STRATEGIES | frozenset({"close_option"})


def lab_facts(
    book: dict[str, Any] | None = None,
    *,
    rows: list[dict[str, Any]] | None = None,
    hide_types: frozenset[str] | set[str] | None = None,
) -> dict[str, Any]:
    """What the lab has under test, and which trunks carry no hypothesis.

    The other half of ``strategy_scores``. That key reports what already traded
    and what it earned, so on its own the only breadth signal on this surface is
    the P&L of what ran — which reads as a reason to run it again. This reports
    what has never been tried. Counts and names only: which structure to test is
    the notebook's business, not the clerk's.

    Locked OPEN starters fill empty trunks so the book is not three flush
    cards plus blank slots. They count in ``cards`` but are not the wake —
    Grok's own hypotheses stay on ``cards_awaiting_first_trade``.

    Only entry structures count as untried. ``modify_stop`` and ``cancel_order``
    adjust risk that already exists, so listing them as gaps would invite a
    hypothesis about cancelling an order.
    """
    state = book if isinstance(book, dict) else load_lab()
    card_rows = rows if rows is not None else card_facts(state)
    by_status = {status: 0 for status in CARD_STATUSES}
    awaiting: list[dict[str, Any]] = []
    idle: list[dict[str, Any]] = []
    resolved_total = 0
    for row in card_rows:
        status = str(row.get("status") or "testing").strip().lower()
        if status in by_status:
            by_status[status] += 1
        n = int(row.get("resolved") or 0)
        resolved_total += n
        if status == "retired":
            continue
        wait_row = {"card": _card_label(row)}
        idle.append(wait_row)
        if n == 0:
            awaiting.append(wait_row)
    skip = {str(x).strip().lower() for x in (hide_types or ()) if str(x).strip()}
    coverage = [
        r for r in type_coverage(state)
        if str(r.get("type") or "").strip().lower() not in skip
    ]
    out = {
        "cards": by_status,
        "resolved_trades": resolved_total,
        "cards_awaiting_first_trade": awaiting[:12],
        "cards_without_trigger": idle[:12],
        "unused_open_types": unused_open_types(
            state, rows=card_rows, hide_types=hide_types
        ),
        "trunks_with_cards": [r["type"] for r in coverage if r["cards"]],
        "entry_trunks_untried": [
            r["type"] for r in coverage
            if not r["cards"] and r["type"] not in _MANAGEMENT_TRUNKS()
        ],
    }
    return out


def unused_open_types(
    book: dict[str, Any] | None = None,
    *,
    rows: list[dict[str, Any]] | None = None,
    hide_types: frozenset[str] | set[str] | None = None,
) -> list[str]:
    """OPEN types with no send. Names only — not a look tally."""
    card_rows = rows if rows is not None else card_facts(book)
    sent: set[str] = set()
    skip = {str(x).strip().lower() for x in (hide_types or ()) if str(x).strip()}
    for row in card_rows:
        try:
            n = int(row.get("sends") or 0)
        except (TypeError, ValueError):
            n = 0
        if n <= 0:
            continue
        parent = str(row.get("type") or "").strip()
        if parent:
            sent.add(parent)
    return [
        name for name in open_playbook_types()
        if name not in sent and name not in skip
    ]


def lab_wake_bit(
    book: dict[str, Any] | None = None,
    *,
    tool_trace: list[str] | None = None,
    last_look: list[str] | None = None,
    flat: bool | None = None,
    quoted: Any = None,
    session_range: Any = None,
    positions: Any = None,
) -> str:
    """Clerk does not assign unused types as the look. Names stay on playbook()."""
    _ = (book, tool_trace, last_look, flat, quoted, session_range, positions)
    return ""


def playbook_facts(scorecard: dict[str, Any] | None = None) -> dict[str, Any]:
    """Forest vs the last write: age and score at write vs now. No lecture."""
    from abcxauto.world_state import pct_of_nl

    lab = load_lab()
    inst = notebook_text(lab)
    at_write = lab.get("paper_score") if isinstance(lab.get("paper_score"), dict) else {}
    now_sc = scorecard if isinstance(scorecard, dict) else {}
    age = playbook_age_hours(lab)
    ledger = [_compact_card(r) for r in ensure_ledger(lab)]
    since = _since_write_score(lab, now_sc)
    nl = now_sc.get("net_liquidation")
    at_edge = at_write.get("edge_usd")
    now_edge = now_sc.get("edge_usd")
    since_edge = since.get("since_write_edge")
    facts = {
        "revision": lab.get("revision"),
        "mode": lab.get("mode") or None,
        "has_instructions": bool(inst),
        "ready_to_promote": bool(lab.get("ready_to_promote")) if inst else None,
        "age_h": round(age, 1) if age is not None else None,
        "at_write_edge": at_edge,
        "at_write_edge_pct_of_nl": pct_of_nl(at_edge, nl),
        "at_write_beating": at_write.get("beating_model"),
        "now_edge": now_edge,
        "now_edge_pct_of_nl": pct_of_nl(now_edge, nl),
        "now_beating": now_sc.get("beating_model"),
        "since_write_edge": since_edge,
        "since_write_edge_pct_of_nl": pct_of_nl(since_edge, nl),
        "since_write_pnl": since.get("since_write_pnl"),
        "lots_at_write": [str(x) for x in (lab.get("lots_at_write") or [])][:16],
        "ledger": ledger[-8:],
    }
    facts.update(_window_edges(now_sc))
    halt = _clerk_halt_slice(now_sc)
    facts.update(halt)
    if isinstance(halt, dict):
        facts["halt_trips_at_pct_of_nl"] = pct_of_nl(halt.get("halt_trips_at_usd"), nl)
        facts["ibkr_day_vs_halt_pct_of_nl"] = pct_of_nl(halt.get("ibkr_day_vs_halt"), nl)
    return facts


def _clerk_halt_slice(scorecard: dict[str, Any] | None) -> dict[str, Any]:
    from abcxauto.book import clerk_halt_facts

    nl = None
    day = None
    if isinstance(scorecard, dict):
        nl = scorecard.get("net_liquidation")
        day = scorecard.get("ibkr_daily_pnl")
        if day is None:
            day = scorecard.get("daily_pnl")
    if nl is None or day is None:
        try:
            from abcxauto.memory import get_journal

            perf = get_journal().account_performance() or {}
            if nl is None:
                nl = perf.get("net_liquidation")
            if day is None:
                day = perf.get("daily_pnl")
        except Exception:
            pass
    return clerk_halt_facts(nl, day)


def format_ledger_line(facts: dict[str, Any] | None) -> str:
    rows = (facts or {}).get("ledger") if isinstance(facts, dict) else None
    if not isinstance(rows, list) or not rows:
        return ""
    bits = []
    for row in rows[-4:]:
        if not isinstance(row, dict) or row.get("revision") is None:
            continue
        bit = f"r{row.get('revision')}:{row.get('edge_usd')}"
        if row.get("closed_edge") is not None:
            bit += f">{row.get('closed_edge')}"
        bits.append(bit)
    return " ".join(bits)


def _live_scorecard(lab: dict[str, Any] | None = None) -> dict[str, Any]:
    """Current scorecard. The stamp on disk is at_write, not now."""
    try:
        from abcxauto.scorecard import compute_scorecard

        sc = compute_scorecard()
        if isinstance(sc, dict) and sc:
            return sc
    except Exception:
        pass
    state = lab if isinstance(lab, dict) else {}
    stamp = state.get("paper_score")
    return stamp if isinstance(stamp, dict) else {}


def format_block() -> str:
    """Notebook pointer. Not a look assignment and not a rev= opener."""
    tag = book_label()
    lab = load_lab()
    if not notebook_text(lab):
        return f"PLAYBOOK ({tag}): none.\n"
    return f"PLAYBOOK ({tag}): notebook. playbook tool for full text.\n"

def _lab_view_without_types(
    lab: dict[str, Any] | None,
    hidden: frozenset[str] | set[str] | None,
) -> dict[str, Any]:
    """Copy of the notebook with overlay (or other) trunks removed. Disk unchanged."""
    state = dict(lab) if isinstance(lab, dict) else {}
    skip = {str(x).strip().lower() for x in (hidden or ()) if str(x).strip()}
    if not skip:
        return state
    types = state.get("types") if isinstance(state.get("types"), dict) else {}
    state["types"] = {
        k: v for k, v in types.items() if str(k).strip().lower() not in skip
    }
    return state


def playbook_payload(
    revision: Any = None,
    *,
    full: bool = False,
    positions: list | None = None,
    orders: list | None = None,
) -> dict[str, Any]:
    """Notebook plus score since write. full is accepted and ignored â€” the notes are the tool.

    ``positions`` / ``orders`` hide overlay trunks on a last-stop-covered
    long. Omit them (desk file, tests) and the seeded starters stay.
    """
    lab = load_lab()
    hidden: frozenset[str] = frozenset()
    try:
        from abcxauto.trade_playbook import overlay_types_to_hide

        hidden = overlay_types_to_hide(positions, orders)
    except Exception:
        hidden = frozenset()
    if hidden:
        lab = _lab_view_without_types(lab, hidden)
    live_sc = _live_scorecard(lab)
    facts = playbook_facts(live_sc)
    inst = notebook_text(lab)
    types = lab.get("types") if isinstance(lab.get("types"), dict) else {}
    current: dict[str, Any] = {
        "revision": lab.get("revision") or lab.get("promoted_revision"),
        "mode": lab.get("mode"),
        "ready_to_promote": bool(lab.get("ready_to_promote")),
        "promoted": bool(lab.get("promoted")),
        "written_at": lab.get("written_at") or lab.get("promoted_at"),
        "paper_score": lab.get("paper_score") or {},
        "instructions_n": len(inst),
        "stale": playbook_is_stale(lab),
    }
    cards = _flat_card_projection(lab)
    facts_by_card = [
        {
            k: v
            for k, v in row.items()
            if k
            not in (
                "looks_without_trigger",
                "days_without_trigger",
                "max_looks_without_trigger",
                "looks",
                "days",
            )
        }
        for row in card_facts(lab)
    ]
    out: dict[str, Any] = {
        "book": book_label(),
        # Facts and the notebook before the score tables. Copying types into
        # current plus a trailing tree used to blow the 24k tool clip and
        # return broken JSON — Grok then re-hunted a card it had already
        # written.
        "lab": lab_facts(lab, rows=card_facts(lab), hide_types=hidden),
        "score": {
            "revision": facts.get("revision"),
            "age_h": facts.get("age_h"),
            "at_write_edge": facts.get("at_write_edge"),
            "now_edge": facts.get("now_edge"),
            "since_write_edge": facts.get("since_write_edge"),
            "since_write_pnl": facts.get("since_write_pnl"),
            "lots_at_write": list(facts.get("lots_at_write") or []),
            "clerk_halted": facts.get("clerk_halted"),
            "halt_kind": facts.get("halt_kind"),
            "halt_reason": facts.get("halt_reason"),
            "daily_loss_limit_pct": facts.get("daily_loss_limit_pct"),
            "halt_trips_at_usd": facts.get("halt_trips_at_usd"),
            "halt_trips_at_pct_of_nl": facts.get("halt_trips_at_pct_of_nl"),
            "ibkr_day_vs_halt": facts.get("ibkr_day_vs_halt"),
            "ibkr_day_vs_halt_pct_of_nl": facts.get("ibkr_day_vs_halt_pct_of_nl"),
            "now_edge_pct_of_nl": facts.get("now_edge_pct_of_nl"),
            "since_write_edge_pct_of_nl": facts.get("since_write_edge_pct_of_nl"),
        },
        "cards": cards,
        "tree": notebook_text(lab),
        "card_scores": facts_by_card,
        "graduated": [_card_label(r) for r in facts_by_card if r.get("graduated")],
        "tripped": [_card_label(r) for r in facts_by_card if r.get("tripped")],
        "needs_declaration": [
            _card_label(r) for r in facts_by_card if _owes_declaration(r)
        ],
        "strategy_scores": strategy_scores(),
        "unfiled_cards": list(lab.get(UNFILED_KEY) or []),
        "ledger": [_compact_card(r) for r in ensure_ledger(lab)],
        "facts": facts,
        "current": current,
        "types": types,
    }
    if revision in (None, ""):
        return out
    try:
        want = int(revision)
    except (TypeError, ValueError):
        out["error"] = "revision must be an int"
        return out
    card = revision_card(want, lab)
    if card is None:
        out["error"] = f"revision {want} not in ledger"
        return out
    out["revision"] = _outcome_card(card)
    return out
