import html as html_lib
from datetime import datetime

import config
import crop
import db


def _esc(value) -> str:
    return html_lib.escape(str(value)) if value is not None else ""


def _fmt_time(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%d %H:%M:%S UTC")


def _img_cell(image_base64: str | None, lightboxes: list, counter: list) -> str:
    # Two different sizes, each embedded exactly once: a small on-the-fly thumbnail for the
    # inline preview (generated here, never touching the stored full-quality image), and the
    # original full-size crop only inside the lightbox overlay for the click-to-enlarge view --
    # unlike the earlier n8n version, which accidentally embedded the same full-size image twice.
    if not image_base64:
        return "(no image)"
    thumbnail_base64 = crop.scale_image_base64(image_base64, config.THUMBNAIL_MAX_DIMENSION)
    lightbox_id = f"lightbox-{counter[0]}"
    counter[0] += 1
    lightboxes.append(
        f'<div class="lightbox" id="{lightbox_id}"><a href="#">'
        f'<img src="data:image/jpeg;base64,{image_base64}"></a></div>'
    )
    return (
        f'<a href="#{lightbox_id}"><img src="data:image/jpeg;base64,{thumbnail_base64}" '
        'alt="crop (click for full size)" '
        'style="max-width:160px;max-height:160px;display:block;cursor:pointer;"></a>'
    )


def generate_report(start: datetime, end: datetime) -> dict:
    data = db.get_report_data(start, end)
    cars = data["vehicles"]
    persons = data["persons"]

    lightboxes: list[str] = []
    counter = [0]

    car_rows = "\n".join(
        f"<tr><td>{_img_cell(c['crop_image_base64'], lightboxes, counter)}</td>"
        f"<td>{_fmt_time(c['start_ts'])}</td><td>{_esc(c['camera'])}</td>"
        f"<td>{_esc(c['color'])}</td><td>{_esc(c['body_type'])}</td>"
        f"<td>{_esc(c['make_guess'])}</td><td>{_esc(c['plate_text_llm'])}</td>"
        f"<td>{_esc(c['plate_text_frigate'])}</td></tr>"
        for c in cars
    )
    person_rows = "\n".join(
        f"<tr><td>{_img_cell(p['crop_image_base64'], lightboxes, counter)}</td>"
        f"<td>{_fmt_time(p['start_ts'])}</td><td>{_esc(p['camera'])}</td>"
        f"<td>{_esc(p['description'])}</td></tr>"
        for p in persons
    )

    html_doc = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><style>
body{{font-family:Arial,sans-serif;color:#222;}}
h1{{color:#2c3e50;}}
h2{{color:#2c3e50;border-bottom:2px solid #2c3e50;padding-bottom:4px;}}
table{{border-collapse:collapse;width:100%;margin-bottom:24px;}}
th,td{{border:1px solid #ddd;padding:6px 10px;text-align:left;font-size:14px;vertical-align:middle;}}
th{{background:#2c3e50;color:#fff;}}
tr:nth-child(even){{background:#f7f7f7;}}
.summary{{font-size:15px;margin-bottom:16px;}}
.lightbox{{display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.9);z-index:1000;text-align:center;}}
.lightbox:target{{display:flex;align-items:center;justify-content:center;}}
.lightbox img{{max-width:95%;max-height:95%;}}
</style></head><body>
<h1>Yard Stats Report</h1>
<div class="summary"><b>{len(cars)}</b> vehicle sighting(s), <b>{len(persons)}</b> person sighting(s) from {_fmt_time(start)} to {_fmt_time(end)}.</div>
<h2>Vehicles ({len(cars)})</h2>
<table><tr><th>Image</th><th>Time</th><th>Camera</th><th>Color</th><th>Body Type</th><th>Make</th><th>Plate (VLM)</th><th>Plate (Frigate)</th></tr>
{car_rows or '<tr><td colspan="8">No vehicle sightings.</td></tr>'}
</table>
<h2>Persons ({len(persons)})</h2>
<table><tr><th>Image</th><th>Time</th><th>Camera</th><th>Description</th></tr>
{person_rows or '<tr><td colspan="4">No person sightings.</td></tr>'}
</table>
{chr(10).join(lightboxes)}
</body></html>"""

    caption = (
        f"Yard Stats Report -- {len(cars)} vehicle sighting(s), {len(persons)} person sighting(s) "
        f"from {_fmt_time(start)} to {_fmt_time(end)}."
    )

    return {
        "start": start,
        "end": end,
        "html": html_doc,
        "caption": caption,
        "car_count": len(cars),
        "person_count": len(persons),
    }
