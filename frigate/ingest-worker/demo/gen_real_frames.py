"""Builds demo 'camera crop' frames from real, freely-licensed stock photos (Pexels license --
free to use and modify, no attribution required) instead of the earlier synthetic vector
illustrations, per user request. Every photo here was chosen so no real, identifiable person's
face is visible -- the delivery-person scene specifically uses a photo shot from directly behind
(search terms like "unrecognizable"/"anonymous" on the source site) rather than the many
candidates that showed a clear face, since presenting a real stranger as if they were caught on a
home security camera felt like the wrong call regardless of the license terms. Any visible license
plate (the red sedan) is blurred out as a further courtesy -- a stranger's real plate, not a
placeholder one."""
import io
import math
import os

from PIL import Image, ImageDraw, ImageFilter, ImageFont

W, H = 960, 540
PHOTO_DIR = os.path.join(os.path.dirname(__file__), "real_photos")
FONT_DIR = "/System/Library/Fonts/Supplemental"


def _font(size, bold=False):
    name = "Arial Bold.ttf" if bold else "Arial.ttf"
    path = os.path.join(FONT_DIR, name)
    if os.path.isfile(path):
        return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def _load(name):
    return Image.open(os.path.join(PHOTO_DIR, name)).convert("RGB")


def _cover_crop(img, zoom=1.0, pan=(0.5, 0.5)):
    """Crops a W:H-aspect region directly out of the full original image (like CSS
    object-fit: cover, but anchored whererever `pan` points instead of always centered --
    a plain center-crop of a tall portrait stock photo tends to land on sky/building/treeline
    rather than the actual subject near the bottom of the frame). `pan` is a fraction (0..1, 0..1)
    of the *full* image; `zoom` shrinks the cropped region further around that same anchor before
    scaling up to WxH -- used both for a single static frame and, with varying zoom/pan per call,
    to build a Ken Burns-style sequence of 'sampled moments' from one still photo (there's no
    burst of real frames for these stock photos, unlike the synthetic frames the visit preview
    normally samples across an actual clip)."""
    iw, ih = img.size
    target_ratio = W / H
    src_ratio = iw / ih
    if src_ratio > target_ratio:
        base_h = ih
        base_w = int(ih * target_ratio)
    else:
        base_w = iw
        base_h = int(iw / target_ratio)
    cw, ch = int(base_w / zoom), int(base_h / zoom)
    cx, cy = int(iw * pan[0]), int(ih * pan[1])
    x0 = max(0, min(iw - cw, cx - cw // 2))
    y0 = max(0, min(ih - ch, cy - ch // 2))
    return img.crop((x0, y0, x0 + cw, y0 + ch)).resize((W, H), Image.LANCZOS)


def _blur_region(img, box):
    """Obscures a rectangular region (e.g. a real stranger's license plate) with a strong blur --
    a courtesy for stock photos of real vehicles, same reasoning documented in this module's
    docstring."""
    img = img.copy()
    x0, y0, x1, y1 = box
    region = img.crop(box).filter(ImageFilter.GaussianBlur(18))
    img.paste(region, (x0, y0))
    return img


def _osd(draw, camera, ts_text):
    f = _font(18)
    draw.text((W - 210, H - 30), ts_text, font=f, fill=(255, 255, 255))
    draw.text((10, H - 30), camera, font=f, fill=(255, 255, 255))


def _label(draw, text):
    f = _font(30, bold=True)
    bbox = draw.textbbox((0, 0), text, font=f)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    pad = 10
    draw.rectangle([16, 16, 16 + tw + pad * 2, 16 + th + pad * 2], fill=(0, 0, 0, 160))
    draw.text((16 + pad, 16 + pad - bbox[1]), text, font=f, fill=(255, 255, 255))


def _finish(img, camera, ts_text, label):
    img = img.copy()
    draw = ImageDraw.Draw(img)
    _osd(draw, camera, ts_text)
    if label:
        _label(draw, label)
    return img


_RED_SEDAN = _load("red_sedan.jpg")
_SILVER_SUV = _load("suv2.jpg")
_BLACK_TRUCK = _load("truck4.jpg")
_DOG = _load("dog.jpg")
_DELIVERY_PERSON = _load("delivery_person.jpg")


def frame_red_sedan(ts_text, zoom=1.35, pan=(0.4, 0.7)):
    img = _cover_crop(_RED_SEDAN, zoom=zoom, pan=pan)
    # Real plate on this real car -- blurred out (see module docstring). Region estimated once
    # against this specific photo's cover-cropped framing at the default zoom/pan -- only valid
    # at that same zoom/pan, which is why the Ken Burns sequence below keeps pan fixed and only
    # varies zoom slightly (the plate stays in roughly the same place either way).
    img = _blur_region(img, (85, 260, 180, 305))
    return _finish(img, "driveway", ts_text, "car")


def frame_silver_suv(ts_text, zoom=1.0, pan=(0.5, 0.55)):
    img = _cover_crop(_SILVER_SUV, zoom=zoom, pan=pan)
    return _finish(img, "driveway", ts_text, "car")


def frame_black_truck(ts_text, zoom=1.1, pan=(0.45, 0.55)):
    img = _cover_crop(_BLACK_TRUCK, zoom=zoom, pan=pan)
    return _finish(img, "street", ts_text, "truck")


def frame_dog(ts_text, zoom=1.05, pan=(0.5, 0.55)):
    img = _cover_crop(_DOG, zoom=zoom, pan=pan)
    return _finish(img, "backyard", ts_text, "dog")


def frame_delivery_person(ts_text, zoom=1.0, pan=(0.55, 0.5)):
    # Shot from directly behind -- no face visible -- see module docstring.
    img = _cover_crop(_DELIVERY_PERSON, zoom=zoom, pan=pan)
    return _finish(img, "front_door", ts_text, "person")


def to_base64_jpeg(img, quality=87):
    import base64
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=quality)
    return base64.b64encode(buf.getvalue()).decode()


def gif_base64(frames, duration_ms=450):
    import base64
    buf = io.BytesIO()
    imgs = [f.convert("P", palette=Image.ADAPTIVE) for f in frames]
    imgs[0].save(buf, format="GIF", save_all=True, append_images=imgs[1:], duration=duration_ms, loop=0, optimize=True)
    return base64.b64encode(buf.getvalue()).decode()
