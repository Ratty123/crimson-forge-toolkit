# Changelog

All notable changes to this project should be documented in this file.

The format is intentionally simple:

- `Added` for new features
- `Changed` for behavior or workflow changes
- `Fixed` for bug fixes
- `Docs` for README, guide, or release-note changes

## [0.7.0] - 2026-04-20

### Added
- Added explicit archive-mutation workflows for supported archive entries, including patch requests, per-entry backup capture, backup restore, and mod-ready loose export helpers instead of keeping archive work extract-only.
- Added real 3D archive preview support for recovered `.pam`, `.pamlod`, and `.pac` geometry, with orbit/zoom/reset controls, textured shading, richer details, and fallback messaging when geometry recovery is incomplete.
- Added mesh export/import tooling for supported archive meshes, including `Export OBJ`, `Export FBX`, `Import OBJ Preview`, `Import OBJ`, paired `PAM -> PAMLOD` transfer handling, and PAC FBX export paths that can attach matching `PAB` skeleton data.
- Added referenced-texture inspection for archive mesh previews, including a dedicated texture list plus `Open`, `Export DDS`, `Replace DDS`, and `Replace From PNG` actions for resolved mesh texture entries.
- Added broader archive media/binary preview coverage, including `.wem` playback via bundled `vgmstream-cli`, `.mp4` / `.bk2` playback through Qt Multimedia, `.bnk` Wwise soundbank summaries, `.pab` skeleton summaries, `.hkx` metadata summaries, and `.pami` text decoding in the archive browser.
- Added flat-vs-tree archive browsing, incremental tree population, full extension-list population from the loaded archive index etc.

### Changed
- Repositioned `Archive Browser` from a purely read-only explorer into a mixed browse/inspect/export/patch surface for supported mesh and audio paths, while still keeping loose DDS workflows and confirm-before-write behavior intact.
- Reworked the archive preview pane so export/import/restore actions are visible beside the preview instead of being hidden behind right-click-only discovery, and moved referenced textures beside the model preview for a larger, more practical preview area.
- Archive filtering and archive preview work now run with more progress-aware background behavior, better cancellation, and lighter UI-thread work when changing large extensions, rebuilding previews, or switching between flat/tree views.
- Text-like archive preview now uses the same styled text presentation more consistently across supported text formats, including decrypted or decoded archive payloads such as `.pami`.
- Model texture resolution is now broader and more semantic: preview/export paths resolve more DDS candidates from parsed mesh data and raw binary references, prefer visible/base-color matches over technical-only maps, and apply those references across `.pam`, `.pamlod`, and `.pac` preview/export flows.
- Onefile packaging is now more defensive, with improved custom PyInstaller hooks, bundled Wwise decode runtime handling, and full embedded-archive validation after build so release EXEs fail fast during packaging instead of at first launch.

### Fixed
- Fixed the archive extension filter/drop-down regressions so supported extensions are populated again from the archive contents instead of forcing manual entry for some workflows.
- Fixed archive DDS preview regressions where selecting `.dds` entries could get stuck on details, fail to switch to the preview tab correctly, or become noticeably slower after later archive-browser changes.
- Fixed `.pami` preview stability and presentation so decoded text no longer opens with mismatched styling and is less likely to leave the browser in a stuck `Preparing archive preview...` state.
- Fixed a long list of `.pam`, `.pamlod`, and `.pac` geometry-recovery problems, including better companion PAMLOD fallback usage, better submesh preservation during companion fallback, additional partial/zero-padded recovery paths, more helpful failure text, and fewer cases where previews stall or degrade into obviously scrambled geometry.
- Fixed more textured-model issues, including missing referenced DDS rows, overly narrow texture-reference columns, companion-preview texture loss, PAC texture availability in preview/export, and incorrect vertical texture orientation on assets such as the shield atlas that were previously mapped into opaque black atlas space.
- Fixed archive-browser responsiveness issues around large extension switches, archive-cache reuse, background preview cancellation, and long-running per-file preview work so the UI spends less time in `Not Responding` during heavy archive browsing.
- Fixed `Texture Editor` grid-state handling so the archive/browser-driven editor path no longer throws `AttributeError: 'TextureEditorTab' object has no attribute '_grid_color'`.
- Fixed onefile extraction/runtime failures involving Pillow/OpenCV/crypto packaging, including corrupt `_imagingft` payload handling, OpenCV FFmpeg plugin extraction failures, and related embedded-DLL extraction errors that could break startup on Windows.
- Fixed `.wem` decode playback launching a visible console window and improved Wwise playback fallback messaging when the local multimedia backend still cannot decode a variant directly.
- Fixed archive patch safety gaps by surfacing backup restore in the UI and tightening patch/build flows so interrupted mesh/audio patch attempts are easier to recover from.

### Docs
- Updated the README for `0.7.0` so the front page now reflects archive patching, mesh preview/modding, media preview, and the broader archive-browser scope instead of describing the app as read-only.
- Added and refreshed `docs/archive_extension_preview_matrix.csv` to capture current extension coverage, what the app can actually preview today, and where future investment is still worthwhile.
- Expanded third-party notices and release metadata to cover vendored MIT-licensed CrimsonForge-derived mesh tooling, bundled `vgmstream` usage, and the newer packaging/build workflow.

## [0.6.5] - 2026-04-18

### Added
- Added persistent appearance controls for the whole app, including global font family, global font size, list/column font size, density, dedicated log/code font family, log/code font size, and optional bold emphasis for log/code views.
- Added `Research > Archive Insights > Archive Files` preview/details support so selected archive DDS files can now be inspected in-place with the same preview/detail flow used by `Archive Browser`.
- Expanded the archive-side DDS inspector with much richer metadata, including additional header flags, resource/cubemap/DX10 details, mip completeness, estimated surface bytes, hashes, and lower-level DDS header fields for deeper inspection.
- Added simple `Texture Editor` grid visibility controls for color and opacity so the grid can stay readable on bright or low-contrast textures.
- Added clearer ambiguous original selection during `Replace Assistant` auto-match when multiple archive DDS candidates share the same basename and no strong path context is available.
- Added a structured per-file workflow profile system to `Texture Workflow`, including reusable named workflow profiles, ordered glob/exact-path rules, a live `Matched Files` view, exact-path profile assignment from selected files, effective per-file DDS/NCNN previews, and export/import support for the new structured profile format.
- Added built-in starter workflow profiles and starter rules for `*.dds`, `*_n.dds`, `*_d.dds`, `*_disp.dds`, and `*_sp.dds` so common color, normal, height/displacement, and specular map families have immediate baseline assignments.
- Added a searchable in-app documentation browser with topic navigation for workflow features, planner profiles, planner paths, Archive Browser, Texture Editor, Replace Assistant, Research, Text Search, and troubleshooting.

