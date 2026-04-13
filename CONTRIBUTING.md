# Contributing

Thanks for helping improve Crimson Texture Forge.

## Before You Open An Issue

- Use the latest release or beta build first.
- Check the [CHANGELOG.md](CHANGELOG.md) to see if the problem was already fixed.
- If the issue is a crash or preview failure, include the app version and any log or crash-report details you have.

## Good Bug Reports

Please include:

- app version, and whether you used the portable EXE or a source checkout
- what you were doing when the problem happened
- expected result vs actual result
- steps to reproduce
- relevant file paths or archive paths, if safe to share
- screenshots or logs when helpful

For preview/build problems, it helps a lot to include:

- the DDS or PNG filename
- whether it came from the archive or a loose file
- the selected backend or workflow mode
- whether crash capture was enabled in `Settings`

## Feature Requests

Feature requests are welcome, especially if they explain:

- the modding workflow you are trying to accomplish
- what is currently awkward or too manual
- what result you want the app to produce

Concrete examples are much more useful than broad tool wishlists.

## Pull Requests

Small, focused pull requests are easier to review than large mixed changes.

Please try to:

- keep the scope narrow
- explain user-facing behavior changes clearly
- mention any affected workflows such as `Texture Workflow`, `Replace Assistant`, `Research`, or `Archive Browser`
- avoid unrelated cleanup in the same PR

## Project Scope

Crimson Texture Forge is intentionally centered on:

- read-only archive browsing
- loose-file DDS and PNG workflows
- rebuild, review, classification, and mod-package export

It does not currently aim to write back into game archives directly.
