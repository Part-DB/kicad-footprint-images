#!/usr/bin/env python3
"""Render KiCad 3D models into PNG preview images.

Models come from a git clone of the official KiCad 3D model repository
(https://gitlab.com/kicad/libraries/kicad-packages3D, the source behind
https://kicad.github.io/packages3d/), which is created on first use
(--repo, default ./kicad-packages3D). The clone is sparse: only the
selected libraries are downloaded. An existing full clone works as well.
Models are rendered offscreen with the colors stored in the model files.

Current KiCad versions (10+) ship STEP models only; KiCad <= 9 tags also
contain the VRML (.wrl) variants. Both formats can be rendered.

Output layout mirrors the footprint libraries:
    <out>/<Library>/<ModelName>.png   e.g. previews/Resistor_SMD/R_0603_1608Metric.png

Examples:
    # list available libraries
    ./kicad3d_preview.py --list

    # render everything in two libraries
    ./kicad3d_preview.py -l Resistor_SMD -l Package_QFP

    # glob patterns for libraries and models
    ./kicad3d_preview.py -l 'Connector_*' -m '*USB*'

    # update the clone to the latest master (or another tag with --ref)
    ./kicad3d_preview.py --update -l Resistor_SMD

    # render local files or directories (no git repository)
    ./kicad3d_preview.py --input ~/my_models/ --out previews
"""

"""
Copyright 2026 Jan Böhmer

Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated documentation files (the “Software”),
to deal in the Software without restriction, including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, 
and/or sell copies of the Software, and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE
FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION 
WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
"""

from __future__ import annotations

import argparse
import fnmatch
import math
import multiprocessing
import os
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass
from pathlib import Path

import numpy as np

REPO_URL = "https://gitlab.com/kicad/libraries/kicad-packages3D.git"

MODEL_EXTS = {"step": (".step", ".stp"), "wrl": (".wrl",)}

# ---------------------------------------------------------------------------
# Model repository (git)
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, text=True, stdout=subprocess.PIPE
    ).stdout


def ensure_repo(repo: Path, ref: str, update: bool) -> None:
    """Clone the model repository, or fetch `ref` into an existing clone if `update` is set.

    The clone is shallow, blobless and sparse: only the tree listing is fetched
    up front, model files are downloaded when their library is checked out.
    """
    if not (repo / ".git").exists():
        print(f"Cloning {REPO_URL} ({ref}) into {repo} ...", file=sys.stderr)
        repo.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["git", "clone", "--depth", "1", "--filter=blob:none", "--sparse",
             "--branch", ref, REPO_URL, str(repo)],
            check=True,
        )
    elif update:
        print(f"Updating {repo} to {ref} ...", file=sys.stderr)
        subprocess.run(["git", "-C", str(repo), "fetch", "--depth", "1", "--filter=blob:none", "origin", ref],
                       check=True)
        subprocess.run(["git", "-C", str(repo), "checkout", "--detach", "FETCH_HEAD"], check=True)


def is_sparse(repo: Path) -> bool:
    try:
        return _git(repo, "config", "--bool", "core.sparseCheckout").strip() == "true"
    except subprocess.CalledProcessError:
        return False


def list_libraries(repo: Path) -> list[str]:
    """Return library names (without the .3dshapes suffix) from the checked out commit."""
    names = _git(repo, "ls-tree", "-d", "--name-only", "HEAD").splitlines()
    return sorted(n.removesuffix(".3dshapes") for n in names if n.endswith(".3dshapes"))


def list_models(repo: Path, lib: str, fmt: str) -> list[str]:
    """Return repository paths of all models of the given format in a library."""
    exts = MODEL_EXTS[fmt]
    paths = _git(repo, "ls-tree", "--name-only", "HEAD", f"{lib}.3dshapes/").splitlines()
    return sorted(p for p in paths if p.lower().endswith(exts))