### Changed
- Refined the overall app UI to be denser and more coherent, with smaller controls, more consistent section headers, tighter spacing, better font application, and broader column autofit behavior when the app opens or appearance settings change.
- Reworked the `Texture Editor` no-document layout into a narrower tools lane with a compact actions menu, smaller tool buttons, cleaner empty-state behavior, theme-aware icons, and less wasted screen space on smaller displays.
- Improved workflow handoff behavior again so `Texture Editor -> Send to Texture Workflow` and `DDS To Workflow` make root-clear decisions at handoff time, stage the required source files more predictably, and keep the intended file focused when you move into `Texture Workflow`.
- `Replace Assistant` queue and matching flows now use stronger path-aware `.png -> .dds` matching, clearer overflow handling in the queue columns, and better guidance when exact archive-path evidence is missing.
- Research/archive-related preview panels, Archive Browser details, and section containers now share a more consistent presentation instead of mixing multiple header/box patterns that felt visually disconnected.
- Replaced the old freeform `Texture rules` authoring surface with a visual `Workflow Profiles`, `Ordered Rules`, and `Matched Files` manager, and expanded `Preview Policy` so it shows matched workflow profiles, matched rules, effective DDS overrides, and effective direct-NCNN settings per file.
- Rebalanced the starter workflow defaults so color/albedo stays enabled on the visible-color path by default while normal, specular, and height/displacement starters now begin as preserve-first or technical-path baselines until explicitly overridden.
- Simplified the top-level app navigation by promoting `Quick Start` to its own menu entry, renaming the old `About` surface into `Documentation`, moving `Export Diagnostics...` under `Documentation`, and removing the redundant `Tools` menu.

### Fixed
- Fixed a long list of workflow and review regressions across `Texture Workflow`, `Replace Assistant`, `Texture Editor`, `Compare`, `Research`, `Text Search`, and `Archive Browser`, including frozen or delayed preview handoff, fit-mode compare flicker, distorted compare previews, stale workflow root contents causing misleading compare lists, and several archive/preview UI stalls.
- Fixed additional `Replace Assistant` stability issues around import, auto-match completion, preview refresh, post-build review, and post-worker cleanup so matching and package build flows no longer crash or lock up as easily.
- Fixed a `Replace Assistant Review` preview/runtime error where normal image inputs such as PNG sources could fail with `cannot convert 'Format' object to bytes` while preparing the edited-input metadata and preview pane after a rebuild.
- Fixed classification/local-approval clarity issues so the app better distinguishes inferred roles from saved local approvals, routes workflow review to the correct DDS more reliably, and provides faster per-file local-save actions.
- Fixed more `Texture Editor` issues around guide clearing, document metadata sizing, hidden tab close buttons, image/atlas action layout, light-theme tool icon visibility, grid visibility, and font consistency across the left and right editor panes.
- Fixed a `Texture Editor` custom-brush preset runtime bug where `json` serialization/deserialization paths were used without the required import, which could break preset load/save behavior in the shipped build.
- Fixed several settings/runtime problems, including slow font-size stepping, typed font-size editing, missing imports and startup crashes, DDS-details exceptions, more accurate DDS surface estimates for arrays/cubemaps, and other late-cycle packaging/runtime errors.
- Fixed another `Archive Browser` DDS preview hang where rapidly browsing many archive textures could eventually leave the app stuck in `Not Responding`, by avoiding eager loose-preview generation and reducing heavy per-click archive selection recomputation on the UI thread.
- Fixed direct `Real-ESRGAN NCNN` runs that could silently accept invalid flat PNG output or Vulkan/OOM-like failures at large tile sizes, by validating the output and retrying smaller tile sizes automatically before DDS rebuild continues.
- Fixed starter workflow profile defaults that could previously force unsafe per-profile NCNN scales or generic visible-path handling for technical maps, which could lead to broken mixed-batch results or black/flat rebuilt DDS output.

### Docs
- Updated the README for `0.6.5`, refreshed the feature summary and screenshots, and replaced the old `docs/screenshots` set with current captures from the renamed and polished `Crimson Forge Toolkit` UI.
- Expanded `Quick Start` and the in-app documentation for `0.6.5` so the shipped help now covers the current workflow/profile system, planner-path and planner-profile meanings, all major tabs, backend behavior, review flow, and troubleshooting.

## [0.6.0] - 2026-04-16

### Added
- Promoted the expanded `Texture Editor` work from the beta cycle into the first full `0.6.0` release, including layered in-app texture editing, masks, adjustments, packed-channel helpers, navigator/rulers/guides, atlas export helpers, and a much broader visible-texture toolset than earlier builds.
- Folded the final late-cycle editor and workflow fixes into the release build, including straighter feathered selection extraction, hole-preserving mask/selection round-trips, layer-mask cleanup on deletion, better ruler/guide behavior, and smoother `Text Search` / `Research` result population on large datasets.

### Changed
- `0.6.0` now represents the stabilized release line after the `0.6.0-beta.x` cycle, so the editor, replacement, DDS preview, research, and packaging workflows described in the current README are now part of the main release instead of being gated behind a prerelease note.
- The app now behaves better during heavier editing/review sessions, with lighter shutdown waits, incremental large-tree population in `Text Search` / `Research`, and additional Texture Editor UI polish around guides, rulers, and atlas controls.
- The shipped `0.6.0` release build is now branded as `Crimson Forge Toolkit`, including the app title, package defaults, docs/help text, build output name, and portable config/profile filenames, while still migrating legacy `CrimsonTextureForge.cfg` and `DDSRebuildApp.cfg` files automatically.
- `Texture Workflow` handoff is now more predictable: `DDS To Workflow` and `Texture Editor -> To Workflow` make their root-clear decisions at handoff time instead of later at `Start`, and editor exports now stage through a dedicated `png_texture_editor` override root instead of mixing with normal upscaled PNG output.

### Fixed
- Fixed additional correctness issues in the release pass, including soft-selection edge extraction, stale mask/adjustment references after layer removal, guide clear/apply behavior, Atlas panel text clipping, and ruler hover alignment when the canvas is centered inside the viewport.
- Fixed more late-cycle workflow rough edges across `Texture Editor`, `Text Search`, and `Research`, so the final `0.6.0` build is more stable and less visibly hitchy than the previous `0.6.0-beta.4` prerelease.
- Fixed multiple late `Replace Assistant` and `Texture Workflow` regressions in the release build, including explicit rather than automatic matching on import/editor handoff, visible auto-match indexing progress, more stable post-match queue handling, and a much lighter post-build review flow that no longer immediately auto-loads heavy 4K previews when package build completes.
- Fixed several preview/review regressions in the shipped build, including `Compare` aspect-ratio distortion, fit-mode flicker, smoother deferred scaling during resize, and removal of synchronous UI-reference scans that were causing freezes when opening textures or preparing compare metadata.
- Fixed workflow/classification clarity issues in the release build, including member-specific classification suggestions, clearer local-vs-inferred approval state in `Classification Review`, one-click `Save Current Role Locally`, and more reliable routing from workflow warnings into focused review of the exact DDS that still needs a saved local approval.
- Fixed the portable onefile packaging issue caused by Pillow AVIF extraction, so the corrected `0.6.0` EXE launches reliably without the `_avif` extraction failure seen in some builds.

