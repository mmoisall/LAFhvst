(function (window, document) {
  "use strict";

  var state = { lastSeq: 0, primed: false, statusTimer: null, busy: false };

  function byId(id) {
    return document.getElementById(id);
  }

  function showToast(message, type) {
    if (window.Toast) {
      window.Toast.show(message, type);
    }
  }

  function siteLabel(event) {
    return event.site_label || event.site || "알림";
  }

  function renderStatus(status) {
    var text = byId("watcherStateText");
    if (!text) {
      return;
    }
    if (!status) {
      text.textContent = "상태 확인 실패";
      return;
    }
    if (!status.available) {
      text.textContent = "Playwright 미설치 — pip install playwright && playwright install chromium";
      return;
    }
    if (!status.running) {
      text.textContent = "중지됨";
      return;
    }
    var mode = status.headless ? "백그라운드(Headless)" : "UI 표시 모드";
    var sites = (status.sites || []).join(", ") || "지정 없음";
    text.textContent = "실행 중 · " + mode + " · 감시: " + sites;
  }

  function refreshStatus() {
    return window.API.getWatcherStatus().then(function (status) {
      state.status = status;
      renderStatus(status);
      return window.API.getSettings();
    }).then(function (settings) {
      var auto = byId("watcherAutoStartToggle");
      if (auto && settings && document.activeElement !== auto) {
        auto.checked = Boolean(settings.watcherAutoStart);
      }
    }).catch(function () {
      renderStatus(null);
    });
  }

  function handleEvent(event) {
    if (!event) {
      return;
    }
    if (event.status === "detected") {
      showToast(
        "🔔 [" + siteLabel(event) + "] '" + (event.keyword || "") + "' 키워드 알림 감지! 즉시 수집 시작",
        "success"
      );
    } else if (event.status === "error" && event.rule_id) {
      showToast(
        "⚠️ [" + siteLabel(event) + "] '" + (event.keyword || "") + "' 수집 실패: " + (event.message || ""),
        "error"
      );
    }
  }

  function pollEvents() {
    return window.API.getWatcherEvents(state.lastSeq).then(function (result) {
      var events = (result && result.events) || [];
      if (result && typeof result.last === "number") {
        state.lastSeq = result.last;
      } else if (events.length) {
        state.lastSeq = events[events.length - 1].seq;
      }
      if (!state.primed) {
        state.primed = true;
        return;
      }
      events.forEach(handleEvent);
    }).catch(function () {});
  }

  function setBusy(busy) {
    state.busy = busy;
    ["watcherOpenBtn", "watcherHeadlessBtn", "watcherStopBtn"].forEach(function (id) {
      var btn = byId(id);
      if (btn) {
        btn.disabled = busy;
      }
    });
  }

  function toggle(payload, message, type) {
    if (state.busy) {
      return Promise.resolve();
    }
    setBusy(true);
    return window.API.toggleWatcher(payload).then(function (result) {
      showToast(message, type || "success");
      return refreshStatus();
    }).catch(function (error) {
      var text = error.message || "감시 브라우저 제어 실패";
      showToast(text, "error");
      var hint = byId("watcherHint");
      if (hint && text.indexOf("playwright") >= 0) {
        hint.textContent = "Playwright 미설치: pip install playwright && playwright install chromium";
      }
    }).then(function () {
      setBusy(false);
    });
  }

  function openHeadful() {
    return toggle(
      { enabled: true, headless: false },
      "감시 브라우저를 UI 표시 모드로 열었습니다. 로그인/캡차를 완료하세요.",
      "success"
    );
  }

  function goHeadless() {
    return toggle(
      { enabled: true, headless: true },
      "백그라운드(Headless) 감시로 전환했습니다.",
      "success"
    );
  }

  function stopWatcher() {
    return toggle({ enabled: false }, "감시 브라우저를 중지했습니다.", "info");
  }

  function saveAutoStart() {
    var auto = byId("watcherAutoStartToggle");
    if (!auto) {
      return;
    }
    window.API.saveSettings({ watcherAutoStart: auto.checked }).catch(function () {});
  }

  function init() {
    var openBtn = byId("watcherOpenBtn");
    if (openBtn) {
      openBtn.addEventListener("click", openHeadful);
    }
    var headlessBtn = byId("watcherHeadlessBtn");
    if (headlessBtn) {
      headlessBtn.addEventListener("click", goHeadless);
    }
    var stopBtn = byId("watcherStopBtn");
    if (stopBtn) {
      stopBtn.addEventListener("click", stopWatcher);
    }
    var auto = byId("watcherAutoStartToggle");
    if (auto) {
      auto.addEventListener("change", saveAutoStart);
    }
    refreshStatus();
    pollEvents();
    if (!state.statusTimer) {
      state.statusTimer = window.setInterval(function () {
        pollEvents();
        refreshStatus();
      }, 4000);
    }
  }

  window.Watcher = {
    init: init,
    refreshStatus: refreshStatus,
    poll: pollEvents
  };
})(window, document);
