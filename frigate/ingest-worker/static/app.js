// Yard Stats web report -- talks to this same origin's /events, /events/{id}/thumbnail,
// /events/{id}/image, /media/video/{id}. No external requests, no build step -- vanilla JS +
// Alpine.js (vendored locally in vendor/alpine.min.js).

const API_KEY_COOKIE = "api_key";
const COOKIE_MAX_AGE_SECONDS = 10 * 365 * 24 * 60 * 60; // ~10 years -- "never" isn't representable
const AUTO_REFRESH_SECONDS = 15;

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

function eventsApp() {
  return {
    apiKey: null,
    apiKeyInput: "",
    hasApiKey: false,
    loginError: "",

    events: [],
    visits: [],
    viewMode: "visits",
    loading: false,
    limit: 24,
    offset: 0,
    objectTypes: [],
    advancedSearch: false,
    // Quick time-range presets for the default view's "Time range" selector -- the advanced
    // panel's From/To date pickers override this when set (see fetchEvents/fetchVisits).
    hoursOptions: [1, 3, 6, 12, 24],

    autoRefreshEnabled: true,
    lastUpdated: null,
    _autoRefreshTimer: null,

    filters: {
      objectType: "all", aiStatus: "all", onlyWithMedia: true, eventId: "", q: "",
      hours: 1, start: "", end: "",
    },

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
        this.refresh();
        this.startAutoRefresh();
      }
    },

    // Polls the currently active view (Events or Visits) on a timer so new items show up without
    // a manual reload or clicking Search. Deliberately conservative about when it actually fires
    // (see autoRefreshTick) so it can't clobber an in-progress pagination or filter edit.
    startAutoRefresh() {
      this.stopAutoRefresh();
      if (!this.autoRefreshEnabled) return;
      this._autoRefreshTimer = setInterval(() => this.autoRefreshTick(), AUTO_REFRESH_SECONDS * 1000);
    },

    stopAutoRefresh() {
      if (this._autoRefreshTimer) {
        clearInterval(this._autoRefreshTimer);
        this._autoRefreshTimer = null;
      }
    },

    toggleAutoRefresh() {
      if (this.autoRefreshEnabled) {
        this.startAutoRefresh();
      } else {
        this.stopAutoRefresh();
      }
    },

    autoRefreshTick() {
      if (document.hidden) return; // tab not visible -- nothing to update on screen right now
      // Only the first/most-recent page -- refetching a later page could silently shift which
      // rows are shown as newer items arrive ahead of it.
      if (this.offset !== 0) return;
      // Don't clobber an in-progress filter edit (e.g. mid-typing in the search box).
      const active = document.activeElement;
      if (active && ["INPUT", "SELECT", "TEXTAREA"].includes(active.tagName)) return;
      this.refresh();
    },

    // Dispatches to whichever list is active -- lets applyFilters/prevPage/nextPage stay
    // view-agnostic instead of each needing an if/else on viewMode.
    async refresh() {
      if (this.viewMode === "visits") {
        await this.fetchVisits();
      } else {
        await this.fetchEvents();
      }
    },

    switchView(mode) {
      if (this.viewMode === mode) return;
      this.viewMode = mode;
      this.offset = 0;
      this.refresh();
    },

    currentList() {
      return this.viewMode === "visits" ? this.visits : this.events;
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
      this.refresh();
      this.startAutoRefresh();
    },

    logout() {
      clearCookie(API_KEY_COOKIE);
      this.apiKey = null;
      this.hasApiKey = false;
      this.events = [];
      this.stopAutoRefresh();
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
      this.refresh();
    },

    resetFilters() {
      this.filters = {
        objectType: "all", aiStatus: "all", onlyWithMedia: true, eventId: "", q: "",
        hours: 1, start: "", end: "",
      };
      this.advancedSearch = false;
      this.applyFilters();
    },

    prevPage() {
      this.offset = Math.max(0, this.offset - this.limit);
      this.refresh();
    },

    nextPage() {
      this.offset += this.limit;
      this.refresh();
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
          } else if (this.filters.start || this.filters.end) {
            // Advanced panel's custom From/To overrides the quick "Time range" preset when set.
            if (this.filters.start) params.set("start", new Date(this.filters.start).toISOString());
            if (this.filters.end) params.set("end", new Date(this.filters.end).toISOString());
          } else {
            params.set("hours", String(this.filters.hours));
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
        this.lastUpdated = new Date();
      } catch (err) {
        console.error(err);
        this.events = [];
      } finally {
        this.loading = false;
      }
    },

    async fetchVisits() {
      // Comparison view alongside fetchEvents -- one card per Frigate review/alert segment
      // (visit) instead of one per raw_event, so duplicate det_ids from tracker re-ID/label
      // flicker collapse into a single card. Only start/end/objectType carry over from the
      // filter bar -- eventId/q/aiStatus/onlyWithMedia are per-raw_event concepts that don't
      // compose cleanly with a grouped view, so this view intentionally ignores them rather than
      // half-applying them.
      this.loading = true;
      try {
        const params = new URLSearchParams({
          limit: String(this.limit),
          offset: String(this.offset),
        });
        if (this.filters.objectType && this.filters.objectType !== "all") {
          params.set("object_type", this.filters.objectType);
        }
        if (this.filters.start || this.filters.end) {
          if (this.filters.start) params.set("start", new Date(this.filters.start).toISOString());
          if (this.filters.end) params.set("end", new Date(this.filters.end).toISOString());
        } else {
          params.set("hours", String(this.filters.hours));
        }

        const resp = await fetch(`/visits?${params.toString()}`, {
          headers: { "X-API-Key": this.apiKey },
        });
        if (resp.status === 401) {
          this.logout();
          return;
        }
        if (!resp.ok) throw new Error(`GET /visits failed: ${resp.status}`);
        this.visits = await resp.json();
        this.lastUpdated = new Date();
      } catch (err) {
        console.error(err);
        this.visits = [];
      } finally {
        this.loading = false;
      }
    },

    openVisitLightbox(visit) {
      // Reuses the existing per-event lightbox on the visit's representative (earliest-linked)
      // raw_event for the image/AI-analysis side -- but a visit's own video (STORE_VIDEO_ALERTS)
      // is a completely separate file from anything on that raw_event, so visitId is carried
      // alongside id and lightboxVideoUrl() picks the right endpoint based on which is set.
      this.openLightbox({
        id: visit.representative_event_id,
        visitId: visit.id,
        has_video: visit.has_video,
        has_image: visit.has_image,
        ai_status: visit.ai_status,
      });
    },

    thumbnailUrl(eventId, full = false) {
      const path = full ? `/events/${eventId}/image` : `/events/${eventId}/thumbnail`;
      return `${path}?api_key=${encodeURIComponent(this.apiKey)}`;
    },

    // Visits get their own image endpoints -- prefers the visit's own thumb_time re-crop
    // (VISIT_THUMB_CROP_ENABLED) over the representative event's crop, server-side (see
    // GET /visits/{id}/thumbnail|image), not something the UI needs to branch on itself.
    visitThumbnailUrl(visitId, full = false) {
      const path = full ? `/visits/${visitId}/image` : `/visits/${visitId}/thumbnail`;
      return `${path}?api_key=${encodeURIComponent(this.apiKey)}`;
    },

    videoUrl(eventId) {
      return `/media/video/${eventId}?api_key=${encodeURIComponent(this.apiKey)}`;
    },

    visitVideoUrl(visitId) {
      return `/media/video/visit/${visitId}?api_key=${encodeURIComponent(this.apiKey)}`;
    },

    // The lightbox is shared between the Events and Visits views -- lightboxEvent.visitId is
    // only set when opened from a visit card, in which case its video (if any) lives under a
    // completely separate visit-video endpoint, not the per-event one.
    lightboxVideoUrl() {
      const e = this.lightboxEvent;
      if (!e) return "";
      return e.visitId ? this.visitVideoUrl(e.visitId) : this.videoUrl(e.id);
    },

    // Same visitId branch as lightboxVideoUrl, for the still-image side of the lightbox.
    lightboxImageUrl() {
      const e = this.lightboxEvent;
      if (!e) return "";
      return e.visitId ? this.visitThumbnailUrl(e.visitId, true) : this.thumbnailUrl(e.id, true);
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