### Docs
- Updated the README and release notes for the final `0.6.0` release and kept the beta changelog history intact underneath for users following the development cycle.
- Updated `Quick Start`, `About`, README, release notes, and related docs/help text to use the new `Crimson Forge Toolkit` name and reflect the corrected workflow, Replace Assistant, and review behavior in the current `0.6.0` build.

## [0.6.0-beta.4] - 2026-04-16

### Added
- `Texture Editor` gained a much deeper set of texture-editing workflows, including direct on-canvas transform handles for floating selections, richer mask/selection handoff, stronger packed-channel tools, custom image-stamp brushes, symmetry painting, editable quick mask, navigator/rulers/guides, pixel inspection, atlas export helpers, and additional non-destructive adjustments such as `Vibrance`, `Selective Color`, `Brightness/Contrast`, `Exposure`, and `Color Balance`.
- The editor now adds more document-level operations and texture-focused utilities, including crop/trim/canvas/image resize actions, region or grid-slice export helpers for atlas-style work, plus stronger channel copy/paste/swap flows for packed-texture cleanup inside the app.

### Changed
- `Texture Editor` now feels more like a real texture compositor, with stronger transform ergonomics, richer brush behavior, better channel-aware editing, finer control over selection/mask workflows, and more practical navigation and precision feedback while working on large textures.
- Heavy editor sessions now use lighter refresh/history behavior in common dirty-region edit paths, while `Text Search` and `Research` also avoid more unnecessary full UI stalls during large result updates.

### Fixed
- Fixed a broad set of `Texture Editor` issues around selection-to-mask creation, selective-color project save/load, masked copy/extract behavior, merge-down correctness with masks/adjustments, floating selection/project persistence, zoom reliability, and several loaded-editor workflow regressions found during the full feature verification pass.
- Fixed more archive-side and app-wide behavior issues, including cancellation-aware loose DDS preview fallback, safer preview shutdown behavior, broader DDS preview compatibility, and app-wide prevention of accidental mouse-wheel setting changes over combo/spin/slider controls.

### Docs
- Updated the README and prerelease notes for `0.6.0-beta.4` to reflect the latest `Texture Editor`, preview, DDS, and workflow improvements now present in the current beta.

## [0.6.0-beta.3] - 2026-04-15

### Added
- `Texture Editor` grew into a much more complete texture-editing workspace with multi-document tabs, stronger layered editing, floating selections, masks, adjustment layers, richer channel/alpha workflows, and tighter handoff into `Replace Assistant`, `Texture Workflow`, `Compare`, and `Archive Browser`.
- The in-app editor now includes a broader set of real editing tools for visible texture work, including `Paint`, `Erase`, `Fill`, `Gradient`, `Smudge`, `Dodge/Burn`, `Patch`, `Clone/Heal`, `Sharpen`, `Soften`, brush presets, brush tips/patterns, custom saved presets, and finer brush control for detail cleanup.

### Changed
- `Texture Editor` now uses a more canvas-first editing layout with compact document tabs above the canvas, lighter side chrome, better zoom/pan behavior, contextual tool settings, richer shortcut coverage, and more status feedback so it feels closer to a real texture-editing workspace.
- The editor’s selection, move, and transform workflows now behave much more like a real compositor, with better floating selection handling, stronger copy/paste between documents, layered move workflows, and more practical selection refinement behavior.

### Fixed
- Fixed a large number of editor/workflow issues across `Texture Editor`, `Replace Assistant`, archive preview, and `Text Search`, including stronger DDS preview fallback behavior, better unusual DDS compatibility, improved archive-load responsiveness, safer preview cancellation/shutdown, and steadier editor adjustment/selection behavior.
- Fixed additional editor-specific issues around zoom anchoring, floating-selection persistence, project save/load, channel-aware editing, and preview/update stability so the current beta is much closer to a usable real editing workflow than the original `0.6.0-beta.1` prerelease.

### Docs
- Updated the README and release notes for `0.6.0-beta.3` to reflect the newer editor, preview, packaging, and workflow capabilities now present in the current beta.

## [0.6.0-beta.1] - 2026-04-13

### Added
- A new top-level `Replace Assistant` tab for edited-texture replacement workflows, so you can import manually edited `PNG` or `DDS` files, match them to their original in-game DDS, preview them, and build a mod-ready loose package without manually juggling the main batch workflow roots.
- `Replace Assistant` can optionally run the same direct `Real-ESRGAN NCNN` feature set exposed in `Texture Workflow`, including model selection, scale, tile size, `NCNN extra args`, retry-with-smaller-tile, texture preset, automatic texture rules, the expert unsafe override, and post-correction modes such as `Source Match`.
- `Replace Assistant` now writes `example_mod`-style package output with `.no_encrypt`, generated `info.json`, and package-prefixed loose DDS paths that follow the matched original texture.
- Successful `Replace Assistant` builds now open a post-build review window that compares the edited input against the rebuilt DDS preview, so you can quickly inspect whether the repackaged result shifted before shipping the mod.
- A new top-level `Texture Editor` tab for visible-texture work, with layered projects, paint/erase, sharpen/soften, rectangular and lasso selections, clone/heal tools, in-editor recolor, and flattened export back into `Replace Assistant` or `Texture Workflow`.
- `Texture Editor` can open loose images and DDS files directly, and it now has handoff entry points from `Replace Assistant`, `Archive Browser`, `Compare`, and the main `Texture Workflow` setup area.
- `Texture Editor` now includes source-aware preview modes for `Edited`, `Original`, `Split`, and per-channel `R/G/B/A` inspection, plus optional atlas/grid guides so UI and packed textures are easier to review without leaving the editor.
- `Texture Editor` now adds deeper paint/retouch coverage with `Gradient`, `Smudge`, `Dodge/Burn`, and `Patch` tools, plus channel-lock editing so visible texture cleanup can stay inside the app for more real workflows.
- `Texture Editor` brush controls now include roundness, angle, smoothing, primary/secondary color handling, size-step modes, and user-saved brush presets on top of the existing preset/tip/pattern system.

