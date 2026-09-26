# KiCad 3D model previews

`kicad3d_preview.py` downloads models from the official
[kicad-packages3D](https://gitlab.com/kicad/libraries/kicad-packages3D) repository
and renders PNG previews (transparent background, isometric view, model colors).

## Setup

    python3 -m venv .venv && .venv/bin/pip install -r requirements.txt

## Usage

    .venv/bin/python kicad3d_preview.py --list                      # list libraries
    .venv/bin/python kicad3d_preview.py -l Resistor_SMD             # one library
    .venv/bin/python kicad3d_preview.py -l 'Package_*' -m '*QFN*'   # globs
    .venv/bin/python kicad3d_preview.py                             # everything (several GB)
    .venv/bin/python kicad3d_preview.py --input some/dir --out out  # local STEP/WRL files

Output: `previews/<Library>/<Model>.png`, matching `<Library>.pretty/<Footprint>`.
Downloads are cached in `cache/<ref>/`; existing PNGs are skipped unless `--force`.

Useful options: `--size`, `--background '#ffffff'`, `--azimuth`, `--elevation`,
`--margin`, `--no-edges`, `--ref 9.0.0` (tag/branch), `-j` (render processes).
Set `GITLAB_TOKEN` to raise GitLab API rate limits.

Notes:
- `master` (KiCad 10+) only contains STEP models. `--format wrl` works with tags <= 9.0.x,
  but VTK's VRML importer ignores shared (`USE`) materials, so colors can be wrong; prefer STEP.
- Headless machines without a display: set `VTK_DEFAULT_OPENGL_WINDOW=vtkEGLRenderWindow`
  (or `vtkOSMesaRenderWindow`).
