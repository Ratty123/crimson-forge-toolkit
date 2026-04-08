# Changelog

All notable changes to this project should be documented in this file.

The format is intentionally simple:

- `Added` for new features
- `Changed` for behavior or workflow changes
- `Fixed` for bug fixes
- `Docs` for README, guide, or release-note changes

## [Unreleased]

### Added
- Placeholder for future changes.

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
