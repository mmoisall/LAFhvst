(function () {
  "use strict";

  var plan = null;
  var tab = "folders";
  var excludeColumns = {};
  var excludeRows = {};

  function byId(id) {
    return document.getElementById(id);
  }

  function esc(value) {
    return String(value === null || value === undefined ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function request(path, body) {
    return fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {})
    }).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (data) {
        if (!response.ok) {
          throw new Error((data && data.detail) || ("요청 실패: " + response.status));
        }
        return data;
      });
    });
  }

  var FOLDER_COLS = [
    ["name", "폴더 이름"], ["parent_id", "부모(트리)"], ["log_level", "로그 레벨"],
    ["config", "config"], ["memo", "메모"]
  ];
  var SOURCE_COLS = [
    ["url", "URL"], ["site", "사이트"], ["key", "Key"], ["cyc", "주기"],
    ["log_level", "로그 레벨"], ["config", "config"], ["memo", "메모"],
    ["custom_fld", "custom_fld"], ["check_point", "check_point"], ["end_point", "end_point"]
  ];

  function excludedColumns() {
    return Object.keys(excludeColumns).filter(function (k) { return excludeColumns[k]; });
  }

  function excludedRows() {
    return Object.keys(excludeRows).filter(function (k) { return excludeRows[k]; });
  }

  function payload() {
    return {
      db_path: byId("dbPath").value.trim(),
      settings_path: byId("settingsPath").value.trim(),
      include_secrets: byId("includeSecrets").checked,
      exclude_columns: excludedColumns(),
      exclude_row_ids: excludedRows()
    };
  }

  function columnControls(kind, cols) {
    return cols.map(function (c) {
      return '<label class="col-item"><input type="checkbox" data-col="' + kind + ":" + c[0] + '"> ' +
        esc(c[1]) + " (" + esc(c[0]) + ")</label>";
    }).join("");
  }

  function bindColumnToggles() {
    document.querySelectorAll('#colFolder input[data-col], #colSource input[data-col]').forEach
      (function (box) {
        box.addEventListener("change", function () {
          excludeColumns[box.dataset.col] = box.checked;
        });
      });
  }

  function renderRowPicker() {
    var wrap = byId("rowPickList");
    if (!plan) {
      wrap.innerHTML = '<p class="muted">먼저 미리보기를 실행하세요.</p>';
      return;
    }
    var q = (byId("rowSearch").value || "").trim().toLowerCase();
    var rows = plan.sources.filter(function (row) {
      if (!q) return true;
      var hay = [row.url, row.site, row.key, row.old_id].join(" ").toLowerCase();
      return hay.indexOf(q) >= 0;
    }).slice(0, 300);
    if (!rows.length) {
      wrap.innerHTML = '<p class="muted">검색 결과 없음</p>';
      updateRowCount();
      return;
    }
    wrap.innerHTML = rows.map(function (row) {
      var key = "source:" + row.old_id;
      var checked = excludeRows[key] ? " checked" : "";
      return '<label class="row-item"><input type="checkbox" data-row="' + key + '"' + checked + "> " +
        '<span class="rid">#' + row.old_id + "</span> " + esc(row.site) + " · " + esc(row.key || row.url) + "</label>";
    }).join("");
    wrap.querySelectorAll("input[data-row]").forEach(function (box) {
      box.addEventListener("change", function () {
        excludeRows[box.dataset.row] = box.checked;
        updateRowCount();
      });
    });
    updateRowCount();
  }

  function updateRowCount() {
    byId("rowSelCount").textContent = "선택 " + excludedRows().length + "건";
  }

  function status(text, isError) {
    var el = byId("status");
    el.textContent = text || "";
    el.style.color = isError ? "#ef4444" : "#94a3b8";
  }

  function renderInspect(info) {
    byId("inspectCard").classList.remove("hidden");
    var sites = Object.keys(info.sites || {}).map(function (key) {
      return '<span class="chip">' + esc(key) + " · " + info.sites[key] + "</span>";
    }).join("");
    byId("inspectBody").innerHTML =
      '<div class="stat">폴더 <b>' + info.folders + "</b></div>" +
      '<div class="stat">소스 <b>' + info.sources + "</b></div>" +
      '<div class="stat">config 보유 <b>' + info.config_count + "</b></div>" +
      '<div class="stat">custom_fld <b>' + info.custom_field_count + "</b></div>" +
      '<div class="sites">' + (sites || '<span class="muted">없음</span>') + "</div>";
  }

  function renderPlan(result) {
    plan = result;
    byId("planCard").classList.remove("hidden");
    renderInspect(result.inspect);
    var sm = result.summary;
    byId("planSummary").innerHTML =
      '<span class="pill">폴더 신규 <b>' + sm.folders_new + "</b> / 중복 " + sm.folders_duplicate +
        " / 제외 " + (sm.folders_excluded || 0) + "</span>" +
      '<span class="pill">소스 신규 <b>' + sm.sources_new + "</b> / 중복 " + sm.sources_duplicate +
        " / 제외 " + (sm.sources_excluded || 0) + "</span>" +
      (result.include_secrets ? '<span class="pill warn">자격증명 포함</span>' : '<span class="pill">자격증명 제외</span>');
    byId("rowSearch").disabled = false;
    renderTable();
    renderRowPicker();
  }

  function renderTable() {
    if (!plan) {
      return;
    }
    var rows = tab === "folders" ? plan.folders : plan.sources;
    var thead = byId("planTable").querySelector("thead");
    var tbody = byId("planTable").querySelector("tbody");

    function state(row) {
      if (row.excluded) return "제외";
      return row.duplicate ? "중복(skip)" : "신규";
    }
    function cls(row) {
      return row.excluded ? "excl" : (row.duplicate ? "dup" : "");
    }
    if (tab === "folders") {
      thead.innerHTML = "<tr><th>old id</th><th>이름</th><th>경로</th><th>log</th><th>상태</th></tr>";
      tbody.innerHTML = rows.map(function (row) {
        var key = "folder:" + row.old_id;
        var checked = excludeRows[key] ? " checked" : "";
        return "<tr class='" + cls(row) + "'><td><input type='checkbox' data-row='" + key + "'" + checked +
          "> " + row.old_id + "</td><td>" + esc(row.name) + "</td><td class='path'>" + esc(row.path) +
          "</td><td>" + esc(row.log_level || "") + "</td><td>" + state(row) + "</td></tr>";
      }).join("");
    } else {
      thead.innerHTML = "<tr><th>제외</th><th>old id</th><th>site</th><th>key</th><th>주기</th><th>config</th><th>상태</th></tr>";
      tbody.innerHTML = rows.map(function (row) {
        var key = "source:" + row.old_id;
        var checked = excludeRows[key] ? " checked" : "";
        return "<tr class='" + cls(row) + "'><td><input type='checkbox' data-row='" + key + "'" + checked +
          "></td><td>" + row.old_id + "</td><td>" + esc(row.site) + "</td><td>" + esc(row.key) +
          "</td><td>" + esc(row.cyc_hours) + "h</td><td>" + (row.has_config ? "있음" : "-") +
          "</td><td>" + state(row) + "</td></tr>";
      }).join("");
    }
    tbody.querySelectorAll("input[data-row]").forEach(function (box) {
      box.addEventListener("change", function () {
        excludeRows[box.dataset.row] = box.checked;
        updateRowCount();
        renderRowPicker();
      });
    });
  }

  function renderReport(report) {
    byId("reportCard").classList.remove("hidden");
    byId("reportBody").innerHTML =
      '<div class="stat">폴더 생성 <b>' + report.folders_created + "</b></div>" +
      '<div class="stat">폴더 스킵 <b>' + report.folders_skipped + "</b></div>" +
      '<div class="stat">소스 생성 <b>' + report.sources_created + "</b></div>" +
      '<div class="stat">소스 스킵 <b>' + report.sources_skipped + "</b></div>" +
      (report.dry_run ? '<div class="stat warn">드라이런</div>' : "");
    var warn = (report.warnings || []).map(function (text) {
      return '<p class="muted">⚠ ' + esc(text) + "</p>";
    }).join("");
    byId("warnings").innerHTML = warn;
  }

  function preview() {
    status("미리보기 중...");
    request("/api/preview", payload()).then(function (result) {
      renderPlan(result);
      status("미리보기 완료. 신규 " + result.summary.sources_new + "개 소스 / " +
        result.summary.folders_new + "개 폴더를 추가할 수 있습니다.");
    }).catch(function (error) {
      status(error.message, true);
    });
  }

  function apply() {
    if (!window.confirm("현재 DB에 자료를 추가합니다. 진행할까요?")) {
      return;
    }
    status("이관 중...");
    request("/api/migrate", payload()).then(function (report) {
      renderReport(report);
      status("이관 완료: 소스 " + report.sources_created + "개, 폴더 " + report.folders_created + "개 추가.");
    }).catch(function (error) {
      status(error.message, true);
    });
  }

  function init() {
    byId("colFolder").innerHTML =
      '<p class="col-group-title">폴더</p>' + columnControls("folder", FOLDER_COLS);
    byId("colSource").innerHTML =
      '<p class="col-group-title">소스</p>' + columnControls("source", SOURCE_COLS);
    bindColumnToggles();
    fetch("/api/defaults").then(function (r) { return r.json(); }).then(function (data) {
      byId("dbPath").value = data.db_path || "";
      byId("settingsPath").value = data.settings_path || "";
    }).catch(function () {});
    byId("rowSearch").addEventListener("input", renderRowPicker);
    byId("previewBtn").addEventListener("click", preview);
    byId("applyBtn").addEventListener("click", apply);
    byId("tabFolders").addEventListener("click", function () {
      tab = "folders";
      byId("tabFolders").classList.add("active");
      byId("tabSources").classList.remove("active");
      renderTable();
    });
    byId("tabSources").addEventListener("click", function () {
      tab = "sources";
      byId("tabSources").classList.add("active");
      byId("tabFolders").classList.remove("active");
      renderTable();
    });
  }

  document.addEventListener("DOMContentLoaded", init);
})();
