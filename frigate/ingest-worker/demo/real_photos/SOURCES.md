# Source photos

All from [Pexels](https://www.pexels.com/license/) -- free to use and modify, no attribution
required. Each was re-encoded/resized (long side capped at 1800px, JPEG quality 85) to keep this
folder small; `gen_real_frames.py` crops/zooms/overlays them at demo time, and blurs the license
plate on `red_sedan.jpg` (a real stranger's plate, not a placeholder one).

- `red_sedan.jpg` -- https://www.pexels.com/photo/red-sedan-parked-on-the-side-of-the-road-1637859/
- `suv2.jpg` -- https://www.pexels.com/photo/silver-toyota-4-runner-17519357/
- `truck4.jpg` -- https://www.pexels.com/photo/black-pickup-truck-6496813/
- `dog.jpg` -- https://www.pexels.com/photo/brown-dog-running-on-field-2197906/
- `delivery_person.jpg` -- https://www.pexels.com/photo/unrecognizable-delivery-man-carrying-cardboard-boxes-placed-on-cart-4968427/
  -- deliberately picked because it's shot from directly behind (no face visible); every other
  delivery-person candidate photo found showed a clear, identifiable face and was rejected for
  that reason (see `gen_real_frames.py`'s module docstring).
