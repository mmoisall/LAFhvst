(function (window, document) {
  "use strict";

  var state = {
    logs: [],
    stats: null,
    unresolvedOnly: false,
    loaded: false,
    timer: null
  };

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

  var LEVEL_CLASS = {
    ERROR: "log-error",
    WARN: "log-warn",
    SUCCESS: "log-success",
    INFO: "log-info",
    DEBUG: "log-debug",
    SYSTEM: "log-info",
    GDL: "log-info",
    DB: "log-info"
  };

  var STATUS_LABEL = {
    open: "미해결",
    resolved: "해결",
    ignored: "무시"
  };

  function categoryLabel(category, scope) {
    var map = {
      run: "실행",
      system: "시스템",
      auth: "인증",
      rate_limit: "한도(88)",
      short_rate: "단기(429)",
      not_found: "없음(404)",
      network: "네트워크",
      browser: "브라우저",
      watcher: "감시",
      error: "오류"
    };
    return map[category] || category || scope || "기타";
  }

  function renderStats() {
    var bar = byId("logStatsBar");
    if (!bar) {
      return;
    }
    var s = state.stats || {};
    bar.innerHTML =
      '<span class="log-stat open">미해결 <b>' + (s.open || 0) + "</b></span>" +
      '<span class="log-stat err">오류 <b>' + (s.errors || 0) + "</b></span>" +
      '<span class="log-stat warn">경고 <b>' + (s.warnings || 0) + "</b></span>" +
      '<span class="log-stat">전체 <b>' + (s.total || 0) + "</b></span>";
  }

  function logRow(log) {
    var levelClass = LEVEL_CLASS[log.level] || "log-info";
    var scopeLabel = log.source_name
      ? escapeHtml(log.source_name)
      : (log.scope === "watcher" ? "감시" : log.scope || "시스템");
    var actions = "";
    if (log.status === "open") {
      actions += '<button class="btn btn-sm primary" type="button" data-action="resolve" title="해결">✔ 해결</button>';
      actions += '<button class="btn btn-sm" type="button" data-action="ignore" title="무시">🚫 무시</button>';
      if (log.source_id) {
        actions += '<button class="btn btn-sm" type="button" data-action="retry" title="재시도">⚡ 재시도</button>';
      }
    } else {
      actions += '<button class="btn btn-sm" type="button" data-action="reopen" title="다시 열기">↺ 열기</button>';
    }
    if (log.source_id) {
      actions += '<button class="btn btn-sm" type="button" data-action="locate-source" title="DB에서 이 소스 위치로 이동">📍 소스 위치</button>';
    }
    if (log.profile_id) {
      actions += '<button class="btn btn-sm" type="button" data-action="locate-profile" title="이 프로파일 열기">👤 프로파일</button>';
    }
    actions += '<button class="btn btn-sm" type="button" data-action="guide" title="해결 가이드">💡</button>';
    actions += '<button class="btn btn-sm danger" type="button" data-action="delete" title="삭제">🗑️</button>';

    var repeat = (log.repeat_count || 1) > 1
      ? '<span class="log-repeat" title="반복 횟수">×' + log.repeat_count + "</span>"
      : "";
    var code = log.error_code
      ? '<span class="log-code">' + escapeHtml(log.error_code) + "</span>"
      : "";
    return '<article class="log-row ' + levelClass + '" data-id="' + log.id +
      '" data-source-id="' + (log.source_id || "") +
      '" data-profile-id="' + (log.profile_id || "") + '">' +
      '<div class="log-row-head">' +
        '<span class="log-level">' + escapeHtml(log.level) + "</span>" +
        '<span class="log-cat">' + escapeHtml(categoryLabel(log.category, log.scope)) + "</span>" +
        code + repeat +
        '<span class="log-status status-' + escapeHtml(log.status) + '">' +
          escapeHtml(STATUS_LABEL[log.status] || log.status) + "</span>" +
        '<span class="log-scope" title="아이템/범위">' + scopeLabel + "</span>" +
        '<span class="log-time">' + escapeHtml(fmtTime(log.last_at || log.created_at)) + "</span>" +
      "</div>" +
      '<div class="log-msg">' + escapeHtml(log.message) + "</div>" +
      (log.resolution ? '<div class="log-resolution">해결: ' + escapeHtml(log.resolution) + "</div>" : "") +
      '<div class="log-actions">' + actions + "</div>" +
      "</article>";
  }

  function render() {
    var list = byId("logList");
    if (list) {
      list.innerHTML = state.logs.map(logRow).join("");
    }
    var empty = byId("logEmpty");
    if (empty) {
      empty.classList.toggle("hidden", state.logs.length > 0);
    }
    var btn = byId("logUnresolvedBtn");
    if (btn) {
      btn.classList.toggle("active", state.unresolvedOnly);
    }
    renderStats();
  }

  function filters() {
    var params = { limit: 200 };
    var status = byId("logStatusFilter");
    var level = byId("logLevelFilter");
    var scope = byId("logScopeFilter");
    var query = byId("logSearchInput");
    if (state.unresolvedOnly) {
      params.status = "open";
    } else if (status && status.value) {
      params.status = status.value;
    }
    if (level && level.value) {
      params.level = level.value;
    }
    if (scope && scope.value) {
      params.scope = scope.value;
    }
    if (query && query.value.trim()) {
      params.q = query.value.trim();
    }
    return params;
  }

  function load() {
    return window.API.getLogs(filters()).then(function (result) {
      state.logs = (result && result.items) || [];
      state.stats = (result && result.stats) || null;
      state.loaded = true;
      render();
    }).catch(function (error) {
      showToast(error.message || "로그 로드 실패", "error");
    });
  }

  function logById(id) {
    for (var i = 0; i < state.logs.length; i += 1) {
      if (state.logs[i].id === id) {
        return state.logs[i];
      }
    }
    return null;
  }

  function showGuide(log) {
    window.API.request("/api/logs/" + log.id).then(function (full) {
      var guide = (full && full.guide) || "추천 조치가 없습니다.";
      window.alert("[" + (log.category || log.scope) + "] 해결 가이드\n\n" + guide);
    }).catch(function () {
      window.alert("가이드를 불러오지 못했습니다.");
    });
  }

  function handleAction(event) {
    var button = event.target.closest("button[data-action]");
    if (!button) {
      return;
    }
    var row = button.closest(".log-row");
    if (!row) {
      return;
    }
    var id = parseInt(row.dataset.id, 10);
    var log = logById(id);
    if (!log) {
      return;
    }
    var action = button.dataset.action;
    if (action === "resolve") {
      var note = window.prompt("해결 메모 (선택)", log.resolution || "");
      if (note === null) {
        return;
      }
      window.API.resolveLog(id, note).then(function () {
        showToast("해결 처리되었습니다.", "success");
        load();
      }).catch(function (error) {
        showToast(error.message || "해결 실패", "error");
      });
    } else if (action === "ignore") {
      window.API.ignoreLog(id).then(function () {
        showToast("무시 처리되었습니다.", "info");
        load();
      }).catch(function (error) {
        showToast(error.message || "무시 실패", "error");
      });
    } else if (action === "reopen") {
      window.API.request("/api/logs/" + id + "/resolve", {
        method: "POST",
        body: { resolution: null }
      }).catch(function () {});
      window.API.getLogs(filters()).then(load);
      // reopen: set open via clear? no dedicated endpoint -> re-fetch will show resolved
      showToast("다시 열기는 지원되지 않습니다. 재시도를 사용하세요.", "info");
    } else if (action === "retry") {
      window.API.retryLog(id).then(function (result) {
        showToast("재시도를 시작했습니다.", "info");
        if (window.DBExplorer && window.DBExplorer.refresh) {
          window.DBExplorer.refresh();
        }
        load();
      }).catch(function (error) {
        showToast(error.message || "재시도 실패", "error");
      });
    } else if (action === "locate-source") {
      var sourceId = parseInt(row.dataset.sourceId, 10);
      if (!sourceId) {
        showToast("연결된 소스가 없습니다.", "info");
        return;
      }
      if (window.Nav && window.Nav.switchPage) {
        window.Nav.switchPage("db");
      }
      if (window.DBExplorer && window.DBExplorer.goToSource) {
        window.DBExplorer.goToSource(sourceId, false);
      }
    } else if (action === "locate-profile") {
      var profileId = parseInt(row.dataset.profileId, 10);
      if (!profileId) {
        showToast("연결된 프로파일이 없습니다.", "info");
        return;
      }
      if (window.Nav && window.Nav.switchPage) {
        window.Nav.switchPage("profiles");
      }
      if (window.Profiles && window.Profiles.focusProfile) {
        window.Profiles.focusProfile(profileId);
      }
    } else if (action === "guide") {
      showGuide(log);
    } else if (action === "delete") {
      if (!window.confirm("이 로그를 삭제할까요?")) {
        return;
      }
      window.API.deleteLog(id).then(function () {
        showToast("삭제되었습니다.", "success");
        load();
      }).catch(function (error) {
        showToast(error.message || "삭제 실패", "error");
      });
    }
  }

  function clearResolved() {
    if (!window.confirm("해결/무시된 로그를 모두 정리할까요?")) {
      return;
    }
    window.API.clearLogs({ status: "resolved" }).then(function () {
      return window.API.clearLogs({ status: "ignored" });
    }).then(function () {
      showToast("정리되었습니다.", "success");
      load();
    }).catch(function (error) {
      showToast(error.message || "정리 실패", "error");
    });
  }

  function init() {
    var refresh = byId("logRefreshBtn");
    if (refresh) {
      refresh.addEventListener("click", load);
    }
    ["logStatusFilter", "logLevelFilter", "logScopeFilter"].forEach(function (id) {
      var el = byId(id);
      if (el) {
        el.addEventListener("change", load);
      }
    });
    var search = byId("logSearchInput");
    if (search) {
      search.addEventListener("input", function () {
        window.clearTimeout(state.timer);
        state.timer = window.setTimeout(load, 300);
      });
    }
    var unresolved = byId("logUnresolvedBtn");
    if (unresolved) {
      unresolved.addEventListener("click", function () {
        state.unresolvedOnly = !state.unresolvedOnly;
        load();
      });
    }
    var clearBtn = byId("logClearResolvedBtn");
    if (clearBtn) {
      clearBtn.addEventListener("click", clearResolved);
    }
    var list = byId("logList");
    if (list) {
      list.addEventListener("click", handleAction);
    }
    load();
  }

  window.Logs = {
    init: init,
    refresh: load,
    render: render
  };
})(window, document);
