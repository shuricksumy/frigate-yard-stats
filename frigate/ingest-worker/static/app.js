// Yard Stats web report -- talks to this same origin's /events, /events/{id}/thumbnail,
// /events/{id}/image, /media/video/{id}. No external requests, no build step -- vanilla JS +
// Alpine.js (vendored locally in vendor/alpine.min.js).

const API_KEY_COOKIE = "api_key";
const COOKIE_MAX_AGE_SECONDS = 10 * 365 * 24 * 60 * 60; // ~10 years -- "never" isn't representable

function getCookie(name) {
  const match = document.cookie.match(new RegExp("(?:^|; )" + name + "=([^;]*)"));
  return match ? decodeURIComponent(match[1]) : null;
}

function setCookie(name, value, maxAgeSeconds) {
  document.cookie = `${name}=${encodeURIComponent(value)}; max-age=${maxAgeSeconds}; path=/; samesite=lax`;
}

function clearCookie(name) {
  document.cookie = `${name}=; max-age=0; path=/`;
}

function defaultWindow() {
  // Matches the API's own default (last 1 hour) so the UI's initial fetch and its displayed
  // filter values agree with each other.
  const end = new Date();
  const start = new Date(end.getTime() - 60 * 60 * 1000);
  const toLocalInput = (d) => {
    const pad = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
  };
  return { start: toLocalInput(start), end: toLocalInput(end) };
}

function eventsApp() {
  return {
    apiKey: null,
    apiKeyInput: "",
    hasApiKey: false,
    loginError: "",

    events: [],
    loading: false,
    limit: 24,
    offset: 0,
    objectTypes: [],

    filters: { objectType: "all", aiStatus: "all", onlyWithMedia: true, eventId: "", q: "", ...defaultWindow() },

    lightboxEvent: null,
    lightboxMode: "video",
    lightboxDetail: null,
    lightboxLoading: false,

    init() {
      const stored = getCookie(API_KEY_COOKIE);
      if (stored) {
        this.apiKey = stored;
        this.hasApiKey = true;
        this.fetchObjectTypes();
        this.fetchEvents();
      }
    },

    async fetchObjectTypes() {
      // Frigate's object labels aren't fixed (depends on your model/config) -- the Type dropdown
      // is populated from the server's OBJECT_TYPES config instead of being hardcoded here.
      try {
        const resp = await fetch("/object-types", { headers: { "X-API-Key": this.apiKey } });
        if (!resp.ok) return;
        const data = await resp.json();
        this.objectTypes = data.object_types || [];
      } catch (err) {
        console.error(err);
      }
    },

    async saveApiKey() {
      // Validate by actually calling the API rather than trusting the input blindly -- a wrong
      // key should surface immediately, not on the first silent 401 later.
      this.loginError = "";
      const candidate = this.apiKeyInput.trim();
      if (!candidate) return;
      const ok = await this.testApiKey(candidate);
      if (!ok) {
        this.loginError = "That key was rejected by the server.";
        return;
      }
      setCookie(API_KEY_COOKIE, candidate, COOKIE_MAX_AGE_SECONDS);
      this.apiKey = candidate;
      this.hasApiKey = true;
      this.apiKeyInput = "";
      this.fetchObjectTypes();
      this.fetchEvents();
    },

    logout() {
      clearCookie(API_KEY_COOKIE);
      this.apiKey = null;
      this.hasApiKey = false;
      this.events = [];
    },

    async testApiKey(key) {
      try {
        const resp = await fetch("/events?limit=1", { headers: { "X-API-Key": key } });
        return resp.ok;
      } catch {
        return false;
      }
    },

    applyFilters() {
      this.offset = 0;
      this.fetchEvents();
    },

    prevPage() {
      this.offset = Math.max(0, this.offset - this.limit);
      this.fetchEvents();
    },

    nextPage() {
      this.offset += this.limit;
      this.fetchEvents();
    },

    async fetchEvents() {
      this.loading = true;
      try {
        const eventId = String(this.filters.eventId || "").trim();
        const q = String(this.filters.q || "").trim();
        const params = new URLSearchParams({
          limit: String(this.limit),
          offset: String(this.offset),
        });
        if (eventId) {
          // Searching by a specific known id -- ignores every other filter (date window
          // included, server-side) rather than trying to compose with them.
          params.set("event_id", eventId);
        } else {
          params.set("has_media", String(!!this.filters.onlyWithMedia));
          if (this.filters.objectType && this.filters.objectType !== "all") {
            params.set("object_type", this.filters.objectType);
          }
          if (this.filters.aiStatus && this.filters.aiStatus !== "all") {
            params.set("ai_status", this.filters.aiStatus);
          }
          if (q) {
            // A text search spans your whole history, not just the visible date window --
            // matches the API's own event_id/q window-bypass behavior.
            params.set("q", q);
          } else {
            if (this.filters.start) params.set("start", new Date(this.filters.start).toISOString());
            if (this.filters.end) params.set("end", new Date(this.filters.end).toISOString());
          }
        }

        const resp = await fetch(`/events?${params.toString()}`, {
          headers: { "X-API-Key": this.apiKey },
        });
        if (resp.status === 401) {
          this.logout();
          return;
        }
        if (!resp.ok) throw new Error(`GET /events failed: ${resp.status}`);
        this.events = await resp.json();
      } catch (err) {
        console.error(err);
        this.events = [];
      } finally {
        this.loading = false;
      }
    },

    thumbnailUrl(eventId, full = false) {
      const path = full ? `/events/${eventId}/image` : `/events/${eventId}/thumbnail`;
      return `${path}?api_key=${encodeURIComponent(this.apiKey)}`;
    },

    videoUrl(eventId) {
      return `/media/video/${eventId}?api_key=${encodeURIComponent(this.apiKey)}`;
    },

    async openLightbox(event) {
      this.lightboxEvent = event;
      // Default to video when both exist -- richer than a still frame -- but the toggle buttons
      // (shown only when both are present) let you switch to the image side and back.
      this.lightboxMode = event.has_video ? "video" : "image";
      this.lightboxDetail = null;
      if (event.ai_status !== "done") return;
      // The AI analysis result (plate, color, description) isn't in the list response -- keeps
      // GET /events light -- so fetch it only when actually opening an analyzed event.
      this.lightboxLoading = true;
      try {
        const resp = await fetch(`/events/${event.id}`, { headers: { "X-API-Key": this.apiKey } });
        if (resp.ok) this.lightboxDetail = await resp.json();
      } catch (err) {
        console.error(err);
      } finally {
        this.lightboxLoading = false;
      }
    },

    closeLightbox() {
      this.lightboxEvent = null;
      this.lightboxDetail = null;
    },

    sightingFields() {
      const d = this.lightboxDetail;
      if (!d) return [];
      if (d.vehicle_sighting) {
        const vs = d.vehicle_sighting;
        return [
          ["Color", vs.color], ["Body type", vs.body_type],
          ["Make", vs.make_guess], ["Model", vs.model_guess],
          ["Plate (LLM)", vs.plate_text_llm], ["Plate (Frigate)", vs.plate_text_frigate],
          ["Notable features", vs.notable_features], ["Notes", vs.notes],
        ].filter(([, value]) => value);
      }
      if (d.person_sighting) {
        const ps = d.person_sighting;
        return [["Description", ps.description], ["Notes", ps.notes]].filter(([, value]) => value);
      }
      return [];
    },

    formatTs(iso) {
      try {
        return new Date(iso).toLocaleString();
      } catch {
        return iso;
      }
    },
  };
}
