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

    filters: { objectType: "all", aiStatus: "all", onlyWithImages: true, ...defaultWindow() },

    lightboxEvent: null,

    init() {
      const stored = getCookie(API_KEY_COOKIE);
      if (stored) {
        this.apiKey = stored;
        this.hasApiKey = true;
        this.fetchEvents();
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
        const params = new URLSearchParams({
          limit: String(this.limit),
          offset: String(this.offset),
          has_image: String(!!this.filters.onlyWithImages),
        });
        if (this.filters.objectType && this.filters.objectType !== "all") {
          params.set("object_type", this.filters.objectType);
        }
        if (this.filters.aiStatus && this.filters.aiStatus !== "all") {
          params.set("ai_status", this.filters.aiStatus);
        }
        if (this.filters.start) params.set("start", new Date(this.filters.start).toISOString());
        if (this.filters.end) params.set("end", new Date(this.filters.end).toISOString());

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

    openLightbox(event) {
      this.lightboxEvent = event;
    },

    closeLightbox() {
      this.lightboxEvent = null;
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
