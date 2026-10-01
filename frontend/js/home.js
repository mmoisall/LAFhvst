(function (window, document) {
  "use strict";

  var state = { sources: [], selected: null, statusTimer: null };

  function byId(id) {
    return document.getElementById(id);
  }

  function fmtRemaining(totalSeconds) {
    var seconds = Math.max(0, Math.floor(totalSeconds || 0));
    if (seconds < 60) {
      return seconds + "초 후";
    }
    var minutes = Math.round(seconds / 60);
    if (minutes < 60) {
      return minutes + "분 후";
    }
    return (minutes / 60).toFixed(1) + "시간 후";
  }

  function loadStatus() {
    return window.API.getSchedulerStatus().then(function (status) {
      var stateEl = byId("schedulerState");
      if (stateEl) {
        stateEl.textContent = (status.enabled ? "켜짐" : "꺼짐") +
          " · 관찰 " + ((status.upcoming && status.upcoming.length) || 0) + "개";
      }
      var conc = byId("concurrencyValue");
      if (conc) {
        conc.textContent = (status.running || 0) + " / " + (status.maxConcurrency || 0);
      }
      var next = byId("nextRunValue");
      if (next) {
        var item = (status.upcoming || [])[0];
        next.textContent = item ? (item.name + " · " + fmtRemaining(item.due_in)) : "-";
      }
    }).catch(function () {});
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

  function init() {
    var select = byId("kdeSourceSelect");
    if (select) {
      select.addEventListener("change", function () {
        loadKde(parseInt(select.value, 10));
      });
    }
    loadSources();
    loadStatus();
    if (!state.statusTimer) {
      state.statusTimer = window.setInterval(loadStatus, 5000);
    }
  }

  window.Home = {
    init: init,
    refresh: loadStatus,
    reload: loadSources
  };
})(window, document);
