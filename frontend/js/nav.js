(function (window, document) {
  "use strict";

  var VIEW_IDS = {
    home: "view-home",
    db: "view-db",
    profiles: "view-profiles",
    rules: "view-rules",
    log: "view-log",
    settings: "view-settings"
  };

  function panels() {
    return document.querySelectorAll(".tab-panel");
  }

  function buttons() {
    return document.querySelectorAll(".nav-btn");
  }

  function moveBall(button) {
    var ball = document.querySelector(".nav-ball");
    if (!ball || !button) {
      return;
    }
    ball.style.width = button.offsetWidth + "px";
    ball.style.transform = "translateX(" + (button.offsetLeft - 6) + "px)";
  }

  function switchPage(name) {
    var targetId = VIEW_IDS[name];
    if (!targetId) {
      return;
    }

    Array.prototype.forEach.call(panels(), function (panel) {
      var isActive = panel.id === targetId;
      panel.classList.toggle("active", isActive);
      panel.classList.toggle("hidden", !isActive);
    });

    var activeButton = null;
    Array.prototype.forEach.call(buttons(), function (button) {
      var isActive = button.dataset.tab === name;
      button.classList.toggle("active", isActive);
      if (isActive) {
        activeButton = button;
      }
    });

    moveBall(activeButton);

    document.body.classList.toggle("db-view", name === "db");

    if (name === "home" && window.Home && window.Home.refresh) {
      window.Home.refresh();
    }
    if (name === "profiles" && window.Profiles && window.Profiles.refresh) {
      window.Profiles.refresh();
    }
    if (name === "rules" && window.Rules && window.Rules.refresh) {
      window.Rules.refresh();
    }
    if (name === "log" && window.Logs && window.Logs.refresh) {
      window.Logs.refresh();
    }
    if (name === "settings" && window.Watcher && window.Watcher.refreshStatus) {
      window.Watcher.refreshStatus();
    }
  }

  function init() {
    Array.prototype.forEach.call(buttons(), function (button) {
      button.addEventListener("click", function () {
        switchPage(button.dataset.tab);
      });
    });
    moveBall(document.querySelector(".nav-btn.active"));
  }

  window.switchPage = switchPage;
  window.Nav = {
    init: init,
    switchPage: switchPage
  };
})(window, document);
