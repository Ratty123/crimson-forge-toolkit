# Changelog

All notable changes to this project should be documented in this file.

The format is intentionally simple:

- `Added` for new features
- `Changed` for behavior or workflow changes
- `Fixed` for bug fixes
- `Docs` for README, guide, or release-note changes

## [Unreleased]

### Added
- Broader archive package root auto-detect support for common non-Steam installs, including custom `Games` folders and shallow `XboxGames` / `ModifiableWindowsApps` style layouts.
- Environment-variable overrides for archive package root detection:
  - `CRIMSON_TEXTURE_FORGE_PACKAGE_ROOT`
  - `CRIMSON_DESERT_PACKAGE_ROOT`

### Changed
- Archive auto-detect now reports that it is checking known install locations instead of only Steam libraries.

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
