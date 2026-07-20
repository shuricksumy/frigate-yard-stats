import html as html_lib
from datetime import datetime

import config
import crop
import db


def _esc(value) -> str:
    return html_lib.escape(str(value)) if value is not None else ""


def _fmt_time(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%d %H:%M:%S UTC")


def _img_cell(
    image_base64: str | None, lightboxes: list, counter: list, gif_base64: str | None = None,
) -> str:
    if gif_base64:
        # The visit's own animated preview GIF -- the richer artifact, preferred over the static
        # composite grid whenever it's ready (see db.get_report_data's gif_image_expr). Embedded
        # once, directly, CSS-constrained to the same on-screen size as the JPEG thumbnail below
        # rather than also re-encoding a separate smaller copy: unlike a JPEG there's no cheap way
        # to produce a second, smaller re-encoded GIF here, and a second lightbox <img> pointing at
        # the exact same base64 bytes would reintroduce the identical double-embed bloat this
        # report already fixed once for JPEGs (the old n8n version's 42MB report bug). Click-to-
        # enlarge instead reuses this same <img> element's already-decoded src (showGifModal(this.
        # src), see generate_report's shared #gif-modal) rather than a second server-rendered <img>
        # tag -- the browser holds one decoded copy either way, but the static HTML source itself
        # never carries the base64 text twice.
        return (
            f'<img src="data:image/gif;base64,{gif_base64}" alt="preview (animated, click to enlarge)" '
            'style="max-width:160px;max-height:160px;display:block;cursor:pointer;" '
            'onclick="showGifModal(this.src)">'
        )
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


def _group_by_visit(sightings: list) -> list[dict]:
    # One entry per visit (or per standalone raw_event, for a sighting that was never grouped into
    # a visit -- visit_id is NULL, so it becomes a group of one) instead of separate per-type
    # tables -- a visit's sightings (e.g. a car and a person, someone getting out of their car)
    # belong together, not unrelated rows a reader has to mentally reassociate by timestamp. One
    # universal "sightings" list per group now, not split by type.
    groups: dict[object, dict] = {}
    order: list[object] = []

    for row in sightings:
        key = row["visit_id"] if row["visit_id"] is not None else ("event", row["raw_event_id"])
        if key not in groups:
            groups[key] = {
                "start_ts": row["start_ts"], "camera": row["camera"],
                "crop_image_base64": row["crop_image_base64"],
                "preview_gif_base64": row.get("preview_gif_base64"),
                "sightings": [],
            }
            order.append(key)
        group = groups[key]
        # Earliest sighting's own time/image represents the group -- consistent with the
        # "representative" choice used everywhere else a visit needs to pick one.
        if row["start_ts"] < group["start_ts"]:
            group["start_ts"] = row["start_ts"]
            group["camera"] = row["camera"]
            group["crop_image_base64"] = row["crop_image_base64"]
            group["preview_gif_base64"] = row.get("preview_gif_base64")
        group["sightings"].append(row)

    return [groups[key] for key in order]


def _build_alert_rows(sightings: list, lightboxes: list, counter: list) -> str:
    # Newest first -- matches get_report_data's own ORDER BY re.start_ts DESC and the web report
    # UI's convention (most recent activity at the top, not buried at the bottom of a long window).
    groups = sorted(_group_by_visit(sightings), key=lambda g: g["start_ts"], reverse=True)
    rows = []
    for g in groups:
        # One labeled line per sighting in the group (e.g. "car: orange suv, plate 10MG407" /
        # "person: wearing a red jacket") -- a visit grouping several distinct object types shows
        # all of them, joined, rather than picking just one.
        summary = "; ".join(f"{s['object_label']}: {s['description']}" for s in g["sightings"] if s["description"]) or None
        rows.append(
            f"<tr><td>{_img_cell(g['crop_image_base64'], lightboxes, counter, g['preview_gif_base64'])}</td>"
            f"<td>{_fmt_time(g['start_ts'])}</td><td>{_esc(g['camera'])}</td>"
            f"<td>{_esc(summary)}</td></tr>"
        )
    return "\n".join(rows)


def generate_report(
    start: datetime, end: datetime, source: str = "events", include_preview: str = "gif",
) -> dict:
    # include_preview is "gif" (default, today's original behavior)/"image"/"none" -- see
    # db.get_report_data. Both narrower modes already come back with the corresponding field(s)
    # NULL at the SQL level, so _img_cell's existing fallbacks (grid/crop when there's no GIF,
    # "(no image)" when there's no image at all) apply with no separate rendering path needed.
    data = db.get_report_data(start, end, source, include_preview)
    sightings = data["sightings"]

    lightboxes: list[str] = []
    counter = [0]

    style = """<style>
body{font-family:Arial,sans-serif;color:#222;}
h1{color:#2c3e50;}
h2{color:#2c3e50;border-bottom:2px solid #2c3e50;padding-bottom:4px;}
table{border-collapse:collapse;width:100%;margin-bottom:24px;}
th,td{border:1px solid #ddd;padding:6px 10px;text-align:left;font-size:14px;vertical-align:middle;}
th{background:#2c3e50;color:#fff;}
tr:nth-child(even){background:#f7f7f7;}
.summary{font-size:15px;margin-bottom:16px;}
.lightbox{display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.9);z-index:1000;text-align:center;}
.lightbox:target{display:flex;align-items:center;justify-content:center;}
.lightbox img{max-width:95%;max-height:95%;}
#gif-modal{display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.9);z-index:1000;align-items:center;justify-content:center;text-align:center;cursor:pointer;}
#gif-modal img{max-width:95%;max-height:95%;}
</style>"""

    # One shared modal + tiny script for every GIF cell's click-to-enlarge, rather than a
    # per-row lightbox <div> the way the JPEG case gets -- see _img_cell's comment: reusing
    # the clicked <img>'s own already-decoded src avoids ever writing the GIF's base64 text
    # into the HTML source a second time. Always included (source="events" never renders a
    # GIF cell, so it's simply never invoked there, at negligible fixed cost).
    gif_modal = (
        '<div id="gif-modal" onclick="this.style.display=\'none\'"><img id="gif-modal-img"></div>'
        "<script>function showGifModal(src){"
        "document.getElementById('gif-modal-img').src=src;"
        "document.getElementById('gif-modal').style.display='flex';"
        "}</script>"
    )

    if source == "visits":
        # One combined row per alert (visit) instead of one row per sighting -- a visit's several
        # sightings (e.g. a car and a person) belong to the same real-world activity, so the
        # thumbnail and every AI result show up together in one row rather than separate ones.
        alert_count = len(_group_by_visit(sightings))
        alert_rows = _build_alert_rows(sightings, lightboxes, counter)
        body = f"""<h1>Yard Stats Alerts Report</h1>
<div class="summary"><b>{alert_count}</b> alert(s) ({len(sightings)} sighting(s)) from {_fmt_time(start)} to {_fmt_time(end)}.</div>
<table><tr><th>Image</th><th>Time</th><th>Camera</th><th>Sightings</th></tr>
{alert_rows or '<tr><td colspan="4">No alerts.</td></tr>'}
</table>"""
        caption = (
            f"Yard Stats Alerts Report -- {alert_count} alert(s) ({len(sightings)} sighting(s)) "
            f"from {_fmt_time(start)} to {_fmt_time(end)}."
        )
    else:
        sighting_rows = "\n".join(
            f"<tr><td>{_img_cell(s['crop_image_base64'], lightboxes, counter)}</td>"
            f"<td>{_fmt_time(s['start_ts'])}</td><td>{_esc(s['camera'])}</td>"
            f"<td>{_esc(s['object_label'])}</td><td>{_esc(s['description'])}</td></tr>"
            for s in sightings
        )
        body = f"""<h1>Yard Stats Report</h1>
<div class="summary"><b>{len(sightings)}</b> sighting(s) from {_fmt_time(start)} to {_fmt_time(end)}.</div>
<table><tr><th>Image</th><th>Time</th><th>Camera</th><th>Type</th><th>Description</th></tr>
{sighting_rows or '<tr><td colspan="5">No sightings.</td></tr>'}
</table>"""
        caption = (
            f"Yard Stats Report -- {len(sightings)} sighting(s) from {_fmt_time(start)} to {_fmt_time(end)}."
        )

    html_doc = (
        f'<!DOCTYPE html><html><head><meta charset="utf-8">{style}</head><body>'
        f"{body}\n{chr(10).join(lightboxes)}\n{gif_modal}\n</body></html>"
    )

    return {
        "start": start,
        "end": end,
        "html": html_doc,
        "caption": caption,
        "sighting_count": len(sightings),
    }
