"""Make the shared look-dev texture sets in blender/assets/materials/ from ambientCG 1K JPGs (CC0).

Run headless from the repo root, with SRC holding the 1K-JPG maps unzipped from ambientCG
(only *_Color.jpg and *_NormalGL.jpg are read; nothing in SRC is ever run). The caliche set
is made from the kit's own Poly Haven gravel (blender/assets/textures/gravel_pad):
  Blender --background --factory-startup --python blender/assets/materials/make_materials.py -- SRC

Each color map becomes a neutral "detail" map: its brightness is normalized so the mean is
DETAIL_MEAN and most of its hue is removed. blender/look.py then sets each material's own
color as the glTF baseColorFactor, so one texture serves many materials, and the console's
status colors (which lerp that factor) stay clean. Normal maps are re-encoded unchanged.
Every map stays tileable, because each step works pixel by pixel.
"""
import os
import sys

import bpy
import numpy as np

OUT = os.path.dirname(os.path.abspath(__file__))
DETAIL_MEAN = 0.78          # sRGB mean of every detail map; look.py divides by its linear value
QUALITY = 82
# name: (source maps, contrast, saturation kept, flip u/v so the grain runs along v). A source
# is an ambientCG asset ID in SRC, or a path under blender/assets for the kit's own maps.
SETS = {
    "paint_worn": ("Metal021", 0.75, 0.35, False),     # slate paint with chips and rust spots
    "paint_light": ("Metal016", 0.35, 0.2, False),     # light coating, faint mottling
    "galvanized": ("Metal024", 0.8, 0.15, False),      # dull hot-dip zinc with grime
    "concrete": ("Concrete036", 0.85, 0.2, False),     # old stained concrete
    "plastic_black": ("Plastic012B", 1.6, 0.0, False),  # scuffed black HDPE and liner
    "wood_weathered": ("Wood059", 0.9, 0.45, True),    # gray weathered pole wood
    "caliche": ("textures/gravel_pad/gravel_pad", 0.8, 0.3, False),   # crushed caliche for pads
}


def load(path):
    img = bpy.data.images.load(path)
    w, h = img.size
    px = np.array(img.pixels[:], dtype=np.float32).reshape(h, w, 4)
    bpy.data.images.remove(img)
    return px


def save(px, path):
    h, w = px.shape[:2]
    img = bpy.data.images.new(os.path.basename(path), w, h)
    img.pixels = np.clip(px, 0.0, 1.0).ravel()
    img.filepath_raw = path
    img.file_format = "JPEG"
    img.save(filepath=path, quality=QUALITY)
    bpy.data.images.remove(img)
    print(f"wrote {os.path.relpath(path, OUT)} ({os.path.getsize(path) // 1024} KB)")


def detail(px, contrast, sat):
    rgb = px[..., :3]
    lum = rgb @ np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)
    out = DETAIL_MEAN + (lum - lum.mean()) * contrast * (DETAIL_MEAN / max(lum.mean(), 0.05)) ** 0.5
    chroma = (rgb - lum[..., None]) * (out / np.maximum(lum, 0.05))[..., None]
    px = px.copy()
    px[..., :3] = out[..., None] + sat * chroma
    px[..., 3] = 1.0
    return px


def flip_uv(px, normal=False):
    """Swap u and v (a transpose), so a texture's grain along u runs along v. A transposed
    tangent-space normal map swaps its x and y components too."""
    px = np.ascontiguousarray(px.transpose(1, 0, 2))
    if normal:
        px[..., [0, 1]] = px[..., [1, 0]]
    return px


def main():
    src = sys.argv[sys.argv.index("--") + 1]
    for name, (asset, contrast, sat, flip) in SETS.items():
        folder = os.path.join(OUT, name)
        os.makedirs(folder, exist_ok=True)
        if "/" in asset:
            kit = os.path.join(os.path.dirname(OUT), asset)
            color_path, normal_path = f"{kit}_color.jpg", f"{kit}_normal_gl.jpg"
        else:
            color_path = os.path.join(src, f"{asset}_1K-JPG_Color.jpg")
            normal_path = os.path.join(src, f"{asset}_1K-JPG_NormalGL.jpg")
        color = detail(load(color_path), contrast, sat)
        normal = load(normal_path)
        if flip:
            color, normal = flip_uv(color), flip_uv(normal, normal=True)
        save(color, os.path.join(folder, f"{name}_color.jpg"))
        save(normal, os.path.join(folder, f"{name}_normal_gl.jpg"))


if __name__ == "__main__":
    main()
