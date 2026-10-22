                     
"""Render a preview (and thumbnail) from a decoded raw, without Adobe's renderer.

This is the piece the replicating harness used to borrow from ADC's own output.
It is deliberately simple — a half-resolution 2x2 demosaic, white balance, the
camera's colour matrix from the camera record, sRGB gamma — which produces a
clean, colour-reasonable preview at the sizes a DNG preview needs (1024 px wide
plus a 256 px thumbnail).

usage (as a library):
    from render_preview import render
    render(cfa, meta, camera_db_entry, "preview.jpg", width=1024)
"""
import io

import numpy as np
import os
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(BASE, "tools")
WORK = os.path.join(BASE, "work")
REF = os.path.join(BASE, "ref")
BIN = os.path.join(BASE, "bin")


                          
XYZ_TO_SRGB = np.array([[3.2404542, -1.5371385, -0.4985314],
                        [-0.9692660, 1.8760108, 0.0415560],
                        [0.0556434, -0.2040259, 1.0572252]])


def _as_matrix(rat_list, rows, cols):
    """[[n, d], ...] -> float matrix, row-major."""
    out = np.zeros(rows * cols, dtype=np.float64)
    for i, pair in enumerate(rat_list[:rows * cols]):
        if isinstance(pair, (list, tuple)) and len(pair) == 2 and pair[1]:
            out[i] = pair[0] / float(pair[1])
        else:
            out[i] = float(pair)
    return out.reshape(rows, cols)


def _demosaic_half(cfa, cfa_pattern):
    """2x2 box demosaic: output is half size, one sample per Bayer quad."""
    h, w = cfa.shape[0] // 2 * 2, cfa.shape[1] // 2 * 2
    q = cfa[:h, :w].astype(np.float64)
                
    a = q[0::2, 0::2]
    b = q[0::2, 1::2]
    c = q[1::2, 0::2]
    d = q[1::2, 1::2]
                                                                             
                                                                         
    channel = {"R": 0, "G": 1, "B": 2}
    chans = [np.zeros_like(a) for _ in range(3)]
    quads = [a, b, c, d]
    if len(cfa_pattern) == 4 and all(ch in channel for ch in cfa_pattern):
        counts = [0, 0, 0]
        for k, ch in enumerate(cfa_pattern):
            chans[channel[ch]] += quads[k]
            counts[channel[ch]] += 1
        for i in range(3):
            if counts[i]:
                chans[i] /= counts[i]
    else:
        chans = [quads[0], quads[1], quads[3]]
    return np.dstack(chans)


def render(cfa, meta, consts, out_path, width=1024, quality=92, thumb=None):
    """Write a JPEG preview (and optionally a thumbnail) and return its size."""
    from PIL import Image

    pat = (meta.get("cfa") or "RGGB").upper()
                                                                                
                                                
    arr = np.asarray(cfa)
    step = max(1, min(arr.shape[0], arr.shape[1]) // 1536)
    if step > 1:
        arr = arr[::step, ::step]
    rgb = _demosaic_half(arr, pat)

    black = float(meta.get("black") or 0)
    white = float(meta.get("maximum") or 0) or float(rgb.max()) or 1.0
    rgb = np.clip((rgb - black) / max(1.0, white - black), 0.0, 1.0)

                                                                                 
                                                                                    
                                                                                 
                                                                         
    mul = meta.get("cam_mul") or [] if meta.get("apply_wb", False) else []
    if len(mul) >= 3 and min(mul[:3]) > 0:
        wb = 1.0 / np.array([mul[0], mul[1], mul[2]], dtype=np.float64)
        rgb *= wb / wb[1]

    cm = (consts or {}).get("subifd/0xC621", {}).get("value") or \
         (consts or {}).get("ifd0/0xC621", {}).get("value")
    if cm:
                                                                                  
                                                
        xyz2cam = _as_matrix(cm, 3, 3)
        cam2xyz = np.linalg.inv(xyz2cam)
        m = XYZ_TO_SRGB @ cam2xyz                                      
        shp = rgb.shape
        rgb = (rgb.reshape(-1, 3) @ m.T).reshape(shp)
    rgb = np.clip(rgb, 0.0, 1.0) ** (1.0 / 2.2)

    img = Image.fromarray((rgb * 255.0 + 0.5).astype(np.uint8), "RGB")
    h = max(1, int(round(img.height * width / float(img.width))))
    img = img.resize((width, h), Image.LANCZOS)
    img.save(out_path, "JPEG", quality=quality, subsampling=2)
    if thumb:
        th = max(1, int(round(h * 256.0 / width)))
        img.resize((256, th), Image.LANCZOS).save(thumb, "JPEG", quality=quality)
    return img.size
