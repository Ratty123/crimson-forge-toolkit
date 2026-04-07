# Third-Party Notices

Crimson Texture Forge uses or interoperates with several third-party projects and tools.

This file is a practical notice list for repository and release packaging. For authoritative license text, copyright ownership, and redistribution terms, always refer to the upstream project itself.

## Python / App Dependencies

### PySide6 / Qt for Python

- Purpose: desktop UI framework
- Upstream: https://doc.qt.io/qtforpython-6/
- Notes: this project provides the Qt for Python bindings used by the app UI

### PyInstaller

- Purpose: Windows executable packaging
- Upstream: https://pyinstaller.org/
- Notes: used to build the one-file Windows executable

### python-lz4

- Purpose: archive decompression support
- Upstream: https://github.com/python-lz4/python-lz4
- Notes: used for supported compressed archive entry handling

### cryptography

- Purpose: archive XML decryption support for text search / preview / export
- Upstream: https://github.com/pyca/cryptography
- Notes: used for deterministic ChaCha20 archive payload decryption where supported

## External Tools

### DirectXTex / texconv

- Purpose: DDS preview conversion, DDS staging, and final DDS rebuild
- Upstream: https://github.com/microsoft/DirectXTex
- Release assets: https://github.com/microsoft/DirectXTex/releases
- Notes: Crimson Texture Forge can download `texconv.exe`, but it remains a separate upstream tool

### chaiNNer

- Purpose: optional external upscaling stage
- Upstream: https://chainner.app/
- Download page: https://chainner.app/download/
- CLI documentation: https://github.com/chaiNNer-org/chaiNNer/wiki/05--CLI
- Notes: Crimson Texture Forge can launch chaiNNer, inspect `.chn` chains, and pass override JSON, but chaiNNer remains a separate upstream application with its own dependencies and licenses

## Archive Format References And Compatibility Validation

### lazorr410/crimson-desert-unpacker

- Purpose: archive format reference and compatibility research
- Upstream: https://github.com/lazorr410/crimson-desert-unpacker
- Notes: informed parts of the read-only `.pamt/.paz` handling and related compatibility work

### Crimson Browser & Mod Manager

- Purpose: behavior/reference comparison while validating some archive DDS reconstruction cases
- Notes: used as a compatibility reference during local validation; not bundled with this repository

## Redistribution Notes
 
- Crimson Texture Forge does not write back to `.pamt` or `.paz` archives.
- External tools such as `texconv.exe` and `chaiNNer.exe` remain separate projects and should be distributed in accordance with their upstream terms.
- If you publish releases of this app, review the upstream licenses of any bundled or redistributed third-party components before shipping them.
