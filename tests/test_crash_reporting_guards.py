from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "cdmw_app.py"
MAIN_WINDOW = ROOT / "cdmw" / "ui" / "main_window.py"
ARCHIVE = ROOT / "cdmw" / "core" / "archive.py"
THEMES = ROOT / "cdmw" / "ui" / "themes.py"


class CrashReportingGuardTests(unittest.TestCase):
    def test_bootstrap_import_failures_are_reported(self) -> None:
        source = APP.read_text(encoding="utf-8")
        self.assertIn("def _write_bootstrap_report", source)
        self.assertIn('"bootstrap_failure"', source)
        self.assertIn("from cdmw.ui.main_window import run_gui", source)

    def test_gui_has_heartbeat_and_hang_watchdog(self) -> None:
        source = MAIN_WINDOW.read_text(encoding="utf-8")
        self.assertIn("heartbeat_path = crash_reports_dir / \"app_heartbeat.json\"", source)
        self.assertIn("def _check_previous_unclean_exit", source)
        self.assertIn("def _process_is_alive", source)
        self.assertIn("previous_pid_alive", source)
        self.assertIn("def _start_hang_watchdog", source)
        self.assertIn('"app_hang_detected"', source)
        self.assertIn('"previous_session_unclean_exit"', source)
        self.assertIn("faulthandler.enable", source)

    def test_background_crash_context_does_not_read_live_qt_widgets(self) -> None:
        source = MAIN_WINDOW.read_text(encoding="utf-8")
        self.assertIn("_cached_crash_context", source)
        self.assertIn("app.thread() != QThread.currentThread()", source)
        self.assertIn("context.update(_cached_crash_context)", source)

    def test_close_waits_for_workers_asynchronously(self) -> None:
        source = MAIN_WINDOW.read_text(encoding="utf-8")
        self.assertIn("def _begin_deferred_close_for_workers", source)
        self.assertIn("event.ignore()", source)
        self.assertIn("thread.finished.connect(self._finish_deferred_close_if_workers_stopped", source)
        self.assertIn("self._close_force_accept = True", source)
        self.assertNotIn("thread.wait(wait_ms)", source)
        self.assertNotIn("wait_ms: int = 1200", source)

    def test_archive_scan_breadcrumbs_are_recorded_for_native_faults(self) -> None:
        main_source = MAIN_WINDOW.read_text(encoding="utf-8")
        archive_source = ARCHIVE.read_text(encoding="utf-8")
        self.assertIn("archive_scan_breadcrumb.json", main_source)
        self.assertIn("def _write_scan_breadcrumb", main_source)
        self.assertIn("on_breadcrumb=self._write_scan_breadcrumb", main_source)
        self.assertIn("on_breadcrumb: Optional[Callable[[Mapping[str, object]], None]]", archive_source)
        self.assertIn('"phase": "parse_archive_pamt"', archive_source)
        self.assertIn('"pamt_path": str(pamt_path)', archive_source)

    def test_archive_scan_progress_is_not_emitted_from_nested_python_thread(self) -> None:
        archive_source = ARCHIVE.read_text(encoding="utf-8")
        self.assertNotIn("emit_parse_heartbeat", archive_source)
        self.assertNotIn("heartbeat_thread = threading.Thread", archive_source)
        self.assertNotIn("heartbeat_stop = threading.Event()", archive_source)

    def test_archive_pamt_parser_avoids_giant_record_lists(self) -> None:
        archive_source = ARCHIVE.read_text(encoding="utf-8")
        self.assertIn("max_cache_entries: int = 200_000", archive_source)
        self.assertIn("seen_offsets: set[int] = set()", archive_source)
        self.assertIn("file_table = memoryview(data)[off : off + file_table_size]", archive_source)
        self.assertIn('struct.iter_unpack("<IIIIHH", file_table)', archive_source)
        self.assertNotIn('files = list(struct.iter_unpack("<IIIIHH"', archive_source)

    def test_archive_preview_inner_splitter_collapses_references_before_overlap(self) -> None:
        source = MAIN_WINDOW.read_text(encoding="utf-8")
        self.assertIn("archive_preview_main_widget.setMinimumWidth(0)", source)
        self.assertIn("self.archive_preview_title_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)", source)
        self.assertIn("self.archive_texture_refs_group.setMinimumWidth(0)", source)
        self.assertIn("self.archive_preview_content_splitter.setChildrenCollapsible(True)", source)
        self.assertIn("min_preview_width = 420", source)
        self.assertIn("right_width = 0", source)
        self.assertIn("self.archive_preview_content_splitter.setSizes([total, 0])", source)

    def test_loose_preview_toggle_is_two_state_action(self) -> None:
        source = MAIN_WINDOW.read_text(encoding="utf-8")
        self.assertIn("def _toggle_archive_loose_preview", source)
        self.assertIn("self.archive_preview_requested_loose = not bool(self.archive_preview_showing_loose)", source)
        self.assertIn("self._show_archive_preview_result(result, use_loose=self.archive_preview_requested_loose)", source)
        self.assertIn('"Archive File" if self.archive_preview_showing_loose else "Loose File"', source)
        self.assertNotIn("def _toggle_archive_loose_preview(self) -> None:\n            self.archive_preview_requested_loose = False", source)

    def test_archive_preview_refresh_respects_loose_asset_arguments(self) -> None:
        source = MAIN_WINDOW.read_text(encoding="utf-8")
        self.assertIn("include_loose_preview_assets=include_loose_preview_assets", source)
        self.assertIn("prefer_loose_preview=self.archive_preview_requested_loose", source)
        self.assertIn("self.archive_preview_requested_loose = bool(entry is not None and prefer_loose_preview)", source)
        self.assertNotIn("include_loose_preview_assets = False\n            prefer_loose_preview = False", source)

    def test_floating_preview_settings_syncs_back_to_settings_tab(self) -> None:
        source = MAIN_WINDOW.read_text(encoding="utf-8")
        self.assertIn("def _sync_model_preview_settings_controls", source)
        self.assertIn("settings_tab._apply_model_preview_controls(settings)", source)
        self.assertIn("dialog.set_settings(settings)", source)
        self.assertIn("self._sync_model_preview_settings_controls()", source)

    def test_startup_splash_has_abstract_animation(self) -> None:
        source = MAIN_WINDOW.read_text(encoding="utf-8")
        self.assertIn("class StartupSignalMark", source)
        self.assertIn("self._timer = QTimer(self)", source)
        self.assertIn("def paintEvent(self, event) -> None", source)
        self.assertIn("self.signal_mark = StartupSignalMark", source)
        self.assertIn("self.signal_mark.stop()", source)
        self.assertIn("remaining_minimum_visible_ms", source)
        self.assertIn("self._minimum_visible_seconds = 3.0", source)
        self.assertIn("QTimer.singleShot(remaining_ms, self._release_startup_splash)", source)
        self.assertIn("def pump_animation_frame", source)
        self.assertIn("MainWindow(startup_splash=startup_splash)", source)
        self.assertIn('pump_startup_splash("Preparing archive browser...")', source)
        self.assertIn("#c56d43", source)
        self.assertNotIn("compass_radius", source)
        self.assertNotIn("route = QPainterPath()", source)
        self.assertNotIn("Qt.DashLine", source)
        self.assertIn("QFrame#StartupSignalMark", source)
        self.assertNotIn('QLabel("CDMW")', source)

    def test_crimson_desert_theme_is_available(self) -> None:
        source = THEMES.read_text(encoding="utf-8")
        self.assertIn('"crimson_desert"', source)
        self.assertIn('"label": "Crimson Desert"', source)
        self.assertIn('"accent": "#c56d43"', source)

    def test_additional_qa_themes_are_available(self) -> None:
        source = THEMES.read_text(encoding="utf-8")
        for key, label in (
            ("midnight_ember", "Midnight Ember"),
            ("glacier", "Glacier"),
            ("black_gold", "Black Gold"),
            ("pine", "Pine"),
            ("violet_steel", "Violet Steel"),
        ):
            self.assertIn(f'"{key}"', source)
            self.assertIn(f'"label": "{label}"', source)
            self.assertIn('"preview_bg"', source)


if __name__ == "__main__":
    unittest.main()