### Changed
- The main `Workflow` tab is now labeled `Texture Workflow`, which better distinguishes the advanced batch pipeline from the new guided `Replace Assistant` flow.
- `Replace Assistant` now sits as its own top-level tab and receives archive-entry refreshes and shared status messages from the main window like the other major tools.
- `Replace Assistant` now hides `Direct Upscale Controls (NCNN only)` unless the build mode is set to upscale, which frees space for the rebuild-only package flow.
- `Replace Assistant` package output now treats the chosen root as the parent mods folder and writes the actual package into a child folder named after the mod title, which better matches `example_mod`-style mod manager layouts.
- `Texture Workflow` can now optionally emit the same ready mod package shape after rebuild, including a child folder named after the mod title plus generated `info.json` and optional `.no_encrypt`, while still keeping the normal `dds_final` output untouched.
- The experimental recolor controls were removed from `Replace Assistant`, because visible-texture editing now lives in the dedicated `Texture Editor` instead of the packaging/rebuild tab.
- `Texture Editor` paint/sharpen/soften/clone/heal tools now expose a more practical first pass of advanced options, including paint blend modes, selectable sharpen/soften modes, visible-layer sampling for filter/clone tools, and a more accurate brush-footprint preview while dragging.
- `Texture Editor` selection tools now have a real companion workflow, including a dedicated `Selection` panel, feathering, invert, `Select All`, `Copy To New Layer`, optional edge-snapped lasso, and a basic `Move` tool for repositioning active-layer or selected content.
- `Texture Editor` now uses a cleaner split layout with a lighter inspector, an icon-first tool rail, narrower default side panels, and a more compact action bar so the canvas keeps more space for actual editing.
- `Texture Editor` now supports multiple open documents in editor tabs, shares copy/paste clipboard content between those tabs, and has been pushed further toward a real texture-compositor workflow with non-destructive layer offsets, richer layer state, stronger selection operations, and cropped pasted layers that move and hide correctly.
- `Texture Editor` now keeps the canvas state more like a real workspace, with per-document view state, a live status strip, contextual docks, async document/open save-export work, and a stronger floating-selection path for copy/cut/paste and transform-style edits.
- `Texture Editor` now supports document-top adjustment layers (`Hue / Saturation`, `Levels`, `Curves`) plus raster layer masks, so visible-texture edits can stay non-destructive longer instead of forcing immediate pixel commits for every tonal change.
- `Texture Editor` now includes `Open In Compare`, which hands the current source binding back to the existing Compare tab instead of duplicating compare preview logic inside the editor.
- `Texture Editor` now adds a real `Fill` tool, quick-mask overlay toggle, custom selection grow/shrink amount, and a `Float Active Layer Copy` transform entry so selection cleanup and isolated transform-style edits are easier without leaving the editor.
- `Texture Editor` now gives the adjustment stack a more professional editing flow with reset/duplicate/reorder/solo controls, direct active-layer mask assignment for adjustments, a richer status strip, and smaller/finer minimum brush footprints for detail work.
- `Texture Editor` brush tools now add presets, selectable brush tips, and pattern-based brush footprints, so paint/erase/clone/heal work can move beyond a single round brush toward more texture-oriented editing.
- `Texture Editor` clone/heal now supports aligned or fixed-source sampling plus a direct source-clear action, which makes retouch work behave more like a real editor tool instead of a one-state helper.
- `Texture Editor` now exposes a dedicated `Channels` panel, gradient secondary color, richer shortcut coverage for tool switching and brush sizing/hardness, and an on-canvas brush HUD so fine paint work is easier to read while you edit.
- Removed the stale hidden in-editor AI-enhance plumbing that was still lingering behind the scenes after the visible editor-side upscale controls were dropped.

### Fixed
- The unfinished `Replace Assistant` implementation is now wired up far enough to be usable, including manual local-original selection, manual archive-original selection, output-folder opening, and better status/build callback handling.
- `Replace Assistant` NCNN model discovery now uses the correct executable/model-dir signature and populates the model picker correctly instead of calling the discovery helper with the wrong argument shape.
- `Replace Assistant` preview follow-up requests now advance their request id correctly when a new selection arrives while an older preview worker is still finishing, which avoids stale preview handoff glitches.
- `Replace Assistant` package builds now honor the `.no_encrypt` toggle instead of always writing the marker file even when the package should stay unmarked.
- `Texture Editor` recolor now applies explicitly to the active layer instead of reprocessing image changes on every setting edit, so tolerance/strength adjustments can be made before committing the recolor action.
- `Texture Editor` history restore is now explicit instead of reloading full snapshots on every list selection change, undo/redo now properly covers layer visibility/opacity edits, and the tool rail now uses icon-backed buttons so the editor feels less rough overall.
- `Texture Editor` now supports wheel zoom directly over the texture, uses collapsible right-side inspector sections so the canvas gets more space by default, and enriches direct file-open document metadata from the configured `PNG root` / `Original DDS root` so relative path, package, original DDS, and semantics are much less likely to stay blank.
- `Texture Editor` now supports right-drag panning, `Show in Archive Browser`, configurable keyboard shortcuts for common editor actions, active-layer copy/paste helpers, and a softer inspector/metadata presentation so the editor is quicker to use and less visually rough.
- `Texture Editor` sharpen/soften now behave more predictably at low strength, and empty edit layers no longer feel broken when `Sample visible layers` is enabled because filter strokes can now read from the merged visible image while still writing into the active layer.
- `Texture Editor` history can now be cleared intentionally so the current document state becomes the new editing baseline, instead of forcing old trial steps to stay in the session forever.
- `Texture Editor` selection copy/paste and move behavior is now much closer to an editor workflow: `Ctrl+C` / `Ctrl+V` respect the current selection, pasted selections become isolated layers instead of whole-image copies, the live selection is cleared after paste so move works on the copied piece, and hiding the original layer now leaves the pasted selection layer visible by itself.
- `Texture Editor` now offers both in-place paste and centered paste for copied layers/selections, and the canvas move-preview path no longer references brush-only overlay state.
- `Texture Editor` floating selections now survive undo/redo correctly, masked cuts no longer clear the entire bounding box for soft/lasso selections, and floating transform state no longer drops out of history just because the move/commit/cancel path changed.
- `Texture Editor` adjustment sliders now preview live without spamming the history list on every tick, and project open/save plus flattened export now run through background workers instead of blocking the UI thread during heavier document operations.
- `Texture Editor` adjustment preview no longer drops selection and makes the controls look disabled while dragging sliders, because live preview now preserves the current adjustment instead of rebuilding the whole list state mid-drag.
- `Texture Editor` now supports finer minimum paint/erase/sharpen/soften footprints and faster `Alt+click` color sampling for paint/fill work, which makes tiny cleanup edits easier on high-resolution textures.
- The left Texture Editor action rail no longer clips utility buttons like `Shortcuts`, because the edit controls were reflowed into a more compact two-row layout instead of being squeezed into one narrow row.
- `Texture Editor` now keeps the canvas at the real scaled image size instead of stretching textures into a forced minimum square, and wheel zoom also works reliably when the pointer is over the texture through the scroll viewport instead of only when the wheel event lands on the canvas widget itself.
- `Texture Editor` floating selections now survive undo/redo correctly in non-checkpoint history restores, project save/load now preserves in-progress floating raster content, reopening an already-open source refreshes the source binding metadata, and wheel zoom behaves more reliably on precision scrolling input while anchoring correctly under the pointer.
- `Texture Editor -> Replace Assistant` handoff now preserves original texture binding metadata even when the editor was opened from Archive Browser, Compare, or loose-file paths, and Replace Assistant review no longer blocks the UI thread while waiting synchronously for the previous preview worker to stop.
- New `Texture Editor` gradient/patch/smudge/dodge-burn paths now route through the editor core correctly instead of being UI-only stubs, and Dodge/Burn no longer fails on the first real stroke because of a bad blend-weight shape.
- Channel-aware editor operations now respect the current `RGBA` edit locks for fill, gradient, brush retouch, and recolor flows instead of always writing all visible channels.
- Replace Assistant now respects the `overwrite existing package files` setting for `info.json` / `.no_encrypt`, and README dependency notes now match the actual bundled/editor runtime stack (`Pillow`, `numpy`, `OpenCV`).
- New textures in `Texture Editor` now open at true 100% zoom instead of being forced into fit-to-window mode, and the zoom readouts now follow the live canvas state correctly instead of leaving stale percentage text behind.
- `Replace Assistant` now imports and matches added files through a background worker with visible status/progress updates, so `Add Files` / `Add Folder` no longer feel like a silent freeze while the app indexes originals and matches imported textures.
- Portable builds now explicitly collect `NumPy`, `OpenCV`, and `Pillow` assets for the new editor stack instead of depending on those libraries only being present in the development environment.
- Archive scan completion no longer eagerly rebuilds the heaviest `Replace Assistant` and `Research` archive indices on the UI thread every time the cache loads, which reduces the short freeze that could happen right after startup archive hydration.
- Startup archive auto-load no longer forces the Archive Browser tab to render immediately if you are working elsewhere, which reduces the visible startup hitch when the cache finishes loading in the background.
- `Settings` now includes an opt-in crash-detail capture toggle that writes local traceback reports for unhandled exceptions and background-worker/archive-preview errors, and the latest crash report is included in the diagnostic bundle when available.
- Archive DDS preview now supports legacy luminance (`DDPF_LUMINANCE`) files, and partial luminance DDS reconstruction now handles raw uncompressed surface sizing correctly, which fixes previously unsupported worldmap `*_sdf_*` previews such as `cd_worldmap_image_compass_sdf_1024x1024.dds` and `cd_worldmap_image_mountain_10026_sdf_2048x2048.dds`.
- Loose DDS preview fallback no longer cascades into a second failure when DDS metadata parsing is unsupported, and archive preview workers now avoid eagerly decoding both archive and loose preview images when only the default archive preview is needed.
- `Research` now passes cancellation all the way through classification-review group assembly, so a cancelled refresh stops more promptly instead of continuing through the final unknown/classified review grouping work.

