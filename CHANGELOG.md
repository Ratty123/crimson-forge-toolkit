# Changelog

All notable changes to this project should be documented in this file.

The format is intentionally simple:

- `Added` for new features
- `Changed` for behavior or workflow changes
- `Fixed` for bug fixes
- `Docs` for README, guide, or release-note changes

## [0.5.0] - 2026-04-12

### Added
- Automatic `Source Match` reconstruction modes for direct `Real-ESRGAN NCNN` and `ONNX Runtime` workflows, including `Source Match Balanced`, `Source Match Extended`, and `Source Match Experimental`.
- A planner-owned `technical_high_precision_path` for eligible non-packed scalar technical DDS files, with support for high-precision staged PNGs or validated direct `PNG root` inputs when the backend is disabled.
- An optional `NCNN extra args` field for advanced Real-ESRGAN NCNN flags such as `-dn 0.2`, with settings/profile persistence and command-line validation.
- An explicit expert override that can force technical maps such as normals, masks, roughness, height, and vectors through the generic visible-color PNG/upscale path when you intentionally want unsafe technical processing.

### Changed
- Texture policy is now planner-authoritative across preview, preflight, direct backend execution, DDS rebuild, `Compare`, and `Research`, so path/profile/backend/alpha decisions come from one shared per-texture plan instead of being re-inferred later in the run.
- Automatic texture policy now routes source-match correction per texture instead of expecting the user to know which post-correction mode belongs to which asset class.
- Built-in output behavior is now formalized through planner-selected processing profiles, explicit path kinds, centralized backend capability gating, and semantic/profile/intermediate overrides in texture rules.
- `chaiNNer`, direct `NCNN`, and direct `ONNX` capability handling now follows the same central planner matrix used by policy preview and preflight reporting.
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
- Preserve-only direct `NCNN` / `ONNX` runs now skip the backend stage cleanly instead of scanning unrelated stale PNGs in `PNG root`.
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
- Unsupported ONNX models, including unusual `2`-channel outputs, now fail earlier with a clearer compatibility error instead of a late generic conversion failure.
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
  - `ONNX Runtime`
- Setup actions for:
  - downloading and unpacking `Real-ESRGAN NCNN`
  - importing NCNN model files
  - importing ONNX model files
  - opening the official `ONNX Runtime` install guide
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
- `Init Workspace` now seeds the newer NCNN / ONNX / mod-export path fields in addition to the original workspace folders.
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
- Workflow now exposes `Texture Policy` as its own always-visible group, so preset/automatic-rule/export behavior is easier to find without opening `Safe Wizard`, while direct NCNN / ONNX scale and tile controls stay clearly separated.
- Top-level tab order now places `Research` ahead of `Text Search`, and the `Research` tab now includes its own `Archive Files` picker so reference and note workflows do not require jumping back to `Archive Browser`.
- Archive related-set extraction prompts now state the destination path up front, explain that the extract root may be created automatically, and make overwrite-vs-keep-both behavior clearer before the extraction starts.
- `Archive Browser -> DDS To Workflow` now respects explicit archive selection first. If files or folders are selected, only selected DDS files are extracted to the workflow root; the filtered DDS view is used only when nothing is selected.
- `Research -> Texture Analysis` now explains where each result set comes from, what each panel requires, and shows the selected-row details in the right-side pane where `Archive Files` normally sits, so mip-analysis details have more room when that subtab is active.
- `Research -> Texture Analysis` now exposes richer texture QA details for matching DDS pairs, including file-size drift, color-space changes, preview-based alpha/brightness/channel checks when texconv previews are available, and extra texture-specific warnings for normals, packed masks, and grayscale technical maps.
- `Workflow -> Upscaling` now keeps the backend-specific area sized to the current backend page instead of inheriting the tallest backend page, reducing the wasted empty space when direct NCNN / ONNX pages are selected.
- Texture classification is now more tolerant of Crimson Desert-style texture sets by recognizing suffixes and explicit names such as `_cd`, `_sp`, `_m`, `_ma`, `_mg`, `_o`, `_disp`, `_dmap`, `_dr`, `_op`, `_wn`, `_emc`, `_emi`, `_subsurface`, `_color`, `_normal`, digit-letter variants like `63a`, family companions, and preview-based fallback hints when names are still ambiguous; `_d` is no longer treated as a strong diffuse/color signal and is instead handled as lower-confidence grayscale/support data.
- `Research`, `Texture Analysis`, normal validation, mip-detail hints, and `Archive Browser` role/exclude filtering now use the same updated suffix semantics, so technical companions such as `_wn`, `_ma`, `_mg`, `_o`, `_dmap`, `_dr`, `_op`, `_emc`, `_emi`, and `_subsurface` are less likely to be mistaken for base/albedo textures.
- Direct Real-ESRGAN NCNN / ONNX workflow controls now expose optional post-correction modes in both `Workflow` and `Safe Upscale Wizard`, and build/preflight logs now report the selected correction mode.
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
- Runs that select an upscale backend but end up preserving every matched DDS under the current preset/automatic rules no longer fail early on missing NCNN / ONNX / chaiNNer runtime setup; backend validation is now deferred until files actually require PNG/upscale processing.
- Backend/staging/PNG indexing work is now skipped when the current semantic policy keeps every matched DDS out of the PNG path, avoiding unnecessary empty-stage work and confusing stale-PNG scans.
- `Research -> Archive Insights -> Groups` selection is now more robust: the first group is auto-selected after refresh, the extract button reflects whether a valid group is selected, and selecting either a group row or one of its member rows resolves correctly for `Extract Selected Set`.
- `Research -> Archive Insights -> Groups` now warns explicitly when the research snapshot has not been built yet, so clicking `Extract Selected Set` before `Refresh Research` no longer feels like a silent failure.
- `Research -> Texture Analysis` no longer repeats the same brightness-range warning in both `Preview comparison` and `Additional analysis warnings` for the same DDS pair.
- `ONNX Runtime` direct upscale support now accepts 4-channel models more reliably, including RGBA input tensors that previously failed or mis-routed alpha-aware inputs.
- Compare/preview sizing no longer wastes as much vertical space above the images, and stale saved splitter states from earlier layouts no longer force the progress block back into an oversized or clipped state.
- Compare previews now use the actual displayed scale when zooming out of `Fit`, avoiding the earlier jumpy behavior where zoom started from an assumed `100%` baseline instead of the real fitted size.

### Docs
- Rewrote `README.md` around the current app structure, including direct NCNN / ONNX support, `Safe Upscale Wizard`, `Texture Policy`, `Preview Policy`, `Research`, compare review workflow, and troubleshooting guidance.
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
- Windows build output now uses a versioned release-style filename pattern such as `CrimsonTextureForge-<version>-windows-portable.exe`.

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
- Initial public release of Crimson Texture Forge.
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
