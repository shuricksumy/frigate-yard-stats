"""Seeds the demo Postgres with a small, varied synthetic dataset -- events, visits (with
animated preview GIFs, never the flat composite grid), sightings/visit_sightings with
deterministic embeddings, and a couple of tiny real mp4 clips for the Video toggle."""
import datetime
import os
import subprocess
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import gen_real_frames as g
from stub_embed_server import embed

import db

NOW = datetime.datetime.now(datetime.timezone.utc)


def ts(minutes_ago):
    return NOW - datetime.timedelta(minutes=minutes_ago)


def fmt_osd(dt):
    return dt.strftime("%m/%d %H:%M:%S")


def insert_event(camera, objects, minutes_ago, crop_img, det_id=None, video_path=None, score=0.87):
    det_id = det_id or f"demo-{uuid.uuid4()}"
    start = ts(minutes_ago)
    end = start + datetime.timedelta(seconds=14)
    video_status = "done" if video_path else "skipped"
    rows = db._execute(
        """
        INSERT INTO yard_stats.raw_events
            (camera, zone, objects, start_ts, end_ts, det_id, has_clip, has_snapshot,
             crop_status, ai_status, crop_image_base64, sub_label, score, video_status, video_path)
        VALUES (%s, %s, %s, %s, %s, %s, true, true, 'done', 'done', %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (camera, camera, objects, start, end, det_id, g.to_base64_jpeg(crop_img), None, score,
         video_status, video_path),
        fetch=True,
    )
    return rows[0]["id"], det_id


def add_sighting(event_id, object_label, description):
    db.complete_sighting(event_id, object_label, description, embedding=embed(description))


def insert_visit(camera, objects, minutes_ago, det_ids, gif_b64, video_path=None):
    start = ts(minutes_ago)
    end = start + datetime.timedelta(seconds=42)
    video_status = "done" if video_path else "skipped"
    rows = db._execute(
        """
        INSERT INTO yard_stats.visits
            (zone, objects, start_ts, end_ts, cameras, camera_count, video_status, video_path,
             preview_gif_base64, thumb_crop_status)
        VALUES (%s, %s, %s, %s, %s, 1, %s, %s, %s, 'done')
        RETURNING id
        """,
        (camera, objects, start, end, camera, video_status, video_path, gif_b64),
        fetch=True,
    )
    visit_id = rows[0]["id"]
    if det_ids:
        db._execute(
            "UPDATE yard_stats.raw_events SET visit_id = %s, reconciled = true WHERE det_id = ANY(%s)",
            (visit_id, det_ids),
        )
    return visit_id


def add_visit_sighting(visit_id, object_label, description):
    db.complete_visit_sighting(visit_id, object_label, description, embedding=embed(description))


def make_clip(frames, out_path, fps=2):
    with tempfile.TemporaryDirectory() as tmp:
        for i, frame in enumerate(frames):
            frame.save(os.path.join(tmp, f"f{i:03d}.png"))
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error", "-framerate", str(fps),
                "-i", os.path.join(tmp, "f%03d.png"),
                "-vf", "scale=960:540", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-r", "15", out_path,
            ],
            check=True,
        )


def main():
    db.ensure_schema()

    video_root = os.environ["VIDEO_STORAGE_PATH"]
    video_alerts_root = os.environ["VIDEO_STORAGE_PATH_ALERTS"]

    # Ken Burns zoom steps -- these real photos are each a single still, not a burst of frames
    # sampled across an actual clip, so a slow zoom simulates "motion" across the 4 sampled
    # moments/video seconds rather than reusing one identical frame four times.
    zoom_steps = [1.0, 1.08, 1.16, 1.24]

    # -- Plain events (no visit) --
    img = g.frame_red_sedan(fmt_osd(ts(22)))
    red_clip = os.path.join(video_root, "car-1-demo.mp4")
    make_clip([g.frame_red_sedan(fmt_osd(ts(22) + datetime.timedelta(seconds=s * 2)), zoom=1.35 * z) for s, z in enumerate(zoom_steps)], red_clip)
    eid, _ = insert_event("driveway", "car", 22, img, video_path=red_clip)
    add_sighting(eid, "car", "Red sedan parked along the curb near the driveway, no visible roof rack or damage.")

    img = g.frame_black_truck(fmt_osd(ts(18)))
    eid, _ = insert_event("street", "truck", 18, img)
    add_sighting(eid, "truck", "Black pickup truck driving past on the street at night, taillights on, did not stop.")

    img = g.frame_dog(fmt_osd(ts(12)))
    eid, _ = insert_event("backyard", "dog", 12, img)
    add_sighting(eid, "dog", "Medium-sized brown and white dog running across the backyard, no collar visible.")

    # -- Visit A: silver SUV pulling into the driveway (two re-tracked det_ids) --
    det_a1 = f"demo-{uuid.uuid4()}"
    det_a2 = f"demo-{uuid.uuid4()}"
    img0 = g.frame_silver_suv(fmt_osd(ts(15)), zoom=zoom_steps[0])
    img3 = g.frame_silver_suv(fmt_osd(ts(15) + datetime.timedelta(seconds=6)), zoom=zoom_steps[3])
    insert_event("driveway", "car", 15, img0, det_id=det_a1)
    insert_event("driveway", "car", 15, img3, det_id=det_a2)
    suv_frames = [g.frame_silver_suv(fmt_osd(ts(15) + datetime.timedelta(seconds=s * 2)), zoom=z) for s, z in enumerate(zoom_steps)]
    suv_gif = g.gif_base64(suv_frames)
    suv_clip = os.path.join(video_alerts_root, "visit-car-1-demo.mp4")
    make_clip(suv_frames, suv_clip)
    visit_a = insert_visit("driveway", "car", 15, [det_a1, det_a2], suv_gif, video_path=suv_clip)
    add_visit_sighting(visit_a, "car", "Silver SUV pulled into the driveway, headlights on, roof rails visible, engine shut off after a few seconds.")

    # -- Visit B: delivery person walking near the front door and back -- a real photo shot from
    # directly behind (no face visible), not the earlier synthetic illustration -- see
    # gen_real_frames' module docstring for why this specific photo was safe to use.
    det_b1 = f"demo-{uuid.uuid4()}"
    img0 = g.frame_delivery_person(fmt_osd(ts(5)), zoom=zoom_steps[0])
    insert_event("front_door", "person", 5, img0, det_id=det_b1)
    person_frames = [g.frame_delivery_person(fmt_osd(ts(5) + datetime.timedelta(seconds=s * 2)), zoom=z) for s, z in enumerate(zoom_steps)]
    person_gif = g.gif_base64(person_frames)
    visit_b = insert_visit("front_door", "person", 5, [det_b1], person_gif)
    add_visit_sighting(visit_b, "person", "Delivery person wearing a blue jacket walked up to the front door, left a package, and walked back toward the street.")

    print("seed complete")


if __name__ == "__main__":
    main()
