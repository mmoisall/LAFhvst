(function (window, document) {
  "use strict";

  var DEBOUNCE_MS = 250;
  var bindings = [];

  function byId(id) {
    return document.getElementById(id);
  }

  function bind(config) {
    var url = byId(config.url);
    var site = byId(config.site);
    var key = byId(config.key);
    if (!url && !site && !key) {
      return null;
    }
    var auto = { url: false, site: false, key: false };
    var timer = null;

    function schedule(fn) {
      if (timer) {
        window.clearTimeout(timer);
      }
      timer = window.setTimeout(function () {
        timer = null;
        fn();
      }, DEBOUNCE_MS);
    }

    function canWrite(element, name) {
      if (!element) {
        return false;
      }
      return !element.value.trim() || auto[name];
    }

    function resolveFromUrl() {
      var value = url ? url.value.trim() : "";
      if (!value || !window.API || !window.API.resolveUrl) {
        return;
      }
      window.API.resolveUrl(value).then(function (data) {
        if (!data) {
          return;
        }
        if (data.site && canWrite(site, "site") && site.value.trim() !== data.site) {
          site.value = data.site;
          auto.site = true;
        }
        if (data.key && canWrite(key, "key") && key.value.trim() !== data.key) {
          key.value = data.key;
          auto.key = true;
        }
      }).catch(function () {});
    }

    function buildFromSiteKey() {
      var siteValue = site ? site.value.trim() : "";
      var keyValue = key ? key.value.trim() : "";
      if (!siteValue || !window.API || !window.API.buildUrl) {
        return;
      }
      window.API.buildUrl(siteValue, keyValue).then(function (data) {
        if (data && data.url && canWrite(url, "url") && url.value.trim() !== data.url) {
          url.value = data.url;
          auto.url = true;
        }
      }).catch(function () {});
    }

    function onUrlInput() {
      auto.url = false;
      schedule(resolveFromUrl);
    }
    function onSiteKeyInput(name) {
      return function () {
        auto[name] = false;
        schedule(buildFromSiteKey);
      };
    }

    if (url) {
      url.addEventListener("input", onUrlInput);
    }
    if (site) {
      site.addEventListener("input", onSiteKeyInput("site"));
      site.addEventListener("change", onSiteKeyInput("site"));
    }
    if (key) {
      key.addEventListener("input", onSiteKeyInput("key"));
    }

    var controller = {
      reset: function () {
        auto.url = false;
        auto.site = false;
        auto.key = false;
      },
      sync: function () {
        var hasUrl = url && url.value.trim();
        var hasSite = site && site.value.trim();
        if (hasUrl && ((site && !site.value.trim()) || (key && !key.value.trim()))) {
          resolveFromUrl();
        } else if (hasSite && url && !url.value.trim()) {
          buildFromSiteKey();
        }
      }
    };
    bindings.push(controller);
    return controller;
  }

  function refreshAll() {
    bindings.forEach(function (controller) {
      controller.reset();
      controller.sync();
    });
  }

  window.UrlSync = {
    bind: bind,
    refresh: refreshAll,
    reset: function () {
      bindings.forEach(function (controller) {
        controller.reset();
      });
    }
  };
})(window, document);
