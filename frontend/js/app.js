(function (window, document) {
  "use strict";

  var Toast = {
    show: function (message, type) {
      var container = document.getElementById("toastContainer");
      if (!container) {
        return;
      }
      var toast = document.createElement("div");
      toast.className = "toast toast-" + (type || "info");
      toast.textContent = message;
      container.appendChild(toast);
      window.setTimeout(function () {
        toast.classList.add("toast-visible");
      }, 10);
      window.setTimeout(function () {
        toast.classList.remove("toast-visible");
        window.setTimeout(function () {
          toast.remove();
        }, 300);
      }, 3400);
    }
  };
  window.Toast = Toast;

  function loadSettings() {
    return window.API.getSettings().then(function (settings) {
      var autoRefresh = document.getElementById("autoRefreshToggle");
      var maxLogCount = document.getElementById("maxLogCountInput");
      var logLevel = document.getElementById("systemLogLevelSelect");
      var offsetBuffer = document.getElementById("offsetBufferInput");
      var schedulerEnabled = document.getElementById("schedulerEnabledToggle");
      var maxConcurrency = document.getElementById("maxConcurrencyInput");

      if (autoRefresh) {
        autoRefresh.checked = Boolean(settings.autoRefresh);
      }
      if (maxLogCount && settings.maxLogCount != null) {
        maxLogCount.value = settings.maxLogCount;
      }
      if (logLevel && settings.systemLogLevel) {
        logLevel.value = settings.systemLogLevel;
      }
      if (offsetBuffer && settings.offset_buffer != null) {
        offsetBuffer.value = settings.offset_buffer;
      }
      if (schedulerEnabled) {
        schedulerEnabled.checked = settings.schedulerEnabled !== false;
      }
      if (maxConcurrency && settings.maxConcurrency != null) {
        maxConcurrency.value = settings.maxConcurrency;
      }
      var dbThumbnails = document.getElementById("dbThumbnailsToggle");
      if (dbThumbnails) {
        dbThumbnails.checked = settings.dbThumbnails !== false;
      }
      var closeToTray = document.getElementById("closeToTrayToggle");
      if (closeToTray) {
        closeToTray.checked = settings.closeToTray !== false;
      }
      var checkUpdate = document.getElementById("checkUpdateOnStartToggle");
      if (checkUpdate) {
        checkUpdate.checked = settings.checkUpdateOnStart !== false;
      }
      var autoUpdate = document.getElementById("autoUpdateToggle");
      if (autoUpdate) {
        autoUpdate.checked = settings.autoUpdate === true;
      }
      var browserChannel = document.getElementById("browserChannelSelect");
      if (browserChannel && settings.browserChannel) {
        browserChannel.value = settings.browserChannel;
      }
      var stealth = document.getElementById("stealthToggle");
      if (stealth) {
        stealth.checked = settings.stealthEnabled !== false;
      }
      var cleanup = document.getElementById("browserCleanupToggle");
      if (cleanup) {
        cleanup.checked = settings.browserCleanup !== false;
      }
      var itemCycOption = document.getElementById("itemCycOptionSelect");
      if (itemCycOption && settings.itemCycOption != null) {
        itemCycOption.value = String(settings.itemCycOption);
      }
      var itemCyc = document.getElementById("itemCycInput");
      if (itemCyc && settings.itemCyc != null) {
        itemCyc.value = settings.itemCyc;
      }
      var itemLogLevel = document.getElementById("itemLogLevelSelect");
      if (itemLogLevel && settings.itemLogLevel) {
        itemLogLevel.value = settings.itemLogLevel;
      }
      var itemConfigArgs = document.getElementById("itemConfigArgs");
      var itemConfigDateMode = document.getElementById("itemConfigDateMode");
      var itemConfig = settings.itemConfig || {};
      if (itemConfigArgs) {
        itemConfigArgs.value = Array.isArray(itemConfig.args) ? itemConfig.args.join("\n") : "";
      }
      if (itemConfigDateMode && itemConfig.date_mode) {
        itemConfigDateMode.value = itemConfig.date_mode;
      }
      var kdeBackfill = document.getElementById("kdeBackfillToggle");
      if (kdeBackfill) {
        kdeBackfill.checked = settings.kdeBackfillOnFirstRun !== false;
      }
      var kdeCluster = document.getElementById("kdeClusterInput");
      if (kdeCluster && settings.kdeClusterMinutes != null) {
        kdeCluster.value = settings.kdeClusterMinutes;
      }
      var itemRetryDelay = document.getElementById("itemRetryDelayInput");
      if (itemRetryDelay && settings.itemRetryDelay != null) {
        itemRetryDelay.value = settings.itemRetryDelay;
      }
      function setVal(id, value) {
        var el = document.getElementById(id);
        if (el && value != null) {
          el.value = value;
        }
      }
      var dateBasis = document.getElementById("itemDateBasisSelect");
      if (dateBasis && settings.itemDateBasis) {
        dateBasis.value = settings.itemDateBasis;
      }
      setVal("itemSleepMinInput", settings.itemSleepMin);
      setVal("itemSleepMaxInput", settings.itemSleepMax);
      setVal("itemSleepReqMinInput", settings.itemSleepRequestMin);
      setVal("itemSleepReqMaxInput", settings.itemSleepRequestMax);
      setVal("itemRetriesInput", settings.itemRetries);
      setVal("itemTimeoutInput", settings.itemTimeout);
      setVal("itemLimitRateInput", settings.itemLimitRate);
      var itemYaml = document.getElementById("itemMetadataYamlToggle");
      if (itemYaml) {
        itemYaml.checked = settings.itemMetadataYaml === true;
      }
      var titleAsName = document.getElementById("itemTitleAsNameToggle");
      if (titleAsName) {
        titleAsName.checked = settings.itemTitleAsName !== false;
      }
      var nameOnCollect = document.getElementById("itemNameUpdateOnCollectToggle");
      if (nameOnCollect) {
        nameOnCollect.checked = settings.itemNameUpdateOnCollect !== false;
      }
      var metaYaml = document.getElementById("metaYamlToggle");
      if (metaYaml) {
        metaYaml.checked = settings.itemMetadataYaml === true;
      }
      window.AppSettings = settings;
      savedSettings = collectSettings();
      updateDirtyCount();
      if (window.DBExplorer && window.DBExplorer.render) {
        window.DBExplorer.render();
      }
      if (window.Update && settings.checkUpdateOnStart !== false) {
        window.Update.check();
      }
    }).catch(function () {});
  }

  var savedSettings = null;

  function collectSettings() {
    var offsetBuffer = document.getElementById("offsetBufferInput");
    var maxConcurrency = document.getElementById("maxConcurrencyInput");
    var schedulerEnabled = document.getElementById("schedulerEnabledToggle");
    var argsRaw = document.getElementById("itemConfigArgs")
      ? document.getElementById("itemConfigArgs").value : "";
    var argsList = argsRaw.split(/[\n,]/).map(function (token) {
      return token.trim();
    }).filter(function (token) {
      return token.length > 0;
    });
    var dateMode = document.getElementById("itemConfigDateMode")
      ? document.getElementById("itemConfigDateMode").value : "incremental";
    return {
      itemCycOption: parseInt(document.getElementById("itemCycOptionSelect").value, 10) || 0,
      itemCyc: parseInt(document.getElementById("itemCycInput").value, 10) || 1,
      itemLogLevel: document.getElementById("itemLogLevelSelect").value,
      itemConfig: { args: argsList, date_mode: dateMode },
      itemDateBasis: document.getElementById("itemDateBasisSelect")
        ? document.getElementById("itemDateBasisSelect").value : "filter",
      itemSleepMin: parseFloat((document.getElementById("itemSleepMinInput") || {}).value) || 1.3,
      itemSleepMax: parseFloat((document.getElementById("itemSleepMaxInput") || {}).value) || 8,
      itemSleepRequestMin: parseFloat((document.getElementById("itemSleepReqMinInput") || {}).value) || 5,
      itemSleepRequestMax: parseFloat((document.getElementById("itemSleepReqMaxInput") || {}).value) || 8,
      itemRetries: parseInt((document.getElementById("itemRetriesInput") || {}).value, 10) || 0,
      itemTimeout: parseFloat((document.getElementById("itemTimeoutInput") || {}).value) || 30,
      itemLimitRate: (document.getElementById("itemLimitRateInput") || {}).value || "",
      itemMetadataYaml: document.getElementById("itemMetadataYamlToggle")
        ? document.getElementById("itemMetadataYamlToggle").checked : false,
      itemTitleAsName: document.getElementById("itemTitleAsNameToggle")
        ? document.getElementById("itemTitleAsNameToggle").checked : true,
      itemNameUpdateOnCollect: document.getElementById("itemNameUpdateOnCollectToggle")
        ? document.getElementById("itemNameUpdateOnCollectToggle").checked : true,
      kdeBackfillOnFirstRun: document.getElementById("kdeBackfillToggle")
        ? document.getElementById("kdeBackfillToggle").checked : true,
      kdeClusterMinutes: document.getElementById("kdeClusterInput")
        ? (parseFloat(document.getElementById("kdeClusterInput").value) || 5) : 5,
      itemRetryDelay: document.getElementById("itemRetryDelayInput")
        ? (parseFloat(document.getElementById("itemRetryDelayInput").value) || 0) : 10,
      autoRefresh: document.getElementById("autoRefreshToggle").checked,
      maxLogCount: parseInt(document.getElementById("maxLogCountInput").value, 10) || 100,
      systemLogLevel: document.getElementById("systemLogLevelSelect").value,
      offset_buffer: offsetBuffer ? parseFloat(offsetBuffer.value) || 0 : 0,
      schedulerEnabled: schedulerEnabled ? schedulerEnabled.checked : true,
      maxConcurrency: maxConcurrency ? (parseInt(maxConcurrency.value, 10) || 3) : 3,
      dbThumbnails: document.getElementById("dbThumbnailsToggle")
        ? document.getElementById("dbThumbnailsToggle").checked
        : true,
      closeToTray: document.getElementById("closeToTrayToggle")
        ? document.getElementById("closeToTrayToggle").checked
        : true,
      checkUpdateOnStart: document.getElementById("checkUpdateOnStartToggle")
        ? document.getElementById("checkUpdateOnStartToggle").checked
        : true,
      autoUpdate: document.getElementById("autoUpdateToggle")
        ? document.getElementById("autoUpdateToggle").checked
        : false,
      browserChannel: document.getElementById("browserChannelSelect")
        ? document.getElementById("browserChannelSelect").value
        : "auto",
      stealthEnabled: document.getElementById("stealthToggle")
        ? document.getElementById("stealthToggle").checked
        : true,
      browserCleanup: document.getElementById("browserCleanupToggle")
        ? document.getElementById("browserCleanupToggle").checked
        : true
    };
  }

  function stableString(value) {
    if (value === null || value === undefined) {
      return "";
    }
    if (typeof value === "object") {
      try {
        return JSON.stringify(value);
      } catch (error) {
        return String(value);
      }
    }
    return String(value);
  }

  function updateDirtyCount() {
    var el = document.getElementById("settingsDirty");
    if (!el) {
      return;
    }
    if (!savedSettings) {
      el.textContent = "";
      return;
    }
    var current = collectSettings();
    var keys = {};
    Object.keys(savedSettings).forEach(function (k) { keys[k] = true; });
    Object.keys(current).forEach(function (k) { keys[k] = true; });
    var changed = 0;
    Object.keys(keys).forEach(function (key) {
      if (stableString(savedSettings[key]) !== stableString(current[key])) {
        changed += 1;
      }
    });
    el.textContent = changed > 0 ? changed + "개 변경됨" : "";
    el.classList.toggle("has-changes", changed > 0);
  }

  function bindDirtyTracking() {
    var form = document.getElementById("settingsForm");
    if (!form) {
      return;
    }
    form.addEventListener("input", updateDirtyCount);
    form.addEventListener("change", updateDirtyCount);
  }

  function saveSettings(event) {
    event.preventDefault();
    var payload = collectSettings();
    window.API.saveSettings(payload).then(function (settings) {
      window.AppSettings = settings || payload;
      savedSettings = payload;
      updateDirtyCount();
      if (window.DBExplorer && window.DBExplorer.render) {
        window.DBExplorer.render();
      }
      Toast.show("설정이 저장되었습니다.", "success");
    }).catch(function (error) {
      Toast.show(error.message || "설정 저장 실패", "error");
    });
  }

  function bindSettingsForm() {
    var form = document.getElementById("settingsForm");
    if (!form) {
      return;
    }
    form.addEventListener("submit", saveSettings);
  }

  function metaPayload(dryRun) {
    var yamlEl = document.getElementById("metaYamlToggle");
    var altEl = document.getElementById("metaIncludeAltToggle");
    return {
      dry_run: Boolean(dryRun),
      yaml: yamlEl ? yamlEl.checked === true : undefined,
      include_alt: altEl ? altEl.checked === true : true
    };
  }

  function metaSummary(result, dryRun) {
    var total = (result && result.total) || {};
    var count = result && result.target_count != null ? result.target_count : 0;
    if (dryRun) {
      return "미리보기: 미격리 JSON " + (total.stray_before || 0) +
        "개 · 대상 " + count + "곳";
    }
    return "정리 완료: 이동 " + (total.moved || 0) +
      " · YAML " + (total.converted || 0) + " · 대상 " + count + "곳";
  }

  function runMetadataAll(dryRun) {
    var status = document.getElementById("metaStatusText");
    if (status) {
      status.textContent = "처리 중...";
    }
    window.API.metadataMigrateAll(metaPayload(dryRun)).then(function (result) {
      var text = metaSummary(result, dryRun);
      if (status) {
        status.textContent = text;
      }
      Toast.show(text, "success");
    }).catch(function (error) {
      if (status) {
        status.textContent = "";
      }
      Toast.show(error.message || "메타데이터 정리 실패", "error");
    });
  }

  function bindMetadataTools() {
    var scan = document.getElementById("metaScanAllBtn");
    if (scan) {
      scan.addEventListener("click", function () {
        runMetadataAll(true);
      });
    }
    var run = document.getElementById("metaMigrateAllBtn");
    if (run) {
      run.addEventListener("click", function () {
        runMetadataAll(false);
      });
    }
  }

  function init() {
    if (window.Help) {
      window.Help.init();
    }
    window.Nav.init();
    window.Modals.init();
    window.DBExplorer.init();
    if (window.Home) {
      window.Home.init();
    }
    if (window.Update) {
      window.Update.init();
    }
    if (window.Profiles) {
      window.Profiles.init();
    }
    if (window.Rules) {
      window.Rules.init();
    }
    if (window.Watcher) {
      window.Watcher.init();
    }
    if (window.Logs) {
      window.Logs.init();
    }
    if (window.SiteSuggestions) {
      window.SiteSuggestions.ensure();
    }
    bindSettingsForm();
    bindDirtyTracking();
    bindMetadataTools();
    loadSettings();
  }

  document.addEventListener("DOMContentLoaded", init);
})(window, document);
