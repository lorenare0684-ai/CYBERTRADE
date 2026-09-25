"""Asset detection + the full Quotex instrument catalog.

Covers: the static floor (size, shape, verified venue ids), live listing
parsing (``{symbol, id}`` rows, ``assets_list`` alias), quote-driven
discovery, feed/engine adoption, snapshot board payloads, the desktop asset
dropdown, and ``cybertrade quotex assets``.
"""

from __future__ import annotations

import io
import re
import unittest
from contextlib import redirect_stdout

from cybertrade.brokers.quotex.catalog import AssetCatalog, infer_kind
from cybertrade.brokers.quotex.models import QXAsset
from cybertrade.brokers.quotex.protocol import parse_instruments
from cybertrade.constants import ASSET_CATALOG, DEFAULT_ASSETS

from tests.tk_shim import fake_tk
from tests.venue_stubs import FakeQuotexAPI, make_engine

NAME_RE = re.compile(r"^[A-Za-z0-9]+(_otc)?$")
KNOWN_KINDS = {
    "forex", "forex_otc", "crypto", "crypto_otc", "metal", "metal_otc",
    "commodity", "commodity_otc", "index", "index_otc", "stock_otc",
}
# Venue ids confirmed against community clients (A11ksa/API-Quotex).
PINNED_IDS = {
    "EURUSD": 1, "XAUUSD": 2, "XAGUSD": 65, "EURUSD_otc": 66,
    "AUDUSD_otc": 71, "GBPUSD_otc": 86, "USDJPY_otc": 93,
    "EURSGD": 123, "EURSGD_otc": 303, "UKBrent_otc": 164,
    "USCrude_otc": 165, "XAUUSD_otc": 169, "MSFT_otc": 176,
    "FB_otc": 187, "INTC_otc": 190, "AXP_otc": 291, "BA_otc": 292,
    "SPXUSD": 323, "NDXUSD": 322, "DJIUSD": 317, "BRLUSD_otc": 332,
    "BTCUSD_otc": 352, "DOGUSD_otc": 353, "ETHUSD_otc": 360,
    "XRPUSD_otc": 364, "ADAUSD_otc": 376,
}


class TestStaticCatalog(unittest.TestCase):
    def test_full_universe_present(self):
        self.assertGreaterEqual(len(ASSET_CATALOG), 200)
        for name in DEFAULT_ASSETS:
            self.assertIn(name, ASSET_CATALOG)

    def test_every_entry_is_well_formed(self):
        for name, meta in ASSET_CATALOG.items():
            self.assertRegex(name, NAME_RE, name)
            self.assertIn(meta.get("kind"), KNOWN_KINDS, name)
            self.assertGreater(float(meta.get("payout", 0)), 0.5, name)
            self.assertLess(float(meta.get("payout", 0)), 0.99, name)
            self.assertGreater(float(meta.get("pip", 0)), 0, name)

    def test_verified_venue_ids(self):
        for name, vid in PINNED_IDS.items():
            self.assertEqual(ASSET_CATALOG[name].get("id"), vid, name)

    def test_static_seed_carries_ids_and_kinds(self):
        cat = AssetCatalog.from_static()
        self.assertGreaterEqual(len(cat), 200)
        eurusd = cat.get("EURUSD")
        self.assertIsNotNone(eurusd)
        assert eurusd is not None
        self.assertEqual(eurusd.asset_id, "1")
        self.assertEqual(cat.kind_of("AAPL_otc"), "stock_otc")
        self.assertEqual(cat.kind_of("SPXUSD"), "index")
        by_kind = cat.by_kind()
        for kind in ("forex", "forex_otc", "crypto_otc", "stock_otc"):
            self.assertIn(kind, by_kind)

    def test_infer_kind(self):
        self.assertEqual(infer_kind("EURUSD_otc"), "forex_otc")
        self.assertEqual(infer_kind("GBPNZD_otc"), "forex_otc")  # via twin
        self.assertEqual(infer_kind("XAUUSD"), "metal")
        self.assertEqual(infer_kind("NOPE_XYZ"), "")

    def test_upsert_preserves_richer_rows(self):
        cat = AssetCatalog.from_static()
        cat.upsert(QXAsset(name="EURUSD", asset_id="EURUSD", payout=0.85,
                           open=True, kind=""))
        kept = cat.get("EURUSD")
        assert kept is not None
        self.assertEqual(kept.asset_id, "1")  # not clobbered by bare sighting
        self.assertEqual(kept.kind, "forex")


