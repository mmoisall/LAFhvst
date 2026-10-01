(function (window, document) {
  "use strict";

  var current = { mode: null, target: null, targets: null };
  var sortable = null;
  var currentProfileMode = "group";

  function byId(id) {
    return document.getElementById(id);
  }

  function escapeHtml(value) {
    return String(value === null || value === undefined ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function fmtTime(value) {
    if (!value) {
      return "-";
    }
    var date = new Date(value);
    if (isNaN(date.getTime())) {
      return "-";
    }
    return date.toLocaleString();
  }

  function splitTags(value) {
    return (value || "")
      .split(",")
      .map(function (token) { return token.trim(); })
      .filter(function (token) { return token.length > 0; });
  }

  function joinTags(list) {
    return (list || []).join(", ");
  }

  function showModal(id) {
    var modal = byId(id);
    if (modal) {
      modal.classList.remove("hidden");
    }
  }

  function hideModal(id) {
    var modal = byId(id);
    if (modal) {
      modal.classList.add("hidden");
    }
  }

  function currentFolderId() {
    return window.DBExplorer && window.DBExplorer.getCurrentFolderId
      ? window.DBExplorer.getCurrentFolderId()
      : null;
  }

  function notifySaved() {
    if (window.DBExplorer && window.DBExplorer.refresh) {
      window.DBExplorer.refresh();
    }
  }

  function populateParents(selectedId, excludeId) {
    return window.API.getFolders().then(function (folders) {
      var select = byId("entityParent");
      select.innerHTML = "";
      var rootOption = document.createElement("option");
      rootOption.value = "";
      rootOption.textContent = "루트 (Root)";
      select.appendChild(rootOption);
      folders.forEach(function (folder) {
        if (excludeId !== null && folder.id === excludeId) {
          return;
        }
        var option = document.createElement("option");
        option.value = String(folder.id);
        option.textContent = folder.name;
        select.appendChild(option);
      });
      select.value = selectedId === null || selectedId === undefined ? "" : String(selectedId);
    });
  }

  function populateProfileGroups(selectedId) {
    var select = byId("entityProfileGroup");
    if (!select) {
      return Promise.resolve();
    }
    return window.API.getProfileGroups().then(function (groups) {
      select.innerHTML = "";
      var noneOption = document.createElement("option");
      noneOption.value = "";
      noneOption.textContent = "상속 사용 (자동)";
      select.appendChild(noneOption);
      (groups || []).forEach(function (group) {
        var option = document.createElement("option");
        option.value = String(group.id);
        option.textContent = group.name + (group.site ? " (" + group.site + ")" : "");
        select.appendChild(option);
      });
      select.value = selectedId === null || selectedId === undefined ? "" : String(selectedId);
    }).catch(function () {});
  }

  function populateProfiles(selectedId) {
    var select = byId("entityProfile");
    if (!select) {
      return Promise.resolve();
    }
    return window.API.getProfiles().then(function (profiles) {
      select.innerHTML = "";
      var noneOption = document.createElement("option");
      noneOption.value = "";
      noneOption.textContent = "상속 사용 (자동)";
      select.appendChild(noneOption);
      (profiles || []).forEach(function (profile) {
        var option = document.createElement("option");
        option.value = String(profile.id);
        var label = profile.name || "(이름 없음)";
        if (profile.site) {
          label += " (" + profile.site + ")";
        }
        if (profile.is_site_default) {
          label = "⭐ " + label;
        }
        option.textContent = label;
        select.appendChild(option);
      });
      select.value = selectedId === null || selectedId === undefined ? "" : String(selectedId);
    }).catch(function () {});
  }

  function clearInheritedMarks() {
    Array.prototype.forEach.call(
      document.querySelectorAll(
        "#entityModal .is-inherited, #entityModal .is-overridden, #entityModal .inherited-badge, #entityModal .overridden-badge"
      ),
      function (el) {
        el.classList.remove("is-inherited", "is-overridden");
        if (el.classList.contains("inherited-badge") || el.classList.contains("overridden-badge")) {
          el.remove();
        }
      }
    );
  }

  function markFieldState(el, state, title) {
    if (!el) {
      return;
    }
    el.classList.remove("is-inherited", "is-overridden");
    var existing = el.parentElement
      ? el.parentElement.querySelector(".inherited-badge, .overridden-badge")
      : null;
    if (existing) {
      existing.remove();
    }
    if (!state) {
      return;
    }
    if (state === "inherited") {
      el.classList.add("is-inherited");
      el.title = title || "부모에게서 상속된 값";
      var badge = document.createElement("span");
      badge.className = "inherited-badge";
      badge.textContent = "상속";
      badge.title = el.title;
      if (el.parentElement) {
        el.parentElement.appendChild(badge);
      }
    } else if (state === "overridden") {
      el.classList.add("is-overridden");
      el.title = title || "직접 설정한 값";
      var obadge = document.createElement("span");
      obadge.className = "overridden-badge";
      obadge.textContent = "자체";
      obadge.title = el.title;
      if (el.parentElement) {
        el.parentElement.appendChild(obadge);
      }
    }
  }

  function fieldState(data, field, title) {
    if (!data || !data.id) {
      return null;
    }
    if (data.inherited && data.inherited[field]) {
      return "inherited";
    }
    if (data.overridden && data.overridden[field]) {
      return "overridden";
    }
    return null;
  }

  function setProfileMode(mode) {
    currentProfileMode = mode === "profile" ? "profile" : "group";
    var label = byId("entityProfileModeLabel");
    if (label) {
      label.textContent = currentProfileMode === "profile"
        ? "단일 프로파일 (Profile)"
        : "합동 프로파일 (Profile Group)";
    }
    var groupSelect = byId("entityProfileGroup");
    var profileSelect = byId("entityProfile");
    if (groupSelect) {
      groupSelect.classList.toggle("hidden", currentProfileMode !== "group");
    }
    if (profileSelect) {
      profileSelect.classList.toggle("hidden", currentProfileMode !== "profile");
    }
  }

  function toggleProfileMode() {
    setProfileMode(currentProfileMode === "group" ? "profile" : "group");
  }

  function applyProfileRef(payload) {
    var groupValue = byId("entityProfileGroup").value;
    var profileValue = byId("entityProfile").value;
    if (currentProfileMode === "profile") {
      payload.profile_id = profileValue === "" ? null : parseInt(profileValue, 10);
      payload.profile_group_id = null;
    } else {
      payload.profile_group_id = groupValue === "" ? null : parseInt(groupValue, 10);
      payload.profile_id = null;
    }
  }

  function updateSavePathInherited(item) {
    var el = byId("entitySavePath");
    if (!el) {
      return;
    }
    var inherited = Boolean(item && item.id && item.inherited && item.inherited.save_path);
    var overridden = Boolean(item && item.id && item.overridden && item.overridden.save_path && !inherited);
    el.classList.toggle("is-inherited", inherited);
    el.classList.toggle("is-overridden", overridden);
    if (inherited) {
      el.title = "부모(루트) 폴더에게서 상속된 저장 경로";
    } else if (overridden) {
      el.title = "직접 지정한 저장 경로";
    } else {
      el.removeAttribute("title");
    }
  }

  function itemDefaults() {
    var s = window.AppSettings || {};
    return {
      cyc_option: s.itemCycOption != null ? s.itemCycOption : 0,
      cyc: s.itemCyc != null ? s.itemCyc : 1,
      log_level: s.itemLogLevel || "INFO"
    };
  }

  function setCommonFields(data) {
    var defaults = itemDefaults();
    var isNew = !(data && data.id);
    byId("entityName").value = data && data.name ? data.name : "";
    byId("entityCycOption").value = (data && data.cyc_option != null)
      ? String(data.cyc_option) : String(defaults.cyc_option);
    byId("entityCyc").value = (data && data.cyc != null) ? data.cyc : defaults.cyc;
    byId("entityLogLevel").value = (data && data.log_level) ? data.log_level : defaults.log_level;
    byId("entityTags").value = data ? joinTags(data.tag_list) : "";
    byId("entityMemo").value = data && data.memo ? data.memo : "";
    if (isNew) {
      var cfg = (window.AppSettings && window.AppSettings.itemConfig) || {};
      var mode = String(cfg.date_mode || "incremental").toLowerCase();
      setDateMode(["incremental", "full", "fixed"].indexOf(mode) >= 0 ? mode : "incremental");
    }
    applyInheritedMarks(data);
  }

  function toLocalDateTimeInput(value) {
    if (!value) {
      return "";
    }
    var text = String(value).replace(" ", "T");
    var parsed = new Date(text);
    if (isNaN(parsed.getTime())) {
      return "";
    }
    var pad = function (n) { return (n < 10 ? "0" : "") + n; };
    return parsed.getFullYear() + "-" + pad(parsed.getMonth() + 1) + "-" + pad(parsed.getDate()) +
      "T" + pad(parsed.getHours()) + ":" + pad(parsed.getMinutes()) + ":" + pad(parsed.getSeconds());
  }

  function setDateMode(mode) {
    var select = byId("entityDateMode");
    var fixed = byId("entityDateFixed");
    if (select) {
      select.value = mode || "incremental";
    }
    if (fixed) {
      fixed.classList.toggle("hidden", (mode || "incremental") !== "fixed");
    }
  }

  function loadDateScope(data) {
    if (!data || !data.id) {
      return;
    }
    var config = (data && data.config) || {};
    var mode = String(config.date_mode || "incremental").toLowerCase();
    if (["incremental", "full", "fixed"].indexOf(mode) < 0) {
      mode = "incremental";
    }
    setDateMode(mode);
    byId("entityDateFixed").value = toLocalDateTimeInput(config.date_fixed);
  }

  function applyDateScopeConfig(config) {
    var select = byId("entityDateMode");
    var mode = select ? select.value : "incremental";
    delete config.date_mode;
    delete config.date_fixed;
    if (mode === "full") {
      config.date_mode = "full";
    } else if (mode === "fixed") {
      var raw = byId("entityDateFixed").value;
      if (raw) {
        config.date_mode = "fixed";
        config.date_fixed = raw.replace("T", " ") + (raw.length <= 16 ? ":00" : "");
      }
    }
    var retry = byId("entityRetryDelay");
    if (retry && String(retry.value).trim() !== "") {
      config.retry_delay = Math.max(0, parseFloat(retry.value) || 0);
    }
    applyGdlConfig(config);
    return config;
  }

  function parseNumField(id) {
    var el = byId(id);
    if (!el || String(el.value).trim() === "") {
      return null;
    }
    var value = parseFloat(el.value);
    return isNaN(value) ? null : value;
  }

  function loadGdlFields(data) {
    var config = (data && data.config) || {};
    var basis = byId("entityDateBasis");
    if (basis) {
      basis.value = config.date_basis || (window.AppSettings && window.AppSettings.itemDateBasis) || "filter";
    }
    var sleepReq = config.sleep_request;
    var retriesEl = byId("entityRetries");
    var timeoutEl = byId("entityTimeout");
    var minEl = byId("entitySleepReqMin");
    var maxEl = byId("entitySleepReqMax");
    var argsEl = byId("entityArgs");
    if (retriesEl) { retriesEl.value = config.retries != null ? config.retries : ""; }
    if (timeoutEl) { timeoutEl.value = config.timeout != null ? config.timeout : ""; }
    if (minEl) { minEl.value = Array.isArray(sleepReq) && sleepReq.length ? sleepReq[0] : ""; }
    if (maxEl) { maxEl.value = Array.isArray(sleepReq) && sleepReq.length > 1 ? sleepReq[1] : ""; }
    if (argsEl) { argsEl.value = Array.isArray(config.args) ? config.args.join("\n") : ""; }
  }

  function applyGdlConfig(config) {
    var basis = byId("entityDateBasis");
    if (basis) {
      config.date_basis = basis.value;
    }
    var retries = parseNumField("entityRetries");
    if (retries != null) { config.retries = Math.max(0, Math.round(retries)); } else { delete config.retries; }
    var timeout = parseNumField("entityTimeout");
    if (timeout != null) { config.timeout = Math.max(0, timeout); } else { delete config.timeout; }
    var min = parseNumField("entitySleepReqMin");
    var max = parseNumField("entitySleepReqMax");
    if (min != null || max != null) {
      var lo = min != null ? min : max;
      var hi = max != null ? max : min;
      config.sleep_request = [lo, hi];
    } else {
      delete config.sleep_request;
    }
    var argsEl = byId("entityArgs");
    if (argsEl) {
      var args = argsEl.value.split(/[\n,]/).map(function (t) { return t.trim(); })
        .filter(function (t) { return t.length > 0; });
      if (args.length) { config.args = args; } else { delete config.args; }
    }
    return config;
  }

  function altRowHtml(kind, value) {
    return '<div class="alt-item" data-kind="' + kind + '">' +
      '<input type="text" class="alt-input" value="' + escapeHtml(value || "") +
      '" placeholder="비우면 기본(원본경로_' + kind + ')">' +
      '<button class="btn btn-sm danger" type="button" data-alt-remove>✕</button></div>';
  }

  function addAltRow(kind, value) {
    var list = byId(kind === "alt" ? "entityAltList" : "entityCmbList");
    if (!list) {
      return;
    }
    var wrap = document.createElement("div");
    wrap.innerHTML = altRowHtml(kind, value || "");
    list.appendChild(wrap.firstChild);
  }

  function renderAltList(kind, values) {
    var list = byId(kind === "alt" ? "entityAltList" : "entityCmbList");
    if (!list) {
      return;
    }
    list.innerHTML = "";
    (values || []).forEach(function (v) {
      addAltRow(kind, v);
    });
  }

  function loadAltPaths(data) {
    var config = (data && data.config) || {};
    var isNew = !(data && data.id);
    renderAltList("alt", Array.isArray(config.alt_paths) ? config.alt_paths : []);
    renderAltList("cmb", Array.isArray(config.cmb_paths) ? config.cmb_paths : []);
    byId("entityAltMode").value = config.alt_mode || "hardlink";
    byId("entityAltSchedule").value = config.alt_schedule || "manual";
    byId("entityAltInterval").value = config.alt_interval_hours != null ? config.alt_interval_hours : "";
    updateAltStatusText(data);
    updateAltIntervalVisibility();
  }

  function updateAltIntervalVisibility() {
    var schedule = byId("entityAltSchedule").value;
    byId("entityAltInterval").parentElement.classList.toggle("hidden", schedule !== "n_hours");
  }

  function updateAltStatusText(data) {
    var el = byId("entityAltStatus");
    if (!el) {
      return;
    }
    if (!data || !data.id) {
      el.textContent = "저장 후 동기화할 수 있습니다. 경로를 비우면 기본(_alt/_cmb)이 사용됩니다.";
      return;
    }
    var config = data.config || {};
    el.textContent = "마지막 동기화: " + (config.alt_last_sync_at ? fmtTime(config.alt_last_sync_at) : "없음");
  }

  function collectAltPaths(kind) {
    var list = byId(kind === "alt" ? "entityAltList" : "entityCmbList");
    if (!list) {
      return [];
    }
    var out = [];
    Array.prototype.forEach.call(list.querySelectorAll(".alt-input"), function (input) {
      var value = (input.value || "").trim();
      if (value) {
        out.push(value);
      }
    });
    return out;
  }

  function applyAltConfig(config) {
    config.alt_paths = collectAltPaths("alt");
    config.cmb_paths = collectAltPaths("cmb");
    config.alt_mode = byId("entityAltMode").value;
    config.alt_schedule = byId("entityAltSchedule").value;
    var interval = byId("entityAltInterval").value;
    if (byId("entityAltSchedule").value === "n_hours" && String(interval).trim() !== "") {
      config.alt_interval_hours = parseFloat(interval) || 0;
    } else {
      delete config.alt_interval_hours;
    }
    return config;
  }

  function syncAltNow(kind) {
    var target = current.target;
    if (!target || !target.id) {
      if (window.Toast) {
        window.Toast.show("먼저 저장하세요.", "info");
      }
      return;
    }
    var itemKind = current.mode === "folder" ? "folder" : "source";
    var config = applyAltConfig(Object.assign({}, target.config || {}));
    var save = current.mode === "folder"
      ? window.API.updateFolder(target.id, { config: config })
      : window.API.updateSource(target.id, { config: config });
    save.then(function () {
      return window.API.altSync(itemKind, target.id, "both");
    }).then(function () {
      if (window.Toast) {
        window.Toast.show("대체경로 동기화 완료", "success");
      }
      return window.API[itemKind === "folder" ? "getFolder" : "getSource"](target.id);
    }).then(function (fresh) {
      updateAltStatusText(fresh);
    }).catch(function (error) {
      if (window.Toast) {
        window.Toast.show(error.message || "대체경로 동기화 실패", "error");
      }
    });
  }

  function loadRetryDelay(data) {
    var config = (data && data.config) || {};
    var value = config.retry_delay;
    if (value === undefined || value === null) {
      value = (window.AppSettings && window.AppSettings.itemRetryDelay != null)
        ? window.AppSettings.itemRetryDelay : 10;
    }
    byId("entityRetryDelay").value = value;
  }

  function renderEntityLogs(result) {
    var box = byId("entityLogList");
    if (!box) {
      return;
    }
    var items = (result && result.items) || [];
    if (!items.length) {
      box.innerHTML = '<p class="muted">오류 로그가 없습니다.</p>';
      return;
    }
    box.innerHTML = items.map(function (log) {
      var statusLabel = log.status === "open" ? "미해결" : (log.status === "resolved" ? "해결" : "무시");
      var actions = "";
      if (log.status === "open") {
        actions = '<button class="btn btn-sm primary" type="button" data-log-action="resolve" data-log-id="' + log.id + '">해결</button>' +
          '<button class="btn btn-sm" type="button" data-log-action="ignore" data-log-id="' + log.id + '">무시</button>';
      }
      actions += '<button class="btn btn-sm" type="button" data-log-action="guide" data-log-id="' + log.id + '">💡</button>';
      return '<div class="entity-log-row log-' + (log.level || "ERROR").toLowerCase() + '">' +
        '<div class="entity-log-head"><span class="log-level">' + escapeHtml(log.level) + "</span>" +
        '<span class="log-status status-' + escapeHtml(log.status) + '">' + statusLabel + "</span>" +
        '<span class="log-time">' + escapeHtml(fmtTime(log.last_at || log.created_at)) + "</span></div>" +
        '<div class="entity-log-msg">' + escapeHtml(log.message) + "</div>" +
        '<div class="entity-log-actions">' + actions + "</div></div>";
    }).join("");
  }

  function loadEntityLogs(sourceId, status) {
    if (!sourceId) {
      renderEntityLogs({ items: [] });
      return;
    }
    var box = byId("entityLogList");
    if (box) {
      box.innerHTML = '<p class="muted">불러오는 중...</p>';
    }
    window.API.getSourceLogs(sourceId, status).then(function (result) {
      renderEntityLogs(result);
    }).catch(function () {
      if (box) {
        box.innerHTML = '<p class="muted">로그를 불러오지 못했습니다.</p>';
      }
    });
  }

  function handleEntityLogAction(event) {
    var button = event.target.closest("button[data-log-action]");
    if (!button) {
      return;
    }
    var id = parseInt(button.dataset.logId, 10);
    var action = button.dataset.logAction;
    var target = current.target;
    if (action === "resolve") {
      var note = window.prompt("해결 메모 (선택)", "");
      if (note === null) {
        return;
      }
      window.API.resolveLog(id, note).then(function () {
        if (target) { loadEntityLogs(target.id, "open"); }
      }).catch(function () {});
    } else if (action === "ignore") {
      window.API.ignoreLog(id).then(function () {
        if (target) { loadEntityLogs(target.id, "open"); }
      }).catch(function () {});
    } else if (action === "guide") {
      window.API.request("/api/logs/" + id).then(function (full) {
        window.alert("해결 가이드\n\n" + ((full && full.guide) || "추천 조치가 없습니다."));
      }).catch(function () {});
    }
  }

  function applyInheritedMarks(data) {
    clearInheritedMarks();
    if (!data || !data.id) {
      return;
    }
    markFieldState(byId("entityCycOption"), fieldState(data, "cyc"), "주기 설정");
    markFieldState(byId("entityCyc"), fieldState(data, "cyc"), "주기 설정");
    markFieldState(byId("entityLogLevel"), fieldState(data, "log_level"), "로그 레벨 설정");
    markFieldState(byId("entityProfileModeLabel"), fieldState(data, "profile"), "프로파일 설정");
    markFieldState(byId("entityTags"), null);
  }

  function setModalTab(name) {
    var tabs = byId("entityTabs");
    if (!tabs) {
      return;
    }
    Array.prototype.forEach.call(tabs.querySelectorAll(".modal-tab"), function (button) {
      button.classList.toggle("active", button.dataset.modalTab === name);
    });
    Array.prototype.forEach.call(document.querySelectorAll("#entityModal .modal-pane"), function (pane) {
      pane.classList.toggle("active", pane.dataset.pane === name);
    });
  }

  function setAvailableTabs(list) {
    var tabs = byId("entityTabs");
    if (!tabs) {
      return;
    }
    var allowed = list || ["basic", "collect", "profile", "alt", "log"];
    tabs.classList.remove("hidden");
    var activeVisible = false;
    Array.prototype.forEach.call(tabs.querySelectorAll(".modal-tab"), function (button) {
      var show = allowed.indexOf(button.dataset.modalTab) >= 0;
      button.classList.toggle("hidden", !show);
      if (show && button.classList.contains("active")) {
        activeVisible = true;
      }
    });
    if (!activeVisible) {
      setModalTab(allowed[0] || "basic");
    }
  }

  function openFolder(folder) {
    var isEdit = Boolean(folder && folder.id);
    current = { mode: "folder", target: folder || null, targets: null };
    byId("entityModalTitle").textContent = isEdit ? "폴더 편집" : "새 폴더";
    byId("entityUrlGroup").classList.add("hidden");
    byId("entitySiteKeyGroup").classList.add("hidden");
    byId("entityParent").parentElement.classList.remove("hidden");
    byId("entityProfileWrap").classList.remove("hidden");
    byId("entityDateScopeWrap").classList.remove("hidden");
    byId("entityRetryWrap").classList.add("hidden");
    byId("entityLogWrap").classList.add("hidden");
    byId("entityAltWrap").classList.remove("hidden");
    setAvailableTabs(["basic", "collect", "profile", "alt"]);
    setModalTab("basic");
    setCommonFields(folder);
    loadDateScope(folder);
    loadGdlFields(folder);
    loadAltPaths(folder);
    updateCollectAllButton(folder);
    byId("entitySavePath").textContent = folder && folder.save_path
      ? "저장 경로: " + folder.save_path
      : "";
    updateSavePathInherited(folder);
    populateParents(
      isEdit ? folder.parent_id : currentFolderId(),
      isEdit ? folder.id : null
    );
    setProfileMode(isEdit && folder.profile_id ? "profile" : "group");
    populateProfileGroups(isEdit ? folder.profile_group_id : null);
    populateProfiles(isEdit ? folder.profile_id : null);
    if (window.UrlSync) {
      window.UrlSync.reset();
    }
    showModal("entityModal");
  }

  function openSource(source) {
    var isEdit = Boolean(source && source.id);
    current = { mode: "source", target: source || null, targets: null };
    byId("entityModalTitle").textContent = isEdit ? "소스 편집" : "새 소스";
    byId("entityUrlGroup").classList.remove("hidden");
    byId("entitySiteKeyGroup").classList.remove("hidden");
    byId("entityParent").parentElement.classList.remove("hidden");
    byId("entityProfileWrap").classList.remove("hidden");
    byId("entityDateScopeWrap").classList.remove("hidden");
    byId("entityRetryWrap").classList.remove("hidden");
    byId("entityAltWrap").classList.remove("hidden");
    byId("entityLogWrap").classList.remove("hidden");
    setAvailableTabs(["basic", "collect", "profile", "alt", "log"]);
    setModalTab("basic");
    setCommonFields(source);
    loadDateScope(source);
    loadRetryDelay(source);
    loadGdlFields(source);
    loadAltPaths(source);
    updateCollectAllButton(source);
    loadEntityLogs(isEdit ? source.id : null, "open");
    byId("entitySavePath").classList.remove("is-inherited");
    updateSavePathInherited(source);
    byId("entityUrl").value = source && source.url ? source.url : "";
    byId("entitySite").value = source && source.site ? source.site : "";
    byId("entityKey").value = source && source.key ? source.key : "";
    byId("entitySavePath").textContent = source && source.save_path
      ? "저장 경로: " + source.save_path
      : "";
    populateParents(
      isEdit ? source.folder_id : currentFolderId(),
      null
    );
    setProfileMode(isEdit && source.profile_id ? "profile" : "group");
    populateProfileGroups(isEdit ? source.profile_group_id : null);
    populateProfiles(isEdit ? source.profile_id : null);
    if (window.SiteSuggestions) {
      window.SiteSuggestions.ensure();
    }
    if (window.UrlSync) {
      window.UrlSync.refresh();
    }
    showModal("entityModal");
  }

  function openBatchEdit(targets) {
    current = { mode: "batch", target: null, targets: targets };
    byId("entityModalTitle").textContent = "일괄 수정 (입력한 값만 적용)";
    byId("entityUrlGroup").classList.add("hidden");
    byId("entitySiteKeyGroup").classList.add("hidden");
    byId("entityParent").parentElement.classList.add("hidden");
    byId("entityProfileWrap").classList.add("hidden");
    byId("entityDateScopeWrap").classList.add("hidden");
    byId("entityRetryWrap").classList.add("hidden");
    byId("entityAltWrap").classList.add("hidden");
    byId("entityLogWrap").classList.add("hidden");
    byId("entityCollectAllBtn").hidden = true;
    byId("entityName").parentElement.classList.add("hidden");
    byId("entityTabs").classList.add("hidden");
    setModalTab("basic");
    setCommonFields(null);
    byId("entityCyc").value = "";
    byId("entitySavePath").textContent = "선택 " + targets.length + "개 항목에 적용됩니다.";
    showModal("entityModal");
  }

  function closeEntity() {
    hideModal("entityModal");
    byId("entityName").parentElement.classList.remove("hidden");
  }

  function updateCollectAllButton(data) {
    var button = byId("entityCollectAllBtn");
    if (!button) {
      return;
    }
    var isSource = current.mode === "source" && data && data.id;
    button.hidden = !isSource;
  }

  function collectAllNow() {
    var target = current.target;
    if (!target || !target.id) {
      return;
    }
    window.API.downloadSource({ source_id: target.id, full: true }).then(function (result) {
      if (window.Toast) {
        window.Toast.show("[" + (target.name || target.url) + "] 전체 수집 시작", "info");
      }
      hideModal("entityModal");
      if (window.DBExplorer && window.DBExplorer.refresh) {
        window.DBExplorer.refresh();
      }
    }).catch(function (error) {
      if (window.Toast) {
        window.Toast.show(error.message || "전체 수집 실패", "error");
      }
    });
  }

  function resetEntityField(field) {
    if (!field) {
      return;
    }
    var tag = (field.tagName || "").toLowerCase();
    if (tag === "select") {
      var hasEmpty = Array.prototype.some.call(field.options || [], function (option) {
        return option.value === "";
      });
      if (hasEmpty) {
        field.value = "";
      } else if (field.options && field.options.length) {
        field.selectedIndex = 0;
      }
    } else if (tag === "textarea") {
      field.value = "";
    } else if (tag === "input") {
      var type = (field.getAttribute("type") || "text").toLowerCase();
      if (type === "checkbox" || type === "radio") {
        field.checked = false;
      } else {
        field.value = "";
      }
    }
    field.dispatchEvent(new Event("input", { bubbles: true }));
    field.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function bindEntityFieldReset(modal) {
    modal.addEventListener("contextmenu", function (event) {
      var field = event.target.closest("input, select, textarea");
      if (!field || !modal.contains(field)) {
        return;
      }
      event.preventDefault();
      resetEntityField(field);
    });
  }

  function confirmMove(oldParentValue, newParentValue, label) {
    if (!current.target || !current.target.id) {
      return true;
    }
    if (String(oldParentValue) === String(newParentValue)) {
      return true;
    }
    return window.confirm("부모가 변경되었습니다.\n실제 저장 폴더도 함께 이동할까요?\n\n" + label);
  }

  function saveFolder() {
    var config = Object.assign({}, current.target ? current.target.config : {});
    applyDateScopeConfig(config);
    applyAltConfig(config);
    var payload = {
      name: byId("entityName").value.trim(),
      config: config,
      cyc_option: parseInt(byId("entityCycOption").value, 10) || 0,
      cyc: parseInt(byId("entityCyc").value, 10) || 1,
      log_level: byId("entityLogLevel").value,
      tag_list: splitTags(byId("entityTags").value),
      memo: byId("entityMemo").value
    };
    if (!payload.name) {
      payload.name = "New Folder";
    }
    var parentValue = byId("entityParent").value;
    payload.parent_id = parentValue === "" ? null : parseInt(parentValue, 10);
    if (current.target && current.target.id) {
      payload.move_files = confirmMove(
        current.target.parent_id === null ? "" : current.target.parent_id,
        parentValue,
        "[" + (current.target.name || "") + "]"
      );
    }
    applyProfileRef(payload);
    if (current.target && current.target.id) {
      return window.API.updateFolder(current.target.id, payload);
    }
    return window.API.createFolder(payload);
  }

  function saveSource() {
    var config = Object.assign({}, current.target ? current.target.config : {});
    applyDateScopeConfig(config);
    applyAltConfig(config);
    var payload = {
      name: byId("entityName").value.trim(),
      url: byId("entityUrl").value.trim(),
      site: byId("entitySite").value.trim(),
      key: byId("entityKey").value.trim(),
      config: config,
      cyc_option: parseInt(byId("entityCycOption").value, 10) || 0,
      cyc: parseInt(byId("entityCyc").value, 10) || 1,
      log_level: byId("entityLogLevel").value,
      tag_list: splitTags(byId("entityTags").value),
      memo: byId("entityMemo").value
    };
    if (!payload.name) {
      payload.name = payload.url;
    }
    var parentValue = byId("entityParent").value;
    payload.folder_id = parentValue === "" ? null : parseInt(parentValue, 10);
    if (current.target && current.target.id) {
      payload.move_files = confirmMove(
        current.target.folder_id === null ? "" : current.target.folder_id,
        parentValue,
        "[" + (current.target.name || current.target.url || "") + "]"
      );
    }
    applyProfileRef(payload);
    if (current.target && current.target.id) {
      return window.API.updateSource(current.target.id, payload);
    }
    return window.API.createSource(payload);
  }

  function saveBatch() {
    var fields = {};
    var tags = splitTags(byId("entityTags").value);
    if (tags.length) {
      fields.tag_list = tags;
    }
    var memo = byId("entityMemo").value;
    if (memo.trim()) {
      fields.memo = memo;
    }
    var cyc = byId("entityCyc").value;
    if (String(cyc).trim() !== "") {
      fields.cyc = parseInt(cyc, 10);
    }
    if (byId("entityLogLevel").value) {
      fields.log_level = byId("entityLogLevel").value;
    }
    if (!Object.keys(fields).length) {
      return Promise.resolve(null);
    }
    return window.API.batchEdit(current.targets, fields);
  }

  function saveEntity() {
    var action;
    if (current.mode === "batch") {
      action = saveBatch();
    } else if (current.mode === "folder") {
      action = saveFolder();
    } else {
      action = saveSource();
    }
    return action.then(function () {
      closeEntity();
      notifySaved();
      if (window.SiteSuggestions) {
        window.SiteSuggestions.invalidate();
      }
    }).catch(function (error) {
      window.alert(error.message || "저장에 실패했습니다.");
    });
  }

  function resetColumnModal() {
    var modal = byId("colSelectModal");
    if (!modal) {
      return;
    }
    modal.classList.remove("anchored");
    var content = modal.querySelector(".modal-content");
    if (content) {
      content.style.left = "";
      content.style.top = "";
    }
  }

  function positionColumnModal(anchor) {
    var modal = byId("colSelectModal");
    if (!modal) {
      return;
    }
    var content = modal.querySelector(".modal-content");
    if (!content || !anchor || typeof anchor.x !== "number" || typeof anchor.y !== "number") {
      modal.classList.remove("anchored");
      content.style.left = "";
      content.style.top = "";
      return;
    }
    modal.classList.add("anchored");
    content.style.left = "0px";
    content.style.top = "0px";
    var width = content.offsetWidth;
    var height = content.offsetHeight;
    var maxX = Math.max(8, window.innerWidth - width - 8);
    var maxY = Math.max(8, window.innerHeight - height - 8);
    var x = Math.min(Math.max(8, anchor.x), maxX);
    var y = Math.min(Math.max(8, anchor.y), maxY);
    content.style.left = x + "px";
    content.style.top = y + "px";
  }

  function openColumns(anchor) {
    var config = window.DBExplorer.getColumnConfig();
    var container = byId("colListContainer");
    container.innerHTML = "";
    config.order.forEach(function (key) {
      var def = window.DBExplorer.getColumnDef(key);
      if (!def) {
        return;
      }
      var item = document.createElement("div");
      item.className = "col-item";
      item.dataset.key = key;
      var handle = document.createElement("span");
      handle.className = "col-drag-handle";
      handle.textContent = "⠿";
      var label = document.createElement("label");
      var checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.className = "col-vis-check";
      checkbox.dataset.key = key;
      checkbox.checked = Boolean(config.visibility[key]);
      label.appendChild(checkbox);
      label.appendChild(document.createTextNode(" " + def.label));
      item.appendChild(handle);
      item.appendChild(label);
      container.appendChild(item);
    });
    if (window.Sortable) {
      if (sortable) {
        sortable.destroy();
      }
      sortable = window.Sortable.create(container, {
        animation: 150,
        handle: ".col-drag-handle",
        ghostClass: "col-sortable-ghost"
      });
    }
    showModal("colSelectModal");
    positionColumnModal(anchor);
  }

  function closeColumns() {
    hideModal("colSelectModal");
    resetColumnModal();
  }

  function saveColumns() {
    var container = byId("colListContainer");
    var order = [];
    var visibility = {};
    Array.prototype.forEach.call(container.querySelectorAll(".col-item"), function (item) {
      order.push(item.dataset.key);
    });
    Array.prototype.forEach.call(container.querySelectorAll(".col-vis-check"), function (box) {
      visibility[box.dataset.key] = box.checked;
    });
    window.DBExplorer.setColumnConfig({ order: order, visibility: visibility });
    closeColumns();
  }

  function init() {
    if (window.UrlSync) {
      window.UrlSync.bind({ url: "entityUrl", site: "entitySite", key: "entityKey" });
    }
    byId("entitySave").addEventListener("click", saveEntity);
    byId("entityCollectAllBtn").addEventListener("click", collectAllNow);
    var tabsBar = byId("entityTabs");
    if (tabsBar) {
      tabsBar.addEventListener("click", function (event) {
        var button = event.target.closest(".modal-tab");
        if (button && button.dataset.modalTab) {
          setModalTab(button.dataset.modalTab);
        }
      });
    }
    var altAdd = byId("entityAltAdd");
    if (altAdd) {
      altAdd.addEventListener("click", function () { addAltRow("alt", ""); });
    }
    var cmbAdd = byId("entityCmbAdd");
    if (cmbAdd) {
      cmbAdd.addEventListener("click", function () { addAltRow("cmb", ""); });
    }
    var altWrap = byId("entityAltWrap");
    if (altWrap) {
      altWrap.addEventListener("click", function (event) {
        var remove = event.target.closest("button[data-alt-remove]");
        if (remove) {
          var item = remove.closest(".alt-item");
          if (item) { item.remove(); }
        }
      });
    }
    var altSchedule = byId("entityAltSchedule");
    if (altSchedule) {
      altSchedule.addEventListener("change", updateAltIntervalVisibility);
    }
    var altSyncBtn = byId("entityAltSync");
    if (altSyncBtn) {
      altSyncBtn.addEventListener("click", syncAltNow);
    }
    var logList = byId("entityLogList");
    if (logList) {
      logList.addEventListener("click", handleEntityLogAction);
    }
    var logRefresh = byId("entityLogRefresh");
    if (logRefresh) {
      logRefresh.addEventListener("click", function () {
        if (current.target && current.target.id) {
          loadEntityLogs(current.target.id, "open");
        }
      });
    }
    byId("entityDateMode").addEventListener("change", function (event) {
      setDateMode(event.target.value);
    });
    byId("entityCancel").addEventListener("click", closeEntity);
    var modeLabel = byId("entityProfileModeLabel");
    if (modeLabel) {
      modeLabel.addEventListener("click", toggleProfileMode);
      modeLabel.addEventListener("keydown", function (event) {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          toggleProfileMode();
        }
      });
    }
    byId("colSelectSave").addEventListener("click", saveColumns);
    byId("colSelectCancel").addEventListener("click", closeColumns);
    document.addEventListener("keydown", function (event) {
      if (event.key !== "Escape") {
        return;
      }
      if (!byId("entityModal").classList.contains("hidden")) {
        closeEntity();
      } else if (!byId("colSelectModal").classList.contains("hidden")) {
        closeColumns();
      }
    });
    var entityModal = byId("entityModal");
    entityModal.addEventListener("mousedown", function (event) {
      if (event.target === entityModal) {
        closeEntity();
      }
    });
    bindEntityFieldReset(entityModal);
    var colModal = byId("colSelectModal");
    colModal.addEventListener("mousedown", function (event) {
      if (event.target === colModal) {
        closeColumns();
      }
    });
  }

  window.Modals = {
    init: init,
    openFolder: openFolder,
    openSource: openSource,
    openBatchEdit: openBatchEdit,
    openColumns: openColumns,
    close: closeEntity
  };
})(window, document);
