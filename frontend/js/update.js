(function (window, document) {
  "use strict";

  function byId(id) {
    return document.getElementById(id);
  }

  function renderBanner(info) {
    var banner = byId("updateBanner");
    var text = byId("updateBannerText");
    if (!banner) {
      return;
    }
    if (info && info.available) {
      if (text) {
        text.textContent = "v" + info.latest + " (현재 v" + info.current + ")";
      }
      banner.classList.remove("hidden");
    } else {
      banner.classList.add("hidden");
    }
  }

  function setStatus(message) {
    var el = byId("updateStatusText");
    if (el) {
      el.textContent = message || "";
    }
  }

  function check() {
    setStatus("확인 중...");
    return window.API.checkUpdate().then(function (info) {
      var version = byId("appVersionText");
      if (version && info.current) {
        version.textContent = "v" + info.current;
      }
      renderBanner(info);
      if (info.error) {
        setStatus("확인 실패: " + info.error);
      } else if (info.available) {
        setStatus("새 버전 v" + info.latest + " 사용 가능");
      } else {
        setStatus("최신 버전입니다 (v" + info.current + ")");
      }
      return info;
    }).catch(function (error) {
      setStatus(error.message || "확인 실패");
    });
  }

  function install() {
    setStatus("업데이트 설치 중...");
    if (window.Toast) {
      window.Toast.show("업데이트를 설치합니다. 잠시 후 재시작됩니다.", "info");
    }
    window.API.applyUpdate().then(function (result) {
      if (!result || result.ok === false) {
        var message = (result && result.error) || "업데이트 실패";
        if (window.Toast) {
          window.Toast.show(message, "error");
        }
        setStatus(message);
      }
    }).catch(function (error) {
      var message = error.message || "업데이트 실패";
      if (window.Toast) {
        window.Toast.show(message, "error");
      }
      setStatus(message);
    });
  }

  function init() {
    var checkBtn = byId("updateCheckBtn");
    if (checkBtn) {
      checkBtn.addEventListener("click", check);
    }
    var installBtn = byId("updateInstallBtn");
    if (installBtn) {
      installBtn.addEventListener("click", install);
    }
    var dismissBtn = byId("updateDismissBtn");
    if (dismissBtn) {
      dismissBtn.addEventListener("click", function () {
        var banner = byId("updateBanner");
        if (banner) {
          banner.classList.add("hidden");
        }
      });
    }
    window.API.getVersion().then(function (data) {
      var version = byId("appVersionText");
      if (version && data.version) {
        version.textContent = "v" + data.version;
      }
    }).catch(function () {});
  }

  window.Update = { init: init, check: check, install: install, renderBanner: renderBanner };
})(window, document);
