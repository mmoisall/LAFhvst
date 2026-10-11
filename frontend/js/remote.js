(function (window, document) {
  "use strict";

  var state = { status: null, refreshTimer: null, countdownTimer: null };

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
      return "없음";
    }
    var date = new Date(value);
    if (isNaN(date.getTime())) {
      return "없음";
    }
    return date.toLocaleString();
  }

  function toast(message, kind) {
    if (window.Toast && window.Toast.show) {
      window.Toast.show(message, kind || "info");
    }
  }

  function isRemoteDevice() {
    return Boolean(state.status && state.status.device);
  }

  function renderDevices(devices) {
    var host = byId("remoteDeviceList");
    if (!host) {
      return;
    }
    if (!devices || !devices.length) {
      host.innerHTML = '<p class="muted">등록된 기기가 없습니다.</p>';
      return;
    }
    host.innerHTML = devices.map(function (device) {
      var revoke = isRemoteDevice()
        ? ""
        : '<button class="btn danger" type="button" data-revoke="' + device.id + '">회수</button>';
      return '<div class="remote-device">' +
        '<div><b>' + escapeHtml(device.name) + '</b>' +
        '<span class="muted"> · ' + escapeHtml(device.role) + '</span></div>' +
        '<div class="muted">마지막 접속 ' + escapeHtml(fmtTime(device.last_seen_at)) +
        (device.last_ip ? ' · ' + escapeHtml(device.last_ip) : "") + '</div>' +
        revoke +
        '</div>';
    }).join("");
    Array.prototype.forEach.call(host.querySelectorAll("[data-revoke]"), function (button) {
      button.addEventListener("click", function () {
        revokeDevice(button.getAttribute("data-revoke"));
      });
    });
  }

  function renderCountdown() {
    var el = byId("remotePairMeta");
    if (!el) {
      return;
    }
    var pair = state.status && state.status.pair;
    if (!pair) {
      el.textContent = "활성 페어링 링크 없음";
      return;
    }
    var left = Math.max(0, Math.round(pair.expires_at - Date.now() / 1000));
    if (left <= 0) {
      el.textContent = "페어링 링크 만료됨 · QR 재발급 필요";
      return;
    }
    var minutes = Math.floor(left / 60);
    var seconds = left % 60;
    el.textContent = "링크 유효 시간 " + minutes + ":" + (seconds < 10 ? "0" : "") + seconds;
  }

  function render(status) {
    state.status = status;
    var remote = isRemoteDevice();
    document.body.classList.toggle("remote-mode", remote);

    var stateText = byId("remoteStateText");
    var toggleBtn = byId("remoteToggleBtn");
    var qr = byId("remoteQrImg");
    var linkBox = byId("remoteLinkText");
    var urlText = byId("remoteUrlText");
    var relayInput = byId("remoteRelayUrlInput");
    var actions = byId("remoteLocalActions");

    if (relayInput && document.activeElement !== relayInput) {
      relayInput.value = status.relay_url || "";
    }
    if (actions) {
      actions.classList.toggle("hidden", remote);
    }
    if (stateText) {
      if (remote) {
        stateText.textContent = "원격 기기로 접속 중입니다. 이 화면에서는 조회·실행·정지만 가능합니다.";
      } else if (!status.enabled) {
        stateText.textContent = "원격 접속이 꺼져 있습니다.";
      } else if (status.running) {
        stateText.textContent = "원격 접속 켜짐 · 전송: " + (status.transport || "-") +
          (status.error ? " · 오류: " + status.error : "");
      } else {
        stateText.textContent = "원격 접속이 켜져 있지만 전송 연결이 없습니다." +
          (status.error ? " · " + status.error : "");
      }
    }
    if (toggleBtn) {
      toggleBtn.textContent = status.enabled ? "원격 접속 끄기" : "원격 접속 켜기";
      toggleBtn.classList.toggle("danger", Boolean(status.enabled));
      toggleBtn.classList.toggle("primary", !status.enabled);
    }
    if (urlText) {
      urlText.textContent = status.url || status.public_url || "-";
    }
    if (linkBox) {
      linkBox.value = status.pair ? status.pair.link : "";
    }
    if (qr) {
      if (status.pair) {
        qr.src = window.API.remoteQrUrl();
        qr.classList.remove("hidden");
      } else {
        qr.removeAttribute("src");
        qr.classList.add("hidden");
      }
    }
    renderCountdown();
    renderDevices(status.devices || []);
  }

  var REQUEST_TIMEOUT_MS = 8000;

  function withTimeout(promise, label) {
    // 요청이 영구 대기(차단/프록시/서버 무응답)하면 사용자에게 이유를 보여준다.
    return new Promise(function (resolve, reject) {
      var timer = window.setTimeout(function () {
        reject(new Error((label || "요청") + " 응답 없음(차단되었거나 서버가 응답하지 않습니다)"));
      }, REQUEST_TIMEOUT_MS);
      promise.then(function (value) {
        window.clearTimeout(timer);
        resolve(value);
      }, function (error) {
        window.clearTimeout(timer);
        reject(error);
      });
    });
  }

  function setStateText(text) {
    var el = byId("remoteStateText");
    if (el) {
      el.textContent = text;
    }
  }

  function refresh() {
    return withTimeout(window.API.remoteStatus(), "상태 조회")
      .then(render)
      .catch(function (error) {
        setStateText(error.message || "원격 접속 상태를 불러오지 못했습니다.");
      });
  }

  function applyResult(payload) {
    return refresh().then(function () {
      if (payload && payload.pair) {
        toast("페어링 링크가 준비되었습니다. QR을 스캔하세요.", "success");
      }
    });
  }

  function saveRelayUrl() {
    var input = byId("remoteRelayUrlInput");
    if (!input) {
      return Promise.resolve();
    }
    return window.API.saveSettings({ remoteRelayUrl: input.value.trim() });
  }

  function toggleRemote() {
    var enabled = Boolean(state.status && state.status.enabled);
    setStateText(enabled ? "원격 접속을 끄는 중…" : "원격 접속을 켜는 중…");
    var action = enabled
      ? withTimeout(window.API.remoteDisable(), "원격 접속 끄기")
      : withTimeout(saveRelayUrl(), "설정 저장").then(function () {
          return withTimeout(window.API.remoteEnable(), "원격 접속 켜기");
        });
    action.then(function (payload) {
      return applyResult(payload);
    }).catch(function (error) {
      setStateText(error.message || "원격 접속 설정 실패");
      toast(error.message || "원격 접속 설정 실패", "error");
    });
  }

  function rotate() {
    withTimeout(window.API.remoteRotate(), "QR 재발급").then(function (payload) {
      return applyResult(payload);
    }).catch(function (error) {
      setStateText(error.message || "QR 재발급 실패");
      toast(error.message || "QR 재발급 실패", "error");
    });
  }

  function copyLink() {
    var pair = state.status && state.status.pair;
    if (!pair) {
      toast("활성 페어링 링크가 없습니다.", "error");
      return;
    }
    var text = pair.link;
    if (window.navigator.clipboard && window.navigator.clipboard.writeText) {
      window.navigator.clipboard.writeText(text).then(function () {
        toast("페어링 링크를 복사했습니다.", "success");
      }).catch(function () {
        window.prompt("페어링 링크", text);
      });
      return;
    }
    window.prompt("페어링 링크", text);
  }

  function revokeDevice(id) {
    if (!window.confirm("이 기기의 원격 접속을 회수할까요?")) {
      return;
    }
    withTimeout(window.API.remoteRevokeDevice(id), "기기 회수").then(function () {
      toast("기기를 회수했습니다.", "success");
      refresh();
    }).catch(function (error) {
      toast(error.message || "기기 회수 실패", "error");
    });
  }

  function settingsVisible() {
    var panel = byId("view-settings");
    return Boolean(panel && panel.classList.contains("active"));
  }

  function init() {
    var toggleBtn = byId("remoteToggleBtn");
    if (toggleBtn) {
      toggleBtn.addEventListener("click", toggleRemote);
    }
    var rotateBtn = byId("remoteRotateBtn");
    if (rotateBtn) {
      rotateBtn.addEventListener("click", rotate);
    }
    var copyBtn = byId("remoteCopyLinkBtn");
    if (copyBtn) {
      copyBtn.addEventListener("click", copyLink);
    }
    refresh();
    state.countdownTimer = window.setInterval(renderCountdown, 1000);
    state.refreshTimer = window.setInterval(function () {
      if (settingsVisible()) {
        refresh();
      }
    }, 20000);
  }

  window.RemotePanel = {
    init: init,
    refresh: refresh
  };
})(window, document);
