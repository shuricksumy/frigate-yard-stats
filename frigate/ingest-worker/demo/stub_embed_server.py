"""Stand-in for llama_slot_proxy's embedding slot, for the demo dataset only. Produces a small
deterministic vector from a fixed keyword vocabulary so semantically related synthetic
descriptions actually land close together (and unrelated ones land far apart) under cosine
distance -- enough for the Search tab demo to rank sensibly without a real embedding model.
"""
import json
import math
from http.server import BaseHTTPRequestHandler, HTTPServer

DIMENSIONS = 64

VOCAB = [
    "car", "sedan", "suv", "truck", "pickup", "vehicle",
    "red", "silver", "gray", "black", "blue", "brown",
    "person", "delivery", "package", "jacket", "walked", "door", "front",
    "dog", "running", "yard", "backyard",
    "driveway", "street", "parked", "pulled", "garage", "engine",
]


def embed(text: str) -> list:
    words = (text or "").lower()
    vec = [1.0 if kw in words else 0.0 for kw in VOCAB]
    vec += [0.0] * (DIMENSIONS - len(vec))
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        text = body.get("input", "")
        resp = {"data": [{"embedding": embed(text)}]}
        payload = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format, *args):
        pass


if __name__ == "__main__":
    HTTPServer(("127.0.0.1", 8930), Handler).serve_forever()