class TestInstrumentParsing(unittest.TestCase):
    def test_symbol_id_rows(self):
        rows = parse_instruments([{"symbol": "EURUSD", "id": 1},
                                  {"symbol": "NEWC_OTC", "id": 999}])
        self.assertEqual(rows[0].name, "EURUSD")
        self.assertEqual(rows[0].asset_id, "1")
        self.assertEqual(rows[1].asset_id, "999")

    def test_payout_percent_and_active_flags(self):
        rows = parse_instruments([
            {"asset": "XAUUSD", "payoutPercent": 92, "active": True},
            {"asset": "OLDONE", "profitPercent": 70, "is_active": False},
        ])
        self.assertAlmostEqual(rows[0].payout, 0.92)
        self.assertTrue(rows[0].open)
        self.assertAlmostEqual(rows[1].payout, 0.70)
        self.assertFalse(rows[1].open)


class TestApiDetection(unittest.TestCase):
    def _api(self):
        from cybertrade.brokers.quotex.api import QuotexAPI

        return QuotexAPI()

    def test_assets_list_alias_is_absorbed(self):
        from cybertrade.brokers.quotex import constants as C

        self.assertIn("assets_list", C.INSTRUMENT_EVENTS)
        self.assertIn("assetList", C.INSTRUMENT_EVENTS)
        api = self._api()
        seen = []
        api.add_listener(lambda kind, payload: seen.append((kind, payload)))
        api._on_socket_event("assets_list", [[
            {"symbol": "EURUSD", "id": 1, "payout": 82, "open": True},
        ]])
        kinds = [k for k, _ in seen]
        self.assertIn("instruments", kinds)
        self.assertAlmostEqual(api.payout_for("EURUSD"), 0.82)
        self.assertGreater(api.catalog.synced_at, 0)

    def test_quote_discovers_unknown_asset(self):
        api = self._api()
        seen = []
        api.add_listener(lambda kind, payload: seen.append((kind, payload)))
        api._on_socket_event("tick", [{"asset": "MYSTERY_otc", "price": 1.23,
                                       "ts": 1700000000000}])
        meta = api.catalog.get("MYSTERY_otc")
        self.assertIsNotNone(meta)
        assert meta is not None
        self.assertTrue(meta.is_otc)
        self.assertIn("instruments", [k for k, _ in seen])
        # second sighting: no duplicate adoption event
        seen.clear()
        api._on_socket_event("tick", [{"asset": "MYSTERY_otc", "price": 1.24,
                                       "ts": 1700000001000}])
        self.assertNotIn("instruments", [k for k, _ in seen])


class TestLiveFeedDetection(unittest.TestCase):
    def test_start_requests_instruments(self):
        from cybertrade.data.livefeed import LiveQuotexFeed

        api = FakeQuotexAPI()
        calls = []
        api.request_instruments = lambda: calls.append(1)  # type: ignore[method-assign]
        feed = LiveQuotexFeed(api, ["EURUSD_otc"], refresh_seconds=0)
        feed.start()
        try:
            self.assertEqual(len(calls), 1)
        finally:
            feed.stop()

    def test_reconnect_re_requests_instruments(self):
        from cybertrade.data.livefeed import LiveQuotexFeed

        api = FakeQuotexAPI()
        calls = []
        api.request_instruments = lambda: calls.append(1)  # type: ignore[method-assign]
        feed = LiveQuotexFeed(api, ["EURUSD_otc"], refresh_seconds=0)
        feed.start()
        try:
            feed._on_api_event("reconnected", {})
            self.assertEqual(len(calls), 2)
        finally:
            feed.stop()

    def test_add_asset_builds_book_and_subscribes(self):
        from cybertrade.data.livefeed import LiveQuotexFeed

        api = FakeQuotexAPI()
        feed = LiveQuotexFeed(api, ["EURUSD_otc"], refresh_seconds=0)
        feed.start()
        try:
            self.assertTrue(feed.add_asset("GBPUSD_otc"))
            self.assertFalse(feed.add_asset("GBPUSD_otc"))  # dupe-safe
            self.assertIn("GBPUSD_otc", feed.assets)
            self.assertIsNotNone(feed.book("GBPUSD_otc"))
            self.assertIn("GBPUSD_otc", api.subscribed)
        finally:
            feed.stop()