### Docs
- Updated README, Quick Start, About, and release notes for the current `Texture Editor` feature set, including the newer retouch tools, channel workflow, and deeper brush controls.

## [0.5.5] - 2026-04-12

### Added
- A persistent local texture-classification registry plus `Research -> Archive Insights -> Classification Review`, so you can review unresolved DDS files, approve a label once, and reuse that approval in future scans and texture-policy planning.
- `Classification Review` now includes an inline archive-style preview, filters for `Name` / `Package`, bulk selection helpers, optional already-classified review, and a file-focused queue that works better on the real archive data than the original family/member split.
- `Start` now performs a pre-run unclassified-DDS check for upscale builds, warns when matched files are still `unknown`, and can jump directly into `Research -> Classification Review` focused on the current run’s unknown DDS files before any build phases begin.
- `Research -> References` now includes `Review In Text Search`, which opens the selected XML/material source file in `Text Search` and highlights the referenced DDS name so you can inspect the exact text-side usage quickly.

### Changed
- Removed the retired direct alternate Python-based upscale backend, its setup/import workflow, and related UI/runtime paths so the app now only exposes direct `Real-ESRGAN NCNN` or external `chaiNNer` for upscaling.
- `Classification Review` now uses the selected file as the main review unit, while bulk actions still apply across the underlying family where that is actually useful.

### Fixed
- The pre-run unclassified-DDS prompt no longer fails before build start, because the GUI classification check now uses the public planner entry point instead of referencing a private backend-matrix helper that was not available in the UI module.
- The pre-run unclassified-DDS prompt now correctly resumes into the build after you classify files and restart, instead of stopping after the “0 matched DDS file(s) are still unclassified” check while the utility worker was still cleaning up.
- Text Search preview no longer gets stuck on `Preparing preview...`, because the preview-ready handler now uses the current result context correctly instead of referencing an undefined local when applying syntax highlighting.
- Local classification approvals now apply correctly to both archive-style and extracted package-prefixed DDS paths, so classifying a file in `Research` is reused by later loose DDS workflow runs.
- Archive-wide DDS classification now recognizes low-risk grayscale/scalar suffixes such as `_grayscale` and `_depth_grayscale` as technical mask data, recognizes `pivotpainter` DDS names as vector-style data, and groups `_ct` variants back into their base texture families instead of splitting them into separate review groups.
- A few obvious suffixless archive misses now classify correctly too, so names such as `snownormal`, `snowmask`, and `nonetexturespecular` no longer stay `unknown` just because they omit the usual underscore-separated token.
- Added a few more conservative archive suffix/classification fixes, including `_1bit`, `_mask_1bit`, `_pivotpos`, `_mask_amg`, and safer handling for bare `rough` names that were previously too easy to misread as roughness maps.
- `Research` archive snapshot work now honors cancellation during the heavy archive-insight pass, and `Mip Analysis` detail views now reuse refresh-time family/path metadata instead of rescanning both DDS roots every time you click a row.
- Loose Text Search file discovery now honors cancellation during the initial loose-file walk, and matched-file preview loading now runs through a debounced worker instead of blocking the UI thread on selection changes.
- Archive Browser preview workers now preload decoded preview images in the worker thread, and preview jobs are now stoppable so stale or shutdown-time preview work can be cancelled instead of only waiting for threads to finish.
- Preview widgets now cache scaled preview pixmaps by source and target size, and failed path-backed image loads are remembered so large previews no longer get repeatedly resampled or retried on every resize/fit update.
- `Research -> Classification Review` now hides the redundant right-side `Archive Files` panel while you review labels, keeps the inline preview as the primary visual aid, and can optionally include already classified DDS families when you want to apply a custom override anyway.
- The expert unsafe technical override now really overrides preset-based preserve behavior for technical textures unless an explicit texture rule still says `skip` or forces a preserve/high-precision path.
- Legacy correction modes now honor planner-visible candidates, and planner-visible `unknown` textures with straight alpha now get the same bounded alpha-correction allowance as other visible textures.
- `chaiNNer` override JSON now fails early and clearly when it references `${staging_png_root}` while DDS staging is disabled, instead of quietly substituting an empty string and failing later in the run.
- `Retry with smaller tile` now keeps `tile size 0` as a true full-frame first attempt and only switches into the fixed tiled fallback ladder `512, 256, 128, 64, 32` after that full-frame attempt fails.
- `Research` no longer fails on startup after the `Unknown Resolver` UI addition, because the missing `QComboBox` import is now included in the Research tab widgets.

### Docs
- Updated README/help/release wording to reflect the local classification registry, the refined `Classification Review` workflow, and the app now being `NCNN` / `chaiNNer` only for upscaling.

## [0.5.0] - 2026-04-12