def checkout_libraries(repo: Path, libs: list[str], all_libs: bool) -> None:
    """Make sure the given libraries are present in the working tree (sparse clones only)."""
    if not is_sparse(repo):
        return  # full checkout, everything is already there
    if all_libs:
        print("Checking out all libraries (this downloads several GB) ...", file=sys.stderr)
        subprocess.run(["git", "-C", str(repo), "sparse-checkout", "disable"], check=True)
        return
    missing = [f"{l}.3dshapes" for l in libs if not (repo / f"{l}.3dshapes").is_dir()]
    if missing:
        print(f"Checking out {len(missing)} libraries ...", file=sys.stderr)
        subprocess.run(["git", "-C", str(repo), "sparse-checkout", "add", *missing], check=True)


# ---------------------------------------------------------------------------
# Model loading -> list of (srgb color, vertices Nx3, triangles Mx3)
# ---------------------------------------------------------------------------


@dataclass
class Mesh:
    color: tuple[float, float, float]
    vertices: np.ndarray  # (N, 3) float
    triangles: np.ndarray  # (M, 3) int


DEFAULT_COLOR = (0.75, 0.75, 0.75)


def load_step(path: Path) -> list[Mesh]:
    from OCP.Bnd import Bnd_Box
    from OCP.BRep import BRep_Tool
    from OCP.BRepBndLib import BRepBndLib
    from OCP.BRepMesh import BRepMesh_IncrementalMesh
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.Quantity import Quantity_Color
    from OCP.STEPCAFControl import STEPCAFControl_Reader
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.TDF import TDF_Label

    try:  # OCP >= 7.8
        from OCP.collections import Sequence_TDF_Label as TDF_LabelSequence
    except ImportError:
        from OCP.TDF import TDF_LabelSequence
    from OCP.TDocStd import TDocStd_Document
    from OCP.TopAbs import TopAbs_FACE, TopAbs_REVERSED, TopAbs_SHELL, TopAbs_SOLID
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopLoc import TopLoc_Location
    from OCP.TopoDS import TopoDS
    from OCP.XCAFDoc import XCAFDoc_ColorTool, XCAFDoc_ColorType, XCAFDoc_DocumentTool, XCAFDoc_ShapeTool

    doc = TDocStd_Document(TCollection_ExtendedString("MDTV-XCAF"))
    reader = STEPCAFControl_Reader()
    reader.SetColorMode(True)
    reader.SetNameMode(False)
    reader.SetLayerMode(False)
    if reader.ReadFile(str(path)) != IFSelect_RetDone:
        raise RuntimeError(f"cannot read STEP file {path}")
    if not reader.Transfer(doc):
        raise RuntimeError(f"cannot transfer STEP file {path}")

    to_face = getattr(TopoDS, "Face_s", None) or TopoDS.Face  # OCP < 8 / >= 8

    shape_tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    color_tool = XCAFDoc_DocumentTool.ColorTool_s(doc.Main())
    color_types = (XCAFDoc_ColorType.XCAFDoc_ColorSurf, XCAFDoc_ColorType.XCAFDoc_ColorGen)

    def to_srgb(c: Quantity_Color) -> tuple[float, float, float]:
        conv = Quantity_Color.Convert_LinearRGB_To_sRGB_s
        return (conv(c.Red()), conv(c.Green()), conv(c.Blue()))

    def label_color(label) -> tuple | None:
        c = Quantity_Color()
        for t in color_types:
            if XCAFDoc_ColorTool.GetColor_s(label, t, c):
                return to_srgb(c)
        return None

    def shape_color(shape) -> tuple | None:
        c = Quantity_Color()
        for t in color_types:
            if color_tool.GetColor(shape, t, c):
                return to_srgb(c)
        return None

    # Collect leaf shapes with their global location and inherited color.
    leaves: list[tuple] = []

    def walk(label, loc: TopLoc_Location, inherited):
        if XCAFDoc_ShapeTool.IsReference_s(label):
            ref = TDF_Label()
            XCAFDoc_ShapeTool.GetReferredShape_s(label, ref)
            color = label_color(label) or label_color(ref) or inherited
            walk(ref, loc.Multiplied(XCAFDoc_ShapeTool.GetLocation_s(label)), color)
            return
        color = label_color(label) or inherited
        if XCAFDoc_ShapeTool.IsAssembly_s(label):
            comps = TDF_LabelSequence()
            XCAFDoc_ShapeTool.GetComponents_s(label, comps, False)
            for i in range(1, comps.Length() + 1):
                walk(comps.Value(i), loc, color)
            return
        leaves.append((XCAFDoc_ShapeTool.GetShape_s(label), loc, color))

    roots = TDF_LabelSequence()
    shape_tool.GetFreeShapes(roots)
    for i in range(1, roots.Length() + 1):
        walk(roots.Value(i), TopLoc_Location(), None)

    # Tessellation tolerance relative to overall model size.
    bbox = Bnd_Box()
    for shape, _, _ in leaves:
        BRepBndLib.Add_s(shape, bbox)
    if bbox.IsVoid():
        return []
    diag = bbox.CornerMin().Distance(bbox.CornerMax()) or 1.0
    lin_defl = diag * 0.0015

    buckets: dict[tuple, tuple[list, list, int]] = {}

    def add_face(face, loc: TopLoc_Location, color):
        face_loc = TopLoc_Location()
        tri = BRep_Tool.Triangulation_s(face, face_loc)
        if tri is None or tri.NbTriangles() == 0:
            return
        trsf = loc.Multiplied(face_loc).Transformation()
        m = np.array([[trsf.Value(r, c) for c in range(1, 5)] for r in range(1, 4)])
        nodes = np.array([tri.Node(i).Coord() for i in range(1, tri.NbNodes() + 1)])
        nodes = nodes @ m[:, :3].T + m[:, 3]
        tris = np.array([tri.Triangle(i).Get() for i in range(1, tri.NbTriangles() + 1)]) - 1
        if face.Orientation() == TopAbs_REVERSED:
            tris = tris[:, ::-1]
        key = tuple(round(v, 4) for v in color)
        verts, faces, count = buckets.get(key, ([], [], 0))
        verts.append(nodes)
        faces.append(tris + count)
        buckets[key] = (verts, faces, count + len(nodes))

    for shape, loc, color in leaves:
        BRepMesh_IncrementalMesh(shape, lin_defl, False, 0.35, True)
        base = shape_color(shape) or color or DEFAULT_COLOR

        seen_faces = set()
        exp_solid = TopExp_Explorer(shape, TopAbs_SOLID)
        while exp_solid.More():
            solid = exp_solid.Current()
            solid_color = shape_color(solid) or base
            exp_face = TopExp_Explorer(solid, TopAbs_FACE)
            while exp_face.More():
                face = to_face(exp_face.Current())
                seen_faces.add(face)
                add_face(face, loc, shape_color(face) or solid_color)
                exp_face.Next()
            exp_solid.Next()
        # faces not part of any solid (open shells, loose faces)
        exp_face = TopExp_Explorer(shape, TopAbs_FACE, TopAbs_SOLID)
        while exp_face.More():
            face = to_face(exp_face.Current())
            if face not in seen_faces:
                add_face(face, loc, shape_color(face) or base)
            exp_face.Next()

    return [
        Mesh(color, np.vstack(verts), np.vstack(faces).astype(np.int64))
        for color, (verts, faces, _) in buckets.items()
    ]