class TestEngineAdoption(unittest.TestCase):
    def test_instruments_event_grows_the_universe(self):
        eng = make_engine(self)
        before = len(eng.feed.assets)
        eng._on_venue_event("instruments", [
            QXAsset(name="AUDJPY", payout=0.80, open=True, kind="forex"),
            {"name": "XAUUSD"},
        ])
        self.assertIn("AUDJPY", eng.feed.assets)
        self.assertIn("XAUUSD", eng.feed.assets)
        self.assertEqual(len(eng.feed.assets), before + 2)
        self.assertIn("AUDJPY", eng.detectors)
        self.assertIn("XAUUSD", eng.flow)

    def test_adopt_is_dupe_safe(self):
        eng = make_engine(self)
        asset = eng.feed.assets[0]
        self.assertFalse(eng.adopt_asset(asset))
        self.assertFalse(eng.adopt_asset(""))

    def test_manual_fire_adopts_unknown_asset(self):
        from cybertrade.constants import Side
        from cybertrade.data.models import Signal

        eng = make_engine(self)
        sig = Signal(asset="SOLUSD_otc", side=Side.CALL, confidence=0.8,
                     strategy="manual", reason="test", expiry_seconds=60,
                     price=100.0)
        eng.inject_signal(sig)  # must not raise; adoption warms tolerantly
        self.assertIn("SOLUSD_otc", eng.feed.assets)

    def test_snapshot_carries_board_payloads(self):
        eng = make_engine(self)
        snap = eng.snapshot()
        self.assertEqual(snap["assets"], eng.feed.assets)
        self.assertIn(eng.feed.assets[0], snap["candles"])
        cat = snap["catalog"]
        self.assertGreaterEqual(len(cat["rows"]), 200)
        self.assertFalse(cat["live"])  # no venue listing in the stub
        self.assertTrue(any(r["name"] == "EURUSD" and r["id"] == "1"
                            for r in cat["rows"]))

    def test_candles_for_unknown_asset_adopts(self):
        eng = make_engine(self)
        rows = eng.candles_for("EURUSD_otc", limit=10)
        self.assertTrue(rows)
        self.assertIn("EURUSD_otc", eng.feed.assets)


class TestTraderDropdown(unittest.TestCase):
    def _panel(self):
        calls = []

        def cmd(body):
            calls.append(body)
            if body.get("cmd") == "candles":
                return {"candles": [
                    {"o": 1.0, "h": 1.1, "l": 0.9, "c": 1.05,
                     "open_ts": 1700000000.0 + i * 60}
                    for i in range(3)
                ]}
            return None

        with fake_tk():
            import tkinter as tk

            from cybertrade.gui.panels import TraderPanel
            from cybertrade.gui.theme import Theme

            root = tk.Tk()
            panel = TraderPanel(root, Theme("neon_abyss"), cmd)
            return panel, calls

    def test_dropdown_lists_the_catalog(self):
        panel, _ = self._panel()
        rows = [
            {"name": "EURUSD_otc", "payout": 0.85, "open": True,
             "kind": "forex_otc"},
            {"name": "XAUUSD", "payout": 0.90, "open": False, "kind": "metal"},
        ]
        panel.update_state({"assets": ["EURUSD_otc"],
                            "candles": {},
                            "catalog": {"rows": rows, "live": True},
                            "health": {"engine_state": "disarmed"}})
        menu = panel.asset_menu["menu"]
        labels = [c[1] for c in menu.calls if c[0] == "add_command"]
        self.assertEqual(labels, ["EURUSD_otc", "XAUUSD"])

    def test_status_shows_payout_and_flag(self):
        panel, _ = self._panel()
        panel.asset_var.set("XAUUSD")
        rows = [{"name": "XAUUSD", "payout": 0.90, "open": False,
                 "kind": "metal"}]
        panel.update_state({"assets": ["EURUSD_otc"],
                            "candles": {},
                            "catalog": {"rows": rows, "live": True},
                            "health": {"engine_state": "armed"}})
        texts = [c[1]["text"] for c in panel.status.calls
                 if c[0] == "config" and "text" in c[1]]
        self.assertTrue(texts)
        self.assertIn("XAUUSD 90% SHUT", texts[-1])

    def test_chart_queries_missing_asset_once(self):
        panel, calls = self._panel()
        state = {"assets": ["EURUSD_otc"], "candles": {},
                 "catalog": {"rows": [{"name": "EURUSD_otc", "payout": 0.85,
                                       "open": True}], "live": False},
                 "health": {"engine_state": "disarmed"}}
        panel.update_state(state)
        panel.update_state(state)  # same asset — no repeat query
        queries = [c for c in calls if c.get("cmd") == "candles"]
        self.assertEqual(len(queries), 1)
        self.assertEqual(queries[0]["asset"], "EURUSD_otc")

    def test_menu_rebuilds_only_on_change(self):
        panel, _ = self._panel()
        rows = [{"name": "EURUSD_otc", "payout": 0.85, "open": True}]
        state = {"assets": ["EURUSD_otc"], "candles": {},
                 "catalog": {"rows": rows, "live": False},
                 "health": {"engine_state": "disarmed"}}
        panel.update_state(state)
        menu = panel.asset_menu["menu"]
        first = list(menu.calls)
        panel.update_state(state)
        self.assertEqual(menu.calls, first)


