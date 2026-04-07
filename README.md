# Crimson Texture Forge

Crimson Texture Forge is a Windows desktop tool for **Crimson Desert texture workflows**.

It is built for people who need one place to:

- browse and extract assets from Crimson Desert archives
- convert loose DDS files to PNG with `texconv`
- optionally run an external upscale step through `chaiNNer`
- rebuild final DDS output with controlled format, size, and mip settings
- compare original and rebuilt DDS files side by side

The app is intentionally focused on **safe, read-only archive access** and **loose-file texture workflows**. It does **not** patch, repack, or write back into `.pamt` / `.paz` archives.

## What The App Is For

Crimson Texture Forge is meant for workflows like:

1. Extract DDS textures from Crimson Desert archives
2. Convert them to PNG
3. Optionally upscale or edit those PNG files
4. Rebuild DDS output with `texconv`
5. Review the result in the built-in compare view

It is useful whether you:

- already have loose DDS files
- want to extract DDS files from archives first
- want to use `chaiNNer`
- want to skip `chaiNNer` and only prepare PNG files

## Core Features

- Read-only `.pamt` / `.paz` archive browser
- Archive extraction to normal folders
- Archive cache for faster repeated scans
- DDS preview and compare support through `texconv`
- Loose DDS scanning and filtering
- Optional DDS-to-PNG conversion before processing
- Optional `chaiNNer` stage before DDS rebuild
- Configurable DDS output:
  - match original format
  - custom format
  - original size, PNG size, or custom size
  - original mip count, full chain, single mip, or custom mip count
- Texture rules and include filters
- Local auto-save config beside the EXE
- Profile export/import
- Diagnostic bundle export

## Safety And Scope

What the app does:

- reads archive indexes and data
- extracts selected files to normal folders
- processes loose files in workspace folders
- writes loose PNG/DDS output to folders you choose

What the app does not do:

- repack `.pamt` / `.paz`
- modify game archives in place
- guarantee that an external `chaiNNer` chain is valid
- guarantee correct output for every texture type without user judgment

## Main Workflow Modes

### 1. Loose DDS workflow

Use this if you already have DDS files in normal folders.

Typical flow:

1. Open `Setup`
2. Click `Init Workspace`
3. Set or download `texconv.exe`
4. Set `Original DDS root`, `PNG root`, and `Output root`
5. Click `Scan`
6. If you want PNG files first, enable `Convert DDS to PNG before processing`
7. Click `Start`
8. Review results in `Compare`

### 2. Archive Browser to workflow

Use this if your source DDS files are still inside Crimson Desert archives.

Typical flow:

1. Open `Archive Browser`
2. Set or auto-detect `Package root`
3. Click `Scan` or `Refresh`
4. Filter the archive tree
5. Use `DDS To Workflow` or extract DDS files to a normal folder
6. Switch to `Workflow`
7. Click `Scan`
8. Run a small test first

### 3. Optional chaiNNer workflow

If enabled, Crimson Texture Forge launches `chaiNNer`, waits for it to finish, and only then continues with DDS rebuild.

Typical flow:

1. Set up the normal workflow first
2. Set or download `chaiNNer.exe`
3. Create and test your `.chn` chain in `chaiNNer` itself first
4. Install the backends/packages your chain needs inside `chaiNNer`
5. Enable `Run chaiNNer before DDS rebuild`
6. If your chain expects PNG input, enable `Convert DDS to PNG before processing`
7. Point the chain at `${staging_png_root}`, `${png_root}`, or another matching folder
8. Run a small test first

## Important ChaiNNer Notes

`chaiNNer` is **optional** and **external**.

Crimson Texture Forge can:

- launch `chaiNNer`
- inspect a `.chn` file
- report likely path mismatches
- warn about missing output

Crimson Texture Forge cannot:

- build your chain for you
- install your model nodes automatically
- guarantee that your chain is valid
- guarantee that a chain that works in GUI will also behave identically in CLI mode

Before using `chaiNNer`, make sure you understand:

1. You must install `chaiNNer` separately.
2. You should launch `chaiNNer` directly at least once.
3. You must install any required dependencies for your chain inside `chaiNNer`, such as:
   - PyTorch
   - NCNN
   - ONNX / ONNX Runtime
4. You must provide your own `.chn` chain.
5. Your chain must include the model and nodes you want to use.
6. Your chain must read the correct input type from the correct folder.

If your chain expects PNG files:

- enable `Convert DDS to PNG before processing`
- make the chain read `${staging_png_root}` or another PNG folder

If your chain reads DDS directly:

- verify that behavior in `chaiNNer` itself first
- do not enable DDS-to-PNG conversion unless your chain is meant to use PNG output