def load_wrl(path: Path) -> list[Mesh]:
    import vtk
    from vtk.util.numpy_support import vtk_to_numpy

    importer = vtk.vtkVRMLImporter()
    importer.SetFileName(str(path))
    importer.Update()
    meshes = []
    actors = importer.GetRenderer().GetActors()
    actors.InitTraversal()
    for _ in range(actors.GetNumberOfItems()):
        actor = actors.GetNextActor()
        mapper = actor.GetMapper()
        if mapper is None:
            continue
        mapper.Update()
        xf = vtk.vtkTransform()
        xf.SetMatrix(actor.GetMatrix())
        tf = vtk.vtkTransformPolyDataFilter()
        tf.SetTransform(xf)
        tf.SetInputData(mapper.GetInput())
        tri = vtk.vtkTriangleFilter()
        tri.SetInputConnection(tf.GetOutputPort())
        tri.Update()
        poly = tri.GetOutput()
        if poly.GetNumberOfPolys() == 0:
            continue
        verts = vtk_to_numpy(poly.GetPoints().GetData()).astype(float)
        cells = vtk_to_numpy(poly.GetPolys().GetConnectivityArray()).reshape(-1, 3)
        meshes.append(Mesh(tuple(actor.GetProperty().GetDiffuseColor()), verts, cells))
    return meshes