class TestCliAssets(unittest.TestCase):
    def test_assets_table_prints_grouped(self):
        from cybertrade.cli import cmd_quotex_assets

        api = FakeQuotexAPI()
        api.catalog = AssetCatalog.from_static()
        seen = []

        def _request():
            seen.append(True)
            api.catalog.update([QXAsset(name="EURUSD", asset_id="1",
                                        payout=0.82, open=True,
                                        kind="forex")])

        api.request_instruments = _request  # type: ignore[method-assign]
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cmd_quotex_assets(api)
        out = buf.getvalue()
        self.assertEqual(rc, 0)
        self.assertTrue(seen)
        self.assertIn("LIVE venue listing", out)
        self.assertIn("[forex]", out)
        self.assertIn("EURUSD", out)
        self.assertIn("82.0%", out)

    def test_static_floor_without_venue(self):
        from cybertrade.cli import cmd_quotex_assets

        class QuietAPI(FakeQuotexAPI):
            def request_instruments(self):
                pass  # venue silent — static floor must still print

        api = QuietAPI()
        api.catalog = AssetCatalog.from_static()
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cmd_quotex_assets(api)
        self.assertEqual(rc, 0)
        self.assertIn("static floor", buf.getvalue())


class TestAssetsBoard(unittest.TestCase):
    ROWS = [
        {"name": "EURUSD_otc", "kind": "forex_otc", "payout": 0.85,
         "open": True, "price": 1.08521},
        {"name": "XAUUSD", "kind": "metal", "payout": 0.90,
         "open": False, "price": None},
        {"name": "BTCUSD_otc", "kind": "crypto_otc", "payout": 0.80,
         "open": True, "price": 67210.5},
    ]

    def _panel(self):
        picked = []
        with fake_tk():
            import tkinter as tk

            from cybertrade.gui.panels import AssetsPanel
            from cybertrade.gui.theme import Theme

            root = tk.Tk()
            panel = AssetsPanel(root, Theme("neon_abyss"), picked.append)
            return panel, picked

    def _state(self, rows=None, **kw):
        st = {"asset": "EURUSD_otc",
              "watch": ["EURUSD_otc", "BTCUSD_otc"],
              "catalog": {"rows": list(self.ROWS if rows is None else rows),
                          "live": True}}
        st.update(kw)
        return st

    def _head_text(self, panel):
        texts = [c[1]["text"] for c in panel.head.calls
                 if c[0] == "config" and "text" in c[1]]
        return texts[-1] if texts else ""

    def test_rows_show_name_payout_flag_price(self):
        panel, _ = self._panel()
        panel.update_state(self._state())
        self.assertEqual(len(panel.board.items), 3)
        first, second, third = panel.board.items
        self.assertIn("EURUSD_otc", first)
        self.assertIn("85%", first)
        self.assertIn("OPEN", first)
        self.assertIn("1.08521", first)
        self.assertIn("SHUT", second)
        self.assertIn("90%", second)
        self.assertIn("67210.50000", third)
        # marks: active ▸, tracked ●, idle ·
        self.assertTrue(first.startswith("▸"))
        self.assertTrue(second.startswith("·"))
        self.assertTrue(third.startswith("●"))

    def test_header_counts_and_live(self):
        panel, _ = self._panel()
        panel.update_state(self._state())
        self.assertIn("3/3 shown", self._head_text(panel))
        self.assertIn("2 open", self._head_text(panel))
        self.assertIn("LIVE", self._head_text(panel))

    def test_search_narrows_the_board(self):
        panel, _ = self._panel()
        panel.search_var.set("btc")
        panel.update_state(self._state())
        self.assertEqual(len(panel.board.items), 1)
        self.assertIn("BTCUSD_otc", panel.board.items[0])
        self.assertIn("1/3 shown", self._head_text(panel))

    def test_kind_filter_and_menu(self):
        panel, _ = self._panel()
        panel.update_state(self._state())
        menu = panel.kind_menu["menu"]
        labels = [c[1] for c in menu.calls if c[0] == "add_command"]
        self.assertEqual(labels, ["ALL", "crypto_otc", "forex_otc", "metal"])
        panel.kind_var.set("metal")
        panel.update_state(self._state())
        self.assertEqual(len(panel.board.items), 1)
        self.assertIn("XAUUSD", panel.board.items[0])

    def test_click_selects_asset_for_trade_tab(self):
        panel, picked = self._panel()
        panel.update_state(self._state())
        panel.board.selection_set(2)
        panel._on_pick()
        self.assertEqual(picked, ["BTCUSD_otc"])
        infos = [c[1]["text"] for c in panel.info.calls
                 if c[0] == "config" and "text" in c[1]]
        self.assertTrue(infos)
        self.assertIn("BTCUSD_otc", infos[-1])
        self.assertIn("crypto_otc", infos[-1])
        self.assertIn("80%", infos[-1])

    def test_selection_survives_price_rebuild(self):
        panel, picked = self._panel()
        panel.update_state(self._state())
        panel.board.selection_set(1)
        rows = [dict(r) for r in self.ROWS]
        rows[1]["price"] = 2380.12345
        panel.update_state(self._state(rows=rows))
        self.assertEqual(panel.board.curselection(), (1,))
        self.assertIn("2380.12345", panel.board.items[1])
        self.assertEqual(picked, [])  # rebuilds never re-fire selection

    def test_no_rebuild_when_nothing_changed(self):
        panel, _ = self._panel()
        state = self._state()
        panel.update_state(state)
        panel.board.calls.clear()
        panel.update_state(state)
        kinds = [c for c in panel.board.calls if c[0] in ("delete", "insert")]
        self.assertEqual(kinds, [])

    def test_empty_catalog(self):
        panel, _ = self._panel()
        panel.update_state(self._state(rows=[]))
        self.assertEqual(panel.board.items, [])
        self.assertIn("0/0 shown", self._head_text(panel))


