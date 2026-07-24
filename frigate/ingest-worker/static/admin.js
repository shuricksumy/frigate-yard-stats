// Yard Stats admin dashboard -- talks to this same origin's /admin/*, /retention/purge,
// /embeddings/backfill, /reports/generate, /object-types. No external requests, no build step --
// vanilla JS + Alpine.js (vendored locally in vendor/alpine.min.js). Shares the api_key cookie
// with the main report UI (app.js) -- logging in on either page logs you in on both.

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

    // Footer build/version info (GET /status, unauthenticated) -- fetched regardless of login
    // state, since it's purely informational and /status needs no API key.
    versionInfo: null,

    overview: null,
    diskUsage: null,
    objectTypes: [],

    checkingEmbed: false,
    embedCheck: null,

    backfilling: false,
    backfillResult: "",
    reindexing: false,
    reindexResult: "",

    requeuing: null,
    requeueResult: {},

    skippingFailed: null,
    skipFailedResult: {},

    purgeDays: 60,
    purgeOnlyMedia: true,
    purgeObjectLabel: "",
    purging: false,
    purgePreview: null,
    purgeResult: "",

    reportSource: "events",
    reportObjectLabel: "",
    reportHours: 24,
    reportIncludePreview: "gif",
    generatingReport: false,
    reportError: "",

    fmtBytes,
    fmtNum,

    init() {
      this.fetchVersionInfo();
      const stored = getCookie(API_KEY_COOKIE);
      if (stored) {
        this.apiKey = stored;
        this.hasApiKey = true;
        this.refreshAll();
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
      if (this.objectTypes.length === 0) {
        this._get("/object-types").then((d) => { this.objectTypes = d.object_types || []; }).catch(() => {});
      }
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

    // Note: these are global .env defaults -- any of them can be overridden per object type in
    // profiles.yaml (telegram_events_mode/telegram_alerts_mode/ai_events_stage_enabled/
    // ai_alerts_enabled), which this overview call has no visibility into (profiles.yaml isn't
    // reloaded/parsed here). The "By object type" section below shows what actually happened
    // (row counts), which reflects any per-type override already in effect.

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

    // Combines row_counts_by_object_type/db_size_by_object_type (from /admin/overview) and
    // video_storage[_alerts]_by_object_type (from /admin/disk-usage) into one row per object
    // type -- three otherwise-separate breakdowns (Postgres row counts, approximate Postgres
    // bytes, on-disk video bytes) sharing the same "object_type" key, so a reader can see
    // everything about one type (e.g. "car") in a single row instead of cross-referencing three
    // tables by hand.
    objectTypeRows() {
      if (!this.overview) return [];
      const rc = this.overview.row_counts_by_object_type || {};
      const dbSize = this.overview.db_size_by_object_type || {};
      const diskEvents = (this.diskUsage && this.diskUsage.video_storage_by_object_type) || {};
      const diskAlerts = (this.diskUsage && this.diskUsage.video_storage_alerts_by_object_type) || {};

      const types = new Set();
      const addKeys = (list) => (list || []).forEach((r) => types.add(r.object_type));
      addKeys(rc.raw_events);
      addKeys(rc.sightings);
      addKeys(rc.visit_sightings);
      Object.keys(diskEvents).forEach((t) => types.add(t));
      Object.keys(diskAlerts).forEach((t) => types.add(t));

      const lookup = (list, type) => {
        const row = (list || []).find((r) => r.object_type === type);
        return row ? (row.count !== undefined ? row.count : row.bytes) : 0;
      };

      return Array.from(types).sort().map((type) => ({
        type,
        events: lookup(rc.raw_events, type),
        sightings: lookup(rc.sightings, type),
        visitSightings: lookup(rc.visit_sightings, type),
        dbBytes: lookup(dbSize.raw_events, type) + lookup(dbSize.sightings, type) + lookup(dbSize.visit_sightings, type),
        videoBytes: (diskEvents[type] ? diskEvents[type].bytes : 0) + (diskAlerts[type] ? diskAlerts[type].bytes : 0),
      }));
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
        this.backfillResult = `Processed ${d.sightings_processed + d.visit_sightings_processed} rows ` +
          `(${d.sightings_updated} event, ${d.visit_sightings_updated} visit sighting(s) updated). Run again if counts above are still nonzero.`;
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

    // The other lever for the same 'failed' bucket -- some failures are permanent (bad media, a
    // det_id Frigate no longer has) and just re-fail on the very next requeue, piling back into
    // the same bucket. This marks anything failed for 7+ days as 'skipped' instead: terminal,
    // never retried again, without deleting the row.
    async skipFailedOlderThan(table, stage, days) {
      const key = table + stage;
      this.skippingFailed = key;
      try {
        const r = await fetch(`/admin/queue/skip-failed?table=${table}&stage=${stage}&days=${days}`, {
          method: "POST", headers: this._headers(),
        });
        const d = await r.json();
        this.skipFailedResult = { ...this.skipFailedResult, [key]: `Skipped ${d.skipped}` };
        await this.refreshAll();
      } catch (e) {
        this.skipFailedResult = { ...this.skipFailedResult, [key]: "Failed: " + e.message };
      } finally {
        this.skippingFailed = null;
      }
    },

    _purgeUrl(confirm) {
      let url = `/retention/purge?older_than_days=${this.purgeDays}&confirm=${confirm}&only_media=${this.purgeOnlyMedia}`;
      if (this.purgeObjectLabel) url += `&object_label=${encodeURIComponent(this.purgeObjectLabel)}`;
      return url;
    },

    async previewPurge() {
      this.purging = true;
      this.purgeResult = "";
      try {
        const r = await fetch(this._purgeUrl(false), { method: "POST", headers: this._headers() });
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
      const scope = this.purgeObjectLabel ? ` (object type: ${this.purgeObjectLabel})` : " (all object types)";
      const ok = this.purgeOnlyMedia
        ? confirm(
            `This will clear ${c.raw_events_video_files + c.visits_video_files} stored video files and ` +
            `${c.raw_events_images + c.visits_images_or_gifs} stored images/GIFs older than ${this.purgeDays} days${scope}. ` +
            `Rows and all AI analysis text are kept. This cannot be undone. Continue?`
          )
        : confirm(
            `This will PERMANENTLY delete ${c.raw_events} events, ${c.visits} visits, ` +
            `${c.sightings} sightings, and ${c.visit_sightings} alert sightings ` +
            `older than ${this.purgeDays} days${scope}, then rebuild the vector search index. This cannot be undone. Continue?`
          );
      if (!ok) return;
      this.purging = true;
      try {
        const r = await fetch(this._purgeUrl(true), { method: "POST", headers: this._headers() });
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

    async generateReport() {
      this.generatingReport = true;
      this.reportError = "";
      try {
        let url = `/reports/generate?source=${this.reportSource}&hours=${this.reportHours}&include_preview=${this.reportIncludePreview}`;
        if (this.reportObjectLabel) url += `&object_label=${encodeURIComponent(this.reportObjectLabel)}`;
        const r = await fetch(url, { headers: this._headers() });
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        const d = await r.json();
        // The endpoint returns JSON with the rendered HTML report as one field -- opening it
        // directly in the browser would just show that raw JSON, so write it into a fresh tab
        // instead so it renders as a real page (same content n8n would email/Telegram).
        const win = window.open("", "_blank");
        if (win) {
          win.document.write(d.html);
          win.document.close();
        } else {
          this.reportError = "Report generated, but the browser blocked the popup -- allow popups for this site and try again.";
        }
      } catch (e) {
        this.reportError = "Failed: " + e.message;
      } finally {
        this.generatingReport = false;
      }
    },
  };
}