Supported override tokens:

- `${original_dds_root}`
- `${staging_png_root}`
- `${png_root}`
- `${output_root}`
- `${texconv_path}`

## DDS Output Behavior

Final DDS output is built with `texconv`.

You can:

- match the original DDS format
- pick a custom output format
- keep original size
- keep PNG size
- use a custom size
- match original mip count
- generate a full mip chain
- force single mip
- use a custom mip count

You can also apply:

- include filters
- per-pattern texture rules

This is useful for handling different texture classes differently, such as:

- albedo / color maps
- normals
- masks / material maps
- UI textures

## Archive Browser

The archive browser is read-only and is intended for:

- scanning archive indexes
- filtering by package/folder/path
- previewing supported assets
- extracting selected files
- handing DDS files off into the workflow

It supports:

- `Scan`
  Uses saved archive cache when valid
- `Refresh`
  Ignores cache and rebuilds it from current `.pamt` files
- package and folder filtering
- dynamic structure filters
- preview for many DDS and text-like assets
- selective extraction to loose folders
- `DDS To Workflow`
  Extracts DDS files directly into the workflow root

Notes:

- archive support is read-only
- some unusual DDS preview cases are still best-effort
- large archive sets can still take time to scan or refresh

## Compare View

Use the `Compare` tab after a build to inspect:

- original DDS vs rebuilt DDS
- metadata on both sides
- zoom and pan
- output quality differences

This is intended to make quick QA easier before you scale up to a larger batch.

## Important Paths

- `Original DDS root`
  Loose DDS files used as the source scan set
- `PNG root`
  PNG files used for rebuild or manual preparation
- `Staging PNG root`
  Intermediate PNG folder used when DDS-to-PNG conversion is enabled before `chaiNNer`
- `Output root`
  Final rebuilt DDS output
- `Extract root`
  Archive extraction target when not extracting directly into the workflow

## First-Run Checklist

1. Use `Init Workspace`
2. Configure or download `texconv.exe`
3. Set the workflow paths
4. If needed, scan archives and extract a small DDS subset first
5. Click `Scan`
6. Run a small test before doing a full batch
7. Review the result in `Compare`
8. Only then scale up

## Config, Cache, Profiles, Diagnostics

The app stores its local state beside the EXE.

Main local files/folders:

- `CrimsonTextureForge.cfg`
  Local app settings
- `archive_cache`
  Cached archive scan data

The app also supports:

- profile export/import
- diagnostic bundle export

Diagnostic bundles are useful for issue reports because they can include:

- current config snapshot
- live log
- archive log
- chain validation summary
- documentation references

## Dependencies

### Python packages used by the app

- `PySide6`
- `PyInstaller`
- `lz4`

Build requirements are listed in `requirements-build.txt`.

### External tools used by the app

- `texconv.exe` from Microsoft DirectXTex
- `chaiNNer` for the optional upscaling stage

These tools are external projects with their own licenses and release cycles.

## Known Limitations

- Archive browsing is read-only only; this app does not repack archives.
- Archive preview support is best-effort for some unusual or partially reconstructed DDS cases.
- Very large archive scans and cache rebuilds can still take time.
- `chaiNNer` remains an external dependency and is only as reliable as the chain and dependencies you provide.
- Some chain behavior cannot be fully inferred from the `.chn` file alone.
- Final DDS quality still depends on source PNG quality, chosen `texconv` settings, and texture type.

## Packaging

Build helper:

- `build_pyside6_app.ps1`

Current packaging target:

- `--windowed --onefile`

## Development

Typical local setup:

1. Create a virtual environment
2. Install requirements
3. Run from source
4. Build the EXE when needed

Example:

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements-build.txt
.\.venv\Scripts\python crimson_texture_forge_app.py
```

One-file build:

```powershell
powershell -ExecutionPolicy Bypass -File .\build_pyside6_app.ps1
```

## Credits And References

Crimson Texture Forge uses or depends on the following:

- `PySide6 / Qt` for the desktop UI
- `PyInstaller` for packaging
- `DirectXTex / texconv` by Microsoft for DDS conversion and preview generation
- `chaiNNer` for the optional external upscaling stage
- `lz4` for archive decompression support

Archive support and compatibility work were informed by community reverse-engineering and tool behavior around Crimson Desert packages, especially:

- `lazorr410/crimson-desert-unpacker`
- [Crimson Browser & Mod Manager](https://www.nexusmods.com/crimsondesert/mods/84)

Please also see:

- `LICENSE`
- `THIRD_PARTY_NOTICES.md`

## License

This repository is currently published under the `MIT` license. See `LICENSE`.