class TestWebBoardContract(unittest.TestCase):
    """No browser here — pin the board's markup/JS/CSS wiring by content."""

    def _static(self, name):
        import os

        import cybertrade

        base = os.path.join(os.path.dirname(cybertrade.__file__),
                            "web", "static")
        with open(os.path.join(base, name), encoding="utf-8") as fh:
            return fh.read()

    def test_markup_has_board_controls(self):
        html = self._static("index.html")
        for ident in ("asset-board", "asset-search", "asset-kind",
                      "catalog-meta"):
            self.assertIn(f'id="{ident}"', html)
        self.assertNotIn("asset-pick", html)  # dropdown replaced by the board

    def test_js_renders_rows_and_charts_on_click(self):
        js = self._static("js/app.js")
        self.assertIn('getElementById("asset-board")', js.replace("$(\"", "getElementById(\""))
        self.assertIn("boardEls", js)
        self.assertIn("watchAsset(r.name)", js)
        self.assertIn("renderCatalog(lastState, true)", js)
        # venue-controlled names go through textContent, never innerHTML
        self.assertIn("nm.textContent = r.name", js)
        for snippet in ("board.innerHTML = \"\";", "kindPick.innerHTML = \"\";"):
            self.assertIn(snippet, js)

    def test_css_styles_the_board(self):
        css = self._static("css/cyber.css")
        for cls in (".asset-board", ".ab-row", ".ab-empty", ".neon-input",
                    ".ab-row .st.open", ".ab-row .px.up"):
            self.assertIn(cls, css)


if __name__ == "__main__":
    unittest.main()
