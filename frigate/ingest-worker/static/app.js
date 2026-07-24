// Yard Stats web report -- talks to this same origin's /events, /events/{id}/thumbnail,
// /events/{id}/image, /media/video/{id}. No external requests, no build step -- vanilla JS +
// Alpine.js (vendored locally in vendor/alpine.min.js).

const API_KEY_COOKIE = "api_key";
const COOKIE_MAX_AGE_SECONDS = 10 * 365 * 24 * 60 * 60; // ~10 years -- "never" isn't representable
const AUTO_REFRESH_SECONDS = 15;

// Search tab's precision dropdown -- named presets over POST /search's own max_distance param
// rather than exposing a raw cosine-distance number, since that's meaningless to a user. "High"
// is the default: without any cutoff, a query with fewer genuinely-relevant sightings than the
// requested limit still pads the result grid out with whatever's next-closest, which reads as
// "search is broken" rather than "there just isn't more to find" (confirmed in practice -- a
// small/general embedding model's false-positive distances can land within ~0.05 of true
// matches, so "no cutoff" and "very lenient cutoff" look almost identical in the noisiest cases).
// null means no filter at all (today's original, unfiltered behavior).
const SEARCH_PRECISION_OPTIONS = [
  { value: "high", label: "High precision", maxDistance: 0.45 },
  { value: "balanced", label: "Balanced", maxDistance: 0.55 },
  { value: "broad", label: "Show everything", maxDistance: null },
];

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

    // Footer build/version info (GET /status, unauthenticated) -- fetched regardless of login
    // state, since it's purely informational and /status needs no API key.
    versionInfo: null,

    events: [],
    visits: [],
    // Semantic search results (POST /search) -- a ranked top-N list, not a paginated browse like
    // events/visits, so there's no offset/totalCount concept for this one.
    searchResults: [],
    searchError: "",
    // Distinguishes "haven't searched yet" from "searched, zero results" for the empty-state text.
    searchAttempted: false,
    viewMode: "visits",
    loading: false,
    limit: 24,
    offset: 0,
    // Total rows matching the current filters (X-Total-Count response header) -- null until the
    // first successful fetch, so totalPages()/currentPage() can tell "unknown yet" apart from
    // "zero results" instead of guessing from currentList().length < limit.
    totalCount: null,
    objectTypes: [],
    cameras: [],
    advancedSearch: false,
    // Quick time-range presets for the default view's "Time range" selector -- the advanced
    // panel's From/To date pickers override this when set (see fetchEvents/fetchVisits).
    hoursOptions: [1, 3, 6, 12, 24],
    searchPrecisionOptions: SEARCH_PRECISION_OPTIONS,

    autoRefreshEnabled: true,
    lastUpdated: null,
    _autoRefreshTimer: null,

    filters: {
      objectType: "all", camera: "all", aiStatus: "all", q: "",
      hours: 1, start: "", end: "", precision: "high", maxDistanceOverride: "0.5",
    },

    // Shared by resetFilters/toggleAdvancedSearch/switchView -- every one of them resets to this
    // same clean slate, just triggered by a different action. Search defaults to a 24-hour window
    // (matching POST /search's own default) rather than Events/Visits' 1-hour default -- a semantic
    // search silently scoped to the last hour returned only the closest-available-but-irrelevant
    // matches with no indication why (e.g. searching "dog" found no dog sighting in the last hour
    // and fell back to ranking car/person sightings instead), which read as broken ranking rather
    // than an invisible, overly narrow time filter.
    _defaultFilters(mode) {
      return {
        objectType: "all", camera: "all", aiStatus: "all", q: "",
        hours: mode === "search" ? 24 : 1, start: "", end: "", precision: "high", maxDistanceOverride: "0.5",
      };
    },

    lightboxEvent: null,
    lightboxMode: "video",
    // Array of {title, fields}, one entry per sighting -- a plain event has at most one (vehicle
    // or person), but a visit can have several: one representative per distinct object type the
    // visit grouped together (see claim_ai_batch's only_visit_representative comment in db.py),
    // e.g. a car and a person in the same visit each get their own entry here.
    lightboxGroups: [],
    // Every raw_event a visit grouped together (GET /events?visit_id=...), for the "Connected
    // events" strip -- always empty for a plain event (no visitId to fetch by).
    lightboxConnectedEvents: [],
    lightboxLoading: false,
    // Set when a connected event's own lightbox was opened from a visit's "Connected events"
    // strip -- a plain EventSummary has no visitId of its own, so without remembering where we
    // came from, drilling into one connected event stranded you there with no way back to the
    // visit/alert view (openVisitLightbox's own shape: {id, representative_event_id, has_video,
    // has_image, has_preview_gif, ai_status}). Null whenever the lightbox wasn't reached that way.
    lightboxParentVisit: null,

    init() {
      this.fetchVersionInfo();
      const stored = getCookie(API_KEY_COOKIE);
      if (stored) {
        this.apiKey = stored;
        this.hasApiKey = true;
        this.fetchObjectTypes();
        this.fetchCameras();
        this.refresh();
        this.startAutoRefresh();
      }
    },

    // GET /status is unauthenticated (same tier as /health) -- the footer shows regardless of
    // login state. Failure just leaves the footer blank rather than surfacing an error banner;
    // this is cosmetic, not worth alarming over.
    async fetchVersionInfo() {
      try {
        const resp = await fetch("/status");
        if (!resp.ok) return;
        const data = await resp.json();
        this.versionInfo = {
          version: data.version, buildSha: data.build_sha,
          buildDate: data.build_date, githubUrl: data.github_url,
        };
      } catch (err) {
        console.error(err);
      }
    },

    // "Yard Stats | v1.1 (build: 74cd4c9) | 07.2026 |" -- the footer's own GitHub link is a
    // separate real <a> in the template, not part of this string.
    footerText() {
      if (!this.versionInfo) return "";
      const v = this.versionInfo;
      return `Yard Stats | ${v.version} (build: ${v.buildSha}) | ${v.buildDate} |`;
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
      } else if (this.viewMode === "search") {
        await this.fetchSearchResults();
      } else {
        await this.fetchEvents();
      }
    },

    switchView(mode) {
      if (this.viewMode === mode) return;
      this.viewMode = mode;
      this.offset = 0;
      // A filter set in one view (an Events-only field, or even a shared one like q) otherwise
      // kept applying on the next fetch after switching -- same class of confusion the advanced/
      // simple mode toggle already resets for (see toggleAdvancedSearch). A clean slate on every
      // tab switch is simpler than reasoning about which values are still meaningful in the new view.
      this.filters = this._defaultFilters(mode);
      this.searchResults = [];
      this.searchAttempted = false;
      this.searchError = "";
      this.refresh();
    },

    currentList() {
      if (this.viewMode === "visits") return this.visits;
      if (this.viewMode === "search") return this.searchResults;
      return this.events;
    },

    currentPage() {
      return Math.floor(this.offset / this.limit) + 1;
    },

    // 1 while totalCount hasn't come back yet (or is 0) -- always at least one page, matching
    // currentPage()'s own floor of 1.
    totalPages() {
      if (!this.totalCount) return 1;
      return Math.max(1, Math.ceil(this.totalCount / this.limit));
    },

    hasNextPage() {
      if (this.totalCount === null) return this.currentList().length >= this.limit;
      return this.offset + this.limit < this.totalCount;
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

    async fetchCameras() {
      // Unlike Type (a manually-maintained env var), the Camera dropdown is populated from
      // whatever cameras actually have data -- see db.get_distinct_cameras' own comment for why.
      try {
        const resp = await fetch("/cameras", { headers: { "X-API-Key": this.apiKey } });
        if (!resp.ok) return;
        const data = await resp.json();
        this.cameras = data.cameras || [];
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
      this.fetchCameras();
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
      // AI status is a per-raw_event concept fetchVisits() intentionally ignores (see there) and
      // is hidden entirely while on the Visits tab (see index.html), but a value can still be
      // left over from before a tab switch in edge cases -- auto-switching to Events when one is
      // actually set makes Search take effect instead of silently doing nothing. q (Search AI
      // analysis) is excluded here since GET /visits now supports it directly.
      const usesEventsOnlyFilter = !!(this.filters.aiStatus && this.filters.aiStatus !== "all");
      if (this.viewMode === "visits" && usesEventsOnlyFilter) {
        this.viewMode = "events";
      }
      this.offset = 0;
      this.refresh();
    },

    resetFilters() {
      this.filters = this._defaultFilters(this.viewMode);
      this.advancedSearch = false;
      this.applyFilters();
    },

    // Switching modes without resetting left stale advanced-only values (From/To, Type, ...)
    // in effect but invisible once their fields hid again -- e.g. leaving From/To set after
    // going back to simple mode silently overrode the reappeared Time range preset with no
    // indication why. Resetting on every toggle (either direction) avoids that class of
    // confusion entirely rather than only patching the one Time-range/From-To case.
    toggleAdvancedSearch() {
      this.advancedSearch = !this.advancedSearch;
      this.filters = this._defaultFilters(this.viewMode);
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
        const q = String(this.filters.q || "").trim();
        const params = new URLSearchParams({
          limit: String(this.limit),
          offset: String(this.offset),
        });
        if (this.filters.objectType && this.filters.objectType !== "all") {
          params.set("object_type", this.filters.objectType);
        }
        if (this.filters.camera && this.filters.camera !== "all") {
          params.set("camera", this.filters.camera);
        }
        if (this.filters.aiStatus && this.filters.aiStatus !== "all") {
          params.set("ai_status", this.filters.aiStatus);
        }
        if (q) {
          params.set("q", q);
        }
        // The time window still applies alongside q -- a search only looks within the
        // currently selected range, same as every other filter, rather than spanning your
        // whole history regardless of what's selected.
        if (this.filters.start || this.filters.end) {
          // Advanced panel's custom From/To overrides the quick "Time range" preset when set.
          if (this.filters.start) params.set("start", new Date(this.filters.start).toISOString());
          if (this.filters.end) params.set("end", new Date(this.filters.end).toISOString());
        } else {
          params.set("hours", String(this.filters.hours));
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
        const totalHeader = resp.headers.get("X-Total-Count");
        this.totalCount = totalHeader !== null ? Number(totalHeader) : null;
        this.lastUpdated = new Date();
      } catch (err) {
        console.error(err);
        this.events = [];
        this.totalCount = null;
      } finally {
        this.loading = false;
      }
    },

    async fetchVisits() {
      // Comparison view alongside fetchEvents -- one card per Frigate review/alert segment
      // (visit) instead of one per raw_event, so duplicate det_ids from tracker re-ID/label
      // flicker collapse into a single card. start/end/objectType/camera/q carry over from the
      // filter bar -- aiStatus is a per-raw_event concept that doesn't compose cleanly with a
      // grouped view, so this view intentionally ignores it rather than half-applying it (and is
      // hidden entirely in this view -- see index.html).
      this.loading = true;
      try {
        const q = String(this.filters.q || "").trim();
        const params = new URLSearchParams({
          limit: String(this.limit),
          offset: String(this.offset),
        });
        if (this.filters.objectType && this.filters.objectType !== "all") {
          params.set("object_type", this.filters.objectType);
        }
        if (this.filters.camera && this.filters.camera !== "all") {
          params.set("camera", this.filters.camera);
        }
        if (q) {
          params.set("q", q);
        }
        // Same as fetchEvents -- the time window still applies alongside q rather than a search
        // spanning your whole history regardless of what's selected.
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
        const totalHeader = resp.headers.get("X-Total-Count");
        this.totalCount = totalHeader !== null ? Number(totalHeader) : null;
        this.lastUpdated = new Date();
      } catch (err) {
        console.error(err);
        this.visits = [];
        this.totalCount = null;
      } finally {
        this.loading = false;
      }
    },

    // Semantic search over both sightings (events) and visit_sightings (alerts), server-embedded
    // (POST /search -- the browser can't call the embedding backend directly). Reuses the same
    // filter bar as Events/Visits (filters.q is the query text here, plus Time range/From-To/
    // Type), but there's no pagination -- this is a ranked top-N grid, not a browsable list, so
    // offset/totalCount don't apply and are left untouched.
    async fetchSearchResults() {
      const query = String(this.filters.q || "").trim();
      this.searchAttempted = true;
      this.searchError = "";
      if (!query) {
        // Nothing typed yet -- don't fire a request (and don't show "no results" for a search
        // that never ran); switching tabs alone shouldn't trigger a network call either.
        this.searchResults = [];
        this.searchAttempted = false;
        return;
      }
      this.loading = true;
      try {
        const body = { query, limit: this.limit };
        if (this.filters.objectType && this.filters.objectType !== "all") {
          body.object_types = [this.filters.objectType];
        }
        if (this.filters.camera && this.filters.camera !== "all") {
          body.camera = this.filters.camera;
        }
        // Advanced mode's exact-value override wins over the simple Precision preset -- the preset
        // is coarse on purpose (three named steps), but a user comparing results across borderline
        // cases (e.g. a cutoff of 0.45 vs. 0.46) wants finer control than three buckets offer.
        // Gated on advancedSearch itself (not just whether the field has a value) so the field's
        // own default (0.5, prefilled the moment advanced mode opens) never silently overrides the
        // simple/default search experience while the advanced panel isn't even shown.
        const override = this.advancedSearch ? String(this.filters.maxDistanceOverride || "").trim() : "";
        if (override !== "" && !Number.isNaN(Number(override))) {
          body.max_distance = Number(override);
        } else {
          const precisionOption = SEARCH_PRECISION_OPTIONS.find((p) => p.value === this.filters.precision);
          if (precisionOption && precisionOption.maxDistance !== null) {
            body.max_distance = precisionOption.maxDistance;
          }
        }
        // Same From/To-overrides-preset precedence fetchEvents/fetchVisits already use.
        if (this.filters.start || this.filters.end) {
          if (this.filters.start) body.start = new Date(this.filters.start).toISOString();
          if (this.filters.end) body.end = new Date(this.filters.end).toISOString();
        } else {
          body.hours = Number(this.filters.hours);
        }

        const resp = await fetch("/search", {
          method: "POST",
          headers: { "X-API-Key": this.apiKey, "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        if (resp.status === 401) {
          this.logout();
          return;
        }
        if (!resp.ok) {
          const detail = await resp.json().catch(() => null);
          throw new Error((detail && detail.detail) || `POST /search failed: ${resp.status}`);
        }
        const data = await resp.json();
        this.searchResults = data.results;
        this.lastUpdated = new Date();
      } catch (err) {
        console.error(err);
        this.searchResults = [];
        this.searchError = err.message || "Search failed.";
      } finally {
        this.loading = false;
      }
    },

    // Rough, human-friendly stand-in for cosine distance on a search result card -- not a
    // calibrated probability, just 1-distance clamped to [0, 100] so a result reads as "closer to
    // 100% = closer match" without exposing the raw distance number, which means nothing to a
    // user unfamiliar with the embedding space. The full distance is still available via the
    // badge's title tooltip for anyone who wants the exact number.
    matchPercent(distance) {
      return Math.max(0, Math.min(100, Math.round((1 - distance) * 100)));
    },

    // Routes a clicked search result into the same shared lightbox Events/Visits already use --
    // same {id, visitId, has_video, has_image, has_preview_gif, ai_status} shape openVisitLightbox
    // builds below, just sourced from the search result row instead of a VisitSummary/EventSummary
    // (POST /search already returns these fields for exactly this reason -- see db.py's
    // semantic_search_combined). A visit-kind result never has its own preview GIF field checked
    // for events (that artifact doesn't exist at the event level).
    openSearchResult(result) {
      if (result.kind === "visit") {
        this.openLightbox({
          id: result.id, visitId: result.id,
          has_video: result.has_video, has_image: result.has_image,
          has_preview_gif: result.has_preview_gif, ai_status: result.ai_status,
        });
      } else {
        this.openLightbox({
          id: result.id, has_video: result.has_video, has_image: result.has_image,
          has_preview_gif: false, ai_status: result.ai_status,
        });
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
        has_preview_gif: visit.has_preview_gif,
        ai_status: visit.ai_status,
      });
    },

    // Opens a connected event's own lightbox (clicked from a visit's "Connected events" strip),
    // remembering the visit we came from first -- a plain EventSummary has no visitId of its own,
    // so without this the connected-events strip (and the whole visit context) was gone the
    // moment you drilled into one, with no way back to the alert you started from. Stored in the
    // same shape openVisitLightbox expects so "back" is just calling that again.
    openConnectedEvent(ev) {
      const current = this.lightboxEvent;
      if (current && current.visitId) {
        this.lightboxParentVisit = {
          id: current.visitId,
          representative_event_id: current.id,
          has_video: current.has_video,
          has_image: current.has_image,
          has_preview_gif: current.has_preview_gif,
          ai_status: current.ai_status,
        };
      }
      this.openLightbox(ev);
    },

    backToVisit() {
      const visit = this.lightboxParentVisit;
      if (!visit) return;
      this.lightboxParentVisit = null;
      this.openVisitLightbox(visit);
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

    // A visit's animated preview GIF (crop.build_visit_preview's slideshow of frames sampled
    // proportionally across the visit's own clip) -- human preview only, a separate artifact from
    // the composite grid image used for the thumbnail/lightbox still and AI analysis.
    visitPreviewGifUrl(visitId) {
      return `/visits/${visitId}/preview.gif?api_key=${encodeURIComponent(this.apiKey)}`;
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

    // Points at whichever of video/image/preview-gif is actually being shown right now (same
    // lightboxMode logic as the toggle buttons) -- the download button always saves what's on
    // screen, not a fixed choice between them.
    lightboxDownloadUrl() {
      const e = this.lightboxEvent;
      if (!e) return "";
      if (this.lightboxMode === "preview" && e.has_preview_gif) return this.visitPreviewGifUrl(e.visitId);
      const showingVideo = e.has_video && this.lightboxMode === "video";
      return showingVideo ? this.lightboxVideoUrl() : this.lightboxImageUrl();
    },

    lightboxDownloadFilename() {
      const e = this.lightboxEvent;
      if (!e) return "";
      const label = e.visitId ? `visit-${e.visitId}` : `event-${e.id}`;
      if (this.lightboxMode === "preview" && e.has_preview_gif) return `${label}.gif`;
      const showingVideo = e.has_video && this.lightboxMode === "video";
      return `${label}.${showingVideo ? "mp4" : "jpg"}`;
    },

    async openLightbox(event) {
      this.lightboxEvent = event;
      // Default to the animated preview GIF when available -- richer than either a still frame or
      // the plain video, since it's already framed to the sampled moments of interest -- then
      // video, then the still image as a last resort. The toggle buttons (shown whenever more
      // than one is available) let you switch between them freely.
      this.lightboxMode = event.has_preview_gif ? "preview" : event.has_video ? "video" : "image";
      this.lightboxGroups = [];
      this.lightboxConnectedEvents = [];
      // A visit's own ai_status (event.ai_status) only reflects its single earliest-linked
      // event -- a second, different-object-type event in the same visit can still be
      // analyzed (or still pending) independently of that one, so the visit branch always
      // fetches rather than gating on it. A plain event has exactly one status, so that gate
      // still applies there.
      if (!event.visitId && event.ai_status !== "done") return;
      // The AI analysis result (plate, color, description) isn't in the list response -- keeps
      // GET /events / GET /visits light -- so fetch it only when actually opening an item.
      this.lightboxLoading = true;
      try {
        if (event.visitId) {
          // Sightings and connected-events are independent fetches -- run them in parallel rather
          // than one after the other, since neither depends on the other's result.
          const [sightingsResp, eventsResp] = await Promise.all([
            fetch(`/visits/${event.visitId}/sightings`, { headers: { "X-API-Key": this.apiKey } }),
            fetch(`/events?visit_id=${event.visitId}&has_media=false&limit=50`, { headers: { "X-API-Key": this.apiKey } }),
          ]);
          if (sightingsResp.ok) {
            const data = await sightingsResp.json();
            // Prefer the visit's own alert-stage analysis (AI_ALERTS_ENABLED, the 2x2 grid) when
            // it's ready -- it's the richer, change-aware result this whole view exists for.
            // Falls back to the per-event sightings (AI_EVENTS_STAGE_ENABLED) when the alert
            // stage is off or hasn't finished this visit yet, so the lightbox never shows nothing
            // just because one specific stage is still catching up.
            if (data.alert_sighting) {
              this.lightboxGroups = [{
                title: `${this.titleCase(data.alert_sighting.object_label)} (alert analysis)`,
                fields: this.sightingFields(data.alert_sighting),
              }];
            } else {
              this.lightboxGroups = data.sightings.map((s) => ({
                title: this.titleCase(s.object_label), fields: this.sightingFields(s),
              }));
            }
          }
          if (eventsResp.ok) {
            // Earliest-first -- GET /events itself orders newest-first for normal browsing, but
            // reading a visit's connected events chronologically (what happened, in order) reads
            // more naturally than newest-first for this specific strip.
            this.lightboxConnectedEvents = (await eventsResp.json()).reverse();
          }
        } else {
          const resp = await fetch(`/events/${event.id}`, { headers: { "X-API-Key": this.apiKey } });
          if (resp.ok) {
            const d = await resp.json();
            if (d.sighting) {
              this.lightboxGroups.push({
                title: this.titleCase(d.sighting.object_label), fields: this.sightingFields(d.sighting),
              });
            }
          }
        }
      } catch (err) {
        console.error(err);
      } finally {
        this.lightboxLoading = false;
      }
    },

    closeLightbox() {
      this.lightboxEvent = null;
      this.lightboxGroups = [];
      this.lightboxConnectedEvents = [];
      this.lightboxParentVisit = null;
    },

    // A sighting is just {object_label, description} in this universal model -- no per-type
    // field table to build (color/body_type/make/model/... no longer exist as separate columns),
    // the model's own free-text answer already is the one-line "Description" to show.
    sightingFields(s) {
      return [["Description", s.description]].filter(([, value]) => value);
    },

    titleCase(label) {
      if (!label) return "Sighting";
      return label.charAt(0).toUpperCase() + label.slice(1);
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