def load_model(path: Path) -> list[Mesh]:
    suffix = path.suffix.lower()
    if suffix in MODEL_EXTS["step"]:
        return load_step(path)
    if suffix in MODEL_EXTS["wrl"]:
        return load_wrl(path)
    raise ValueError(f"unsupported model format: {path}")


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


@dataclass
class RenderOptions:
    size: int = 512
    supersample: int = 3
    azimuth: float = 35.0  # degrees, 0 = looking from the front (-Y), positive = from the right
    elevation: float = 35.0  # degrees above the board plane
    margin: float = 0.06  # fraction of the image kept free around the model
    background: tuple[float, float, float] | None = None  # None = transparent
    edges: bool = True


def _material(prop, color):
    """Plastic-ish shading for dark bodies, shinier for bright (metal) parts."""
    r, g, b = color
    brightness = max(color)
    saturation = brightness - min(color)
    metallic = brightness > 0.55 and saturation < 0.12
    prop.SetColor(r, g, b)
    prop.SetAmbient(0.25)
    prop.SetDiffuse(0.8)
    prop.SetSpecular(0.55 if metallic else 0.25)
    prop.SetSpecularPower(40 if metallic else 15)


def render_meshes(meshes: list[Mesh], out_path: Path, opt: RenderOptions) -> None:
    import vtk
    from PIL import Image
    from vtk.util.numpy_support import numpy_to_vtk, numpy_to_vtkIdTypeArray, vtk_to_numpy

    if not meshes:
        raise RuntimeError("model contains no geometry")

    all_pts = np.vstack([m.vertices for m in meshes])
    center = (all_pts.min(0) + all_pts.max(0)) / 2
    extent = float(np.linalg.norm(all_pts.max(0) - all_pts.min(0))) or 1.0

    renderer = vtk.vtkRenderer()
    for mesh in meshes:
        points = vtk.vtkPoints()
        points.SetData(numpy_to_vtk(np.ascontiguousarray(mesh.vertices - center), deep=True))
        cells = vtk.vtkCellArray()
        offsets = np.arange(0, 3 * len(mesh.triangles) + 1, 3, dtype=np.int64)
        cells.SetData(
            numpy_to_vtkIdTypeArray(offsets, deep=True),
            numpy_to_vtkIdTypeArray(np.ascontiguousarray(mesh.triangles.ravel()), deep=True),
        )
        poly = vtk.vtkPolyData()
        poly.SetPoints(points)
        poly.SetPolys(cells)

        normals = vtk.vtkPolyDataNormals()
        normals.SetInputData(poly)
        normals.SetFeatureAngle(35)
        normals.SplittingOn()
        normals.ConsistencyOff()
        normals.AutoOrientNormalsOff()

        mapper = vtk.vtkPolyDataMapper()
        mapper.SetInputConnection(normals.GetOutputPort())
        actor = vtk.vtkActor()
        actor.SetMapper(mapper)
        _material(actor.GetProperty(), mesh.color)
        actor.GetProperty().BackfaceCullingOff()
        renderer.AddActor(actor)

        if opt.edges:
            # Draw sharp edges slightly darker than the surface so that
            # dark bodies (IC packages) still show their shape.
            fe = vtk.vtkFeatureEdges()
            fe.SetInputData(poly)
            fe.BoundaryEdgesOff()
            fe.ManifoldEdgesOff()
            fe.NonManifoldEdgesOff()
            fe.FeatureEdgesOn()
            fe.SetFeatureAngle(40)
            fe.ColoringOff()
            emapper = vtk.vtkPolyDataMapper()
            emapper.SetInputConnection(fe.GetOutputPort())
            emapper.SetResolveCoincidentTopologyToPolygonOffset()
            eactor = vtk.vtkActor()
            eactor.SetMapper(emapper)
            r, g, b = mesh.color
            lum = 0.3 * r + 0.59 * g + 0.11 * b
            if lum < 0.25:
                ec = tuple(min(1.0, c + 0.22) for c in mesh.color)
            else:
                ec = tuple(c * 0.55 for c in mesh.color)
            eactor.GetProperty().SetColor(*ec)
            eactor.GetProperty().SetLineWidth(max(1.0, opt.supersample * 0.6))
            eactor.GetProperty().LightingOff()
            renderer.AddActor(eactor)

    # Camera direction (from focal point towards the camera)
    az, el = math.radians(opt.azimuth), math.radians(opt.elevation)
    to_cam = np.array([math.sin(az) * math.cos(el), -math.cos(az) * math.cos(el), math.sin(el)])
    forward = -to_cam
    up_hint = np.array([0.0, 0.0, 1.0]) if abs(to_cam[2]) < 0.999 else np.array([0.0, 1.0, 0.0])
    right = np.cross(forward, up_hint)
    right /= np.linalg.norm(right)
    up = np.cross(right, forward)

    # Fit the projected model tightly into the (square) image.
    pts = all_pts - center
    pr, pu = pts @ right, pts @ up
    cx, cy = (pr.min() + pr.max()) / 2, (pu.min() + pu.max()) / 2
    half = max(pr.max() - pr.min(), pu.max() - pu.min()) / 2
    focal = cx * right + cy * up

    cam = renderer.GetActiveCamera()
    cam.ParallelProjectionOn()
    cam.SetFocalPoint(*focal)
    cam.SetPosition(*(focal + to_cam * extent * 3))
    cam.SetViewUp(*up)
    cam.SetParallelScale(half / (1 - 2 * opt.margin) if half > 0 else 1.0)
    renderer.ResetCameraClippingRange()

    # Lighting: key light from upper left, fill from the right, soft ambient.
    renderer.RemoveAllLights()
    renderer.SetAmbient(1.0, 1.0, 1.0)
    for pos, intensity in (((-0.6, 0.4, 1.0), 0.75), ((1.0, -0.8, 0.5), 0.45), ((0.0, -1.0, -0.2), 0.2)):
        light = vtk.vtkLight()
        light.SetLightTypeToCameraLight()
        light.SetPosition(*pos)
        light.SetFocalPoint(0, 0, 0)
        light.SetIntensity(intensity)
        renderer.AddLight(light)

    size = opt.size * opt.supersample
    win = vtk.vtkRenderWindow()
    win.SetOffScreenRendering(1)
    win.SetSize(size, size)
    win.SetAlphaBitPlanes(1)
    win.SetMultiSamples(0)
    win.AddRenderer(renderer)
    if opt.background is None:
        renderer.SetBackground(1.0, 1.0, 1.0)
        renderer.SetBackgroundAlpha(0.0)
    else:
        renderer.SetBackground(*opt.background)
        renderer.SetBackgroundAlpha(1.0)
    win.Render()

    grab = vtk.vtkWindowToImageFilter()
    grab.SetInput(win)
    grab.SetInputBufferTypeToRGBA()
    grab.ReadFrontBufferOff()
    grab.Update()
    img = grab.GetOutput()
    w, h, _ = img.GetDimensions()
    arr = vtk_to_numpy(img.GetPointData().GetScalars()).reshape(h, w, 4)[::-1]
    win.Finalize()

    # Downsample (supersampling anti-aliasing) with premultiplied alpha.
    rgba = arr.astype(np.float32) / 255.0
    if opt.background is None:
        rgba[..., :3] *= rgba[..., 3:4]
    else:
        rgba[..., 3] = 1.0
    s = opt.supersample
    rgba = rgba.reshape(opt.size, s, opt.size, s, 4).mean(axis=(1, 3))
    if opt.background is None:
        alpha = rgba[..., 3:4]
        rgba[..., :3] = np.where(alpha > 0, rgba[..., :3] / np.maximum(alpha, 1e-6), 0)
    out = (np.clip(rgba, 0, 1) * 255 + 0.5).astype(np.uint8)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(out_path.stem + ".part.png")
    Image.fromarray(out, "RGBA" if opt.background is None else None).convert(
        "RGBA" if opt.background is None else "RGB"
    ).save(tmp, optimize=True)
    tmp.replace(out_path)