### Added
- Automatic `Source Match` reconstruction modes for direct `Real-ESRGAN NCNN` workflows, including `Source Match Balanced`, `Source Match Extended`, and `Source Match Experimental`.
- A planner-owned `technical_high_precision_path` for eligible non-packed scalar technical DDS files, with support for high-precision staged PNGs or validated direct `PNG root` inputs when the backend is disabled.
- An optional `NCNN extra args` field for advanced Real-ESRGAN NCNN flags such as `-dn 0.2`, with settings/profile persistence and command-line validation.
- An explicit expert override that can force technical maps such as normals, masks, roughness, height, and vectors through the generic visible-color PNG/upscale path when you intentionally want unsafe technical processing.

### Changed
- Texture policy is now planner-authoritative across preview, preflight, direct backend execution, DDS rebuild, `Compare`, and `Research`, so path/profile/backend/alpha decisions come from one shared per-texture plan instead of being re-inferred later in the run.
- Automatic texture policy now routes source-match correction per texture instead of expecting the user to know which post-correction mode belongs to which asset class.
- Built-in output behavior is now formalized through planner-selected processing profiles, explicit path kinds, centralized backend capability gating, and semantic/profile/intermediate overrides in texture rules.
- `chaiNNer` and direct `NCNN` capability handling now follows the same central planner matrix used by policy preview and preflight reporting.
- `Compare`, `Preview Policy`, and `Research` now surface richer planner metadata, including selected profile, processing path, backend compatibility, alpha policy, and preserve reasons.
- `Safe Wizard` has been replaced by a read-only `Run Summary` dialog, so the editable backend and texture-policy controls live only in the main Workflow panel while the dialog is reserved for source and run-context review.

### Fixed
- Planner-driven preserve handling is now more reliable for technical DDS files because technical textures no longer silently fall back into the generic visible-color PNG path.
- Scalar technical DDS files such as roughness, height/displacement, AO, metallic, specular, subsurface, emissive-intensity, and similar non-packed grayscale data can now rebuild through a safer high-precision path instead of always collapsing into preserve-only or generic color-path behavior.
- High-precision technical rebuilds now validate their `16-bit` grayscale-style PNG intermediates before use, and missing or invalid inputs are called out in preflight and fall back per file to preserving the original DDS instead of rebuilding from a bad intermediate.
- `Research` mip analysis and normal validation now include planner-path-aware warnings, making suspicious visible-color routing, suspicious high-precision routing, and scalar-format mismatches easier to catch during QA.
- The app no longer fails on startup when refreshing `chaiNNer` chain info, because the UI chain-analysis path now passes the staging PNG root expected by the planner-aware `chaiNNer` validator.
- Rebuild format precedence now respects manual `Match original DDS format` when automatic color/format rules are disabled, so visible color textures no longer get silently promoted to planner profile formats such as `BC7_UNORM_SRGB`.
- Automatic texture safety rules no longer inject extra texconv sRGB conversion flags for visible textures, which reduces the darker output shifts some users were seeing when the safety checkbox was enabled.
- `Source Match Balanced` and `Source Match Extended` no longer skip obviously color-like textures just because their semantic hint stayed `unknown`, as long as the planner already routed them through a visible-color profile.
- Browsing rebuilt DDS files in `Compare` is more responsive because compare preview application now avoids eagerly materializing full preview pixmaps on the UI thread, and rapid compare-row changes are briefly debounced before preview startup.
- Large DDS files in `Compare` now use a lighter display-preview cache capped for pane browsing, which reduces the lag from cold 4K preview generation/loading without changing the higher-detail preview path used by `Research` analysis.
- Archive Browser DDS preview no longer fails with `Preview failed: 'NoneType' object is not iterable` after the recent compare-preview refactor, because the shared preview command builder now always returns a valid texconv command.
- Archive Browser DDS preview now uses the lighter display-preview cache for pane browsing too, reducing freezes or long stalls when selecting larger DDS files.
- DDS staging for direct backend runs now passes the source DDS path correctly to texconv again, fixing cases where staging appeared to run but the NCNN stage immediately failed with `Expected planner-selected PNG does not exist`.
- Compare preview shutdown is now safer because queued preview work no longer respawns while the window is closing.
- Settings persistence and `chaiNNer` chain inspection are now debounced in the UI, reducing stalls from keystroke-by-keystroke disk syncs and chain revalidation.
- Preserve-only direct `NCNN` runs now skip the backend stage cleanly instead of scanning unrelated stale PNGs in `PNG root`.
- `Retry with smaller tile` now steps down correctly from a `tile size 0` full-frame attempt into real smaller tiles.
- `Research -> Mip Analysis` now only reports DDS files that exist in both Original and Output roots, instead of turning unmatched files into broken comparison rows.
- DDS preview cache invalidation now includes the active `texconv.exe`, so Compare and Research previews are refreshed when the texconv binary changes.
- Family-aware classification now upgrades base files such as `cd_wood_planks_02.dds` to color/albedo when sibling variants like `cd_wood_planks_02b.dds` and `cd_wood_planks_02c.dds` indicate a visible color texture family.
- Family-aware classification now also upgrades trailing-letter variant-only sets such as `cd_wood_planks_02a.dds` and `cd_wood_planks_02b.dds` to color/albedo variants even when the plain base file is missing from that package.
- Bare `rough` in names such as `cd_wood_rough_06.dds` is no longer treated as a hard roughness-map token, so material-name families can fall back to family/preview evidence instead of being misclassified as roughness.
- Compare preview is more defensive when browsing rebuilt DDS files because preview widgets now cache the decoded display image instead of re-reading the same preview file on every resize/zoom, and the compare display-preview cap has been lowered to reduce memory pressure on large upscaled textures.
- Compare preview loading now preloads the decoded display image in the worker thread before applying it to the UI, further reducing main-thread PNG decode work when Compare opens right after a build or when rapidly selecting rebuilt DDS files.
- Compare preview no longer goes blank after the worker-thread preload change, because preview widgets now correctly treat preloaded in-memory images as a valid preview source.

### Docs
- Updated README/help/release wording to reflect `Run Summary`, browser-only external setup/model pages, automatic `Source Match` correction, the high-precision technical path, the expert unsafe technical override, and the current direct-backend workflow.

## [0.4.1] - 2026-04-11

### Changed
- Setup download actions for `chaiNNer`, `texconv`, and `Real-ESRGAN NCNN` now open the official external pages in the user browser instead of downloading files inside the app.
- `NCNN Model Catalog` now exposes source/model pages and opens non-downloading external browser pages instead of downloading selected model files inside the app.
- `Research` refresh now computes archive-side grouping, classification, and heatmap data in one shared snapshot pass, and repeated refreshes can reuse that archive snapshot while the current archive view is unchanged.

