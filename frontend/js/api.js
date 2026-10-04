(function (window) {
  "use strict";

  var BASE_URL = window.location.origin;

  function request(path, options) {
    var opts = options || {};
    var headers = {};
    var config = {
      method: opts.method || "GET",
      headers: headers
    };
    if (opts.body !== undefined && opts.body !== null) {
      headers["Content-Type"] = "application/json";
      config.body = JSON.stringify(opts.body);
    }
    return fetch(BASE_URL + path, config).then(function (response) {
      var contentType = response.headers.get("content-type") || "";
      var parsed = contentType.indexOf("application/json") >= 0
        ? response.json()
        : response.text();
      return parsed.then(function (data) {
        if (!response.ok) {
          var message = data && data.detail ? data.detail : (data && data.error) || response.statusText;
          throw new Error(message || ("request failed: " + response.status));
        }
        return data;
      });
    });
  }

  function encodeId(value) {
    return value === null || value === undefined ? "" : encodeURIComponent(value);
  }

  window.API = {
    baseUrl: BASE_URL,
    request: request,

    getSettings: function () {
      return request("/api/settings");
    },
    getSites: function () {
      return request("/api/sites");
    },
    getSources: function () {
      return request("/api/sources");
    },
    resolveUrl: function (url) {
      return request("/api/url/resolve?url=" + encodeURIComponent(url || ""));
    },
    buildUrl: function (site, key) {
      return request("/api/url/build?site=" + encodeURIComponent(site || "") +
        "&key=" + encodeURIComponent(key || ""));
    },
    getSourceKde: function (id) {
      return request("/api/sources/" + encodeId(id) + "/kde");
    },
    getSourceImages: function (id, limit) {
      return request("/api/sources/" + encodeId(id) + "/images?limit=" + encodeId(limit || 4));
    },
    getSchedulerStatus: function () {
      return request("/api/scheduler/status");
    },
    schedulerToggle: function (enabled) {
      return request("/api/scheduler/toggle", {
        method: "POST",
        body: enabled === undefined ? {} : { enabled: enabled }
      });
    },
    getDashboardLive: function () {
      return request("/api/dashboard/live");
    },
    getRecommendations: function (query) {
      return request("/api/recommendations" + (query ? "?" + query : ""));
    },
    getRecommendStatus: function () {
      return request("/api/recommendations/status");
    },
    refreshRecommendations: function (payload) {
      return request("/api/recommendations/refresh", { method: "POST", body: payload || {} });
    },
    quickDownload: function (payload) {
      return request("/api/quick-download", { method: "POST", body: payload || {} });
    },
    watcherToggle: function (enabled) {
      return request("/api/watcher/toggle", {
        method: "POST",
        body: { enabled: enabled }
      });
    },
    saveSettings: function (payload) {
      return request("/api/settings", { method: "POST", body: payload });
    },

    getItems: function (folderId) {
      return request("/api/explorer/items?folder_id=" + encodeId(folderId));
    },
    getTree: function () {
      return request("/api/explorer/tree");
    },
    getPath: function (folderId) {
      return request("/api/explorer/path?folder_id=" + encodeId(folderId));
    },

    getFolder: function (id) {
      return request("/api/folders/" + encodeId(id));
    },
    getFolders: function () {
      return request("/api/folders");
    },
    createFolder: function (payload) {
      return request("/api/folders", { method: "POST", body: payload });
    },
    updateFolder: function (id, payload) {
      return request("/api/folders/" + encodeId(id), { method: "PUT", body: payload });
    },
    deleteFolder: function (id) {
      return request("/api/folders/" + encodeId(id), { method: "DELETE" });
    },

    getSource: function (id) {
      return request("/api/sources/" + encodeId(id));
    },
    createSource: function (payload) {
      return request("/api/sources", { method: "POST", body: payload });
    },
    updateSource: function (id, payload) {
      return request("/api/sources/" + encodeId(id), { method: "PUT", body: payload });
    },
    deleteSource: function (id) {
      return request("/api/sources/" + encodeId(id), { method: "DELETE" });
    },
    downloadSource: function (payload) {
      return request("/api/sources/download", { method: "POST", body: payload || {} });
    },

    batchMove: function (items, targetFolderId) {
      return request("/api/explorer/batch-move", {
        method: "POST",
        body: { items: items, target_folder_id: targetFolderId }
      });
    },
    batchEdit: function (items, fields, depth) {
      return request("/api/explorer/batch-edit", {
        method: "POST",
        body: { items: items, fields: fields, depth: depth === undefined ? null : depth }
      });
    },
    batchDelete: function (items) {
      return request("/api/explorer/batch-delete", { method: "POST", body: { items: items } });
    },
    batchRun: function (items) {
      return request("/api/explorer/batch-run", { method: "POST", body: { items: items } });
    },
    reorderExplorer: function (type, parentId, orderedIds) {
      return request("/api/explorer/reorder", {
        method: "POST",
        body: { type: type, parent_id: parentId, ordered_ids: orderedIds }
      });
    },
    reorderProfiles: function (orderedIds) {
      return request("/api/profiles/reorder", {
        method: "POST",
        body: { ordered_ids: orderedIds }
      });
    },
    reorderProfileGroups: function (orderedIds) {
      return request("/api/profile-groups/reorder", {
        method: "POST",
        body: { ordered_ids: orderedIds }
      });
    },

    getProfiles: function () {
      return request("/api/profiles");
    },
    getProfile: function (id) {
      return request("/api/profiles/" + encodeId(id));
    },
    createProfile: function (payload) {
      return request("/api/profiles", { method: "POST", body: payload });
    },
    updateProfile: function (id, payload) {
      return request("/api/profiles/" + encodeId(id), { method: "PUT", body: payload });
    },
    deleteProfile: function (id) {
      return request("/api/profiles/" + encodeId(id), { method: "DELETE" });
    },
    resetProfile: function (id) {
      return request("/api/profiles/" + encodeId(id) + "/reset", { method: "POST", body: {} });
    },
    setProfileDefault: function (id, value) {
      return request("/api/profiles/" + encodeId(id) + "/default", {
        method: "POST",
        body: { value: value !== false }
      });
    },
    getProfileLearning: function (id, limit) {
      return request("/api/profiles/" + encodeId(id) + "/learning?limit=" + encodeId(limit || 30));
    },
    altStatus: function (kind, id) {
      var base = kind === "folder" ? "/api/folders/" : "/api/sources/";
      return request(base + encodeId(id) + "/alt-status");
    },
    altSync: function (kind, id, which) {
      var base = kind === "folder" ? "/api/folders/" : "/api/sources/";
      return request(base + encodeId(id) + "/alt-sync", {
        method: "POST",
        body: { kind: which || "both" }
      });
    },
    altPrune: function (id, which) {
      return request("/api/sources/" + encodeId(id) + "/alt-prune", {
        method: "POST",
        body: { kind: which || "both" }
      });
    },
    metadataStatus: function (kind, id) {
      var base = kind === "folder" ? "/api/folders/" : "/api/sources/";
      return request(base + encodeId(id) + "/metadata-status");
    },
    metadataMigrate: function (kind, id, payload) {
      var base = kind === "folder" ? "/api/folders/" : "/api/sources/";
      return request(base + encodeId(id) + "/metadata-migrate", {
        method: "POST",
        body: payload || {}
      });
    },
    metadataMigrateAll: function (payload) {
      return request("/api/metadata/migrate", {
        method: "POST",
        body: payload || {}
      });
    },
    getSourceLogs: function (id, status) {
      return request("/api/sources/" + encodeId(id) + "/logs" +
        (status ? "?status=" + encodeId(status) : ""));
    },
    relearnSource: function (id) {
      return request("/api/sources/" + encodeId(id) + "/relearn", { method: "POST", body: {} });
    },
    relearnFolder: function (id) {
      return request("/api/folders/" + encodeId(id) + "/relearn", { method: "POST", body: {} });
    },
    revealSource: function (id) {
      return request("/api/sources/" + encodeId(id) + "/reveal", { method: "POST", body: {} });
    },
    revealFolder: function (id) {
      return request("/api/folders/" + encodeId(id) + "/reveal", { method: "POST", body: {} });
    },
    launchProfile: function (id, url) {
      return request("/api/profiles/" + encodeId(id) + "/launch", {
        method: "POST",
        body: { url: url || null }
      });
    },
    getLaunchingProfiles: function () {
      return request("/api/profiles/launching");
    },

    getProfileGroups: function () {
      return request("/api/profile-groups");
    },
    createProfileGroup: function (payload) {
      return request("/api/profile-groups", { method: "POST", body: payload });
    },
    updateProfileGroup: function (id, payload) {
      return request("/api/profile-groups/" + encodeId(id), { method: "PUT", body: payload });
    },
    deleteProfileGroup: function (id) {
      return request("/api/profile-groups/" + encodeId(id), { method: "DELETE" });
    },
    resetProfileGroup: function (id) {
      return request("/api/profile-groups/" + encodeId(id) + "/reset", {
        method: "POST",
        body: {}
      });
    },

    getReactiveRules: function () {
      return request("/api/reactive-rules");
    },
    createReactiveRule: function (payload) {
      return request("/api/reactive-rules", { method: "POST", body: payload });
    },
    updateReactiveRule: function (id, payload) {
      return request("/api/reactive-rules/" + encodeId(id), { method: "PUT", body: payload });
    },
    deleteReactiveRule: function (id) {
      return request("/api/reactive-rules/" + encodeId(id), { method: "DELETE" });
    },

    getWatcherStatus: function () {
      return request("/api/watcher/status");
    },
    getWatcherEvents: function (since) {
      return request("/api/watcher/events?since=" + encodeId(since || 0));
    },

    getLogs: function (params) {
      var p = params || {};
      var qs = [];
      ["level", "category", "status", "scope", "source_id", "folder_id", "profile_id",
        "q", "limit", "offset"].forEach(function (key) {
        if (p[key] !== undefined && p[key] !== null && p[key] !== "") {
          qs.push(encodeURIComponent(key) + "=" + encodeURIComponent(p[key]));
        }
      });
      return request("/api/logs" + (qs.length ? "?" + qs.join("&") : ""));
    },
    getLogStats: function () {
      return request("/api/logs/stats");
    },
    resolveLog: function (id, resolution) {
      return request("/api/logs/" + encodeId(id) + "/resolve", {
        method: "POST",
        body: { resolution: resolution || null }
      });
    },
    ignoreLog: function (id, note) {
      return request("/api/logs/" + encodeId(id) + "/ignore", {
        method: "POST",
        body: { resolution: note || null }
      });
    },
    retryLog: function (id) {
      return request("/api/logs/" + encodeId(id) + "/retry", { method: "POST", body: {} });
    },
    deleteLog: function (id) {
      return request("/api/logs/" + encodeId(id), { method: "DELETE" });
    },
    clearLogs: function (payload) {
      return request("/api/logs/clear", { method: "POST", body: payload || {} });
    },
    toggleWatcher: function (payload) {
      return request("/api/watcher/toggle", { method: "POST", body: payload || {} });
    },

    getVersion: function () {
      return request("/api/version");
    },
    checkUpdate: function () {
      return request("/api/update/check", { method: "POST", body: {} });
    },
    getUpdateStatus: function () {
      return request("/api/update/status");
    },
    applyUpdate: function () {
      return request("/api/update/apply", { method: "POST", body: {} });
    }
  };
})(window);