# ---------------------------------------------------------------------------
# PNG optimization
# ---------------------------------------------------------------------------


@dataclass
class OptimizeOptions:
    colors: int = 256  # palette size for lossy quantization, 0 = lossless only
    size: int | None = None  # downscale to this size (square), None = keep
    level: int = 4  # oxipng effort 0-6


def optimize_png(src: Path, dst: Path, opt: OptimizeOptions) -> tuple[int, int]:
    """Write a smaller copy of `src` to `dst`: optional downscale, palette
    quantization (libimagequant, keeps smooth alpha edges) and lossless
    recompression (oxipng). Returns (source size, optimized size) in bytes."""
    import io

    import imagequant
    import oxipng
    from PIL import Image

    img = Image.open(src)
    img.load()
    if opt.size and img.size != (opt.size, opt.size):
        img = img.convert("RGBA").resize((opt.size, opt.size), Image.LANCZOS)
    if opt.colors:
        img = imagequant.quantize_pil_image(img.convert("RGBA"), dithering_level=1.0, max_colors=opt.colors)

    buf = io.BytesIO()
    img.save(buf, "PNG")
    data = oxipng.optimize_from_memory(buf.getvalue(), level=opt.level, strip=oxipng.StripChunks.safe())

    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.stem + ".part.png")
    tmp.write_bytes(data)
    tmp.replace(dst)
    return src.stat().st_size, len(data)


