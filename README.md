# KiCad 3D model previews

`kicad3d_preview.py` renders PNG previews (transparent background, isometric view,
model colors) of the models in the official
[kicad-packages3D](https://gitlab.com/kicad/libraries/kicad-packages3D) git repository.

On first run it clones the repository into `./kicad-packages3D` (change with `--repo`).
The clone is shallow and sparse: only the libraries you select get downloaded.
Pointing `--repo` at an existing full clone also works (it is left untouched).

## Setup

    python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

## Usage

    .venv/bin/python kicad3d_preview.py --list                      # list libraries
    .venv/bin/python kicad3d_preview.py -l Resistor_SMD             # one library
    .venv/bin/python kicad3d_preview.py -l 'Package_*' -m '*QFN*'   # globs
    .venv/bin/python kicad3d_preview.py                             # everything (several GB)
    .venv/bin/python kicad3d_preview.py --update                    # pull latest master first
    .venv/bin/python kicad3d_preview.py --input some/dir --out out  # local STEP/WRL files

Output: `previews/<Library>/<Model>.png`, matching `<Library>.pretty/<Footprint>`.
Existing PNGs are skipped unless `--force`.

After rendering, size-optimized copies are written to `previews_optimized/` (same layout):
palette quantization with libimagequant (keeps transparent edges smooth) plus lossless
oxipng recompression, roughly 40-60% smaller. Only new or changed images are processed.
Options: `--optimized-out DIR`, `--colors N` (0 = lossless only), `--optimized-size PX`
(e.g. 128 for thumbnails), `--optimize-only` (skip rendering), `--no-optimize`.
`--update` fetches the latest `--ref` (default `master`, or a tag like `9.0.0`) into the clone.

Useful options: `--size`, `--background '#ffffff'`, `--azimuth`, `--elevation`,
`--margin`, `--no-edges`, `--checkout-only`, `-j` (render processes).

Notes:
- `master` (KiCad 10+) only contains STEP models. `--format wrl` works with tags <= 9.0.x,
  but VTK's VRML importer ignores shared (`USE`) materials, so colors can be wrong; prefer STEP.
- Headless machines without a display: set `VTK_DEFAULT_OPENGL_WINDOW=vtkEGLRenderWindow`
  (or `vtkOSMesaRenderWindow`).
