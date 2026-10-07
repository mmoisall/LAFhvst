(function (window, document) {
  "use strict";

  var LS = {
    view: "lafhvst.profiles.view",
    manual: "lafhvst.profiles.manual"
  };
  var VIEW_MODES = ["grid", "compact"];

  function readLS(key) {
    try {
      return window.localStorage.getItem(key);
    } catch (error) {
      return null;
    }
  }

  function writeLS(key, value) {
    try {
      window.localStorage.setItem(key, value);
    } catch (error) {
      /* noop */
    }
  }

  var state = {
    profiles: [],
    groups: [],
    events: {},
    loaded: false,
    reloadScheduled: false,
    view: VIEW_MODES.indexOf(readLS(LS.view)) >= 0 ? readLS(LS.view) : "grid",
    manualOrder: readLS(LS.manual) === "true"
  };

  var currentProfile = null;
  var currentGroup = null;
  var tickTimer = null;
  var profileSortable = null;
  var groupSortable = null;

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

  function showToast(message, type) {
    if (window.Toast) {
      window.Toast.show(message, type);
    }
  }

  function parseTimestamp(value) {
    if (!value) {
      return null;
    }
    var parsed = Date.parse(value);
    if (!isNaN(parsed)) {
      return parsed;
    }
    var normalized = String(value).replace(/(\.\d{3})\d+/, "$1");
    parsed = Date.parse(normalized);
    return isNaN(parsed) ? null : parsed;
  }

  function formatDuration(totalSeconds) {
    var seconds = Math.max(0, Math.floor(totalSeconds));
    var hours = Math.floor(seconds / 3600);
    var minutes = Math.floor((seconds % 3600) / 60);
    var secs = seconds % 60;
    if (hours > 0) {
      return hours + "시간 " + minutes + "분";
    }
    if (minutes > 0) {
      return minutes + "분 " + secs + "초";
    }
    return secs + "초";
  }

  // 인증 문제(쿠키 누락 / pixiv 토큰 무효) → 카드 배지
  function cookieBadge(profile) {
    var status = profile.cookie_status;
    if (!status || !status.problem) {
      return "";
    }
    var tip = status.problem;
    var label = "⚠ 재로그인";
    if (status.problem_kind === "token") {
      label = "⚠ pixiv 토큰";
      tip += "\npixiv 는 gallery-dl config 의 refresh-token 으로 로그인합니다." +
        "\n`gallery-dl oauth:pixiv` 로 재발급하거나 값(전역 config)을 유효한 토큰으로 바꾸세요.";
    } else {
      if (status.updated_at) {
        tip += "\n마지막 갱신: " + String(status.updated_at).replace("T", " ").slice(0, 16);
      }
      tip += "\n프로파일을 '브라우저 열기 (로그인)'로 다시 로그인하세요.";
    }
    return '<span class="cookie-badge" title="' + escapeHtml(tip) + '">' + label + "</span>";
  }

  function statusClass(status) {
    if (status === "차단") {
      return "profile-status-blocked";
    }
    if (status === "잠금") {
      return "profile-status-locked";
    }
    return "profile-status-normal";
  }

  function kindLabel(kind) {
    if (kind === "rate_limit") {
      return "88 한도";
    }
    if (kind === "short_rate") {
      return "429 단기";
    }
    if (kind === "auth") {
      return "인증 필요";
    }
    return kind || "";
  }

  function sparkline(events) {
    var points = (events || []).slice().reverse();
    if (points.length < 2) {
      return "";
    }
    var values = points.map(function (event) {
      return Number(event.capacity_after || 0);
    });
    var min = Math.min.apply(null, values);
    var max = Math.max.apply(null, values);
    var span = Math.max(1, max - min);
    var width = 220;
    var height = 40;
    var step = width / (values.length - 1);
    var path = values.map(function (value, index) {
      var x = index * step;
      var y = height - ((value - min) / span) * (height - 6) - 3;
      return (index === 0 ? "M" : "L") + x.toFixed(1) + " " + y.toFixed(1);
    }).join(" ");
    var last = values[values.length - 1];
    return '<div class="sparkline-wrap" title="학습 곡선 (최근 ' + values.length +
      "회, 현재 " + last + ')">' +
      '<svg class="sparkline" viewBox="0 0 ' + width + " " + height +
      '" preserveAspectRatio="none"><path d="' + path + '"/></svg>' +
      '<span class="sparkline-range">' + min + " ~ " + max + "</span></div>";
  }

  function profileCard(profile, index) {
    var ratio = Number(profile.usage_ratio || 0);
    var percent = Math.min(100, Math.max(0, Math.round(ratio * 100)));
    var barClass = percent >= 90 ? "danger" : (percent >= 70 ? "warn" : "");
    var cooldownText = profile.in_cooldown
      ? "쿨다운 해제까지 " + formatDuration(profile.cooldown_seconds || 0)
      : (profile.probe_pending ? "프로빙 대기" : "가용 (쿨다운 없음)");
    var cooldownClass = profile.in_cooldown ? "cooling" : "ready";
    var proxy = profile.proxy_url ? " · proxy" : "";
    var context = profile.context_dir || "";
    var rate = Number(profile.learned_rate_per_hour || 0);
    var kind = profile.last_error_kind ? kindLabel(profile.last_error_kind) : "";
    var handle = state.manualOrder
      ? '<span class="drag-handle" title="드래그로 순서 변경">⠿</span>'
      : "";
    return '<article class="profile-card' + (state.manualOrder ? " manual" : "") +
      '" data-id="' + profile.id + '" data-kind="profile">' +
      '<header class="profile-card-head">' + handle +
        '<span class="seq-badge">#' + ((index || 0) + 1) + "</span>" +
        '<span class="id-badge">ID ' + escapeHtml(profile.id) + "</span>" +
        (profile.is_site_default ? '<span class="default-badge" title="사이트 디폴트">⭐</span>' : "") +
        '<h3>' + escapeHtml(profile.name || "(이름 없음)") + "</h3>" +
        '<span class="status-badge ' + statusClass(profile.status) + '">' +
          escapeHtml(profile.status || "정상") + "</span>" +
        cookieBadge(profile) +
      "</header>" +
      '<p class="muted profile-meta">' + escapeHtml(profile.site || "사이트 미지정") + proxy + "</p>" +
      '<div class="capacity-badges">' +
        '<span class="stat-badge">예측 한도 <b>' + escapeHtml(profile.learned_capacity) + "</b></span>" +
        '<span class="stat-badge">구간 <b>' + escapeHtml(profile.capacity_lo) + "-" + escapeHtml(profile.capacity_hi) + "</b></span>" +
        '<span class="stat-badge">사용 <b>' + escapeHtml(profile.recent_usage_count) + "</b></span>" +
        '<span class="stat-badge">잔여 <b>' + escapeHtml(profile.remaining_capacity) + "</b></span>" +
      "</div>" +
      '<div class="progress" title="' + percent + '%">' +
        '<span class="progress-fill ' + barClass + '" style="width:' + percent + '%;"></span>' +
      "</div>" +
      '<p class="cooldown-line ' + cooldownClass + '" data-cooldown="' +
        escapeHtml(profile.cooldown_until || "") + '">' + escapeHtml(cooldownText) + "</p>" +
      '<div class="capacity-badges">' +
        '<span class="stat-badge">신뢰도 <b>' + escapeHtml(Math.round((profile.confidence || 0) * 100)) + "%</b></span>" +
        '<span class="stat-badge">관측 <b>' + escapeHtml(profile.observations) + "</b></span>" +
        '<span class="stat-badge">속도 <b>' + escapeHtml(rate) + "/h</b></span>" +
        '<span class="stat-badge">안정 <b>' + escapeHtml(profile.success_streak) + "</b></span>" +
      "</div>" +
      '<p class="muted profile-foot">연속 오류 ' + escapeHtml(profile.consecutive_errors) +
        " · 쿨다운 기준 " + escapeHtml(profile.learned_cooldown_minutes) + "분" +
        (profile.observed_recovery_minutes ? " · 실측 회복 " + escapeHtml(profile.observed_recovery_minutes) + "분" : "") +
        (kind ? " · 최근 " + escapeHtml(kind) : "") + "</p>" +
      sparkline(state.events[profile.id]) +
      '<p class="muted profile-path" title="' + escapeHtml(context) + '">' + escapeHtml(context) + "</p>" +
      '<div class="card-actions">' +
        '<button class="btn btn-sm primary" type="button" data-action="launch">🖥 브라우저 열기 (로그인)</button>' +
        (profile.site === "pixiv" ? '<button class="btn btn-sm" type="button" data-action="pixiv-token" title="pixiv refresh token 발급(브라우저 자동)">🔑 pixiv 토큰</button>' : "") +
        '<button class="btn btn-sm' + (profile.is_site_default ? " active-toggle" : "") + '" type="button" data-action="default" title="사이트 디폴트로 설정/해제">⭐</button>' +
        '<button class="btn btn-sm" type="button" data-action="edit" title="편집">⚙️</button>' +
        '<button class="btn btn-sm" type="button" data-action="reset" title="학습 초기화">♻️</button>' +
        '<button class="btn btn-sm danger" type="button" data-action="delete" title="삭제">🗑️</button>' +
      "</div>" +
    "</article>";
  }

  function groupCard(group, index) {
    var members = (group.profiles || []).map(function (profile) {
      return '<span class="member-chip ' + statusClass(profile.status) + '">' +
        escapeHtml(profile.name) + "</span>";
    }).join("");
    if (!members) {
      members = '<span class="muted">포함된 프로파일이 없습니다.</span>';
    }
    var cooldownText = group.in_cooldown
      ? "그룹 쿨다운 해제까지 " + formatDuration(group.cooldown_seconds || 0)
      : (group.shared_limit ? "공유 한도 관리 중" : "독립 계정 운영");
    var cooldownClass = group.in_cooldown ? "cooling" : "ready";
    var handle = state.manualOrder
      ? '<span class="drag-handle" title="드래그로 순서 변경">⠿</span>'
      : "";
    return '<article class="group-card' + (state.manualOrder ? " manual" : "") +
      '" data-id="' + group.id + '" data-kind="group">' +
      '<header class="group-card-head">' + handle +
        '<span class="seq-badge">#' + ((index || 0) + 1) + "</span>" +
        '<span class="id-badge">ID ' + escapeHtml(group.id) + "</span>" +
        '<h3>' + escapeHtml(group.name || "(이름 없음)") + "</h3>" +
        '<span class="count-badge">' + (group.profiles || []).length + "개 계정</span>" +
      "</header>" +
      '<p class="muted profile-meta">' + escapeHtml(group.site || "사이트 미지정") + "</p>" +
      '<div class="capacity-badges">' +
        '<span class="stat-badge">동시 <b>' + escapeHtml(group.concurrent_limit) + "</b></span>" +
        '<span class="stat-badge">pace <b>' + escapeHtml(group.pace_seconds) + "s</b></span>" +
        '<span class="stat-badge">' + (group.shared_limit ? "공유 한도" : "독립") + "</span>" +
      "</div>" +
      '<p class="cooldown-line ' + cooldownClass + '" data-group-cooldown="' +
        escapeHtml(group.cooldown_until || "") + '">' + escapeHtml(cooldownText) + "</p>" +
      '<div class="group-members">' + members + "</div>" +
      '<div class="card-actions">' +
        '<button class="btn btn-sm" type="button" data-action="edit">⚙️ 편집</button>' +
        '<button class="btn btn-sm" type="button" data-action="reset" title="그룹 쿨다운 해제">♻️ 리셋</button>' +
        '<button class="btn btn-sm danger" type="button" data-action="delete">🗑️ 삭제</button>' +
      "</div>" +
    "</article>";
  }

  function profileCompactRow(profile, index) {
    var percent = Math.min(100, Math.max(0, Math.round(Number(profile.usage_ratio || 0) * 100)));
    var handle = state.manualOrder ? '<span class="drag-handle">⠿</span>' : "";
    var cooldown = profile.in_cooldown
      ? "쿨다운 " + formatDuration(profile.cooldown_seconds || 0)
      : (profile.probe_pending ? "프로빙 대기" : "가용");
    return '<div class="profile-compact-row' + (state.manualOrder ? " manual" : "") +
      '" data-id="' + profile.id + '" data-kind="profile">' + handle +
      '<span class="seq-badge">#' + ((index || 0) + 1) + "</span>" +
      '<span class="id-badge">' + escapeHtml(profile.id) + "</span>" +
      '<span class="compact-title">' + escapeHtml(profile.name || "(이름 없음)") + "</span>" +
      (profile.is_site_default ? '<span class="default-badge" title="사이트 디폴트">⭐</span>' : "") +
      '<span class="muted compact-meta">' + escapeHtml(profile.site || "-") + "</span>" +
      '<span class="status-badge ' + statusClass(profile.status) + '">' +
        escapeHtml(profile.status || "정상") + "</span>" +
      cookieBadge(profile) +
      '<span class="muted compact-meta">한도 ' + escapeHtml(profile.learned_capacity) +
        " · 사용 " + escapeHtml(profile.recent_usage_count) + " (" + percent + "%)</span>" +
      '<span class="cooldown-line ' + (profile.in_cooldown ? "cooling" : "ready") + '">' +
        escapeHtml(cooldown) + "</span>" +
      '<span class="row-actions">' +
        '<button class="btn btn-sm primary" type="button" data-action="launch" title="브라우저 열기">🖥</button>' +
        (profile.site === "pixiv" ? '<button class="btn btn-sm" type="button" data-action="pixiv-token" title="pixiv 토큰 발급">🔑</button>' : "") +
        '<button class="btn btn-sm' + (profile.is_site_default ? " active-toggle" : "") + '" type="button" data-action="default" title="사이트 디폴트">⭐</button>' +
        '<button class="btn btn-sm" type="button" data-action="edit" title="편집">⚙️</button>' +
        '<button class="btn btn-sm" type="button" data-action="reset" title="초기화">♻️</button>' +
        '<button class="btn btn-sm danger" type="button" data-action="delete" title="삭제">🗑️</button>' +
      "</span></div>";
  }

  function groupCompactRow(group, index) {
    var handle = state.manualOrder ? '<span class="drag-handle">⠿</span>' : "";
    var cooldown = group.in_cooldown
      ? "쿨다운 " + formatDuration(group.cooldown_seconds || 0)
      : (group.shared_limit ? "공유 한도" : "독립");
    return '<div class="profile-compact-row' + (state.manualOrder ? " manual" : "") +
      '" data-id="' + group.id + '" data-kind="group">' + handle +
      '<span class="seq-badge">#' + ((index || 0) + 1) + "</span>" +
      '<span class="id-badge">' + escapeHtml(group.id) + "</span>" +
      '<span class="compact-title">' + escapeHtml(group.name || "(이름 없음)") + "</span>" +
      '<span class="muted compact-meta">' + escapeHtml(group.site || "-") + "</span>" +
      '<span class="count-badge">' + (group.profiles || []).length + "개</span>" +
      '<span class="cooldown-line ' + (group.in_cooldown ? "cooling" : "ready") + '">' +
        escapeHtml(cooldown) + "</span>" +
      '<span class="row-actions">' +
        '<button class="btn btn-sm" type="button" data-action="edit">⚙️ 편집</button>' +
        '<button class="btn btn-sm" type="button" data-action="reset">♻️ 리셋</button>' +
        '<button class="btn btn-sm danger" type="button" data-action="delete">🗑️ 삭제</button>' +
      "</span></div>";
  }

  function render() {
    var compact = state.view === "compact";
    var grid = byId("profileGrid");
    if (grid) {
      grid.className = "profile-grid" + (compact ? " compact" : "");
      grid.innerHTML = (compact
        ? state.profiles.map(profileCompactRow)
        : state.profiles.map(profileCard)).join("");
    }
    var groupGrid = byId("groupGrid");
    if (groupGrid) {
      groupGrid.className = "group-grid" + (compact ? " compact" : "");
      groupGrid.innerHTML = (compact
        ? state.groups.map(groupCompactRow)
        : state.groups.map(groupCard)).join("");
    }
    var profileEmpty = byId("profileEmpty");
    if (profileEmpty) {
      profileEmpty.classList.toggle("hidden", state.profiles.length > 0);
    }
    var groupEmpty = byId("groupEmpty");
    if (groupEmpty) {
      groupEmpty.classList.toggle("hidden", state.groups.length > 0);
    }
    updateViewButtons();
    ensureSortables();
    updateCountdowns();
  }

  function updateCountdowns() {
    updateGroupCountdowns();
    var nodes = document.querySelectorAll("#profileGrid .cooldown-line[data-cooldown]");
    var needReload = false;
    Array.prototype.forEach.call(nodes, function (node) {
      var raw = node.getAttribute("data-cooldown");
      var line = node.closest(".profile-card");
      if (!raw) {
        node.textContent = "가용 (쿨다운 없음)";
        node.classList.remove("cooling");
        node.classList.add("ready");
        if (line) {
          var fill = line.querySelector(".progress-fill");
          if (fill) {
            fill.classList.remove("warn", "danger");
          }
        }
        return;
      }
      var target = parseTimestamp(raw);
      if (target === null) {
        return;
      }
      var remaining = (target - Date.now()) / 1000;
      if (remaining <= 0) {
        node.textContent = "가용 (쿨다운 해제됨)";
        node.classList.remove("cooling");
        node.classList.add("ready");
        needReload = true;
      } else {
        node.textContent = "쿨다운 해제까지 " + formatDuration(remaining);
        node.classList.add("cooling");
        node.classList.remove("ready");
      }
    });
    if (needReload && !state.reloadScheduled) {
      state.reloadScheduled = true;
      window.setTimeout(function () {
        state.reloadScheduled = false;
        load();
      }, 2500);
    }
  }

  function updateGroupCountdowns() {
    var nodes = document.querySelectorAll("#groupGrid .cooldown-line[data-group-cooldown]");
    var needReload = false;
    Array.prototype.forEach.call(nodes, function (node) {
      var raw = node.getAttribute("data-group-cooldown");
      if (!raw) {
        node.textContent = "공유 한도 관리 중";
        node.classList.remove("cooling");
        node.classList.add("ready");
        return;
      }
      var target = parseTimestamp(raw);
      if (target === null) {
        return;
      }
      var remaining = (target - Date.now()) / 1000;
      if (remaining <= 0) {
        node.textContent = "그룹 쿨다운 해제됨";
        node.classList.remove("cooling");
        node.classList.add("ready");
        needReload = true;
      } else {
        node.textContent = "그룹 쿨다운 해제까지 " + formatDuration(remaining);
        node.classList.add("cooling");
        node.classList.remove("ready");
      }
    });
    if (needReload && !state.reloadScheduled) {
      state.reloadScheduled = true;
      window.setTimeout(function () {
        state.reloadScheduled = false;
        load();
      }, 2500);
    }
  }

  function loadLearning(profiles) {
    var requests = (profiles || []).map(function (profile) {
      return window.API.getProfileLearning(profile.id, 30).then(function (events) {
        return { id: profile.id, events: events || [] };
      }).catch(function () {
        return { id: profile.id, events: [] };
      });
    });
    return Promise.all(requests).then(function (results) {
      var map = {};
      results.forEach(function (entry) {
        map[entry.id] = entry.events;
      });
      state.events = map;
    });
  }

  function load() {
    return Promise.all([
      window.API.getProfiles(),
      window.API.getProfileGroups()
    ]).then(function (results) {
      state.profiles = results[0] || [];
      state.groups = results[1] || [];
      state.loaded = true;
      return loadLearning(state.profiles);
    }).then(function () {
      render();
    }).catch(function (error) {
      showToast(error.message || "프로파일 로드 실패", "error");
    });
  }

  function profileById(id) {
    for (var i = 0; i < state.profiles.length; i += 1) {
      if (state.profiles[i].id === id) {
        return state.profiles[i];
      }
    }
    return null;
  }

  function groupById(id) {
    for (var i = 0; i < state.groups.length; i += 1) {
      if (state.groups[i].id === id) {
        return state.groups[i];
      }
    }
    return null;
  }

  function openProfileModal(profile) {
    currentProfile = profile || null;
    byId("profileModalTitle").textContent = profile ? "단일 프로파일 편집" : "새 단일 프로파일";
    byId("profileName").value = profile ? (profile.name || "") : "";
    byId("profileSite").value = profile ? (profile.site || "") : "";
    byId("profileKey").value = profile ? (profile.account_key || "") : "";
    byId("profileUrl").value = profile ? (profile.account_url || "") : "";
    byId("profileContextDir").value = profile ? (profile.context_dir || "") : "";
    byId("profileProxy").value = profile ? (profile.proxy_url || "") : "";
    byId("profileStatus").value = profile ? (profile.status || "정상") : "정상";
    byId("profileCapacity").value = profile ? profile.learned_capacity : 500;
    byId("profileCooldownMinutes").value = profile ? profile.learned_cooldown_minutes : 15;
    if (window.SiteSuggestions) {
      window.SiteSuggestions.ensure();
    }
    if (window.UrlSync) {
      window.UrlSync.refresh();
    }
    byId("profileModal").classList.remove("hidden");
  }

  function saveProfile() {
    var payload = {
      name: byId("profileName").value.trim(),
      site: byId("profileSite").value.trim() || null,
      account_key: byId("profileKey").value.trim() || null,
      account_url: byId("profileUrl").value.trim() || null,
      context_dir: byId("profileContextDir").value.trim(),
      proxy_url: byId("profileProxy").value.trim() || null,
      status: byId("profileStatus").value,
      learned_capacity: parseInt(byId("profileCapacity").value, 10) || 500,
      learned_cooldown_minutes: parseInt(byId("profileCooldownMinutes").value, 10) || 15
    };
    var action = currentProfile
      ? window.API.updateProfile(currentProfile.id, payload)
      : window.API.createProfile(payload);
    action.then(function () {
      byId("profileModal").classList.add("hidden");
      showToast("프로파일이 저장되었습니다.", "success");
      if (window.SiteSuggestions) {
        window.SiteSuggestions.invalidate();
      }
      load();
    }).catch(function (error) {
      showToast(error.message || "프로파일 저장 실패", "error");
    });
  }

  function openGroupModal(group) {
    currentGroup = group || null;
    byId("groupModalTitle").textContent = group ? "합동 프로파일 편집" : "새 합동 프로파일";
    byId("groupName").value = group ? (group.name || "") : "";
    byId("groupSite").value = group ? (group.site || "") : "";
    byId("groupKey").value = group ? (group.account_key || "") : "";
    byId("groupUrl").value = group ? (group.account_url || "") : "";
    byId("groupConcurrent").value = group ? (group.concurrent_limit || 1) : 1;
    byId("groupPace").value = group ? (group.pace_seconds || 0) : 0;
    byId("groupShared").checked = group ? Boolean(group.shared_limit) : true;
    if (window.SiteSuggestions) {
      window.SiteSuggestions.ensure();
    }
    if (window.UrlSync) {
      window.UrlSync.refresh();
    }
    var selected = {};
    ((group && group.profile_ids) || []).forEach(function (id) {
      selected[id] = true;
    });
    var container = byId("groupProfileList");
    if (!state.profiles.length) {
      container.innerHTML = '<p class="muted">먼저 단일 프로파일을 등록하세요.</p>';
    } else {
      container.innerHTML = state.profiles.map(function (profile) {
        var checked = selected[profile.id] ? " checked" : "";
        return '<label class="group-profile-item"><input type="checkbox" value="' +
          profile.id + '"' + checked + "> <span>" + escapeHtml(profile.name) +
          (profile.site ? " (" + escapeHtml(profile.site) + ")" : "") + "</span></label>";
      }).join("");
    }
    byId("groupModal").classList.remove("hidden");
  }

  function saveGroup() {
    var ids = [];
    Array.prototype.forEach.call(
      byId("groupProfileList").querySelectorAll("input[type=checkbox]"),
      function (box) {
        if (box.checked) {
          ids.push(parseInt(box.value, 10));
        }
      }
    );
    var payload = {
      name: byId("groupName").value.trim(),
      site: byId("groupSite").value.trim() || null,
      account_key: byId("groupKey").value.trim() || null,
      account_url: byId("groupUrl").value.trim() || null,
      profile_ids: ids,
      concurrent_limit: parseInt(byId("groupConcurrent").value, 10) || 1,
      pace_seconds: parseFloat(byId("groupPace").value) || 0,
      shared_limit: byId("groupShared").checked
    };
    var action = currentGroup
      ? window.API.updateProfileGroup(currentGroup.id, payload)
      : window.API.createProfileGroup(payload);
    action.then(function () {
      byId("groupModal").classList.add("hidden");
      showToast("합동 프로파일이 저장되었습니다.", "success");
      if (window.SiteSuggestions) {
        window.SiteSuggestions.invalidate();
      }
      load();
    }).catch(function (error) {
      showToast(error.message || "합동 프로파일 저장 실패", "error");
    });
  }

  function issuePixivToken(profile) {
    if (!window.confirm("[" + (profile.name || profile.id) + "]\n\n" +
        "pixiv refresh token 을 발급합니다.\n" +
        "프로파일 브라우저가 열리고, 로그인되어 있으면 자동으로 완료됩니다.\n" +
        "(로그인 화면이 뜨면 로그인하세요. 로그인하면 자동으로 완료됩니다. 최대 10분)\n\n" +
        "진행할까요?")) {
      return;
    }
    showToast("pixiv 토큰 발급 중… (열린 창을 닫지 마세요)", "info");
    window.API.issuePixivToken(profile.id).then(function (result) {
      if (result && result.ok) {
        showToast("pixiv 토큰 발급 완료" + (result.user ? " · " + result.user : ""), "success");
      } else {
        showToast((result && result.error) || "pixiv 토큰 발급 실패", "error");
      }
      refresh();
    }).catch(function (error) {
      showToast(error.message || "pixiv 토큰 발급 실패", "error");
    });
  }

  function launchProfile(profile) {
    showToast("[" + profile.name + "] 브라우저를 여는 중...", "info");
    window.API.launchProfile(profile.id).then(function (result) {
      if (result && result.already_running) {
        showToast("이미 브라우저가 실행 중입니다.", "info");
      } else {
        showToast("브라우저에서 로그인/캡차를 완료한 뒤 창을 닫아주세요.", "success");
      }
    }).catch(function (error) {
      var message = error.message || "브라우저 실행 실패";
      showToast(message, "error");
      var hint = byId("playwrightHint");
      if (hint && message.indexOf("playwright") >= 0) {
        hint.textContent = "Playwright 미설치: pip install playwright && playwright install chromium";
      }
    });
  }

  function handleGridAction(event) {
    var button = event.target.closest("button[data-action]");
    if (!button) {
      return;
    }
    var card = button.closest(".profile-card, .group-card, .profile-compact-row");
    if (!card) {
      return;
    }
    var id = parseInt(card.dataset.id, 10);
    var action = button.dataset.action;
    var isProfile = card.dataset.kind === "profile" || card.classList.contains("profile-card");

    if (isProfile) {
      var profile = profileById(id);
      if (!profile) {
        return;
      }
      if (action === "launch") {
        launchProfile(profile);
      } else if (action === "pixiv-token") {
        issuePixivToken(profile);
      } else if (action === "default") {
        var makeDefault = !profile.is_site_default;
        window.API.setProfileDefault(id, makeDefault).then(function () {
          showToast(
            makeDefault
              ? "[" + profile.name + "] 를 사이트 디폴트로 설정했습니다."
              : "[" + profile.name + "] 사이트 디폴트를 해제했습니다.",
            "success"
          );
          load();
        }).catch(function (error) {
          showToast(error.message || "사이트 디폴트 설정 실패", "error");
        });
      } else if (action === "edit") {
        openProfileModal(profile);
      } else if (action === "reset") {
        if (!window.confirm("[" + profile.name + "] 학습 데이터를 초기화할까요?")) {
          return;
        }
        window.API.resetProfile(id).then(function () {
          showToast("학습 데이터가 초기화되었습니다.", "success");
          load();
        }).catch(function (error) {
          showToast(error.message || "초기화 실패", "error");
        });
      } else if (action === "delete") {
        if (!window.confirm("[" + profile.name + "] 프로파일을 삭제할까요?")) {
          return;
        }
        window.API.deleteProfile(id).then(function () {
          showToast("삭제되었습니다.", "success");
          load();
        }).catch(function (error) {
          showToast(error.message || "삭제 실패", "error");
        });
      }
      return;
    }

    var group = groupById(id);
    if (!group) {
      return;
    }
    if (action === "edit") {
      openGroupModal(group);
    } else if (action === "reset") {
      window.API.resetProfileGroup(id).then(function () {
        showToast("그룹 쿨다운/pace가 초기화되었습니다.", "success");
        load();
      }).catch(function (error) {
        showToast(error.message || "그룹 리셋 실패", "error");
      });
    } else if (action === "delete") {
      if (!window.confirm("[" + group.name + "] 합동 프로파일을 삭제할까요?")) {
        return;
      }
      window.API.deleteProfileGroup(id).then(function () {
        showToast("삭제되었습니다.", "success");
        load();
      }).catch(function (error) {
        showToast(error.message || "삭제 실패", "error");
      });
    }
  }

  function bindModalDismiss() {
    var profileModal = byId("profileModal");
    if (profileModal) {
      profileModal.addEventListener("mousedown", function (event) {
        if (event.target === profileModal) {
          profileModal.classList.add("hidden");
        }
      });
    }
    var groupModal = byId("groupModal");
    if (groupModal) {
      groupModal.addEventListener("mousedown", function (event) {
        if (event.target === groupModal) {
          groupModal.classList.add("hidden");
        }
      });
    }
    document.addEventListener("keydown", function (event) {
      if (event.key !== "Escape") {
        return;
      }
      if (profileModal && !profileModal.classList.contains("hidden")) {
        profileModal.classList.add("hidden");
      } else if (groupModal && !groupModal.classList.contains("hidden")) {
        groupModal.classList.add("hidden");
      }
    });
  }

  function setupSortable(container, instance, kind) {
    if (instance) {
      try {
        instance.destroy();
      } catch (error) {
        /* noop */
      }
      instance = null;
    }
    if (!container || !state.manualOrder || !window.Sortable) {
      return null;
    }
    return window.Sortable.create(container, {
      animation: 150,
      handle: ".drag-handle",
      draggable: "[data-id]",
      filter: "button, a, input, select, textarea",
      preventOnFilter: true,
      onEnd: function (evt) {
        handleReorder(kind, evt);
      }
    });
  }

  function ensureSortables() {
    profileSortable = setupSortable(byId("profileGrid"), profileSortable, "profile");
    groupSortable = setupSortable(byId("groupGrid"), groupSortable, "group");
  }

  function handleReorder(kind, evt) {
    var container = evt.from;
    if (!container) {
      return;
    }
    var ids = [];
    Array.prototype.forEach.call(container.querySelectorAll("[data-id]"), function (element) {
      var value = parseInt(element.dataset.id, 10);
      if (!isNaN(value) && ids.indexOf(value) < 0) {
        ids.push(value);
      }
    });
    if (!ids.length || evt.oldIndex === evt.newIndex) {
      load();
      return;
    }
    var action = kind === "group"
      ? window.API.reorderProfileGroups(ids)
      : window.API.reorderProfiles(ids);
    action.then(function () {
      load();
    }).catch(function (error) {
      showToast(error.message || "순서 저장 실패", "error");
      load();
    });
  }

  function updateViewButtons() {
    ["grid", "compact"].forEach(function (mode) {
      var id = "profilesView" + mode.charAt(0).toUpperCase() + mode.slice(1);
      var btn = byId(id);
      if (btn) {
        btn.classList.toggle("active", state.view === mode);
      }
    });
    var chip = byId("profilesManualBtn");
    if (chip) {
      chip.classList.toggle("active", state.manualOrder);
    }
  }

  function setView(mode) {
    if (VIEW_MODES.indexOf(mode) < 0 || state.view === mode) {
      return;
    }
    state.view = mode;
    writeLS(LS.view, mode);
    render();
  }

  function toggleManualOrder() {
    state.manualOrder = !state.manualOrder;
    writeLS(LS.manual, state.manualOrder ? "true" : "false");
    showToast(
      state.manualOrder
        ? "수동 순서 모드 ON: ⠿ 핸들을 드래그하세요."
        : "수동 순서 모드 OFF",
      "info"
    );
    render();
  }

  function wireViewControls() {
    var gridBtn = byId("profilesViewGrid");
    if (gridBtn) {
      gridBtn.addEventListener("click", function () {
        setView("grid");
      });
    }
    var compactBtn = byId("profilesViewCompact");
    if (compactBtn) {
      compactBtn.addEventListener("click", function () {
        setView("compact");
      });
    }
    var manualBtn = byId("profilesManualBtn");
    if (manualBtn) {
      manualBtn.addEventListener("click", toggleManualOrder);
    }
    updateViewButtons();
  }

  function init() {
    if (window.UrlSync) {
      window.UrlSync.bind({ url: "profileUrl", site: "profileSite", key: "profileKey" });
      window.UrlSync.bind({ url: "groupUrl", site: "groupSite", key: "groupKey" });
    }
    var newProfileBtn = byId("newProfileBtn");
    if (newProfileBtn) {
      newProfileBtn.addEventListener("click", function () {
        openProfileModal(null);
      });
    }
    var newGroupBtn = byId("newGroupBtn");
    if (newGroupBtn) {
      newGroupBtn.addEventListener("click", function () {
        openGroupModal(null);
      });
    }
    var refreshBtn = byId("profilesRefreshBtn");
    if (refreshBtn) {
      refreshBtn.addEventListener("click", load);
    }
    var profileGrid = byId("profileGrid");
    if (profileGrid) {
      profileGrid.addEventListener("click", handleGridAction);
    }
    var groupGrid = byId("groupGrid");
    if (groupGrid) {
      groupGrid.addEventListener("click", handleGridAction);
    }
    byId("profileSave").addEventListener("click", saveProfile);
    byId("profileCancel").addEventListener("click", function () {
      byId("profileModal").classList.add("hidden");
    });
    byId("groupSave").addEventListener("click", saveGroup);
    byId("groupCancel").addEventListener("click", function () {
      byId("groupModal").classList.add("hidden");
    });
    wireViewControls();
    bindModalDismiss();
    if (!tickTimer) {
      tickTimer = window.setInterval(updateCountdowns, 1000);
    }
    load();
  }

  function focusProfile(profileId) {
    return load().then(function () {
      var card = document.querySelector('#profileGrid [data-id="' + profileId + '"]');
      if (!card) {
        showToast("해당 프로파일을 찾을 수 없습니다.", "info");
        return;
      }
      card.classList.add("locate-flash");
      if (card.scrollIntoView) {
        card.scrollIntoView({ block: "center", behavior: "smooth" });
      }
      window.setTimeout(function () {
        card.classList.remove("locate-flash");
      }, 2600);
      var profile = profileById(profileId);
      if (profile) {
        var mode = state.view === "compact" ? "compact" : "grid";
        if (mode === "grid") {
          openProfileModal(profile);
        }
      }
    });
  }

  window.Profiles = {
    init: init,
    refresh: load,
    render: render,
    focusProfile: focusProfile
  };
})(window, document);