def _optimize_job(args: tuple) -> tuple[str, str | None, int, int]:
    src, dst, opt = args
    try:
        return src, None, *optimize_png(Path(src), Path(dst), opt)
    except Exception as e:  # noqa: BLE001 - report and continue with other images
        return src, f"{e}\n{traceback.format_exc(limit=3)}", 0, 0


def optimize_all(src_dir: Path, dst_dir: Path, opt: OptimizeOptions, jobs: int, force: bool) -> int:
    """Optimize every PNG in `src_dir` into `dst_dir` (same layout) that is missing or outdated."""
    tasks = []
    for src in sorted(src_dir.rglob("*.png")):
        if src.name.endswith(".part.png"):
            continue
        dst = dst_dir / src.relative_to(src_dir)
        if force or not dst.exists() or dst.stat().st_mtime < src.stat().st_mtime:
            tasks.append((str(src), str(dst), opt))
    if not tasks:
        print(f"optimized images in {dst_dir} are up to date", file=sys.stderr)
        return 0

    print(f"Optimizing {len(tasks)} images into {dst_dir} ...", file=sys.stderr)
    failures = total_in = total_out = 0
    with multiprocessing.Pool(jobs) as pool:
        for i, (src, err, n_in, n_out) in enumerate(pool.imap_unordered(_optimize_job, tasks), 1):
            if err:
                failures += 1
                print(f"[{i}/{len(tasks)}] FAILED optimizing {src}: {err}", file=sys.stderr)
            else:
                total_in += n_in
                total_out += n_out
                print(f"[{i}/{len(tasks)}] {Path(src).name} {n_in // 1024} -> {n_out // 1024} KiB", file=sys.stderr)

    ratio = 100 * total_out / total_in if total_in else 0
    print(f"optimized: {total_in / 1e6:.1f} MB -> {total_out / 1e6:.1f} MB ({ratio:.0f}%), "
          f"{failures} failed -> {dst_dir}", file=sys.stderr)
    return failures