### Fixed
- Archive Browser DDS preview is less likely to freeze the app while browsing cached archives because image preview loading now avoids eagerly materializing the full preview pixmap on the UI thread.
- Archive Browser DDS preview is more stable while rapidly browsing `.dds` entries because preview requests are now briefly debounced before worker startup.
- DDS preview cache generation is now serialized per cached source file, reducing random crashes or invalid preview loads when multiple fast preview requests hit the same cached PNG at nearly the same time.
- Automatic texture rules now preserve technical DDS files more reliably even when the upscale backend is disabled, instead of rebuilding some of them from staged PNGs.
- Normal maps that appear to use alpha are now rebuilt with an alpha-capable linear format instead of dropping alpha through the default BC5 path.
- Closing the app during long-running scans or `Research` refresh work now signals those workers to stop before thread shutdown, which makes shutdown behavior less rough.
- `Retry with smaller tile` now steps down through real fallback tile sizes even when the configured tile size is `0`.
- `Compare -> Mip Details` now clears its pending target when a `Research` refresh fails, avoiding stale focus jumps on the next refresh.
- `_ct` texture variants are now classified as color maps before loose token matching, reducing false roughness/metalness classification when the base name contains those words.
- The `Safe Upscale Wizard` now preserves caller-provided summary or notes text instead of overwriting it with its generated footer summary.
- `Research -> Archive Insights -> References` now drives the `Archive Files` picker to the relevant archive file when you select a reference or sidecar row, making it easier to inspect the specific `.dds` or related archive file in the current workflow.
- `Research -> Archive Insights -> References` now resolves nested archive folder paths more reliably when focusing the `Archive Files` picker from a selected reference or sidecar row.
- Closing the app during a long `Research` reference resolve now signals that resolver to stop before thread shutdown instead of leaving it to run to completion.
- `Research` refresh progress now reports the current archive snapshot, mip analysis, and normal-validation stages with consistent step counts instead of jumping over missing progress indices.
- Archive Browser refresh/scanning no longer errors when preparing the cached browser state, because `prepare_archive_browser_state` now accepts the worker cancellation token passed by the archive scan path.

## [0.4.0] - 2026-04-11

### Added
- `Research` tab for texture-focused support work, including:
  - texture-type classifier
  - texture set grouper
  - material-to-texture reference resolver
  - archive-side sidecar discovery
  - extract-related-set actions
  - mip/export report support
  - bulk normal validation
  - texture usage heatmap
  - local research notes
- `Safe Upscale Wizard` for guided backend, preset, retry, and export setup.
- Direct in-app upscaling backend support for:
  - `Real-ESRGAN NCNN`
- Setup actions for:
  - downloading and unpacking `Real-ESRGAN NCNN`
  - importing NCNN model files
- Grouped `NCNN Model Catalog` with:
  - short model descriptions
  - intended-use notes
  - source links
  - direct download for selected ready-to-use `.param` / `.bin` pairs
  - grouped recommendations for visible color/albedo, compressed color, cleaner color, stylized/UI, and experimental models
  - detected local NCNN models shown beside the built-in list
- Optional direct-backend post-upscale color correction modes:
  - `match_mean_luma`
  - `match_levels`
  - `match_histogram`
- Compare preview-size presets that scale both compare panes together.
- Mouse-wheel zoom on image previews in `Compare` and archive image preview.
- Quick `Mip Details` action in `Compare` that refreshes `Research`, opens `Texture Analysis`, and jumps to the selected compare file when a matching mip-analysis row exists.
- VS Code-style live-log highlighting for actions, statuses, paths, dimensions, texture tags, and key values.
- Archive Browser exclude filtering with:
  - custom semicolon-separated substring or glob exclusions
  - a one-click option to hide common DDS companion suffixes
  - a `Base / likely albedo images` role filter for easier base-texture browsing

### Changed
- Workflow upscaling now supports backend selection, texture-type-aware presets, automatic color/format safety rules, retry with smaller tile, and mod-ready loose export.
- `Init Workspace` now seeds the newer NCNN and mod-export path fields in addition to the original workspace folders.
- Real-ESRGAN NCNN setup now handles the current upstream Windows package layout, which may ship without bundled models, by creating a model folder automatically and prompting model import instead of failing.
- Safe Upscale Wizard and direct-backend help text now explain more clearly that presets only decide what gets sent to the upscaler, while the selected model can still shift brightness, contrast, and detail.
- Workflow now includes a `Preview Policy` action that shows a per-texture plan before `Start`, including inferred semantic subtype, action, alpha/intermediate policy, and planned DDS rebuild format.
- DDS parser support now includes legacy numeric `D3DFORMAT`-style FOURCC values used by some Crimson Desert float/vector DDS files.
- Texture-type classification and automatic policy rules now treat `height` / `displacement` / `bump` and `vector` / `position` style maps as higher-risk technical data instead of generic image textures.
- Semantic inference now uses a broader loose-sidecar text set (`.xml`, `.material`, `.shader`, `.json`, `.lua`, `.txt`, `.ini`, `.cfg`, `.yaml`, `.yml`) so displacement, packed-mask, and alpha-cutout intent can be inferred from neighboring material/shader files instead of filenames alone.
- Safer presets now preserve excluded technical DDS files by copying the original DDS through unchanged instead of rebuilding them from PNG intermediates.
- Preflight reporting now summarizes detected texture types, semantic subtypes, and per-texture action counts, and warns when float/vector DDS files are present, so risky PNG-intermediate cases are visible before a run starts.
- DDS Output help text now states more clearly where source PNGs, final PNGs, and rebuilt DDS files end up, and clarifies that `Use final PNG size for rebuilt DDS` only affects DDS dimensions.
- The direct-backend controls area is now hidden when `chaiNNer` is the active backend.
- Workflow now exposes `Texture Policy` as its own always-visible group, so preset/automatic-rule/export behavior is easier to find without opening `Safe Wizard`, while direct NCNN scale and tile controls stay clearly separated.
- Top-level tab order now places `Research` ahead of `Text Search`, and the `Research` tab now includes its own `Archive Files` picker so reference and note workflows do not require jumping back to `Archive Browser`.
- Archive related-set extraction prompts now state the destination path up front, explain that the extract root may be created automatically, and make overwrite-vs-keep-both behavior clearer before the extraction starts.
- `Archive Browser -> DDS To Workflow` now respects explicit archive selection first. If files or folders are selected, only selected DDS files are extracted to the workflow root; the filtered DDS view is used only when nothing is selected.
- `Research -> Texture Analysis` now explains where each result set comes from, what each panel requires, and shows the selected-row details in the right-side pane where `Archive Files` normally sits, so mip-analysis details have more room when that subtab is active.
- `Research -> Texture Analysis` now exposes richer texture QA details for matching DDS pairs, including file-size drift, color-space changes, preview-based alpha/brightness/channel checks when texconv previews are available, and extra texture-specific warnings for normals, packed masks, and grayscale technical maps.
- `Workflow -> Upscaling` now keeps the backend-specific area sized to the current backend page instead of inheriting the tallest backend page, reducing the wasted empty space when direct NCNN pages are selected.
- Texture classification is now more tolerant of Crimson Desert-style texture sets by recognizing suffixes and explicit names such as `_cd`, `_sp`, `_m`, `_ma`, `_mg`, `_o`, `_disp`, `_dmap`, `_dr`, `_op`, `_wn`, `_emc`, `_emi`, `_subsurface`, `_color`, `_normal`, digit-letter variants like `63a`, family companions, and preview-based fallback hints when names are still ambiguous; `_d` is no longer treated as a strong diffuse/color signal and is instead handled as lower-confidence grayscale/support data.
- `Research`, `Texture Analysis`, normal validation, mip-detail hints, and `Archive Browser` role/exclude filtering now use the same updated suffix semantics, so technical companions such as `_wn`, `_ma`, `_mg`, `_o`, `_dmap`, `_dr`, `_op`, `_emc`, `_emi`, and `_subsurface` are less likely to be mistaken for base/albedo textures.
- Direct Real-ESRGAN NCNN workflow controls now expose optional post-correction modes in both `Workflow` and `Safe Upscale Wizard`, and build/preflight logs now report the selected correction mode.
- `Compare` now acts as a focused review mode: the progress area collapses while `Compare` is active, the top chrome is more compact, the default compare splitter favors preview space more strongly, and previews stay top-aligned instead of floating in the middle of the pane.
- Compare review now supports shared preview-size presets, wheel zoom, drag pan, per-side zoom, and stronger space prioritization so side-by-side review is easier on smaller or scaled displays.
- Workflow, Research, Text Search, archive preview, and global theme sizing were adjusted to behave better under UI scaling, including safer button/progress heights, tab/group title spacing, and toolbar wrapping in dense panes.
- The right-side workflow layout now remembers a normal progress-panel size separately from Compare focus mode so switching tabs does not save a broken collapsed state.

