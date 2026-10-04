(function (window, document) {
  "use strict";

  var MIN_COL_WIDTH = 48;

  function detectTouchMode() {
    try {
      if ((navigator.maxTouchPoints || 0) > 0) {
        return true;
      }
      if ("ontouchstart" in window) {
        return true;
      }
      if (window.matchMedia && window.matchMedia("(any-pointer: coarse)").matches) {
        return true;
      }
    } catch (error) {
      /* noop */
    }
    return false;
  }

  var touchMode = detectTouchMode();
  var touchUpgradeBound = false;
  var lastPointerType = "mouse";

  function upgradeTouchMode() {
    if (touchMode) {
      return;
    }
    touchMode = true;
    if (sortableMain) {
      sortableMain.option("delay", 400);
      sortableMain.option("delayOnTouchOnly", true);
    }
    if (headerSortable && !headerDragging) {
      try {
        headerSortable.destroy();
      } catch (error) {
        /* noop */
      }
      headerSortable = null;
    }
  }

  function bindTouchUpgrade() {
    if (touchUpgradeBound) {
      return;
    }
    touchUpgradeBound = true;
    document.addEventListener("touchstart", upgradeTouchMode, { passive: true, capture: true });
    document.addEventListener("pointerdown", function (event) {
      lastPointerType = event.pointerType || "mouse";
      if (event.pointerType === "touch") {
        upgradeTouchMode();
      }
    }, true);

    function resetDragState() {
      if (!dragActive) {
        return;
      }
      dragActive = false;
      document.body.classList.remove("drag-active");
      clearHighlight();
      flushPending();
    }
    document.addEventListener("pointercancel", resetDragState, true);
    document.addEventListener("touchcancel", resetDragState, { passive: true, capture: true });
  }

  var COLUMNS = [
    { key: "id", label: "ID", className: "col-id", sortable: true, width: 64 },
    { key: "name", label: "이름 / URL", className: "col-name", sortable: true, width: 260 },
    { key: "site", label: "Site", className: "col-site", sortable: true, width: 120 },
    { key: "key", label: "Key / Count", className: "col-key", sortable: true, width: 150 },
    { key: "file_count", label: "파일 수", className: "col-filecount", sortable: true, width: 80 },
    { key: "cyc", label: "주기", className: "col-cyc", sortable: true, width: 80 },
    { key: "recent_file_mtime", label: "최근 파일", className: "col-file", sortable: true, width: 158 },
    { key: "recent_run_at", label: "최근 실행", className: "col-run", sortable: true, width: 158 },
    { key: "oldest_err_at", label: "최초 에러", className: "col-err", sortable: true, width: 158 },
    { key: "tags", label: "태그", className: "col-tags", sortable: true, width: 190 },
    { key: "status", label: "상태", className: "col-status", sortable: true, width: 96 }
  ];

  var LS = {
    sortCol: "lafhvst.sort.col",
    sortDir: "lafhvst.sort.dir",
    keepTop: "lafhvst.keepFoldersTop",
    colOrder: "lafhvst.col.order",
    colVis: "lafhvst.col.vis",
    colWidths: "lafhvst.col.widths",
    view: "lafhvst.db.view",
    manual: "lafhvst.db.manual",
    lastFolder: "lafhvst.db.lastFolder"
  };

  var VIEW_MODES = ["table", "grid", "compact"];

  var DEFAULT_ORDER = COLUMNS.map(function (column) { return column.key; });

  function read(key) {
    try {
      return window.localStorage.getItem(key);
    } catch (error) {
      return null;
    }
  }

  function write(key, value) {
    try {
      window.localStorage.setItem(key, value);
    } catch (error) {
      return null;
    }
  }

  function readJson(key, fallback) {
    var raw = read(key);
    if (!raw) {
      return fallback;
    }
    try {
      return JSON.parse(raw);
    } catch (error) {
      return fallback;
    }
  }

  var state = {
    folderId: null,
    parentId: null,
    breadcrumb: [],
    folders: [],
    sources: [],
    selection: {},
    selectionMode: false,
    sortCol: read(LS.sortCol) || "name",
    sortDir: read(LS.sortDir) === "desc" ? "desc" : "asc",
    keepTop: read(LS.keepTop) !== "false",
    view: VIEW_MODES.indexOf(read(LS.view)) >= 0 ? read(LS.view) : "table",
    manualOrder: read(LS.manual) === "true",
    dragGroup: null,
    dragItem: null,
    colOrder: DEFAULT_ORDER.slice(),
    colVis: {},
    colWidths: {}
  };

  COLUMNS.forEach(function (column) { state.colWidths[column.key] = column.width; });

  (function restoreWidths() {
    var saved = readJson(LS.colWidths, null);
    if (saved && typeof saved === "object") {
      Object.keys(state.colWidths).forEach(function (key) {
        var value = Number(saved[key]);
        if (!isNaN(value) && value >= MIN_COL_WIDTH) {
          state.colWidths[key] = Math.round(value);
        }
      });
    }
  })();

  COLUMNS.forEach(function (column) { state.colVis[column.key] = true; });

  (function restoreColumns() {
    var savedOrder = readJson(LS.colOrder, null);
    if (Array.isArray(savedOrder)) {
      var valid = savedOrder.filter(function (key) {
        return DEFAULT_ORDER.indexOf(key) >= 0;
      });
      DEFAULT_ORDER.forEach(function (key) {
        if (valid.indexOf(key) < 0) {
          valid.push(key);
        }
      });
      state.colOrder = valid;
    }
    var savedVis = readJson(LS.colVis, null);
    if (savedVis && typeof savedVis === "object") {
      Object.keys(state.colVis).forEach(function (key) {
        if (typeof savedVis[key] === "boolean") {
          state.colVis[key] = savedVis[key];
        }
      });
    }
  })();

  function byId(id) {
    return document.getElementById(id);
  }

  function columnDef(key) {
    for (var i = 0; i < COLUMNS.length; i += 1) {
      if (COLUMNS[i].key === key) {
        return COLUMNS[i];
      }
    }
    return null;
  }

  var SITE_STYLES = {
    twitter: { label: "X", color: "#1d9bf0" },
    pixiv: { label: "P", color: "#0096fa" },
    bluesky: { label: "B", color: "#0a7aff" },
    tumblr: { label: "T", color: "#3a4a63" },
    instagram: { label: "IG", color: "#c13584" },
    naver: { label: "N", color: "#03c75a" },
    naverwebtoon: { label: "NW", color: "#00b34a" },
    deviantart: { label: "DA", color: "#05cc47" },
    artstation: { label: "AS", color: "#13aff0" },
    furaffinity: { label: "FA", color: "#ff8f00" },
    e621: { label: "E6", color: "#152f56" },
    danbooru: { label: "DB", color: "#00659e" },
    gelbooru: { label: "GB", color: "#a9825f" },
    sankaku: { label: "SB", color: "#1f6feb" },
    baraag: { label: "BA", color: "#6b4fbb" },
    kemono: { label: "K", color: "#f96854" },
    "the-collection": { label: "TC", color: "#e91e63" },
    rule34: { label: "R34", color: "#8bc34a" }
  };

  function siteChipHtml(site, extraClass) {
    var name = String(site || "").trim();
    if (!name) {
      return '<span class="type-icon" title="사이트 미상">🔗</span>';
    }
    var meta = SITE_STYLES[name.toLowerCase()];
    if (!meta) {
      var letters = name.replace(/[^a-z0-9]/gi, "").slice(0, 2).toUpperCase() || "?";
      var hue = 0;
      for (var i = 0; i < name.length; i += 1) {
        hue = (hue * 31 + name.charCodeAt(i)) % 360;
      }
      meta = { label: letters, color: "hsl(" + hue + ", 55%, 42%)" };
    }
    return '<span class="site-chip' + (extraClass ? " " + extraClass : "") +
      '" style="--site-color:' + meta.color + '" title="' + escapeHtml(name) + '">' +
      escapeHtml(meta.label) + "</span>";
  }

  function escapeHtml(value) {
    return String(value === null || value === undefined ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function fmtDate(value) {
    if (!value) {
      return "-";
    }
    var date = new Date(value);
    if (isNaN(date.getTime())) {
      return "-";
    }
    return date.toLocaleString();
  }

  function cycleLabel(item) {
    if (item.cyc_option === 0) {
      return "수동";
    }
    if (item.cyc_option === 2) {
      return "AI";
    }
    return (item.cyc || 1) + "h";
  }

  var STATUS_LABELS = {
    idle: "대기",
    running: "수집중",
    done: "완료",
    error: "오류"
  };

  function itemKey(item) {
    return item.type + ":" + item.id;
  }

  function selectionSize() {
    return Object.keys(state.selection).length;
  }

  function selectionList() {
    return Object.keys(state.selection).map(function (key) {
      return state.selection[key];
    });
  }

  function saveSortPrefs() {
    write(LS.sortCol, state.sortCol);
    write(LS.sortDir, state.sortDir);
    write(LS.keepTop, state.keepTop ? "true" : "false");
  }

  function visibleItems() {
    var query = (byId("dbSearchInput").value || "").trim().toLowerCase();
    var merged = state.folders.concat(state.sources);
    if (query) {
      merged = merged.filter(function (item) {
        var haystack = [
          item.name,
          item.url,
          item.site,
          item.key,
          item.memo,
          (item.effective_tag_list || []).join(" ")
        ].join(" ").toLowerCase();
        return haystack.indexOf(query) >= 0;
      });
    }
    merged.sort(state.manualOrder ? compareManual : compareItems);
    return merged;
  }

  function compareManual(a, b) {
    if (a.type !== b.type) {
      return a.type === "folder" ? -1 : 1;
    }
    var ao = a.order_index === null || a.order_index === undefined ? Infinity : a.order_index;
    var bo = b.order_index === null || b.order_index === undefined ? Infinity : b.order_index;
    if (ao !== bo) {
      return ao - bo;
    }
    return a.id - b.id;
  }

  function sortValue(item, key) {
    switch (key) {
      case "id":
        return Number(item.id || 0);
      case "name":
        return String(item.name || item.url || "").toLowerCase();
      case "site":
        return String(item.site || "").toLowerCase();
      case "key":
        if (item.type === "folder") {
          return (item.sub_folder_count || 0) * 1000 + (item.item_count || 0);
        }
        return String(item.key || "").toLowerCase();
      case "file_count":
        return Number(item.file_count || 0);
      case "cyc":
        return Number(item.cyc || 0);
      case "recent_file_mtime":
        return item.recent_file_mtime ? Date.parse(item.recent_file_mtime) : 0;
      case "recent_run_at":
        return (item.last_attempt_at || item.recent_run_at)
          ? Date.parse(item.last_attempt_at || item.recent_run_at) : 0;
      case "oldest_err_at":
        return item.oldest_err_at ? Date.parse(item.oldest_err_at) : 0;
      case "tags":
        return (item.effective_tag_list || []).join(",").toLowerCase();
      case "status":
        return String(item.status || "").toLowerCase();
      default:
        return "";
    }
  }

  function compareItems(a, b) {
    if (state.keepTop && a.type !== b.type) {
      return a.type === "folder" ? -1 : 1;
    }
    var av = sortValue(a, state.sortCol);
    var bv = sortValue(b, state.sortCol);
    var result;
    if (typeof av === "number" && typeof bv === "number") {
      result = av - bv;
    } else {
      result = String(av) < String(bv) ? -1 : (String(av) > String(bv) ? 1 : 0);
    }
    if (result === 0) {
      result = a.type === b.type ? (a.id - b.id) : (a.type === "folder" ? -1 : 1);
    }
    return state.sortDir === "asc" ? result : -result;
  }

  function inheritedFlag(item, field) {
    return Boolean(item && item.inherited && item.inherited[field]);
  }

  function overriddenFlag(item, field) {
    return Boolean(item && item.overridden && item.overridden[field] && !inheritedFlag(item, field));
  }

  function inheritedSpan(text, inherited, title, overridden) {
    var cls = inherited ? ' class="is-inherited"' : (overridden ? ' class="is-overridden"' : "");
    var badge = inherited
      ? '<span class="inherited-badge" title="' + escapeHtml(title || "부모에게서 상속됨") + '">상속</span>'
      : (overridden ? '<span class="overridden-badge" title="직접 설정한 값">자체</span>' : "");
    return '<span' + cls + '>' + escapeHtml(text) + "</span>" + badge;
  }

  function tagChips(item) {
    var tags = item.effective_tags || [];
    if (!tags.length) {
      return "";
    }
    return tags.map(function (tag) {
      var classes = "tag-chip" + (tag.inherited ? " inherited-active" : "");
      var title = tag.inherited ? "상속된 태그 (" + tag.scope + ")" : "자체 태그";
      return '<span class="' + classes + '" data-tag="' + escapeHtml(tag.name) + '" title="' +
        title + '">' + escapeHtml(tag.name) + "</span>";
    }).join("");
  }

  function cellHtml(item, key) {
    switch (key) {
      case "id":
        return '<td class="col-id">' + escapeHtml(item.id) + "</td>";
      case "name": {
        if (item.type === "folder") {
          var count = "📁 " + (item.sub_folder_count || 0) + " · 🔗 " + (item.item_count || 0);
          return '<td class="col-name"><div class="cell-name"><span class="type-icon">📁</span>' +
            '<span class="name-text folder-name">' + escapeHtml(item.name) +
            '</span><span class="count-badge">' + count + "</span></div></td>";
        }
        var label = item.name || item.url || "(이름 없음)";
        return '<td class="col-name"><div class="cell-name">' + siteChipHtml(item.site) +
          '<a class="source-link" href="' + escapeHtml(item.url) +
          '" target="_blank" rel="noreferrer">' + escapeHtml(label) + "</a></div></td>";
      }
      case "site":
        return "<td class=\"col-site\">" + escapeHtml(item.type === "folder" ? "-" : (item.site || "-")) + "</td>";
      case "key":
        if (item.type === "folder") {
          return "<td class=\"col-key\">-</td>";
        }
        return "<td class=\"col-key\">" + escapeHtml(item.key || "-") + "</td>";
      case "file_count":
        if (item.type === "folder") {
          return "<td class=\"col-filecount\">-</td>";
        }
        return '<td class="col-filecount">' + escapeHtml(item.file_count || 0) + "</td>";
      case "cyc": {
        var cycleInherited = inheritedFlag(item, "cyc");
        var cycleOverridden = overriddenFlag(item, "cyc");
        var cycleText = inheritedSpan(cycleLabel(item), cycleInherited, "부모 폴더에게서 상속된 주기", cycleOverridden);
        var ai = "";
        if (item.type === "source" && item.ai_state && item.ai_state.label) {
          var density = Number(item.ai_state.relative_density || 0);
          var aiClass = density >= 0.6 ? "ai-hot" : (density <= 0.2 ? "ai-cold" : "ai-mid");
          ai = ' <span class="ai-badge ' + aiClass + '" title="KDE 업로드 확률 예측">' +
            escapeHtml(item.ai_state.label) + "</span>";
        }
        return '<td class="col-cyc">' + cycleText + ai + "</td>";
      }
      case "recent_file_mtime":
        return "<td class=\"col-file\">" + escapeHtml(item.type === "source" ? fmtDate(item.recent_file_mtime) : "-") + "</td>";
      case "recent_run_at":
        return "<td class=\"col-run\">" + escapeHtml(item.type === "source" ? fmtDate(item.last_attempt_at || item.recent_run_at) : "-") + "</td>";
      case "oldest_err_at":
        return "<td class=\"col-err\">" + escapeHtml(item.type === "source" ? fmtDate(item.oldest_err_at) : "-") + "</td>";
      case "tags":
        return '<td class="col-tags"><div class="tag-container">' + tagChips(item) + "</div></td>";
      case "status": {
        if (item.type === "folder") {
          return '<td class="col-status">-</td>';
        }
        var status = item.status || "idle";
        var text = STATUS_LABELS[status] || status;
        return '<td class="col-status"><span class="status-badge status-' + escapeHtml(status) +
          '">' + escapeHtml(text) + "</span></td>";
      }
      default:
        return "<td></td>";
    }
  }

  function actionButtons(item) {
    var html = '<button class="btn btn-sm" type="button" data-action="edit" title="편집">⚙️</button>';
    if (item.type === "source") {
      html += '<button class="btn btn-sm" type="button" data-action="run" title="즉시 수집">⚡</button>';
    }
    return html;
  }

  function actionHtml(item) {
    return '<td class="col-actions"><div class="row-actions">' + actionButtons(item) + "</div></td>";
  }

  function itemTitle(item) {
    return item.type === "folder"
      ? (item.name || "(이름 없음)")
      : (item.name || item.url || "(이름 없음)");
  }

  function inheritsAny(item) {
    var flags = (item && item.inherited) || {};
    return Boolean(flags.cyc || flags.log_level || flags.profile || flags.config || flags.save_path);
  }

  function overridesAny(item) {
    var flags = (item && item.overridden) || {};
    return Boolean(flags.cyc || flags.log_level || flags.profile || flags.config || flags.save_path);
  }

  function inheritedBadges(item) {
    var labels = { cyc: "주기", log_level: "로그", profile: "프로파일", config: "config", save_path: "경로" };
    var order = ["cyc", "log_level", "profile", "config", "save_path"];
    var html = order.filter(function (k) {
      return item.inherited && item.inherited[k];
    }).map(function (k) {
      return '<span class="inherited-badge">' + labels[k] + " 상속</span>";
    }).join("");
    html += order.filter(function (k) {
      return item.overridden && item.overridden[k] && !(item.inherited && item.inherited[k]);
    }).map(function (k) {
      return '<span class="overridden-badge">' + labels[k] + " 자체</span>";
    }).join("");
    return html;
  }

  function itemMeta(item) {
    if (item.type === "folder") {
      return "📁 " + (item.sub_folder_count || 0) + " · 🔗 " + (item.item_count || 0);
    }
    return (item.site || "-") + (item.key ? " · " + item.key : "") +
      " · 📄 " + (item.file_count || 0);
  }

  function statusHtml(item) {
    if (item.type !== "source") {
      return "";
    }
    var status = item.status || "idle";
    var text = STATUS_LABELS[status] || status;
    return '<span class="status-badge status-' + escapeHtml(status) + '">' +
      escapeHtml(text) + "</span>";
  }

  function rowHtml(item, index) {
    var key = itemKey(item);
    var selected = state.selection[key] ? " selected-row" : "";
    var cells = state.colOrder.filter(function (columnKey) {
      return state.colVis[columnKey];
    }).map(function (columnKey) {
      return cellHtml(item, columnKey);
    }).join("");
    return '<tr class="db-item ' + item.type + '-row' + selected + '" data-key="' + key +
      '" data-type="' + item.type + '" data-id="' + item.id + '">' +
      '<td class="col-seq">' + (index + 1) + "</td>" + cells +
      '<td class="col-filler"></td>' + actionHtml(item) + "</tr>";
  }

  function thumbEnabled() {
    var settings = window.AppSettings;
    return !(settings && settings.dbThumbnails === false);
  }

  function gridThumbHtml(item) {
    if (item.type !== "source" || !thumbEnabled()) {
      return "";
    }
    return '<div class="db-card-thumb" data-thumb-id="' + item.id +
      '" data-thumb-limit="4"><span class="db-thumb-placeholder">🖼</span></div>';
  }

  function compactThumbHtml(item) {
    if (item.type !== "source" || !thumbEnabled()) {
      return "";
    }
    return '<span class="db-compact-thumb" data-thumb-id="' + item.id +
      '" data-thumb-limit="1"><span class="db-thumb-placeholder">🖼</span></span>';
  }

  function gridCard(item, index) {
    var key = itemKey(item);
    var selected = state.selection[key] ? " selected-row" : "";
    var typeIcon = item.type === "folder" ? "📁" : siteChipHtml(item.site);
    var handle = state.manualOrder ? '<span class="drag-handle" title="드래그로 순서 변경">⠿</span>' : "";
    return '<article class="db-grid-card db-item' + selected + '" data-key="' + key +
      '" data-type="' + item.type + '" data-id="' + item.id + '">' +
      '<div class="db-card-head">' + handle +
        '<span class="seq-badge">#' + (index + 1) + "</span>" +
        '<span class="id-badge">ID ' + escapeHtml(item.id) + "</span>" +
        statusHtml(item) +
      "</div>" +
      gridThumbHtml(item) +
      '<div class="db-card-title">' + typeIcon + " " + escapeHtml(itemTitle(item)) + "</div>" +
      '<div class="muted db-card-sub">' + escapeHtml(itemMeta(item)) + "</div>" +
      (inheritsAny(item) || overridesAny(item) ? '<div class="inherited-badges">' + inheritedBadges(item) + "</div>" : "") +
      '<div class="tag-container">' + tagChips(item) + "</div>" +
      '<div class="card-actions">' + actionButtons(item) + "</div>" +
      "</article>";
  }

  function compactRow(item, index) {
    var key = itemKey(item);
    var selected = state.selection[key] ? " selected-row" : "";
    var typeIcon = item.type === "folder" ? "📁" : siteChipHtml(item.site);
    var handle = state.manualOrder ? '<span class="drag-handle" title="드래그로 순서 변경">⠿</span>' : "";
    return '<div class="db-compact-row db-item' + selected + '" data-key="' + key +
      '" data-type="' + item.type + '" data-id="' + item.id + '">' + handle +
      '<span class="seq-badge">#' + (index + 1) + "</span>" +
      '<span class="id-badge">' + escapeHtml(item.id) + "</span>" +
      compactThumbHtml(item) +
      '<span class="compact-title">' + typeIcon + " " + escapeHtml(itemTitle(item)) + "</span>" +
      '<span class="muted compact-meta">' + escapeHtml(itemMeta(item)) + "</span>" +
      (inheritsAny(item) || overridesAny(item) ? '<span class="inherited-badges">' + inheritedBadges(item) + "</span>" : "") +
      statusHtml(item) +
      '<span class="row-actions">' + actionButtons(item) + "</span>" +
      "</div>";
  }

  var thumbObserver = null;
  var thumbData = {};

  function thumbCacheKey(id, item) {
    return id + ":" + (item ? (item.recent_file_mtime || item.recent_run_at || "") : "");
  }

  function renderThumbNode(node, images, limit) {
    if (!images || !images.length) {
      return;
    }
    var picked = images.slice(0, limit);
    node.classList.add("has-thumb");
    node.dataset.count = String(picked.length);
    node.innerHTML = picked.map(function (image) {
      var src = image.url + "?v=" + encodeURIComponent(image.mtime || "");
      return '<img class="db-thumb-img" loading="lazy" draggable="false" alt="" src="' +
        escapeHtml(src) + '">';
    }).join("");
  }

  function loadThumb(node) {
    var id = parseInt(node.dataset.thumbId, 10);
    var limit = parseInt(node.dataset.thumbLimit, 10) || 1;
    if (isNaN(id)) {
      return;
    }
    var item = itemByKey("source:" + id);
    var key = thumbCacheKey(id, item);
    if (thumbData[key]) {
      renderThumbNode(node, thumbData[key], limit);
      return;
    }
    window.API.getSourceImages(id, limit).then(function (result) {
      var images = (result && result.images) || [];
      thumbData[key] = images;
      renderThumbNode(node, images, limit);
    }).catch(function () {});
  }

  function setupThumbnails() {
    var nodes = document.querySelectorAll(
      ".db-card-thumb[data-thumb-id], .db-compact-thumb[data-thumb-id]"
    );
    if (!nodes.length) {
      return;
    }
    if (!("IntersectionObserver" in window)) {
      Array.prototype.forEach.call(nodes, loadThumb);
      return;
    }
    if (!thumbObserver) {
      thumbObserver = new IntersectionObserver(function (entries) {
        entries.forEach(function (entry) {
          if (entry.isIntersecting) {
            thumbObserver.unobserve(entry.target);
            loadThumb(entry.target);
          }
        });
      }, { rootMargin: "120px" });
    }
    Array.prototype.forEach.call(nodes, function (node) {
      thumbObserver.observe(node);
    });
  }

  function openLightbox(url) {
    var box = byId("imageLightbox");
    if (!box) {
      window.open(url, "_blank");
      return;
    }
    var img = box.querySelector("img");
    if (img) {
      img.src = url;
    }
    box.classList.remove("hidden");
  }

  function closeLightbox() {
    var box = byId("imageLightbox");
    if (!box) {
      return;
    }
    box.classList.add("hidden");
    var img = box.querySelector("img");
    if (img) {
      img.removeAttribute("src");
    }
  }

  function bindThumbInteractions() {
    function handle(event) {
      var img = event.target.closest ? event.target.closest(".db-thumb-img") : null;
      if (!img) {
        return;
      }
      event.preventDefault();
      event.stopPropagation();
      var src = img.getAttribute("src") || "";
      openLightbox(src + (src.indexOf("?") >= 0 ? "&" : "?") + "full=1");
    }
    document.addEventListener("click", handle, true);
    document.addEventListener("dblclick", handle, true);
    var box = byId("imageLightbox");
    if (box) {
      box.addEventListener("click", closeLightbox);
    }
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape") {
        closeLightbox();
      }
    });
  }

  function applyViewVisibility() {
    var wrap = document.querySelector("#explorerMainView .table-wrap");
    var alt = byId("dbAltView");
    var mainView = byId("explorerMainView");
    if (wrap) {
      wrap.classList.toggle("hidden", state.view !== "table");
    }
    if (alt) {
      alt.classList.toggle("hidden", state.view === "table");
    }
    if (mainView) {
      mainView.classList.remove("view-table", "view-grid", "view-compact");
      mainView.classList.add("view-" + state.view);
    }
    var manualChip = byId("manualOrderBtn");
    if (manualChip) {
      manualChip.classList.toggle("active", state.manualOrder);
    }
  }

  function render() {
    if (dragActive) {
      pendingRender = true;
      return;
    }
    renderBreadcrumb();
    renderHeader();
    updateStatusBar();
    applyViewVisibility();
    var items = visibleItems();
    var emptyTip = byId("emptyTip");
    var mainView = byId("explorerMainView");
    var tbody = document.querySelector("#itemTable tbody");
    var altView = byId("dbAltView");
    if (!items.length) {
      if (tbody) {
        tbody.innerHTML = "";
      }
      if (altView) {
        altView.innerHTML = "";
      }
      emptyTip.classList.remove("hidden");
      if (mainView) {
        mainView.classList.add("is-empty");
      }
      updateBatchBar();
      return;
    }
    emptyTip.classList.add("hidden");
    if (mainView) {
      mainView.classList.remove("is-empty");
    }
    if (state.view === "table") {
      if (altView) {
        altView.innerHTML = "";
      }
      tbody.innerHTML = items.map(rowHtml).join("");
    } else if (state.view === "grid") {
      tbody.innerHTML = "";
      altView.innerHTML = '<div class="db-grid">' + items.map(gridCard).join("") + "</div>";
    } else {
      tbody.innerHTML = "";
      altView.innerHTML = '<div class="db-compact">' + items.map(compactRow).join("") + "</div>";
    }
    ensureSortable();
    bindRowEvents();
    setupThumbnails();
    updateBatchBar();
  }

  function renderBreadcrumb() {
    var bar = byId("breadcrumbBar");
    bar.innerHTML = "";
    state.breadcrumb.forEach(function (crumb, index) {
      var isLast = index === state.breadcrumb.length - 1;
      var span = document.createElement("span");
      span.className = "breadcrumb-item" + (isLast ? " current" : "");
      span.textContent = crumb.id === null ? "루트" : crumb.name;
      span.dataset.crumbId = crumb.id === null ? "" : String(crumb.id);
      span.dataset.crumbName = crumb.id === null ? "루트" : (crumb.name || "");
      if (!isLast) {
        span.addEventListener("click", function () {
          navigateTo(crumb.id);
        });
        bar.appendChild(span);
        var sep = document.createElement("span");
        sep.className = "breadcrumb-sep";
        sep.textContent = "›";
        bar.appendChild(sep);
      } else {
        bar.appendChild(span);
      }
    });
    var upButton = byId("dbUpBtn");
    if (state.parentId === null) {
      upButton.disabled = true;
      upButton.classList.add("muted");
    } else {
      upButton.disabled = false;
      upButton.classList.remove("muted");
    }
  }

  function renderHeader() {
    var thead = document.querySelector("#itemTable thead");
    var html = state.colOrder.filter(function (key) {
      return state.colVis[key];
    }).map(function (key) {
      var def = columnDef(key);
      var width = state.colWidths[key] || def.width;
      var icon = state.sortCol === key ? (state.sortDir === "asc" ? " ▲" : " ▼") : "";
      return '<th class="' + def.className + ' resizable" data-key="' + key + '" style="width:' +
        width + 'px;">' + escapeHtml(def.label) +
        '<span class="sort-icon">' + icon + "</span>" +
        '<span class="resizer" title="드래그하여 폭 조절"></span></th>';
    }).join("");
    thead.innerHTML = '<tr><th class="col-seq" title="표시 순번">#</th>' + html +
      '<th class="col-filler"></th><th class="col-actions"></th></tr>';
    bindHeaderEvents();
    setupHeaderSortable();
  }

  function applySort(key) {
    if (state.manualOrder) {
      return;
    }
    if (state.sortCol === key) {
      state.sortDir = state.sortDir === "asc" ? "desc" : "asc";
    } else {
      state.sortCol = key;
      state.sortDir = "asc";
    }
    saveSortPrefs();
    render();
  }

  var headerSortable = null;
  var headerDragging = false;
  var suppressSortClick = false;

  function isFinePointer() {
    try {
      return window.matchMedia("(hover: hover) and (pointer: fine)").matches;
    } catch (error) {
      return true;
    }
  }

  function setupHeaderSortable() {
    if (headerSortable) {
      try {
        headerSortable.destroy();
      } catch (error) {
        /* noop */
      }
      headerSortable = null;
    }
    if (!window.Sortable || !isFinePointer() || touchMode) {
      return;
    }
    var row = document.querySelector("#itemTable thead tr");
    if (!row) {
      return;
    }
    headerSortable = window.Sortable.create(row, {
      animation: 150,
      delay: 300,
      delayOnTouchOnly: false,
      draggable: "th.resizable",
      filter: ".resizer",
      preventOnFilter: true,
      forceFallback: true,
      fallbackClass: "header-sortable-fallback",
      ghostClass: "header-sortable-ghost",
      onStart: function () {
        headerDragging = true;
        suppressSortClick = true;
      },
      onEnd: function () {
        headerDragging = false;
        window.setTimeout(function () {
          var keys = [];
          Array.prototype.forEach.call(row.querySelectorAll("th.resizable"), function (th) {
            if (th.dataset.key) {
              keys.push(th.dataset.key);
            }
          });
          if (!keys.length) {
            render();
            return;
          }
          var cursor = 0;
          var merged = state.colOrder.map(function (key) {
            if (state.colVis[key]) {
              return keys[cursor++] || key;
            }
            return key;
          });
          state.colOrder = merged;
          write(LS.colOrder, JSON.stringify(state.colOrder));
          render();
        }, 0);
      },
      onUnchoose: function () {
        headerDragging = false;
      }
    });
  }

  function bindHeaderEvents() {
    var headerRow = document.querySelector("#itemTable thead tr");
    var pressTimer = null;
    var startX = 0;
    var startY = 0;
    var longPressed = false;

    Array.prototype.forEach.call(headerRow.querySelectorAll("th.resizable"), function (th) {
      var resizer = th.querySelector(".resizer");
      if (!resizer) {
        return;
      }
      resizer.addEventListener("pointerdown", function (event) {
        event.stopPropagation();
        event.preventDefault();
        var startPosition = event.clientX;
        var startWidth = th.offsetWidth;
        var key = th.dataset.key;
        th.classList.add("resizing");
        function move(moveEvent) {
          var width = startWidth + (moveEvent.clientX - startPosition);
          if (width < MIN_COL_WIDTH) {
            width = MIN_COL_WIDTH;
          }
          th.style.width = width + "px";
          state.colWidths[key] = Math.round(width);
        }
        function up() {
          document.removeEventListener("pointermove", move);
          document.removeEventListener("pointerup", up);
          th.classList.remove("resizing");
          write(LS.colWidths, JSON.stringify(state.colWidths));
        }
        document.addEventListener("pointermove", move);
        document.addEventListener("pointerup", up);
      });
    });

    headerRow.addEventListener("pointerdown", function (event) {
      if (event.target.closest(".resizer")) {
        return;
      }
      startX = event.clientX;
      startY = event.clientY;
      longPressed = false;
      suppressSortClick = false;
      if (!touchMode && event.pointerType === "mouse") {
        return;
      }
      pressTimer = window.setTimeout(function () {
        longPressed = true;
        window.Modals.openColumns({ x: event.clientX, y: event.clientY });
      }, 450);
    });

    headerRow.addEventListener("pointermove", function (event) {
      if (pressTimer && (Math.abs(event.clientX - startX) > 10 || Math.abs(event.clientY - startY) > 10)) {
        window.clearTimeout(pressTimer);
        pressTimer = null;
      }
    });

    headerRow.addEventListener("pointerup", function (event) {
      if (pressTimer) {
        window.clearTimeout(pressTimer);
        pressTimer = null;
      }
      if (longPressed) {
        return;
      }
      if (suppressSortClick) {
        suppressSortClick = false;
        return;
      }
      if (headerDragging) {
        return;
      }
      var th = event.target.closest("th.resizable");
      if (th && th.dataset.key) {
        applySort(th.dataset.key);
      }
    });

    headerRow.addEventListener("contextmenu", function (event) {
      if (event.target.closest(".resizer")) {
        return;
      }
      event.preventDefault();
      window.Modals.openColumns({ x: event.clientX, y: event.clientY });
    });
  }

  function selectItem(item, selected) {
    var key = itemKey(item);
    if (selected) {
      state.selection[key] = { type: item.type, id: item.id };
      if (!state.selectionMode) {
        state.selectionMode = true;
      }
    } else {
      delete state.selection[key];
      if (!selectionSize()) {
        state.selectionMode = false;
      }
    }
    updateRowSelection();
    updateBatchBar();
  }

  function clearSelection() {
    state.selection = {};
    state.selectionMode = false;
    updateRowSelection();
    updateBatchBar();
  }

  function updateRowSelection() {
    Array.prototype.forEach.call(document.querySelectorAll(".db-item"), function (row) {
      row.classList.toggle("selected-row", Boolean(state.selection[row.dataset.key]));
    });
  }

  function updateBatchBar() {
    var bar = byId("batchActionsBar");
    var count = selectionSize();
    byId("batchCount").textContent = count;
    if (count > 0) {
      bar.classList.remove("hidden");
      bar.scrollLeft = 0;
      populateMoveSelect();
    } else {
      bar.classList.add("hidden");
    }
  }

  function populateMoveSelect() {
    var select = byId("batchMoveSelect");
    window.API.getFolders().then(function (folders) {
      select.innerHTML = "";
      var rootOption = document.createElement("option");
      rootOption.value = "";
      rootOption.textContent = "루트로 이동";
      select.appendChild(rootOption);
      folders.forEach(function (folder) {
        var option = document.createElement("option");
        option.value = String(folder.id);
        option.textContent = folder.name;
        select.appendChild(option);
      });
    }).catch(function () {});
  }

  function itemByKey(key) {
    var parts = key.split(":");
    var id = parseInt(parts[1], 10);
    return (parts[0] === "folder" ? state.folders : state.sources).filter(function (item) {
      return item.id === id;
    })[0] || null;
  }

  function openRow(row) {
    var item = itemByKey(row.dataset.key);
    if (!item) {
      return;
    }
    if (item.type === "folder") {
      navigateTo(item.id);
    } else {
      window.Modals.openSource(item);
    }
  }

  function updateStatusBar() {
    var path = state.breadcrumb.map(function (crumb) {
      return crumb.id === null ? "루트" : crumb.name;
    }).join(" › ") || "루트";
    byId("dbStatusPath").textContent = path;
    byId("dbStatusCounts").textContent =
      "폴더 " + state.folders.length + " · 소스 " + state.sources.length;
  }

  function setLoading() {
    var visibleColumns = state.colOrder.filter(function (key) {
      return state.colVis[key];
    }).length + 3;
    document.querySelector("#itemTable tbody").innerHTML =
      '<tr><td class="loading-cell" colspan="' + visibleColumns + '">불러오는 중...</td></tr>';
    byId("emptyTip").classList.add("hidden");
    byId("dbStatusCounts").textContent = "불러오는 중...";
  }

  function bindRowEvents() {
    Array.prototype.forEach.call(document.querySelectorAll(".db-item"), function (row) {
      row.addEventListener("dblclick", function (event) {
        // 터치에서는 더블탭이 dblclick 을 합성하므로 무시(컨텍스트 메뉴로 처리)
        if (lastPointerType === "touch" || touchMode) {
          return;
        }
        if (event.target.closest("a, button")) {
          return;
        }
        if (state.selectionMode) {
          return;
        }
        openRow(row);
      });
      row.addEventListener("click", function (event) {
        if (event.target.closest("a, button, input, select, textarea, .tag-chip")) {
          return;
        }
        if (row.dataset.longPressed === "1") {
          row.dataset.longPressed = "";
          return;
        }
        if (!state.selectionMode) {
          return;
        }
        var item = itemByKey(row.dataset.key);
        if (item) {
          selectItem(item, !state.selection[itemKey(item)]);
        }
      });
      Array.prototype.forEach.call(row.querySelectorAll("button[data-action]"), function (button) {
        button.addEventListener("click", function (event) {
          event.stopPropagation();
          var item = itemByKey(row.dataset.key);
          if (!item) {
            return;
          }
          if (button.dataset.action === "edit") {
            if (item.type === "folder") {
              window.Modals.openFolder(item);
            } else {
              window.Modals.openSource(item);
            }
          } else if (button.dataset.action === "run") {
            window.API.downloadSource({ source_id: item.id }).then(function (result) {
              showToast("[" + item.name + "] 수집 시작", "info");
              watchSources((result && result.started) || [{ id: item.id, name: item.name }]);
              refresh();
            }).catch(function (error) {
              showToast(error.message || "수집 요청 실패", "error");
            });
          }
        });
      });
      Array.prototype.forEach.call(row.querySelectorAll(".tag-chip"), function (chip) {
        chip.addEventListener("click", function (event) {
          event.stopPropagation();
          byId("dbSearchInput").value = chip.dataset.tag;
          render();
        });
      });
      bindRowPointer(row);
    });
  }

  var rowPress = null;
  var rowPressGuardsBound = false;
  var ROW_LONG_PRESS_MS = 450;
  var ROW_PRESS_TOLERANCE_PX = 10;
  var ROW_PRESS_TOLERANCE_TOUCH_PX = 16;
  var sortableMain = null;
  var dragActive = false;
  var pendingRender = false;
  var pendingRefresh = false;

  function flushPending() {
    if (dragActive) {
      return;
    }
    if (pendingRefresh) {
      pendingRefresh = false;
      pendingRender = false;
      refresh();
      return;
    }
    if (pendingRender) {
      pendingRender = false;
      render();
    }
  }

  function cancelRowPress() {
    if (rowPress && rowPress.timer) {
      window.clearTimeout(rowPress.timer);
    }
    rowPress = null;
  }

  function rowStillUnderPointer(press) {
    if (!press || !press.row || !press.row.isConnected) {
      return false;
    }
    var element = document.elementFromPoint(press.x, press.y);
    if (!element) {
      // 렌더링되지 않는 상태(백그라운드 탭 등)에서는 판단 불가 → 스크롤/이동 가드에 맡긴다
      return true;
    }
    return element === press.row || press.row.contains(element);
  }

  // 롱프레스 취소 가드(문서 전역 1회 바인딩).
  //  - 손가락이 움직였거나(스크롤 포함) 스크롤 이벤트가 나면 취소
  //  - pointermove 는 행이 아니라 document(capture)에서 받는다: 스크롤로 행이
  //    손가락 밑에서 벗어나도 이벤트를 놓치지 않기 위함
  function bindRowPressGuards() {
    if (rowPressGuardsBound) {
      return;
    }
    rowPressGuardsBound = true;
    document.addEventListener("pointermove", function (event) {
      if (!rowPress || event.pointerId !== rowPress.pointerId) {
        return;
      }
      var limit = rowPress.touch ? ROW_PRESS_TOLERANCE_TOUCH_PX : ROW_PRESS_TOLERANCE_PX;
      if (Math.abs(event.clientX - rowPress.x) > limit ||
          Math.abs(event.clientY - rowPress.y) > limit) {
        cancelRowPress();
      }
    }, { passive: true, capture: true });
    document.addEventListener("pointerup", function (event) {
      if (rowPress && event.pointerId === rowPress.pointerId) {
        cancelRowPress();
      }
    }, { passive: true, capture: true });
    document.addEventListener("pointercancel", cancelRowPress, { passive: true, capture: true });
    document.addEventListener("scroll", cancelRowPress, { passive: true, capture: true });
    window.addEventListener("blur", cancelRowPress);
    window.addEventListener("resize", cancelRowPress);
  }

  function bindRowPointer(row) {
    row.addEventListener("pointerdown", function (event) {
      if (event.target.closest("a, button, input, select, textarea, .tag-chip")) {
        return;
      }
      if (event.button !== undefined && event.button !== 0) {
        return;
      }
      var item = itemByKey(row.dataset.key);
      if (!item) {
        return;
      }
      cancelRowPress();
      // 롱프레스 = 즉시 선택 모드 (움직이지 않아도 진입)
      var press = {
        item: item,
        row: row,
        pointerId: event.pointerId,
        x: event.clientX,
        y: event.clientY,
        touch: event.pointerType === "touch",
        timer: null
      };
      press.timer = window.setTimeout(function () {
        var active = rowPress;
        rowPress = null;
        if (!active || active !== press) {
          return;
        }
        // 스크롤로 아이템이 손가락 밑에서 벗어났으면 선택하지 않는다
        if (!rowStillUnderPointer(press)) {
          return;
        }
        press.row.dataset.longPressed = "1";
        selectItem(press.item, true);
      }, ROW_LONG_PRESS_MS);
      rowPress = press;
    });
    row.addEventListener("pointerup", function (event) {
      cancelRowPress();
      if (event.pointerType === "mouse") {
        return;
      }
      if (dragActive) {
        return;
      }
      if (row.dataset.longPressed === "1") {
        row.dataset.longPressed = "";
        return;
      }
      var now = Date.now();
      if (row._lastTap && now - row._lastTap < 350) {
        // 터치 더블탭 → 모달 대신 컨텍스트 메뉴
        row._lastTap = 0;
        event.preventDefault();
        event.stopPropagation();
        var item = itemByKey(row.dataset.key);
        if (item) {
          contextMenuGuardUntil = Date.now() + 450;
          openContextMenu({ clientX: event.clientX, clientY: event.clientY }, item);
        }
      } else {
        row._lastTap = now;
      }
    });
  }

  function clearHighlight() {
    Array.prototype.forEach.call(document.querySelectorAll(".drag-hover-row"), function (row) {
      row.classList.remove("drag-hover-row");
    });
    Array.prototype.forEach.call(
      document.querySelectorAll(".breadcrumb-item.drag-hover-crumb"),
      function (crumb) {
        crumb.classList.remove("drag-hover-crumb");
      }
    );
  }

  function folderRowAt(x, y) {
    var element = document.elementFromPoint(x, y);
    if (!element) {
      return null;
    }
    return element.closest("[data-key][data-type='folder']") || null;
  }

  function crumbAt(x, y) {
    var element = document.elementFromPoint(x, y);
    if (!element) {
      return null;
    }
    var crumb = element.closest(".breadcrumb-item");
    if (!crumb || crumb.classList.contains("current")) {
      return null;
    }
    return crumb;
  }

  function applyDragSources(group) {
    Array.prototype.forEach.call(document.querySelectorAll(".db-item.drag-source"), function (el) {
      el.classList.remove("drag-source");
    });
    if (!group || !group.length) {
      return;
    }
    var keys = {};
    group.forEach(function (entry) {
      keys[entry.type + ":" + entry.id] = true;
    });
    Array.prototype.forEach.call(document.querySelectorAll(".db-item"), function (el) {
      if (keys[el.dataset.key]) {
        el.classList.add("drag-source");
      }
    });
  }

  function buildDragGroup(item) {
    if (!item) {
      return null;
    }
    var selected = state.selection[itemKey(item)] ? selectionList() : [];
    if (selected.length > 1) {
      var group = selected;
      if (state.manualOrder) {
        group = group.filter(function (entry) {
          return entry.type === item.type;
        });
      }
      if (group.length) {
        return group;
      }
    }
    return [{ type: item.type, id: item.id }];
  }

  var sortableContainer = null;

  function activeContainer() {
    if (state.view === "table") {
      return document.querySelector("#itemTable tbody");
    }
    var alt = byId("dbAltView");
    if (!alt) {
      return null;
    }
    return alt.querySelector(state.view === "grid" ? ".db-grid" : ".db-compact");
  }

  function ensureSortable() {
    if (!window.Sortable) {
      return;
    }
    var container = activeContainer();
    if (!container) {
      return;
    }
    if (sortableMain && sortableContainer === container) {
      return;
    }
    if (sortableMain) {
      try {
        sortableMain.destroy();
      } catch (error) {
        /* noop */
      }
      sortableMain = null;
    }
    sortableContainer = container;
    sortableMain = window.Sortable.create(container, {
      animation: 150,
      delay: touchMode ? 400 : 0,
      delayOnTouchOnly: true,
      touchStartThreshold: 10,
      forceFallback: true,
      fallbackClass: "sortable-fallback",
      ghostClass: "sortable-ghost",
      fallbackTolerance: 5,
      filter: "button, a, input, select, textarea, .tag-chip, img, .db-thumb-placeholder",
      preventOnFilter: true,
      scroll: true,
      scrollSensitivity: 55,
      scrollSpeed: 14,
      onStart: function (evt) {
        dragActive = true;
        cancelRowPress();
        var item = evt && evt.item ? itemByKey(evt.item.dataset.key) : null;
        if (touchMode && item && !state.selection[itemKey(item)]) {
          selectItem(item, true);
        }
        state.dragItem = item;
        state.dragGroup = buildDragGroup(item);
        applyDragSources(state.dragGroup);
        if (state.dragGroup && state.dragGroup.length > 1) {
          document.body.classList.add("drag-multiple");
        }
        document.body.classList.add("drag-active");
      },
      onMove: function (evt) {
        clearHighlight();
        var coords = pointerCoords(evt.originalEvent);
        var crumb = coords ? crumbAt(coords.x, coords.y) : null;
        if (crumb) {
          crumb.classList.add("drag-hover-crumb");
          return true;
        }
        if (state.manualOrder) {
          if (evt.dragged && evt.related &&
              evt.dragged.dataset.type !== evt.related.dataset.type) {
            return false;
          }
          return true;
        }
        var related = evt.related;
        if (related && related.dataset && related.dataset.type === "folder" && related !== evt.dragged) {
          related.classList.add("drag-hover-row");
        }
        return true;
      },
      onEnd: function (evt) {
        clearHighlight();
        dragActive = false;
        document.body.classList.remove("drag-active", "drag-multiple");
        applyDragSources(null);
        var group = state.dragGroup;
        state.dragGroup = null;
        state.dragItem = null;
        flushPending();
        handleSortEnd(evt, group);
      },
      onUnchoose: function () {
        dragActive = false;
        clearHighlight();
        document.body.classList.remove("drag-active", "drag-multiple");
        applyDragSources(null);
        state.dragGroup = null;
        state.dragItem = null;
        flushPending();
      }
    });
  }

  function pointerCoords(event) {
    if (!event) {
      return null;
    }
    if (event.changedTouches && event.changedTouches.length) {
      return { x: event.changedTouches[0].clientX, y: event.changedTouches[0].clientY };
    }
    if (event.touches && event.touches.length) {
      return { x: event.touches[0].clientX, y: event.touches[0].clientY };
    }
    if (typeof event.clientX === "number") {
      return { x: event.clientX, y: event.clientY };
    }
    return null;
  }

  function resolveCrumbTarget(evt) {
    var coords = pointerCoords(evt.originalEvent);
    if (!coords) {
      return null;
    }
    var crumb = crumbAt(coords.x, coords.y);
    if (!crumb) {
      return null;
    }
    var rawId = crumb.dataset.crumbId;
    return {
      kind: "crumb",
      id: rawId === "" ? null : parseInt(rawId, 10),
      name: crumb.dataset.crumbName || "루트"
    };
  }

  function folderTargetFromElement(element) {
    if (!element) {
      return null;
    }
    var folder = itemByKey(element.dataset.key);
    if (!folder) {
      return null;
    }
    return { kind: "folder", id: folder.id, name: folder.name || "폴더" };
  }

  function blockedDropFolders(evt, group) {
    var blocked = {};
    (group || []).forEach(function (entry) {
      if (entry && entry.type === "folder") {
        blocked[entry.id] = true;
      }
    });
    // 함께 끌린 그룹이 없어도, 끌고 있는 폴더 자신은 대상에서 제외한다.
    if (evt && evt.item && evt.item.dataset && evt.item.dataset.type === "folder") {
      var dragged = itemByKey(evt.item.dataset.key);
      if (dragged) {
        blocked[dragged.id] = true;
      }
    }
    return blocked;
  }

  function resolveDropTarget(evt, group) {
    var blocked = blockedDropFolders(evt, group);
    var crumb = resolveCrumbTarget(evt);
    if (crumb) {
      return crumb;
    }
    var coords = pointerCoords(evt.originalEvent);
    if (coords) {
      var row = folderRowAt(coords.x, coords.y);
      var target = folderTargetFromElement(row);
      if (target && !blocked[target.id]) {
        return target;
      }
    }
    var container = activeContainer();
    var candidate = container ? container.children[evt.newIndex] : null;
    if (candidate && candidate.dataset && candidate.dataset.type === "folder" && candidate !== evt.item) {
      var fallback = folderTargetFromElement(candidate);
      if (fallback && !blocked[fallback.id]) {
        return fallback;
      }
    }
    return null;
  }

  function dragGroupLabel(group) {
    if (!group || !group.length) {
      return "선택한 항목";
    }
    if (group.length > 1) {
      return "선택한 " + group.length + "개 항목";
    }
    var entry = group[0];
    var pool = entry.type === "folder" ? state.folders : state.sources;
    var item = pool.filter(function (candidate) {
      return candidate.id === entry.id;
    })[0];
    return "'" + ((item && (item.name || item.url)) || "항목") + "'";
  }

  function performMove(target, group) {
    if (!target || !group || !group.length) {
      render();
      return;
    }
    if (target.id === state.folderId) {
      render();
      return;
    }
    var selfDrop = group.some(function (entry) {
      return entry && entry.type === "folder" && entry.id === target.id;
    });
    if (selfDrop) {
      // 폴더를 자기 자신(또는 함께 끌린 폴더) 위로 놓은 경우 → 무시
      render();
      return;
    }
    var message = dragGroupLabel(group) + "을(를) '" + target.name + "'(으)로 이동할까요?";
    if (!window.confirm(message)) {
      render();
      return;
    }
    window.API.batchMove(group, target.id).then(function (result) {
      if (result && result.errors && result.errors.length) {
        window.alert(result.errors.join("\n"));
      }
      clearSelection();
      refresh();
    }).catch(function (error) {
      window.alert(error.message || "이동 실패");
      refresh();
    });
  }

  function performReorder(evt, item, group) {
    var container = evt.from || activeContainer();
    if (!container || !group || !group.length) {
      render();
      return;
    }
    if (evt.oldIndex === evt.newIndex) {
      render();
      return;
    }
    var type = item.type;
    var blockIds = [];
    group.forEach(function (entry) {
      if (entry.type === type && blockIds.indexOf(entry.id) < 0) {
        blockIds.push(entry.id);
      }
    });
    if (!blockIds.length) {
      render();
      return;
    }
    var blockSet = {};
    blockIds.forEach(function (id) {
      blockSet[id] = true;
    });
    var domIds = [];
    Array.prototype.forEach.call(container.querySelectorAll("[data-key]"), function (element) {
      var candidate = itemByKey(element.dataset.key);
      if (candidate && candidate.type === type && domIds.indexOf(candidate.id) < 0) {
        domIds.push(candidate.id);
      }
    });
    var orderedBlock = domIds.filter(function (id) {
      return blockSet[id];
    });
    var remaining = domIds.filter(function (id) {
      return !blockSet[id];
    });
    var anchorId = null;
    var seenDragged = false;
    var children = container.querySelectorAll("[data-key]");
    for (var i = 0; i < children.length; i += 1) {
      var candidate = itemByKey(children[i].dataset.key);
      if (!candidate || candidate.type !== type) {
        continue;
      }
      if (blockSet[candidate.id]) {
        if (candidate.id === item.id) {
          seenDragged = true;
        }
        continue;
      }
      if (seenDragged) {
        anchorId = candidate.id;
        break;
      }
    }
    var insertAt = remaining.length;
    if (anchorId !== null) {
      var pos = remaining.indexOf(anchorId);
      if (pos >= 0) {
        insertAt = pos;
      }
    }
    var finalIds = remaining.slice(0, insertAt).concat(orderedBlock, remaining.slice(insertAt));
    if (!finalIds.length) {
      render();
      return;
    }
    window.API.reorderExplorer(type, state.folderId, finalIds).then(function () {
      refresh();
    }).catch(function (error) {
      showToast(error.message || "순서 저장 실패", "error");
      refresh();
    });
  }

  function handleReorderEnd(evt, item, group) {
    if (!item) {
      render();
      return;
    }
    var groupItems = group && group.length ? group : [{ type: item.type, id: item.id }];
    window.setTimeout(function () {
      var crumbTarget = resolveCrumbTarget(evt);
      if (crumbTarget) {
        performMove(crumbTarget, groupItems);
        return;
      }
      performReorder(evt, item, groupItems);
    }, 0);
  }

  function handleSortEnd(evt, group) {
    var item = itemByKey(evt.item.dataset.key);
    var groupItems = group && group.length
      ? group
      : (item ? [{ type: item.type, id: item.id }] : []);
    if (state.manualOrder) {
      handleReorderEnd(evt, item, groupItems);
      return;
    }
    var target = resolveDropTarget(evt, groupItems);
    var moved = evt.oldIndex !== evt.newIndex;
    window.setTimeout(function () {
      if (!item || !groupItems.length) {
        render();
        return;
      }
      if (!target) {
        if (moved) {
          refresh();
        } else {
          render();
        }
        return;
      }
      performMove(target, groupItems);
    }, 0);
  }

  var navHistory = [];
  var navIndex = -1;

  function navigateTo(folderId) {
    navHistory = navHistory.slice(0, navIndex + 1);
    if (navIndex < 0 || navHistory[navIndex] !== folderId) {
      navHistory.push(folderId);
      navIndex = navHistory.length - 1;
    }
    return loadFolder(folderId);
  }

  function goHistory(step) {
    var next = navIndex + step;
    if (next < 0 || next >= navHistory.length) {
      return Promise.resolve();
    }
    navIndex = next;
    return loadFolder(navHistory[navIndex]);
  }

  function dbTabActive() {
    var panel = byId("view-db");
    return Boolean(panel && panel.classList.contains("active"));
  }

  function bindMouseNavigation() {
    document.addEventListener("mousedown", function (event) {
      if (event.button === 3 || event.button === 4) {
        event.preventDefault();
      }
    });
    document.addEventListener("mouseup", function (event) {
      if (!dbTabActive()) {
        return;
      }
      if (event.button === 3) {
        event.preventDefault();
        goHistory(-1);
      } else if (event.button === 4) {
        event.preventDefault();
        goHistory(1);
      }
    });
  }

  function readLastFolderId() {
    var raw = read(LS.lastFolder);
    if (!raw) {
      return null;
    }
    var value = parseInt(raw, 10);
    return isNaN(value) ? null : value;
  }

  function saveLastFolder() {
    if (state.folderId === null || state.folderId === undefined) {
      write(LS.lastFolder, "");
    } else {
      write(LS.lastFolder, String(state.folderId));
    }
  }

  function loadFolder(folderId, options) {
    var opts = options || {};
    setLoading();
    return window.API.getItems(folderId).then(function (data) {
      if (data.folder_id !== null && data.folder_id !== undefined && !data.folder) {
        throw new Error("folder not found");
      }
      state.folderId = data.folder_id;
      state.parentId = data.folder ? data.folder.parent_id : null;
      state.breadcrumb = data.breadcrumb || [];
      state.folders = data.folders || [];
      state.sources = data.sources || [];
      clearSelection();
      saveLastFolder();
      render();
    }).catch(function (error) {
      if (opts.silent) {
        throw error;
      }
      window.alert(error.message || "탐색기 로드 실패");
    });
  }

  function restoreSession() {
    var savedId = readLastFolderId();
    if (savedId === null) {
      navigateTo(null);
      return;
    }
    navHistory = [savedId];
    navIndex = 0;
    loadFolder(savedId, { silent: true }).catch(function () {
      navHistory = [];
      navIndex = -1;
      navigateTo(null);
    });
  }

  function refresh() {
    if (dragActive) {
      pendingRefresh = true;
      return Promise.resolve();
    }
    return loadFolder(state.folderId);
  }

  function handleBatchRun() {
    var items = selectionList();
    if (!items.length) {
      return;
    }
    window.API.batchRun(items).then(function (result) {
      clearSelection();
      refresh();
      var started = (result && result.started) || [];
      showToast(started.length + "개 소스 수집 시작", "info");
      watchSources(started);
    }).catch(function (error) {
      showToast(error.message || "일괄 수집 실패", "error");
    });
  }

  function showToast(message, type) {
    if (window.Toast) {
      window.Toast.show(message, type);
    }
  }

  function watchSources(started) {
    (started || []).forEach(function (entry) {
      if (entry.already_running) {
        return;
      }
      pollSource(entry.id, entry.name, 0);
    });
  }

  function pollSource(sourceId, name, attempt) {
    if (attempt > 150) {
      return;
    }
    window.setTimeout(function () {
      window.API.getSource(sourceId).then(function (source) {
        if (source.status === "done") {
          showToast("[" + (name || source.name) + "] 수집 완료", "success");
          refresh();
        } else if (source.status === "error") {
          showToast("[" + (name || source.name) + "] 수집 실패", "error");
          refresh();
        } else {
          pollSource(sourceId, name, attempt + 1);
        }
      }).catch(function () {
        pollSource(sourceId, name, attempt + 1);
      });
    }, 2000);
  }

  function handleBatchDelete() {
    var items = selectionList();
    if (!items.length) {
      return;
    }
    if (!window.confirm("선택한 " + items.length + "개 항목을 삭제하시겠습니까?")) {
      return;
    }
    window.API.batchDelete(items).then(function () {
      clearSelection();
      refresh();
    }).catch(function (error) {
      window.alert(error.message || "일괄 삭제 실패");
    });
  }

  function handleBatchMove() {
    var items = selectionList();
    var select = byId("batchMoveSelect");
    var value = select.value;
    var target = value === "" ? null : parseInt(value, 10);
    if (!items.length) {
      return;
    }
    var targetName = "루트";
    if (value !== "" && select.selectedIndex >= 0) {
      targetName = select.options[select.selectedIndex].textContent || "폴더";
    }
    if (!window.confirm("선택한 " + items.length + "개 항목을 '" + targetName + "'(으)로 이동할까요?")) {
      select.value = "";
      return;
    }
    window.API.batchMove(items, target).then(function (result) {
      if (result && result.errors && result.errors.length) {
        window.alert(result.errors.join("\n"));
      }
      clearSelection();
      refresh();
    }).catch(function (error) {
      window.alert(error.message || "일괄 이동 실패");
    });
  }

  function getColumnConfig() {
    return {
      order: state.colOrder.slice(),
      visibility: Object.assign({}, state.colVis)
    };
  }

  function setColumnConfig(config) {
    if (config.order) {
      state.colOrder = config.order.slice();
    }
    if (config.visibility) {
      state.colVis = Object.assign({}, config.visibility);
    }
    write(LS.colOrder, JSON.stringify(state.colOrder));
    write(LS.colVis, JSON.stringify(state.colVis));
    render();
  }

  function updateViewButtons() {
    ["table", "grid", "compact"].forEach(function (mode) {
      var id = "dbView" + mode.charAt(0).toUpperCase() + mode.slice(1);
      var btn = byId(id);
      if (btn) {
        btn.classList.toggle("active", state.view === mode);
      }
    });
  }

  function setView(mode) {
    if (VIEW_MODES.indexOf(mode) < 0 || state.view === mode) {
      return;
    }
    state.view = mode;
    write(LS.view, mode);
    updateViewButtons();
    closeMoreMenu();
    render();
  }

  function toggleManualOrder() {
    state.manualOrder = !state.manualOrder;
    write(LS.manual, state.manualOrder ? "true" : "false");
    var chip = byId("manualOrderBtn");
    if (chip) {
      chip.classList.toggle("active", state.manualOrder);
    }
    showToast(
      state.manualOrder
        ? "수동 순서 모드: 같은 종류끼리 드래그해 순서를 바꾸세요."
        : "수동 순서 모드를 해제했습니다.",
      "info"
    );
    render();
  }

  function closeMoreMenu() {
    var menu = byId("dbMoreMenu");
    var btn = byId("dbMoreBtn");
    if (menu) {
      menu.classList.remove("open");
    }
    if (btn) {
      btn.classList.remove("active");
    }
  }

  function toggleMoreMenu() {
    var menu = byId("dbMoreMenu");
    var btn = byId("dbMoreBtn");
    if (!menu) {
      return;
    }
    var open = !menu.classList.contains("open");
    menu.classList.toggle("open", open);
    if (btn) {
      btn.classList.toggle("active", open);
    }
  }

  function toggleSearchBar() {
    var toolbar = byId("dbToolbar");
    if (!toolbar) {
      return;
    }
    var open = !toolbar.classList.contains("search-open");
    toolbar.classList.toggle("search-open", open);
    var toggle = byId("dbSearchToggle");
    if (toggle) {
      toggle.classList.toggle("active", open);
    }
    if (open) {
      var input = byId("dbSearchInput");
      if (input) {
        input.focus();
      }
    }
  }

  function wireToolbar() {
    var searchToggle = byId("dbSearchToggle");
    if (searchToggle) {
      searchToggle.addEventListener("click", toggleSearchBar);
    }
    var moreBtn = byId("dbMoreBtn");
    if (moreBtn) {
      moreBtn.addEventListener("click", function (event) {
        event.stopPropagation();
        toggleMoreMenu();
      });
    }
    var menu = byId("dbMoreMenu");
    if (menu) {
      menu.addEventListener("click", function (event) {
        if (event.target.closest("button")) {
          closeMoreMenu();
        }
      });
    }
    document.addEventListener("click", function (event) {
      if (event.target.closest("#dbMoreMenu") || event.target.closest("#dbMoreBtn")) {
        return;
      }
      closeMoreMenu();
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape") {
        closeMoreMenu();
      }
    });
  }

  function bindWheelHorizontalScroll() {
    var selector = ".db-row-actions, .batch-actions-bar, .breadcrumb-bar, .table-wrap";
    document.addEventListener("wheel", function (event) {
      if (!dbTabActive()) {
        return;
      }
      var target = event.target.closest ? event.target.closest(selector) : null;
      if (!target) {
        return;
      }
      if (target.scrollWidth <= target.clientWidth + 1) {
        return;
      }
      if (target.scrollHeight > target.clientHeight + 1) {
        return;
      }
      if (Math.abs(event.deltaX) > Math.abs(event.deltaY)) {
        return;
      }
      var unit = 1;
      if (event.deltaMode === 1) {
        unit = 16;
      } else if (event.deltaMode === 2) {
        unit = target.clientWidth || 1;
      }
      target.scrollLeft += event.deltaY * unit;
      event.preventDefault();
    }, { passive: false });
  }

  function wireViewControls() {
    var map = { dbViewTable: "table", dbViewGrid: "grid", dbViewCompact: "compact" };
    Object.keys(map).forEach(function (id) {
      var btn = byId(id);
      if (btn) {
        btn.addEventListener("click", function () {
          setView(map[id]);
        });
      }
    });
    var manual = byId("manualOrderBtn");
    if (manual) {
      manual.addEventListener("click", toggleManualOrder);
      manual.classList.toggle("active", state.manualOrder);
    }
    updateViewButtons();
  }

  function init() {
    byId("dbUpBtn").addEventListener("click", function () {
      if (state.parentId !== null) {
        navigateTo(state.parentId);
      }
    });
    byId("dbRefreshBtn").addEventListener("click", refresh);
    byId("newFolderBtn").addEventListener("click", function () {
      window.Modals.openFolder(null);
    });
    byId("newSourceBtn").addEventListener("click", function () {
      window.Modals.openSource(null);
    });
    byId("emptyNewFolderBtn").addEventListener("click", function () {
      window.Modals.openFolder(null);
    });
    byId("emptyNewSourceBtn").addEventListener("click", function () {
      window.Modals.openSource(null);
    });
    byId("dbSearchInput").addEventListener("input", render);
    byId("batchRunBtn").addEventListener("click", handleBatchRun);
    byId("batchDeleteBtn").addEventListener("click", handleBatchDelete);
    byId("batchMoveSelect").addEventListener("change", handleBatchMove);
    byId("batchCancelBtn").addEventListener("click", clearSelection);
    byId("batchEditBtn").addEventListener("click", function () {
      var items = selectionList();
      if (items.length) {
        window.Modals.openBatchEdit(items);
      }
    });
    var keepButton = byId("keepFoldersTopBtn");
    keepButton.classList.toggle("active", state.keepTop);
    keepButton.addEventListener("click", function () {
      state.keepTop = !state.keepTop;
      keepButton.classList.toggle("active", state.keepTop);
      saveSortPrefs();
      render();
    });
    bindTouchUpgrade();
    bindRowPressGuards();
    wireToolbar();
    wireViewControls();
    bindWheelHorizontalScroll();
    bindThumbInteractions();
    ensureSortable();
    bindContextMenu();
    bindMouseNavigation();
    restoreSession();
  }

  var contextMenuEl = null;
  var contextMenuGuardUntil = 0;

  function onDocClickClose(event) {
    if (Date.now() < contextMenuGuardUntil) {
      return;
    }
    closeContextMenu();
  }

  function closeContextMenu() {
    if (contextMenuEl) {
      contextMenuEl.remove();
      contextMenuEl = null;
    }
    document.removeEventListener("click", onDocClickClose, true);
    document.removeEventListener("keydown", onContextEscape, true);
  }

  function onContextEscape(event) {
    if (event.key === "Escape") {
      closeContextMenu();
    }
  }

  function runSourceAction(action, item) {
    if (action === "run") {
      window.API.downloadSource({ source_id: item.id }).then(function (result) {
        showToast("[" + item.name + "] 수집 시작", "info");
        watchSources((result && result.started) || [{ id: item.id, name: item.name }]);
        refresh();
      }).catch(function (error) {
        showToast(error.message || "수집 요청 실패", "error");
      });
    } else if (action === "run-full") {
      window.API.downloadSource({ source_id: item.id, full: true }).then(function (result) {
        showToast("[" + item.name + "] 전체 수집 시작", "info");
        watchSources((result && result.started) || [{ id: item.id, name: item.name }]);
        refresh();
      }).catch(function (error) {
        showToast(error.message || "전체 수집 요청 실패", "error");
      });
    } else if (action === "alt-sync") {
      window.API.altSync("source", item.id, "both").then(function (result) {
        var rep = (result.reports || []).map(function (r) { return r.kind + ":" + (r.linked + r.copied); }).join(", ");
        showToast("대체경로 동기화 완료 (" + rep + ")", "success");
      }).catch(function (error) {
        showToast(error.message || "대체경로 동기화 실패", "error");
      });
    } else if (action === "alt-prune") {
      if (!window.confirm("원본에 없는 대체경로 파일을 제거할까요?")) {
        return;
      }
      window.API.altPrune(item.id, "both").then(function (result) {
        var removed = (result.reports || []).reduce(function (a, r) { return a + (r.removed || 0); }, 0);
        showToast("대체경로 정리: " + removed + "개 제거", "success");
      }).catch(function (error) {
        showToast(error.message || "정리 실패", "error");
      });
    } else if (action === "relearn") {
      window.API.relearnSource(item.id).then(function (result) {
        showToast("KDE 재학습: 이벤트 " + (result.added || 0) + "건 반영 (이력 " + (result.history_count || 0) + ")", "success");
        refresh();
      }).catch(function (error) {
        showToast(error.message || "재학습 실패", "error");
      });
    } else if (action === "edit") {
      window.Modals.openSource(item);
    } else if (action === "reveal") {
      window.API.revealSource(item.id).then(function () {
        showToast("탐색기를 열었습니다.", "success");
      }).catch(function (error) {
        showToast(error.message || "탐색기를 열 수 없습니다.", "error");
      });
    } else if (action === "locate") {
      highlightItem(itemKey(item));
    } else if (action === "open-url") {
      if (item.url) {
        window.open(item.url, "_blank");
      }
    } else if (action === "copy-url") {
      copyText(item.url || "");
    } else if (action === "delete") {
      if (!window.confirm("[" + (item.name || item.url) + "] 소스를 삭제할까요?")) {
        return;
      }
      window.API.deleteSource(item.id).then(function () {
        showToast("삭제되었습니다.", "success");
        refresh();
      }).catch(function (error) {
        showToast(error.message || "삭제 실패", "error");
      });
    }
  }

  function runFolderAction(action, item) {
    if (action === "edit") {
      window.Modals.openFolder(item);
    } else if (action === "reveal") {
      window.API.revealFolder(item.id).then(function () {
        showToast("탐색기를 열었습니다.", "success");
      }).catch(function (error) {
        showToast(error.message || "탐색기를 열 수 없습니다.", "error");
      });
    } else if (action === "open") {
      navigateTo(item.id);
    } else if (action === "alt-sync-folder") {
      window.API.altSync("folder", item.id, "both").then(function () {
        showToast("이 폴더의 대체경로 동기화 완료", "success");
      }).catch(function (error) {
        showToast(error.message || "대체경로 동기화 실패", "error");
      });
    } else if (action === "relearn-folder") {
      window.API.relearnFolder(item.id).then(function (result) {
        showToast("하위 재학습: 소스 " + (result.sources || 0) + "개, 이벤트 " + (result.added || 0) + "건", "success");
        refresh();
      }).catch(function (error) {
        showToast(error.message || "재학습 실패", "error");
      });
    } else if (action === "copy-path") {
      copyText(item.save_path || "");
    } else if (action === "delete") {
      if (!window.confirm("[" + item.name + "] 폴더를 삭제할까요?")) {
        return;
      }
      window.API.deleteFolder(item.id).then(function () {
        showToast("삭제되었습니다.", "success");
        refresh();
      }).catch(function (error) {
        showToast(error.message || "삭제 실패", "error");
      });
    }
  }

  function copyText(text) {
    if (!text) {
      return;
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(function () {
        showToast("복사되었습니다.", "success");
      }).catch(function () {});
    } else {
      showToast(text, "info");
    }
  }

  function openContextMenu(event, item) {
    closeContextMenu();
    var entries;
    if (item.type === "source") {
      entries = [
        ["run", "⚡ 즉시 수집"],
        ["run-full", "🌐 전체 수집 (범위 없음)"],
        ["relearn", "🧠 KDE 재학습 (파일 mtime)"],
        ["alt-sync", "🔗 대체경로 동기화 (alt+cmb)"],
        ["alt-prune", "🧹 대체경로 정리"],
        ["edit", "⚙️ 편집"],
        ["reveal", "📂 폴더 열기 (탐색기)"],
        ["locate", "📍 위치로 이동"],
        ["open-url", "🔗 URL 열기"],
        ["copy-url", "📋 URL 복사"],
        ["delete", "🗑️ 삭제"]
      ];
    } else {
      entries = [
        ["open", "📂 폴더 열기 (진입)"],
        ["edit", "⚙️ 편집"],
        ["reveal", "🗂️ 저장 위치 열기 (탐색기)"],
        ["relearn-folder", "🧠 하위 전체 KDE 재학습"],
        ["alt-sync-folder", "🔗 대체경로 동기화 (이 폴더)"],
        ["copy-path", "📋 경로 복사"],
        ["delete", "🗑️ 삭제"]
      ];
    }
    var menu = document.createElement("div");
    menu.className = "context-menu";
    entries.forEach(function (entry) {
      var button = document.createElement("button");
      button.type = "button";
      button.className = "context-item" + (entry[0] === "delete" ? " danger" : "");
      button.textContent = entry[1];
      button.addEventListener("click", function (clickEvent) {
        clickEvent.stopPropagation();
        closeContextMenu();
        if (item.type === "source") {
          runSourceAction(entry[0], item);
        } else {
          runFolderAction(entry[0], item);
        }
      });
      menu.appendChild(button);
    });
    document.body.appendChild(menu);
    var x = event.clientX;
    var y = event.clientY;
    var width = menu.offsetWidth;
    var height = menu.offsetHeight;
    x = Math.min(x, window.innerWidth - width - 8);
    y = Math.min(y, window.innerHeight - height - 8);
    menu.style.left = Math.max(8, x) + "px";
    menu.style.top = Math.max(8, y) + "px";
    contextMenuEl = menu;
    document.addEventListener("click", onDocClickClose, true);
    document.addEventListener("keydown", onContextEscape, true);
  }

  function bindContextMenu() {
    var view = byId("explorerMainView");
    if (!view) {
      return;
    }
    view.addEventListener("contextmenu", function (event) {
      var el = event.target.closest ? event.target.closest(".db-item") : null;
      if (!el || !el.dataset.key) {
        return;
      }
      event.preventDefault();
      // 터치 롱프레스의 contextmenu 는 무시 (롱프레스는 DnD 전용)
      if (lastPointerType === "touch") {
        return;
      }
      var item = itemByKey(el.dataset.key);
      if (!item) {
        return;
      }
      openContextMenu(event, item);
    });
  }

  function highlightItem(itemKeyValue) {
    Array.prototype.forEach.call(document.querySelectorAll(".db-item"), function (el) {
      el.classList.toggle("locate-flash", el.dataset.key === itemKeyValue);
    });
    var target = document.querySelector('.db-item[data-key="' + itemKeyValue + '"]');
    if (target && target.scrollIntoView) {
      target.scrollIntoView({ block: "center", behavior: "smooth" });
    }
    window.setTimeout(function () {
      Array.prototype.forEach.call(document.querySelectorAll(".db-item.locate-flash"), function (el) {
        el.classList.remove("locate-flash");
      });
    }, 2600);
  }

  // 로그 등 외부에서 특정 소스의 위치(폴더)로 이동 + 하이라이트
  function goToSource(sourceId, openEditor) {
    return window.API.getSource(sourceId).then(function (source) {
      if (!source) {
        return;
      }
      var key = "source:" + source.id;
      return loadFolder(source.folder_id).then(function () {
        highlightItem(key);
        if (openEditor) {
          window.Modals.openSource(source);
        }
      });
    }).catch(function (error) {
      if (window.Toast) {
        window.Toast.show(error.message || "소스를 찾을 수 없습니다.", "error");
      }
    });
  }

  // 특정 폴더로 이동 (프로파일 등에서 폴더 위치로 이동)
  function goToFolder(folderId) {
    navHistory = navHistory.slice(0, navIndex + 1);
    navHistory.push(folderId);
    navIndex = navHistory.length - 1;
    return loadFolder(folderId);
  }

  window.DBExplorer = {
    init: init,
    refresh: refresh,
    render: render,
    loadFolder: loadFolder,
    getCurrentFolderId: function () { return state.folderId; },
    getColumnConfig: getColumnConfig,
    setColumnConfig: setColumnConfig,
    getColumnDef: columnDef,
    goToSource: goToSource,
    goToFolder: goToFolder
  };
  window.DB = window.DBExplorer;
})(window, document);