# ---------------------------------------------------------------------------
# Jobs
# ---------------------------------------------------------------------------


def _render_job(model_path: str, out_path: str, opt: RenderOptions) -> tuple[str, str | None, float]:
    t0 = time.time()
    try:
        render_meshes(load_model(Path(model_path)), Path(out_path), opt)
        return model_path, None, time.time() - t0
    except Exception as e:  # noqa: BLE001 - report and continue with other models
        return model_path, f"{e}\n{traceback.format_exc(limit=3)}", time.time() - t0


def _render_job_star(args: tuple) -> tuple[str, str | None, float]:
    return _render_job(*args)


def parse_color(value: str) -> tuple[float, float, float] | None:
    if value.lower() in ("transparent", "none"):
        return None
    v = value.lstrip("#")
    if len(v) != 6:
        raise argparse.ArgumentTypeError("background must be 'transparent' or a hex color like #ffffff")
    return tuple(int(v[i : i + 2], 16) / 255 for i in (0, 2, 4))


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Download KiCad 3D models and render PNG previews.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("Examples:", 1)[1] if __doc__ else None,
    )
    ap.add_argument("-l", "--lib", action="append", default=[],
                    help="library name or glob, e.g. 'Resistor_SMD' or 'Package_*' (repeatable; default: all)")
    ap.add_argument("-m", "--model", action="append", default=[],
                    help="model name glob, e.g. '*QFN-32*' (repeatable; default: all)")
    ap.add_argument("--repo", type=Path, default=Path("kicad-packages3D"),
                    help="clone of kicad-packages3D, created if missing (default: ./kicad-packages3D)")
    ap.add_argument("--ref", default="master",
                    help="branch/tag to clone, or to fetch with --update (default: master)")
    ap.add_argument("--update", action="store_true", help="fetch --ref into an existing clone and check it out")
    ap.add_argument("--format", choices=sorted(MODEL_EXTS), default="step",
                    help="model format to render (default: step; wrl only exists up to tag 9.0.x)")
    ap.add_argument("--input", type=Path, action="append", default=[],
                    help="render local model files/directories instead of using the repository (repeatable)")
    ap.add_argument("--out", type=Path, default=Path("previews"), help="output directory (default: ./previews)")
    ap.add_argument("--size", type=int, default=512, help="image size in pixels (square, default: 512)")
    ap.add_argument("--supersample", type=int, default=3, help="anti-aliasing factor (default: 3)")
    ap.add_argument("--azimuth", type=float, default=35.0, help="view azimuth in degrees (default: 35)")
    ap.add_argument("--elevation", type=float, default=35.0, help="view elevation in degrees (default: 35)")
    ap.add_argument("--margin", type=float, default=0.06, help="free border as fraction of the image (default: 0.06)")
    ap.add_argument("--background", type=parse_color, default=None,
                    help="'transparent' (default) or a hex color like '#ffffff'")
    ap.add_argument("--no-edges", action="store_true", help="do not draw feature edge outlines")
    ap.add_argument("-j", "--jobs", type=int, default=max(1, min(8, (os.cpu_count() or 2) // 2)),
                    help="parallel render processes")
    ap.add_argument("--optimized-out", type=Path, default=Path("previews_optimized"),
                    help="directory for size-optimized copies of the previews (default: ./previews_optimized)")
    ap.add_argument("--no-optimize", action="store_true", help="skip the PNG optimization step")
    ap.add_argument("--optimize-only", action="store_true",
                    help="do not render, only optimize the existing images in --out")
    ap.add_argument("--colors", type=int, default=256,
                    help="palette colors for the optimized PNGs, 2-256; 0 = lossless only (default: 256)")
    ap.add_argument("--optimized-size", type=int, default=None,
                    help="downscale optimized PNGs to this size in pixels (default: same as --size)")
    ap.add_argument("--force", action="store_true", help="re-render / re-optimize existing images")
    ap.add_argument("--checkout-only", action="store_true", help="only clone/check out models, do not render")
    ap.add_argument("--list", action="store_true", help="list available libraries and exit")
    args = ap.parse_args()

    opt = RenderOptions(
        size=args.size,
        supersample=max(1, args.supersample),
        azimuth=args.azimuth,
        elevation=args.elevation,
        margin=args.margin,
        background=args.background,
        edges=not args.no_edges,
    )

    optimize_opt = OptimizeOptions(colors=max(0, min(256, args.colors)), size=args.optimized_size)

    if args.optimize_only:
        return 1 if optimize_all(args.out, args.optimized_out, optimize_opt, args.jobs, args.force) else 0

    def model_selected(name: str) -> bool:
        return not args.model or any(fnmatch.fnmatch(name, p) for p in args.model)

    # (model file, output png)
    tasks: list[tuple[Path, Path]] = []

    if args.input:
        all_exts = tuple(e for exts in MODEL_EXTS.values() for e in exts)
        for inp in args.input:
            files = [inp] if inp.is_file() else sorted(p for p in inp.rglob("*") if p.suffix.lower() in all_exts)
            for f in files:
                if not model_selected(f.stem):
                    continue
                lib = f.parent.name.removesuffix(".3dshapes")
                tasks.append((f, args.out / lib / f"{f.stem}.png"))
    else:
        ensure_repo(args.repo, args.ref, args.update)
        libs = list_libraries(args.repo)
        if args.list:
            print("\n".join(libs))
            return 0
        if args.lib:
            libs = [l for l in libs if any(fnmatch.fnmatch(l, p) for p in args.lib)]
            if not libs:
                print("no library matches the given --lib patterns (see --list)", file=sys.stderr)
                return 1

        repo_paths = [p for l in libs for p in list_models(args.repo, l, args.format)
                      if model_selected(Path(p).stem)]
        if not repo_paths:
            print(f"no {args.format} models found (note: wrl models only exist up to tag 9.0.x)", file=sys.stderr)
            return 1

        needed_libs = sorted({Path(p).parent.name.removesuffix(".3dshapes") for p in repo_paths})
        checkout_libraries(args.repo, needed_libs, all_libs=not args.lib and not args.model)
        if args.checkout_only:
            return 0

        for rp in repo_paths:
            lib = Path(rp).parent.name.removesuffix(".3dshapes")
            tasks.append((args.repo / rp, args.out / lib / f"{Path(rp).stem}.png"))
        print(f"{len(repo_paths)} models selected", file=sys.stderr)

    if not args.force:
        tasks = [(m, o) for m, o in tasks if not o.exists()]
    tasks.sort()

    failures = 0
    if not tasks:
        print("nothing to render (use --force to re-render)", file=sys.stderr)
    else:
        print(f"Rendering {len(tasks)} models with {args.jobs} processes ...", file=sys.stderr)
        # Workers are recycled to bound OCC/VTK memory growth. multiprocessing.Pool is used
        # instead of ProcessPoolExecutor(max_tasks_per_child=...), which hangs on Python 3.12
        # once the first generation of workers has exited.
        jobs = [(str(m), str(o), opt) for m, o in tasks]
        with multiprocessing.Pool(args.jobs, maxtasksperchild=20) as pool:
            for i, (model, err, dt) in enumerate(pool.imap_unordered(_render_job_star, jobs), 1):
                if err:
                    failures += 1
                    print(f"[{i}/{len(tasks)}] FAILED {model}: {err}", file=sys.stderr)
                else:
                    print(f"[{i}/{len(tasks)}] {Path(model).name} ({dt:.1f}s)", file=sys.stderr)
        print(f"done: {len(tasks) - failures} rendered, {failures} failed -> {args.out}", file=sys.stderr)

    if not args.no_optimize and args.out.is_dir():
        failures += optimize_all(args.out, args.optimized_out, optimize_opt, args.jobs, args.force)
    return 1 if failures else 0

if __name__ == "__main__":
    sys.exit(main())