### Fixed
- Archive Browser DDS preview is less likely to freeze the app while browsing cached archives because image preview loading now avoids eagerly materializing the full preview pixmap on the UI thread.
- Restored the missing workspace helper functions used by `Init Workspace` and `Create Folders`, which caused `name 'create_missing_directories_for_config' is not defined` style failures in the Setup section.
- Profile export and diagnostic bundle export now serialize config data correctly for slotted dataclasses, fixing `vars() argument must have __dict__ attribute` failures.
- Harmless chaiNNer shutdown/deprecation noise such as `body not consumed` and `log.catchErrors is deprecated` is now filtered so successful runs do not look like hard failures.
- Legacy float/vector DDS files that previously failed with unsupported FOURCC errors now parse and rebuild correctly, including real tested cases such as `pivotpos` and `xvector` effect textures.
- Runs that select an upscale backend but end up preserving every matched DDS under the current preset/automatic rules no longer fail early on missing NCNN / chaiNNer runtime setup; backend validation is now deferred until files actually require PNG/upscale processing.
- Backend/staging/PNG indexing work is now skipped when the current semantic policy keeps every matched DDS out of the PNG path, avoiding unnecessary empty-stage work and confusing stale-PNG scans.
- `Research -> Archive Insights -> Groups` selection is now more robust: the first group is auto-selected after refresh, the extract button reflects whether a valid group is selected, and selecting either a group row or one of its member rows resolves correctly for `Extract Selected Set`.
- `Research -> Archive Insights -> Groups` now warns explicitly when the research snapshot has not been built yet, so clicking `Extract Selected Set` before `Refresh Research` no longer feels like a silent failure.
- `Research -> Texture Analysis` no longer repeats the same brightness-range warning in both `Preview comparison` and `Additional analysis warnings` for the same DDS pair.
- Compare/preview sizing no longer wastes as much vertical space above the images, and stale saved splitter states from earlier layouts no longer force the progress block back into an oversized or clipped state.
- Compare previews now use the actual displayed scale when zooming out of `Fit`, avoiding the earlier jumpy behavior where zoom started from an assumed `100%` baseline instead of the real fitted size.

### Docs
- Rewrote `README.md` around the current app structure, including direct NCNN support, `Safe Upscale Wizard`, `Texture Policy`, `Preview Policy`, `Research`, compare review workflow, and troubleshooting guidance.
- Updated the in-app `Quick Start` guide so it now describes the current safe-first workflow, backend choices, texture-policy safety behavior, compare controls, and `Research` usage more clearly.
- Expanded `Unreleased/In testing` notes to include the recent compare UX, preview interaction, live-log, and UI-scaling changes.

## [0.3.0] - 2026-04-08

### Added
- New global `Settings` tab for persistent app-wide preferences such as theme, startup behavior, layout memory, and cleanup confirmations.

### Changed
- Archive refresh and cache-building performance were optimized significantly by fixing the real bottlenecks in `.pamt` parsing and cache generation.
- On the large development archive set used during testing, full refresh + cache build dropped from roughly `315s` to about `4s` (about `99%` faster), while cached tree preparation dropped from about `3.7s` to `2.0s`.
- Archive tree/browser-state preparation was also reduced further during cached loads.
- README was reorganized into a shorter, more scannable structure.

### Fixed
- Removed the experimental 3D/model viewer path from the live app so the shipped workflow stays focused and stable.
- Removed the top-menu theme picker now that theme selection lives in `Settings`.

## [0.2.1] - 2026-04-08

### Changed
- Windows build output now uses a versioned release-style filename pattern such as `CrimsonForgeToolkit-<version>-windows-portable.exe`.

## [0.2.0] - 2026-04-08

### Added
- Broader archive package root auto-detect support for common non-Steam installs, including custom `Games` folders and shallow `XboxGames` / `ModifiableWindowsApps` style layouts.
- Environment-variable overrides for archive package root detection:
  - `CRIMSON_TEXTURE_FORGE_PACKAGE_ROOT`
  - `CRIMSON_DESERT_PACKAGE_ROOT`
- New read-only `Text Search` tab for archive or loose text-like files, with content search, highlighted preview, and export of matched files while preserving folder structure.
- Archive text search now supports deterministic ChaCha20 decryption for supported encrypted XML entries, so those files can be searched, previewed, and exported as readable text.
- Editor-style text preview with syntax coloring, line numbers, local find/next/previous navigation, wrap toggle, and font-size controls.

### Changed
- Archive auto-detect now reports that it is checking known install locations instead of only Steam libraries.
- Text Search preview now uses a larger three-pane layout and shows full text for normal-sized files with clearer match highlighting.
- Text Search results now prioritize file name first, while keeping the full relative path visible in a dedicated column and tooltips.
- Small-window layout pressure was reduced slightly so the workflow and utility panes degrade more gracefully.

### Fixed
- Text Search preview font size controls now update the editor text, gutter, and document font correctly.

## [0.1.0] - 2026-04-07

### Added
- Initial public release of Crimson Forge Toolkit.
- Read-only `.pamt` / `.paz` archive browser with selective DDS extraction.
- Archive cache for faster repeated archive scans.
- Loose DDS scan/filter workflow.
- Optional DDS-to-PNG conversion with `texconv`.
- Optional external `chaiNNer` stage before DDS rebuild.
- DDS rebuild with configurable format, size, and mip behavior.
- Side-by-side DDS compare view with zoom and pan.
- Profile export/import and diagnostic bundle export.
- Built-in Quick Start and About dialogs.

### Changed
- App configuration is stored beside the executable for portable use.

### Docs
- Added project README, dependency notes, credits, limitations, and screenshots.
