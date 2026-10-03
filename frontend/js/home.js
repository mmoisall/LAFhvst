(function (window, document) {
  "use strict";

  var state = {
    sources: [],
    selected: null,
    statusTimer: null,
    liveTimer: null,
    trendMode: "daily",
    live: null
  };

  function byId(id) {
    return document.getElementById(id);
  }

  function esc(text) {
    return String(text == null ? "" : text)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function fmtRemaining(totalSeconds) {
    var seconds = Math.max(0, Math.floor(totalSeconds || 0));
    if (seconds < 60) {
      return seconds + "초 남음";
    }
    var minutes = Math.floor(seconds / 60);
    if (minutes < 60) {
      return minutes + "분 남음";
    }
    var hours = Math.floor(minutes / 60);
    var rem = minutes % 60;
    return hours + "시간 " + rem + "분 남음";
  }

  function fmtDuration(totalSeconds) {
    var s = Math.max(0, Math.floor(totalSeconds || 0));
    var h = Math.floor(s / 3600);
    var m = Math.floor((s % 3600) / 60);
    var sec = s % 60;
    function pad(n) { return (n < 10 ? "0" : "") + n; }
    return pad(h) + ":" + pad(m) + ":" + pad(sec);
  }

  // ---------------------------------------------------------------- KDE
  function loadKde(sourceId) {
    return window.API.getSourceKde(sourceId).then(function (data) {
      state.selected = data;
      renderSummary(data);
      renderHeatmap(data);
    }).catch(function (error) {
      var summary = byId("kdeSummary");
      if (summary) {
        summary.textContent = error.message || "KDE 로드 실패";
      }
    });
  }

  function loadSources() {
    return window.API.getSources().then(function (list) {
      state.sources = list || [];
      var select = byId("kdeSourceSelect");
      if (!select) {
        return;
      }
      select.innerHTML = "";
      var scheduled = state.sources.filter(function (source) {
        return source.cyc_option === 2;
      });
      var pool = scheduled.length ? scheduled : state.sources;
      pool.forEach(function (source) {
        var option = document.createElement("option");
        option.value = String(source.id);
        option.textContent = source.name || source.url;
        select.appendChild(option);
      });
      if (pool.length) {
        select.value = String(pool[0].id);
        loadKde(pool[0].id);
      } else {
        var summary = byId("kdeSummary");
        if (summary) {
          summary.textContent = "등록된 소스가 없습니다.";
        }
        var heat = byId("kdeHeatmap");
        if (heat) {
          heat.innerHTML = "";
        }
      }
    }).catch(function () {});
  }

  function renderSummary(data) {
    var prediction = data.prediction || {};
    var summary = byId("kdeSummary");
    if (!summary) {
      return;
    }
    summary.textContent =
      "[" + data.name + "] 예측 주기 " + (data.learned_upload_cycle || 0) + "분 · " +
      (prediction.label || "-") +
      " · 다음 대기 " + (prediction.next_wait || 0) + "분" +
      " · 이력 " + (data.history_count || 0) + "건" +
      (data.cyc_option === 2 ? "" : " (지능형 모드 아님)");
  }

  function heatColor(value) {
    var a = Math.min(1, Math.max(0, value));
    var r = Math.round(17 + (249 - 17) * a);
    var g = Math.round(24 + (115 - 24) * a);
    var b = Math.round(39 + (22 - 39) * a);
    return "rgb(" + r + "," + g + "," + b + ")";
  }

  function renderHeatmap(data) {
    var container = byId("kdeHeatmap");
    if (!container) {
      return;
    }
    var grid = data.grid || [];
    var days = data.days || ["월", "화", "수", "목", "금", "토", "일"];
    var cell = 16;
    var gap = 2;
    var left = 28;
    var top = 16;
    var width = left + 24 * (cell + gap);
    var height = top + 7 * (cell + gap);
    var parts = [];
    for (var hour = 0; hour < 24; hour += 3) {
      parts.push('<text x="' + (left + hour * (cell + gap) + cell / 2) +
        '" y="' + (top - 5) + '" text-anchor="middle" class="kde-axis">' + hour + "</text>");
    }
    for (var day = 0; day < 7; day += 1) {
      parts.push('<text x="' + (left - 6) + '" y="' +
        (top + day * (cell + gap) + cell / 2 + 4) +
        '" text-anchor="end" class="kde-axis">' + days[day] + "</text>");
      for (var h = 0; h < 24; h += 1) {
        var value = (grid[day] && grid[day][h]) || 0;
        parts.push('<rect x="' + (left + h * (cell + gap)) +
          '" y="' + (top + day * (cell + gap)) +
          '" width="' + cell + '" height="' + cell + '" rx="3" fill="' + heatColor(value) +
          '"><title>' + days[day] + " " + h + "시 · " + Math.round(value * 100) + "%</title></rect>");
      }
    }
    container.innerHTML = '<svg viewBox="0 0 ' + width + " " + height +
      '" class="kde-svg" preserveAspectRatio="xMinYMin meet">' + parts.join("") + "</svg>";
  }

  function setupServerModeButton() {
    var btn = byId("serverModeBtn");
    if (!btn) {
      return;
    }
    function attach() {
      var api = window.pywebview && window.pywebview.api;
      if (api && api.hide_to_tray) {
        btn.hidden = false;
        btn.onclick = function () {
          api.hide_to_tray();
        };
      } else {
        btn.hidden = true;
      }
    }
    if (window.pywebview && window.pywebview.api) {
      attach();
    } else {
      window.addEventListener("pywebviewready", attach);
    }
  }

  // ------------------------------------------------------- Master control
  function renderMaster(live) {
    var sched = (live && live.scheduler) || {};
    var watcher = (live && live.watcher) || {};
    var schedEl = byId("masterSchedulerToggle");
    if (schedEl) {
      schedEl.checked = sched.enabled === true;
    }
    var watchEl = byId("masterWatcherToggle");
    if (watchEl) {
      watchEl.checked = watcher.running === true;
      watchEl.disabled = watcher.available === false;
      if (watcher.available === false) {
        watchEl.title = "playwright 미설치";
      } else {
        watchEl.title = "";
      }
    }
    var status = byId("masterStatus");
    if (status) {
      status.textContent =
        "스케줄러 " + (sched.enabled ? "ON" : "OFF") +
        " · 동시 " + (sched.running || 0) + "/" + (sched.maxConcurrency || 0) +
        " · 감시자 " + (watcher.running ? "ON" : "OFF") +
        (watcher.available === false ? " (미설치)" : "");
    }
  }

  // --------------------------------------------------------- Trend chart
  function renderTrend(live) {
    var trend = (live && live.trend) || {};
    var daily = trend.daily || [];
    var monthly = trend.monthly || [];
    var rows = state.trendMode === "monthly" ? monthly : daily;
    var keyField = state.trendMode === "monthly" ? "month" : "date";
    var max = 1;
    rows.forEach(function (row) { max = Math.max(max, row.count || 0); });
    var total = rows.reduce(function (acc, row) { return acc + (row.count || 0); }, 0);
    var totalEl = byId("trendTotal");
    if (totalEl) {
      totalEl.textContent = (state.trendMode === "monthly" ? "월별 최근 " : "최근 30일 ") +
        total + "건";
    }
    var chart = byId("trendChart");
    if (!chart) {
      return;
    }
    if (!rows.length) {
      chart.innerHTML = '<p class="muted">데이터 없음</p>';
      return;
    }
    chart.innerHTML = rows.map(function (row) {
      var count = row.count || 0;
      var h = count > 0 ? Math.max(4, Math.round((count / max) * 100)) : 2;
      var label = row[keyField] || "";
      return '<div class="trend-bar' + (count ? "" : " zero") +
        '" style="height:' + h + '%" title="' + esc(label) + " · " + count + '건"></div>';
    }).join("");
  }

  // -------------------------------------------------------- Live queue
  function renderQueue(live) {
    var box = byId("liveQueueList");
    if (!box) {
      return;
    }
    var queue = (live && live.queue) || [];
    var items = queue.slice();
    if (!items.length) {
      box.innerHTML = '<p class="muted">진행 중인 작업이 없습니다.</p>';
      return;
    }
    box.innerHTML = items.map(function (item) {
      return '<div class="queue-item"><span class="mini-spinner"></span>' +
        '<span class="queue-name">' + esc(item.name || item.id) + '</span>' +
        '<span class="badge">진행</span></div>';
    }).join("");
  }

  function renderNextScheduled(live) {
    var next = (live && live.next) || [];
    var first = next[0];
    var valueEl = byId("nextScheduledValue");
    var metaEl = byId("nextScheduledMeta");
    if (!valueEl) {
      return;
    }
    if (!first) {
      valueEl.textContent = "-";
      if (metaEl) {
        metaEl.textContent = "예약된 소스가 없습니다.";
      }
      return;
    }
    valueEl.textContent = fmtRemaining(first.due_in || 0);
    var running = (first.state === "running");
    if (metaEl) {
      metaEl.textContent = (first.name || "") +
        (first.label ? " · " + first.label : "") +
        (running ? " (진행 중)" : "");
    }
  }

  function renderActiveSources(live) {
    var valueEl = byId("activeSourcesValue");
    var metaEl = byId("activeSourcesMeta");
    if (!valueEl) {
      return;
    }
    valueEl.textContent = String(live.active_sources || 0);
    if (metaEl) {
      metaEl.textContent = "자동 " + (live.active_sources || 0) +
        " · 수동 " + (live.manual_sources || 0) +
        " · 전체 " + (live.total_sources || 0);
    }
  }

  function renderErrors(live) {
    var valueEl = byId("unresolvedErrorsValue");
    if (!valueEl) {
      return;
    }
    var count = live.unresolved_errors || 0;
    valueEl.textContent = String(count);
    valueEl.classList.toggle("danger", count > 0);
    var card = byId("widgetErrors");
    if (card) {
      card.classList.toggle("widget-clickable", true);
    }
  }

  function renderProfiles(live) {
    var box = byId("profileCooldownList");
    if (!box) {
      return;
    }
    var profiles = (live && live.profiles) || [];
    if (!profiles.length) {
      box.innerHTML = '<p class="muted">쿨다운 중인 프로파일 없음</p>';
      return;
    }
    box.innerHTML = profiles.map(function (p) {
      var ratio = Math.round((p.usage_ratio || 0) * 100);
      var cooling = p.cooldown_seconds > 0;
      var statusText = cooling
        ? "쿨다운 " + fmtDuration(p.cooldown_seconds)
        : (p.probe_pending ? "프로브 대기" : "정상");
      var statusClass = cooling ? "danger" : "";
      return '<div class="profile-row">' +
        '<div class="profile-top">' +
        '<span>' + esc(p.name || ("#" + p.id)) +
        (p.site ? ' <span class="muted">(' + esc(p.site) + ")</span>" : "") + "</span>" +
        '<span class="badge ' + statusClass + '">' + esc(statusText) + "</span>" +
        "</div>" +
        '<div class="progress-bar"><div class="progress-fill" style="width:' + ratio +
        '%"></div></div>' +
        '<div class="muted" style="font-size:11px;margin-top:4px">사용 ' +
        (p.recent_usage_count || 0) + " / 한도 " + (p.learned_capacity || 0) +
        (p.last_error_kind ? " · " + esc(p.last_error_kind) : "") + "</div>" +
        "</div>";
    }).join("");
  }

  function renderHotPicks(live) {
    var track = byId("hotPicksTrack");
    if (!track) {
      return;
    }
    var picks = (live && live.hot_picks) || [];
    if (!picks.length) {
      track.innerHTML = '<p class="muted">표시할 항목이 없습니다.</p>';
      return;
    }
    track.innerHTML = picks.map(function (p) {
      var img = p.thumbnail;
      return '<a class="hot-card" href="#" data-source-id="' + p.source_id + '">' +
        '<img class="hot-thumb" loading="lazy" src="' + esc(img) +
        '" alt="' + esc(p.name || "") + '" onerror="this.style.opacity=0.2">' +
        '<div class="hot-meta"><span class="hot-name">' + esc(p.name || "") + "</span>" +
        '<span class="muted">' + (p.image_count || 0) + "개</span></div></a>";
    }).join("");
  }

  function renderAlerts(live) {
    var box = byId("alertHistoryList");
    if (!box) {
      return;
    }
    var alerts = (live && live.recent_alerts) || [];
    if (!alerts.length) {
      box.innerHTML = '<p class="muted">알림 없음</p>';
      return;
    }
    box.innerHTML = alerts.map(function (a) {
      var cls = a.level === "ERROR" ? "error" : (a.status === "resolved" ? "done" : "");
      var ts = (a.last_at || "").replace("T", " ").slice(0, 19);
      return '<div class="alert-item ' + cls + '">' +
        esc(a.message || "") +
        '<span class="alert-ts">' + esc(ts) + "</span></div>";
    }).join("");
  }

  // --------------------------------------------------------- Quick links
  var DEFAULT_LINKS = [
    { label: "X (Twitter)", url: "https://x.com" },
    { label: "Pixiv", url: "https://www.pixiv.net" },
    { label: "Bluesky", url: "https://bsky.app" },
    { label: "Tumblr", url: "https://www.tumblr.com" }
  ];

  function renderQuickLinks() {
    var box = byId("quickLinksList");
    if (!box) {
      return;
    }
    var links = DEFAULT_LINKS.slice();
    state.sources.slice(0, 2).forEach(function (source) {
      if (source.url) {
        links.unshift({ label: source.name || source.url, url: source.url });
      }
    });
    box.innerHTML = links.slice(0, 5).map(function (link) {
      return '<a class="quick-link" href="' + esc(link.url) +
        '" target="_blank" rel="noopener">' + esc(link.label) +
        "<span>↗</span></a>";
    }).join("");
  }

  function setupDiscord() {
    var link = byId("discordLink");
    if (!link) {
      return;
    }
    var url = (window.AppSettings && window.AppSettings.discordUrl) || "";
    if (url) {
      link.href = url;
      link.classList.remove("disabled");
    } else {
      link.href = "https://discord.com";
    }
  }

  function renderLive(live) {
    state.live = live;
    renderMaster(live);
    renderTrend(live);
    renderQueue(live);
    renderNextScheduled(live);
    renderActiveSources(live);
    renderErrors(live);
    renderProfiles(live);
    renderHotPicks(live);
    renderAlerts(live);
  }

  function loadLive() {
    return window.API.getDashboardLive().then(function (live) {
      renderLive(live);
    }).catch(function () {});
  }

  // ------------------------------------------------------- Interactions
  function quickDownload() {
    var input = byId("quickUrlInput");
    var btn = byId("quickDownloadBtn");
    var status = byId("quickStatus");
    var url = input ? input.value.trim() : "";
    if (!url) {
      if (status) {
        status.textContent = "URL을 입력하세요.";
      }
      return;
    }
    if (btn) {
      btn.disabled = true;
    }
    if (status) {
      status.textContent = "수집 중... " + url;
    }
    window.API.quickDownload({ url: url }).then(function (result) {
      var ok = result && result.ok;
      var msg = ok
        ? "완료: " + url + (result.yaml_count ? " (YAML " + result.yaml_count + ")" : "")
        : "실패: " + url;
      if (status) {
        status.textContent = msg;
      }
      if (window.Toast) {
        window.Toast.show(msg, ok ? "success" : "error");
      }
      if (input) {
        input.value = "";
      }
    }).catch(function (error) {
      var msg = error.message || "수집 실패";
      if (status) {
        status.textContent = msg;
      }
      if (window.Toast) {
        window.Toast.show(msg, "error");
      }
    }).finally(function () {
      if (btn) {
        btn.disabled = false;
      }
    });
  }

  function bindControls() {
    var quickBtn = byId("quickDownloadBtn");
    if (quickBtn) {
      quickBtn.addEventListener("click", quickDownload);
    }
    var quickInput = byId("quickUrlInput");
    if (quickInput) {
      quickInput.addEventListener("keydown", function (event) {
        if (event.key === "Enter") {
          event.preventDefault();
          quickDownload();
        }
      });
    }
    var schedToggle = byId("masterSchedulerToggle");
    if (schedToggle) {
      schedToggle.addEventListener("change", function () {
        window.API.schedulerToggle(schedToggle.checked).then(function () {
          loadLive();
        }).catch(function (error) {
          schedToggle.checked = !schedToggle.checked;
          if (window.Toast) {
            window.Toast.show(error.message || "전환 실패", "error");
          }
        });
      });
    }
    var watchToggle = byId("masterWatcherToggle");
    if (watchToggle) {
      watchToggle.addEventListener("change", function () {
        window.API.watcherToggle(watchToggle.checked).then(function () {
          loadLive();
        }).catch(function (error) {
          watchToggle.checked = !watchToggle.checked;
          if (window.Toast) {
            window.Toast.show(error.message || "전환 실패", "error");
          }
        });
      });
    }
    var trendButtons = document.querySelectorAll("[data-trend]");
    Array.prototype.forEach.call(trendButtons, function (button) {
      button.addEventListener("click", function () {
        Array.prototype.forEach.call(trendButtons, function (b) {
          b.classList.toggle("active", b === button);
        });
        state.trendMode = button.dataset.trend === "monthly" ? "monthly" : "daily";
        if (state.live) {
          renderTrend(state.live);
        }
      });
    });
    var errCard = byId("widgetErrors");
    if (errCard) {
      function openLogs() {
        if (window.Nav && window.Nav.switchPage) {
          window.Nav.switchPage("log");
        }
      }
      errCard.addEventListener("click", openLogs);
      errCard.addEventListener("keydown", function (event) {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          openLogs();
        }
      });
    }
    var hotTrack = byId("hotPicksTrack");
    if (hotTrack) {
      hotTrack.addEventListener("click", function (event) {
        var card = event.target.closest ? event.target.closest(".hot-card") : null;
        if (!card) {
          return;
        }
        event.preventDefault();
        var sourceId = parseInt(card.dataset.sourceId, 10);
        var match = state.sources.filter(function (s) { return s.id === sourceId; })[0];
        if (match && window.Modals && window.Modals.openSource) {
          window.Modals.openSource(match);
        } else if (window.Nav) {
          window.Nav.switchPage("db");
        }
      });
    }
  }

  function init() {
    var select = byId("kdeSourceSelect");
    if (select) {
      select.addEventListener("change", function () {
        loadKde(parseInt(select.value, 10));
      });
    }
    setupServerModeButton();
    setupDiscord();
    bindControls();
    loadSources().then(renderQuickLinks);
    loadLive();
    if (!state.liveTimer) {
      state.liveTimer = window.setInterval(function () {
        if (document.hidden) {
          return;
        }
        loadLive();
      }, 4000);
    }
  }

  window.Home = {
    init: init,
    refresh: loadLive,
    reload: loadSources
  };
})(window, document);
