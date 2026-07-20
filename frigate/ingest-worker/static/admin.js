// Yard Stats admin dashboard -- talks to this same origin's /admin/*, /retention/purge,
// /embeddings/backfill. No external requests, no build step -- vanilla JS + Alpine.js (vendored
// locally in vendor/alpine.min.js). Shares the api_key cookie with the main report UI (app.js) --
// logging in on either page logs you in on both.

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

function fmtBytes(bytes) {
  if (bytes === null || bytes === undefined) return "...";
  if (bytes === 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.min(units.length - 1, Math.floor(Math.log(bytes) / Math.log(1024)));
  return (bytes / Math.pow(1024, i)).toFixed(i === 0 ? 0 : 1) + " " + units[i];
}

function fmtNum(n) {
  if (n === null || n === undefined) return "...";
  return n.toLocaleString();
}

function adminApp() {
  return {
    apiKey: null,
    apiKeyInput: "",
    hasApiKey: false,
    loginError: "",
    loadError: "",
    lastRefreshed: null,

    overview: null,
    diskUsage: null,

    checkingEmbed: false,
    embedCheck: null,

    backfilling: false,
    backfillResult: "",
    reindexing: false,
    reindexResult: "",

    requeuing: null,
    requeueResult: {},

    purgeDays: 60,
    purgeOnlyMedia: true,
    purging: false,
    purgePreview: null,
    purgeResult: "",

    fmtBytes,
    fmtNum,

    init() {
      const stored = getCookie(API_KEY_COOKIE);
      if (stored) {
        this.apiKey = stored;
        this.hasApiKey = true;
        this.refreshAll();
      }
    },

    async saveApiKey() {
      this.loginError = "";
      const candidate = this.apiKeyInput.trim();
      if (!candidate) return;
      const ok = await this._testApiKey(candidate);
      if (!ok) {
        this.loginError = "That key was rejected by the server.";
        return;
      }
      setCookie(API_KEY_COOKIE, candidate, COOKIE_MAX_AGE_SECONDS);
      this.apiKey = candidate;
      this.hasApiKey = true;
      this.apiKeyInput = "";
      this.refreshAll();
    },

    logout() {
      clearCookie(API_KEY_COOKIE);
      this.apiKey = null;
      this.hasApiKey = false;
      this.overview = null;
      this.diskUsage = null;
    },

    async _testApiKey(key) {
      try {
        const resp = await fetch("/admin/overview", { headers: { "X-API-Key": key } });
        return resp.ok;
      } catch {
        return false;
      }
    },

    _headers() {
      return { "X-API-Key": this.apiKey, "Content-Type": "application/json" };
    },

    async _get(path) {
      const resp = await fetch(path, { headers: this._headers() });
      if (resp.status === 401) {
        this.logout();
        throw new Error("API key rejected");
      }
      if (!resp.ok) throw new Error(`${path} -> HTTP ${resp.status}`);
      return resp.json();
    },

    async _post(path) {
      const resp = await fetch(path, { method: "POST", headers: this._headers() });
      if (resp.status === 401) {
        this.logout();
        throw new Error("API key rejected");
      }
      if (!resp.ok) throw new Error(`${path} -> HTTP ${resp.status}`);
      return resp.json();
    },

    async refreshAll() {
      this.loadError = "";
      try {
        this.overview = await this._get("/admin/overview");
        this.lastRefreshed = new Date().toLocaleTimeString();
      } catch (e) {
        this.loadError = "Failed to load overview: " + e.message;
        return;
      }
      // Disk usage is a real filesystem walk -- loaded separately so a slow scan never blocks the
      // rest of the dashboard from showing up.
      this._get("/admin/disk-usage").then((d) => { this.diskUsage = d; }).catch((e) => {
        this.loadError = "Failed to load disk usage: " + e.message;
      });
    },

    flagEntries() {
      if (!this.overview) return [];
      const f = this.overview.feature_flags;
      const boolFlag = (label, value) => ({ label, value: value ? "on" : "off", ok: value });
      const modeFlag = (label, value) => ({ label, value, ok: value !== "none" });
      return [
        boolFlag("AI events stage", f.ai_events_stage_enabled),
        boolFlag("AI alerts stage", f.ai_alerts_enabled),
        boolFlag("Store video", f.store_video),
        boolFlag("Store video (alerts)", f.store_video_alerts),
        boolFlag("Visit preview", f.visit_thumb_crop_enabled),
        boolFlag("Crop disabled", f.crop_disabled),
        boolFlag("Frigate snapshot (events)", f.frigate_snapshot_enabled),
        modeFlag("Telegram (events)", f.telegram_events_mode),
        modeFlag("Telegram (alerts)", f.telegram_alerts_mode),
      ];
    },

    stageList() {
      if (!this.overview) return [];
      const sc = this.overview.stage_counts;
      return [
        { table: "raw_events", stage: "crop", counts: sc.raw_events.crop_status },
        { table: "raw_events", stage: "video", counts: sc.raw_events.video_status },
        { table: "raw_events", stage: "ai", counts: sc.raw_events.ai_status },
        { table: "visits", stage: "video", counts: sc.visits.video_status },
        { table: "visits", stage: "thumb_crop", counts: sc.visits.thumb_crop_status },
        { table: "visits", stage: "alert_ai", counts: sc.visits.alert_ai_status },
      ];
    },

    async checkEmbeddingBackend() {
      this.checkingEmbed = true;
      this.embedCheck = null;
      try {
        this.embedCheck = await this._get("/admin/embedding-backend/check");
      } catch (e) {
        this.embedCheck = { ok: false, detail: e.message };
      } finally {
        this.checkingEmbed = false;
      }
    },

    async backfillEmbeddings() {
      this.backfilling = true;
      this.backfillResult = "";
      try {
        const r = await fetch("/embeddings/backfill?confirm=true&limit=200", {
          method: "POST", headers: this._headers(),
        });
        const d = await r.json();
        this.backfillResult = `Processed ${d.vehicles_processed + d.persons_processed} rows ` +
          `(${d.vehicles_updated} vehicle, ${d.persons_updated} person updated). Run again if counts above are still nonzero.`;
        await this.refreshAll();
      } catch (e) {
        this.backfillResult = "Failed: " + e.message;
      } finally {
        this.backfilling = false;
      }
    },

    async reindexVector() {
      this.reindexing = true;
      this.reindexResult = "";
      try {
        const d = await this._post("/admin/vector/reindex");
        this.reindexResult = "Reindexed: " + d.reindexed.join(", ");
      } catch (e) {
        this.reindexResult = "Failed: " + e.message;
      } finally {
        this.reindexing = false;
      }
    },

    async requeueFailed(table, stage) {
      const key = table + stage;
      this.requeuing = key;
      try {
        const r = await fetch(`/admin/queue/requeue-failed?table=${table}&stage=${stage}`, {
          method: "POST", headers: this._headers(),
        });
        const d = await r.json();
        this.requeueResult = { ...this.requeueResult, [key]: `Requeued ${d.requeued}` };
        await this.refreshAll();
      } catch (e) {
        this.requeueResult = { ...this.requeueResult, [key]: "Failed: " + e.message };
      } finally {
        this.requeuing = null;
      }
    },

    async previewPurge() {
      this.purging = true;
      this.purgeResult = "";
      try {
        const r = await fetch(`/retention/purge?older_than_days=${this.purgeDays}&confirm=false&only_media=${this.purgeOnlyMedia}`, {
          method: "POST", headers: this._headers(),
        });
        this.purgePreview = await r.json();
      } catch (e) {
        this.purgeResult = "Preview failed: " + e.message;
      } finally {
        this.purging = false;
      }
    },

    async confirmPurge() {
      if (!this.purgePreview) return;
      const c = this.purgePreview.counts;
      const ok = this.purgeOnlyMedia
        ? confirm(
            `This will clear ${c.raw_events_video_files + c.visits_video_files} stored video files and ` +
            `${c.raw_events_images + c.visits_images_or_gifs} stored images/GIFs older than ${this.purgeDays} days. ` +
            `Rows and all AI analysis text are kept. This cannot be undone. Continue?`
          )
        : confirm(
            `This will PERMANENTLY delete ${c.raw_events} events, ${c.visits} visits, ` +
            `${c.vehicle_sightings} vehicle sightings, ${c.person_sightings} person sightings, ` +
            `${c.visit_vehicle_sightings} alert vehicle sightings, and ${c.visit_person_sightings} alert person ` +
            `sightings older than ${this.purgeDays} days, then rebuild the vector search index. This cannot be undone. Continue?`
          );
      if (!ok) return;
      this.purging = true;
      try {
        const r = await fetch(`/retention/purge?older_than_days=${this.purgeDays}&confirm=true&only_media=${this.purgeOnlyMedia}`, {
          method: "POST", headers: this._headers(),
        });
        const d = await r.json();
        this.purgeResult = this.purgeOnlyMedia
          ? `Cleared: ${JSON.stringify(d.counts)}`
          : `Deleted: ${JSON.stringify(d.counts)}` + (d.reindexed ? ` -- reindexed: ${d.reindexed.join(", ")}` : "");
        this.purgePreview = null;
        await this.refreshAll();
      } catch (e) {
        this.purgeResult = "Delete failed: " + e.message;
      } finally {
        this.purging = false;
      }
    },
  };
}
