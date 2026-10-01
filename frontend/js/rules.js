(function (window, document) {
  "use strict";

  var state = { rules: [], folders: [], loaded: false };
  var currentRule = null;

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

  function siteLabel(site) {
    var map = { twitter: "트위터", pixiv: "픽시브", tumblr: "텀블러", instagram: "인스타그램" };
    return site ? (map[site] || site) : "전체 사이트";
  }

  function ruleCard(rule) {
    var patterns = (rule.keyword_pattern || []).map(function (token) {
      return '<span class="keyword-chip">' + escapeHtml(token) + "</span>";
    }).join("");
    if (!patterns) {
      patterns = '<span class="muted">감지 키워드 없음</span>';
    }
    var target = rule.target_folder_name
      ? "📁 " + escapeHtml(rule.target_folder_name)
      : "📁 루트 경로";
    return '<article class="rule-card' + (rule.is_active ? "" : " rule-inactive") +
      '" data-id="' + rule.id + '">' +
      '<header class="rule-card-head">' +
        '<h3>' + escapeHtml(rule.name || "(이름 없음)") + "</h3>" +
        '<span class="status-badge ' + (rule.is_active ? "profile-status-normal" : "profile-status-blocked") + '">' +
          (rule.is_active ? "활성" : "비활성") + "</span>" +
      "</header>" +
      '<p class="muted profile-meta">' + escapeHtml(siteLabel(rule.site)) + "</p>" +
      '<div class="keyword-chips">' + patterns + "</div>" +
      '<p class="muted profile-foot">' + target + "</p>" +
      '<div class="card-actions">' +
        '<button class="btn btn-sm" type="button" data-action="edit">⚙️ 편집</button>' +
        '<button class="btn btn-sm danger" type="button" data-action="delete">🗑️ 삭제</button>' +
      "</div>" +
    "</article>";
  }

  function render() {
    var grid = byId("ruleGrid");
    if (grid) {
      grid.innerHTML = state.rules.map(ruleCard).join("");
    }
    var empty = byId("ruleEmpty");
    if (empty) {
      empty.classList.toggle("hidden", state.rules.length > 0);
    }
  }

  function populateFolders(selectedId) {
    var select = byId("ruleFolder");
    if (!select) {
      return;
    }
    select.innerHTML = "";
    var root = document.createElement("option");
    root.value = "";
    root.textContent = "루트 (Root)";
    select.appendChild(root);
    (state.folders || []).forEach(function (folder) {
      var option = document.createElement("option");
      option.value = String(folder.id);
      option.textContent = folder.name;
      select.appendChild(option);
    });
    select.value = selectedId === null || selectedId === undefined ? "" : String(selectedId);
  }

  function load() {
    return Promise.all([
      window.API.getReactiveRules(),
      window.API.getFolders()
    ]).then(function (results) {
      state.rules = results[0] || [];
      state.folders = results[1] || [];
      state.loaded = true;
      render();
    }).catch(function (error) {
      showToast(error.message || "반응형 규칙 로드 실패", "error");
    });
  }

  function ruleById(id) {
    for (var i = 0; i < state.rules.length; i += 1) {
      if (state.rules[i].id === id) {
        return state.rules[i];
      }
    }
    return null;
  }

  function splitKeywords(value) {
    return (value || "")
      .split(/[\n,]/)
      .map(function (token) { return token.trim(); })
      .filter(function (token) { return token.length > 0; });
  }

  function openModal(rule) {
    currentRule = rule || null;
    byId("ruleModalTitle").textContent = rule ? "반응형 규칙 편집" : "새 반응형 규칙";
    byId("ruleName").value = rule ? (rule.name || "") : "";
    byId("ruleSite").value = rule ? (rule.site || "") : "";
    byId("ruleKeywords").value = rule ? (rule.keyword_pattern || []).join("\n") : "";
    byId("ruleActive").checked = rule ? rule.is_active !== false : true;
    if (window.SiteSuggestions) {
      window.SiteSuggestions.ensure();
    }
    return window.API.getFolders().then(function (folders) {
      state.folders = folders || state.folders;
      populateFolders(rule ? rule.target_folder_id : null);
      byId("ruleModal").classList.remove("hidden");
    }).catch(function () {
      populateFolders(rule ? rule.target_folder_id : null);
      byId("ruleModal").classList.remove("hidden");
    });
  }

  function save() {
    var keywords = splitKeywords(byId("ruleKeywords").value);
    var folderValue = byId("ruleFolder").value;
    var payload = {
      name: byId("ruleName").value.trim(),
      site: byId("ruleSite").value.trim() || null,
      keyword_pattern: keywords,
      target_folder_id: folderValue === "" ? null : parseInt(folderValue, 10),
      is_active: byId("ruleActive").checked
    };
    if (!payload.name) {
      payload.name = "New Rule";
    }
    var action = currentRule
      ? window.API.updateReactiveRule(currentRule.id, payload)
      : window.API.createReactiveRule(payload);
    action.then(function () {
      byId("ruleModal").classList.add("hidden");
      showToast("반응형 규칙이 저장되었습니다.", "success");
      if (window.SiteSuggestions) {
        window.SiteSuggestions.invalidate();
      }
      load();
      if (window.Watcher && window.Watcher.refreshStatus) {
        window.Watcher.refreshStatus();
      }
    }).catch(function (error) {
      showToast(error.message || "규칙 저장 실패", "error");
    });
  }

  function handleGridAction(event) {
    var button = event.target.closest("button[data-action]");
    if (!button) {
      return;
    }
    var card = button.closest(".rule-card");
    if (!card) {
      return;
    }
    var id = parseInt(card.dataset.id, 10);
    var rule = ruleById(id);
    if (!rule) {
      return;
    }
    var action = button.dataset.action;
    if (action === "edit") {
      openModal(rule);
    } else if (action === "delete") {
      if (!window.confirm("[" + rule.name + "] 규칙을 삭제할까요?")) {
        return;
      }
      window.API.deleteReactiveRule(id).then(function () {
        showToast("삭제되었습니다.", "success");
        load();
      }).catch(function (error) {
        showToast(error.message || "삭제 실패", "error");
      });
    }
  }

  function bindDismiss() {
    var modal = byId("ruleModal");
    if (!modal) {
      return;
    }
    modal.addEventListener("mousedown", function (event) {
      if (event.target === modal) {
        modal.classList.add("hidden");
      }
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && !modal.classList.contains("hidden")) {
        modal.classList.add("hidden");
      }
    });
  }

  function init() {
    var newBtn = byId("newRuleBtn");
    if (newBtn) {
      newBtn.addEventListener("click", function () {
        openModal(null);
      });
    }
    var refreshBtn = byId("rulesRefreshBtn");
    if (refreshBtn) {
      refreshBtn.addEventListener("click", load);
    }
    var grid = byId("ruleGrid");
    if (grid) {
      grid.addEventListener("click", handleGridAction);
    }
    var saveBtn = byId("ruleSave");
    if (saveBtn) {
      saveBtn.addEventListener("click", save);
    }
    var cancelBtn = byId("ruleCancel");
    if (cancelBtn) {
      cancelBtn.addEventListener("click", function () {
        byId("ruleModal").classList.add("hidden");
      });
    }
    bindDismiss();
    load();
  }

  window.Rules = {
    init: init,
    refresh: load,
    render: render
  };
})(window, document);
