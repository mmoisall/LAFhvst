(function (window) {
  "use strict";

  var BASE_URL = window.location.origin;
  var TOKEN_KEY = "lafDeviceToken";
  var PAIR_PAGE = "/pair.html";

  function deviceToken() {
    try {
      return window.localStorage.getItem(TOKEN_KEY) || "";
    } catch (error) {
      return "";
    }
  }

  function setDeviceToken(token) {
    try {
      if (token) {
        window.localStorage.setItem(TOKEN_KEY, token);
      } else {
        window.localStorage.removeItem(TOKEN_KEY);
      }
    } catch (error) {
      /* 저장 실패(사생활 보호 모드 등)는 무시한다 */
    }
  }

  function redirectToPairing() {
    if (window.location.pathname === PAIR_PAGE) {
      return;
    }
    window.location.replace(PAIR_PAGE);
  }

  var E2E_DEVICE_KEY = "lafE2EDeviceId";
  var E2E_APP_PUBLIC_KEY = "lafE2EAppPublic";

  function readStorage(key) {
    try {
      return window.localStorage.getItem(key) || "";
    } catch (error) {
      return "";
    }
  }

  function writeStorage(key, value) {
    try {
      if (value === null || value === undefined || value === "") {
        window.localStorage.removeItem(key);
      } else {
        window.localStorage.setItem(key, String(value));
      }
    } catch (error) {
      /* 저장 실패는 무시 */
    }
  }

  function e2eAvailable() {
    return Boolean(window.E2E && window.E2E.supported && window.E2E.supported());
  }

  var lastE2EConfig = { deviceId: 0, token: "", appPublic: "" };

  function configureE2E() {
    if (!e2eAvailable()) {
      return false;
    }
    var token = deviceToken();
    var deviceId = parseInt(readStorage(E2E_DEVICE_KEY) || "0", 10) || 0;
    var appPublic = readStorage(E2E_APP_PUBLIC_KEY);
    if (!token || !deviceId || !appPublic) {
      window.E2E.reset();
      lastE2EConfig = { deviceId: 0, token: "", appPublic: "" };
      return false;
    }
    if (
      window.E2E.configured() &&
      lastE2EConfig.deviceId === deviceId &&
      lastE2EConfig.token === token &&
      lastE2EConfig.appPublic === appPublic
    ) {
      return true; // 이미 설정됨 — configure 는 요청 카운터를 리셋하므로 다시 호출하지 않는다
    }
    window.E2E.configure({ deviceId: deviceId, deviceToken: token, appPublicB64: appPublic });
    lastE2EConfig = { deviceId: deviceId, token: token, appPublic: appPublic };
    return window.E2E.configured();
  }

  function setE2ESession(deviceId, appPublicB64) {
    writeStorage(E2E_DEVICE_KEY, deviceId);
    writeStorage(E2E_APP_PUBLIC_KEY, appPublicB64);
    configureE2E();
  }

  function clearE2ESession() {
    writeStorage(E2E_DEVICE_KEY, "");
    writeStorage(E2E_APP_PUBLIC_KEY, "");
    lastE2EConfig = { deviceId: 0, token: "", appPublic: "" };
    if (window.E2E) {
      window.E2E.reset();
    }
  }

  function e2eRequest(path, options) {
    var opts = options || {};
    var index = path.indexOf("?");
    var pathname = index >= 0 ? path.slice(0, index) : path;
    var query = index >= 0 ? path.slice(index + 1) : "";
    var headers = {};
    var body;
    if (opts.body !== undefined && opts.body !== null) {
      headers["content-type"] = "application/json";
      body = new window.TextEncoder().encode(JSON.stringify(opts.body));
    }
    return window.E2E.request({
      method: opts.method || "GET",
      path: pathname,
      query: query,
      headers: headers,
      body: body
    }).then(function (result) {
      if (result.status === 401) {
        setDeviceToken("");
        clearE2ESession();
        redirectToPairing();
        throw new Error("기기 인증이 필요합니다. 페어링을 다시 진행하세요.");
      }
      var headerValue = "";
      var keys = Object.keys(result.headers || {});
      for (var i = 0; i < keys.length; i += 1) {
        if (keys[i].toLowerCase() === "content-type") {
          headerValue = result.headers[keys[i]];
        }
      }
      var text = new window.TextDecoder().decode(result.body || new Uint8Array());
      var data = headerValue.indexOf("application/json") >= 0 ? JSON.parse(text) : text;
      if (result.status < 200 || result.status >= 300) {
        var message = data && data.detail ? data.detail : (data && data.error) || ("request failed: " + result.status);
        throw new Error(message);
      }
      return data;
    });
  }

  function request(path, options) {
    var opts = options || {};
    if (configureE2E()) {
      return e2eRequest(path, opts);
    }
    var headers = {};
    var config = {
      method: opts.method || "GET",
      headers: headers
    };
    var token = deviceToken();
    if (token) {
      headers["Authorization"] = "Bearer " + token;
    }
    if (opts.body !== undefined && opts.body !== null) {
      headers["Content-Type"] = "application/json";
      config.body = JSON.stringify(opts.body);
    }
    return fetch(BASE_URL + path, config).then(function (response) {
      if (response.status === 401) {
        setDeviceToken("");
        clearE2ESession();
        redirectToPairing();
        throw new Error("기기 인증이 필요합니다. 페어링을 다시 진행하세요.");
      }
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

  function openEventStream(handlers) {
    var options = handlers || {};
    if (configureE2E()) {
      // E2E 활성 시에는 SSE 를 쓰지 않는다(봉투는 단일 왕복이라 스트리밍 불가).
      // 호출부는 폴링으로 폴백한다.
      return {
        close: function () {},
        encrypted: true
      };
    }
    var stopped = false;
    var controller = null;
    var timer = null;
    var retry = 0;

    function schedule() {
      if (stopped) {
        return;
      }
      var delay = Math.min(15000, 1000 * Math.pow(2, retry));
      retry += 1;
      timer = window.setTimeout(connect, delay);
    }

    function dispatchFrame(frame) {
      var event = "message";
      var data = [];
      frame.split("\n").forEach(function (line) {
        if (line.indexOf(":") === 0) {
          return; // 주석/하트비트
        }
        if (line.indexOf("event:") === 0) {
          event = line.slice(6).trim();
        } else if (line.indexOf("data:") === 0) {
          data.push(line.slice(5).trim());
        }
      });
      if (!data.length || !options.onEvent) {
        return;
      }
      var parsed = null;
      try {
        parsed = JSON.parse(data.join("\n"));
      } catch (error) {
        parsed = null;
      }
      options.onEvent(event, parsed);
    }

    function readStream(body) {
      var reader = body.getReader();
      var decoder = new window.TextDecoder();
      var buffer = "";
      function pump() {
        return reader.read().then(function (result) {
          if (result.done) {
            return null;
          }
          buffer += decoder.decode(result.value, { stream: true });
          var index = buffer.indexOf("\n\n");
          while (index >= 0) {
            dispatchFrame(buffer.slice(0, index));
            buffer = buffer.slice(index + 2);
            index = buffer.indexOf("\n\n");
          }
          return pump();
        });
      }
      return pump();
    }

    function connect() {
      if (stopped) {
        return;
      }
      controller = typeof window.AbortController !== "undefined" ? new window.AbortController() : null;
      var headers = { Accept: "text/event-stream" };
      var token = deviceToken();
      if (token) {
        headers["Authorization"] = "Bearer " + token;
      }
      fetch(BASE_URL + "/api/events", {
        headers: headers,
        signal: controller ? controller.signal : undefined
      }).then(function (response) {
        if (!response.ok || !response.body) {
          throw new Error("event stream failed: " + response.status);
        }
        retry = 0;
        if (options.onOpen) {
          options.onOpen();
        }
        return readStream(response.body);
      }).catch(function (error) {
        if (options.onError) {
          options.onError(error);
        }
      }).then(function () {
        if (options.onClose) {
          options.onClose();
        }
        schedule();
      });
    }

    connect();
    return {
      close: function () {
        stopped = true;
        if (timer) {
          window.clearTimeout(timer);
        }
        if (controller) {
          controller.abort();
        }
      }
    };
  }

  window.API = {
    baseUrl: BASE_URL,
    request: request,
    openEventStream: openEventStream,
    getDeviceToken: deviceToken,
    setDeviceToken: setDeviceToken,

    remoteStatus: function () {
      return request("/api/remote/status");
    },
    remoteEnable: function () {
      return request("/api/remote/enable", { method: "POST", body: {} });
    },
    remoteDisable: function () {
      return request("/api/remote/disable", { method: "POST", body: {} });
    },
    remoteRotate: function () {
      return request("/api/remote/rotate", { method: "POST", body: {} });
    },
    remoteRevokeDevice: function (id) {
      return request("/api/remote/devices/" + encodeId(id), { method: "DELETE" });
    },
    remoteQrUrl: function () {
      return BASE_URL + "/api/remote/qr.png?ts=" + Date.now();
    },
    pairClaim: function (token, name) {
      return request("/api/pair/claim", {
        method: "POST",
        body: { t: token, name: name || "" }
      });
    },
    notifyStatus: function () {
      return request("/api/notify/status");
    },
    notifyTest: function () {
      return request("/api/notify/test", { method: "POST", body: {} });
    },

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
    stopSource: function (id) {
      return request("/api/sources/" + encodeId(id) + "/stop", { method: "POST", body: {} });
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
    issuePixivToken: function (id) {
      return request("/api/profiles/" + encodeId(id) + "/pixiv-token", {
        method: "POST",
        body: {}
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
