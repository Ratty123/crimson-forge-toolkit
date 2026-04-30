from __future__ import annotations

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MAIN_WINDOW = ROOT / "cdmw" / "ui" / "main_window.py"


def _main_window_source() -> str:
    return MAIN_WINDOW.read_text(encoding="utf-8")


class AlignmentDialogSourceGuardTests(unittest.TestCase):
    def test_alignment_dialog_qsize_runtime_dependency_is_imported(self) -> None:
        source = _main_window_source()
        self.assertIn("QSize(", source)
        self.assertIn("QSettings, QSize, Qt", source)

    def test_orientation_presets_are_explicit_apply_only(self) -> None:
        source = _main_window_source()
        self.assertIn("apply_orientation_preset_button.clicked.connect", source)
        self.assertNotIn("orientation_preset_combo.currentIndexChanged.connect(_apply_orientation_preset)", source)
        self.assertIn("reset_placement_button.clicked.connect(_reset_placement_values)", source)

    def test_manual_texture_override_uses_assignment_store(self) -> None:
        source = _main_window_source()
        self.assertIn("texture_override_assignments: Dict[Tuple[str, str, str], str] = {}", source)
        self.assertIn("selected_source_combo = QComboBox()", source)
        self.assertIn("selected_source_combo.currentIndexChanged.connect(_selected_texture_source_changed)", source)
        self.assertIn("def _texture_source_choices_for_row", source)
        self.assertIn("def _texture_row_effective_source", source)
        self.assertIn("def _sync_texture_row_assignment_state", source)
        self.assertIn("texture_override_assignments[row_key] = normalized_source_path", source)
        self.assertIn('texture_override_assignments[row_key] = ""', source)
        self.assertIn("_commit_texture_row_source(row_state, source_path)", source)
        self.assertIn("texture_override_tree.itemActivated.connect(_texture_table_item_activated)", source)
        self.assertIn(
            "def _refresh_texture_row_in_place(row_state: Dict[str, Any], *, sync_editor: bool = True)",
            source,
        )
        self.assertIn('row_state["checked"] = bool(normalized_source_path)', source)
        self.assertNotIn("selected_source_combo.activated.connect", source)
        self.assertNotIn("selected_source_combo.textActivated.connect", source)
        self.assertNotIn("_stage_selected_texture_source", source)

    def test_loose_export_builds_final_output_preview_after_commit(self) -> None:
        source = _main_window_source()
        self.assertIn("build_final_package_preview(", source)
        self.assertIn("final_preview: Optional[FinalPackagePreviewResult]", source)
        self.assertIn("Building final output preview from packaged sidecar/DDS payloads", source)
        self.assertIn("original_dds_resolver=_archive_dds_preview_source_for_path", source)
        self.assertIn("original_dds_basename_resolver=_archive_dds_preview_sources_for_basename", source)
        self.assertIn("copied_dds_specs", source)
        self.assertIn("The preview is using final package texture paths where they could be validated.", source)

    def test_alignment_dialog_has_in_memory_test_build_preview(self) -> None:
        source = _main_window_source()
        self.assertIn('QPushButton("Test Build Preview")', source)
        self.assertIn('QPushButton("Back to Live Preview")', source)
        self.assertIn("Final Test Build Preview - not written", source)
        self.assertIn("def _test_build_final_preview", source)
        self.assertIn("_build_static_options_from_dialog(show_messages=True)", source)
        self.assertIn("preview_result = build_mesh_import_preview(", source)
        self.assertIn("final_preview = build_final_package_preview(", source)
        self.assertIn("supplemental_file_specs=tuple(preview_result.supplemental_file_specs or ()) + tuple(extra_supplemental_specs or ())", source)
        self.assertIn("original_dds_resolver=_archive_dds_preview_source_for_path", source)
        self.assertIn("original_dds_basename_resolver=_archive_dds_preview_sources_for_basename", source)
        self.assertIn("final_model_for_display = final_preview.preview_model", source)
        self.assertIn("static_dialog_preview.set_model(final_model_for_display)", source)
        self.assertNotIn("_copy_final_texture_slots", source)
        self.assertIn("def _mark_final_test_preview_stale", source)
        self.assertIn("test_build_preview_button.clicked.connect(_test_build_final_preview)", source)

    def test_alignment_dialog_sidecar_toggles_flow_into_preview_build(self) -> None:
        source = _main_window_source()
        self.assertIn("rebuild_material_sidecar=bool(rebuild_sidecar_checkbox.isChecked())", source)
        self.assertIn("enable_missing_base_color_parameters=bool(inject_base_color_checkbox.isChecked())", source)
        self.assertIn("static_replacement_options=static_options", source)
        self.assertIn("extra_supplemental_specs=setup.extra_supplemental_specs", source)

    def test_texture_plan_has_single_bulk_apply_action(self) -> None:
        source = _main_window_source()
        self.assertIn('QPushButton("Apply Texture Plan to Overrides...")', source)
        self.assertIn("def _apply_replacement_texture_plan_to_overrides", source)
        self.assertNotIn('QPushButton("Apply Suggested...")', source)
        self.assertNotIn('QPushButton("Assign Matching Role...")', source)

    def test_in_game_mesh_swap_reuses_alignment_import_path(self) -> None:
        source = _main_window_source()
        self.assertIn('QPushButton("Swap With In-Game Mesh...")', source)
        self.assertIn("pending_in_game_mesh_swap_target", source)
        self.assertIn("def _handle_archive_in_game_mesh_swap_entry", source)
        self.assertIn('"Start In-Game Mesh Swap..."', source)
        self.assertIn('"Use This as Swap Source..."', source)
        self.assertIn('"Use as Swap Source..."', source)
        self.assertIn("def _start_archive_in_game_mesh_swap", source)
        self.assertIn("def _load_archive_mesh_scene_import_result", source)
        self.assertIn("def _build_archive_swap_source_texture_evidence", source)
        self.assertIn("def _source_texture_relevance_score", source)
        self.assertIn('texture_entries_by_basename: "OrderedDict[str, ArchiveEntry]"', source)
        self.assertIn("source_texture_evidence_by_local_path", source)
        self.assertIn("parse_material_sidecar_profile", source)
        self.assertIn("material_profile_label", source)
        self.assertIn("material_profile_shader", source)
        self.assertIn("target_shader_family", source)
        self.assertIn("def _source_sidecar_evidence_score", source)
        self.assertIn("Found {len(source_texture_paths):,} source DDS texture candidate(s) from source .pac_xml/sidecars.", source)
        self.assertIn("force_static_replacement=True", source)
        self.assertIn('placement_review_title="In-Game Mesh Swap Placement"', source)
        self.assertIn("swap_placement_note = (", source)
        self.assertIn("Review offset, rotation, scale, and part mapping before export.", source)
        self.assertIn("Replacement Preview is the candidate location/rotation/scale that will be written.", source)
        self.assertIn("Final loose export preview may differ if packaged material sidecar or DDS bindings resolve differently.", source)
        self.assertIn('CollapsibleSection("Import Notes", expanded=False)', source)
        self.assertIn('CollapsibleSection("Compatibility Details", expanded=False)', source)
        self.assertIn('placement_group = QWidget()', source)
        self.assertIn('QPushButton("Review Placement" if placement_context_note.strip() else "Continue")', source)
        self.assertIn("dialog_title=setup.placement_review_title or \"Mesh Replacement Alignment\"", source)
        self.assertIn("placement_context_note=setup.placement_context_note", source)
        self.assertIn("source_texture_evidence=setup.source_texture_evidence", source)
        self.assertIn("scene_import_result=setup.scene_import_result", source)
        self.assertIn("source_display_label=setup.source_label", source)
        self.assertNotIn("def _prompt_archive_in_game_mesh_source", source)
        self.assertIn("class InGameMeshSwapScopeSelection", source)
        self.assertIn('dialog.setWindowTitle("In-Game Mesh Swap Scope")', source)
        self.assertIn("replace_target_sidecar_with_source", source)
        self.assertIn("def _build_in_game_mesh_swap_extra_specs", source)
        self.assertIn("extra_supplemental_specs", source)
        self.assertIn("preferred_rebuild_material_sidecar", source)
        self.assertIn("def _archive_model_source_texture_entries_for_swap", source)
        self.assertIn("_archive_model_source_texture_entries_for_swap(source_entry)", source)
        self.assertIn("sidecar_texture_references=source_sidecar_bindings", source)
        self.assertIn("_extract_archive_sidecar_texture_lookup_paths(sidecar_text)", source)
        self.assertIn('str(candidate.extension or "").strip().lower() != ".dds"', source)
        self.assertIn("Checked rows are written as loose replacement payloads", source)
        self.assertIn("REPLACES this game file while enabled; rig/physics-sensitive", source)
        self.assertIn(".pab skeleton and .hkx animation/physics rows are not merged", source)

    def test_mesh_import_setup_dialog_is_compact(self) -> None:
        source = _main_window_source()
        self.assertIn('dialog.setObjectName("MeshImportSetupDialog")', source)
        self.assertIn("dialog.setMinimumSize(760, 460)", source)
        self.assertIn('QGroupBox("Import Summary")', source)
        self.assertIn('QGroupBox("Preflight & Files")', source)
        self.assertLess(
            source.index('summary_layout = QVBoxLayout(summary_group)'),
            source.index("summary_layout.addWidget(source_group)"),
        )
        self.assertIn('QLabel#MetricChip', source)
        self.assertIn('def _compact_path', source)
        self.assertIn("content_scroll = QScrollArea(dialog)", source)
        self.assertIn("def _fit_mesh_import_setup_dialog_to_screen", source)
        self.assertIn("dialog.setMaximumSize(max_width, max_height)", source)
        self.assertIn('preflight_tree = QTreeWidget()', source)
        self.assertIn('preflight_tree.setHeaderLabels(["Check", "Value"])', source)
        self.assertIn('preflight_tree.setMaximumHeight(128)', source)
        self.assertIn('supplemental_list.setMaximumHeight(112)', source)
        self.assertIn("<b>Live Alignment Preview</b> is the transform workspace.", source)
        self.assertIn("After loose export, the Archive Preview switches to a final-output view when possible", source)
        self.assertNotIn("Mesh Replacement automates common part and texture mappings, but some assets still need manual texture-slot review.", source)


if __name__ == "__main__":
    unittest.main()
