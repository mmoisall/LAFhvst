(function (window, document) {
  "use strict";

  var state = {
    loaded: false,
    loading: null,
    used: [],
    supported: []
  };

  var hintBindings = [];

  function byId(id) {
    return document.getElementById(id);
  }

  function lower(value) {
    return String(value === null || value === undefined ? "" : value).trim().toLowerCase();
  }

  function render() {
    var list = byId("siteOptions");
    if (!list) {
      return;
    }
    var seen = {};
    var html = [];
    function add(value, label) {
      var text = String(value || "").trim();
      if (!text) {
        return;
      }
      var key = text.toLowerCase();
      if (seen[key]) {
        return;
      }
      seen[key] = true;
      html.push('<option value="' + escapeAttr(text) + '"' +
        (label ? ' label="' + escapeAttr(label) + '"' : "") + "></option>");
    }
    state.used.forEach(function (site) { add(site, "사용됨"); });
    state.supported.forEach(function (site) { add(site, null); });
    list.innerHTML = html.join("");
  }

  function escapeAttr(value) {
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function ensure(force) {
    if (state.loaded && !force) {
      return Promise.resolve(state);
    }
    if (state.loading && !force) {
      return state.loading;
    }
    state.loading = window.API.getSites().then(function (data) {
      state.used = (data && data.used) || [];
      state.supported = (data && data.supported) || [];
      state.loaded = true;
      state.loading = null;
      render();
      return state;
    }).catch(function () {
      state.loading = null;
      if (!state.loaded) {
        render();
      }
      return state;
    });
    return state.loading;
  }

  function invalidate() {
    state.loaded = false;
    state.loading = null;
    return ensure(true);
  }

  function extractHost(url) {
    var text = String(url || "").trim();
    if (!text) {
      return "";
    }
    try {
      var parsed = new window.URL(text.indexOf("://") >= 0 ? text : ("https://" + text));
      return lower(parsed.hostname);
    } catch (error) {
      var match = text.match(/^(?:[a-z]+:\/\/)?([^\/?#:]+)/i);
      return match ? lower(match[1]) : "";
    }
  }

  function suggestFromUrl(url) {
    var host = extractHost(url).replace(/^www\./, "");
    if (!host) {
      return "";
    }
    var candidates = state.supported.concat(state.used);
    var best = "";
    var seen = {};
    candidates.forEach(function (site) {
      var token = lower(site);
      if (token.length < 3 || seen[token]) {
        return;
      }
      seen[token] = true;
      if (host.indexOf(token) >= 0 && token.length > best.length) {
        best = token;
      }
    });
    if (best) {
      return best;
    }
    return registrableLabel(host);
  }

  function registrableLabel(host) {
    var labels = host.split(".").filter(function (part) { return part.length > 0; });
    if (!labels.length) {
      return "";
    }
    if (labels.length === 1) {
      return labels[0];
    }
    var tld = labels[labels.length - 1];
    var sld = labels[labels.length - 2];
    var compound = ["co", "com", "net", "org", "gov", "edu", "ac"];
    var index = (tld.length === 2 && compound.indexOf(sld) >= 0 && labels.length >= 3)
      ? labels.length - 3
      : labels.length - 2;
    if ((labels[index] || "").length < 3) {
      return host;
    }
    return labels[index];
  }

  function refreshHints() {
    hintBindings.forEach(function (update) {
      update();
    });
  }

  function bindUrlHint(urlInputId, siteInputId, hintId) {
    var urlInput = byId(urlInputId);
    var siteInput = byId(siteInputId);
    var hint = byId(hintId);
    if (!urlInput || !siteInput || !hint || urlInput.dataset.siteHintBound === "1") {
      return;
    }
    urlInput.dataset.siteHintBound = "1";
    var current = "";

    function update() {
      ensure().then(function () {
        var suggestion = suggestFromUrl(urlInput.value);
        var existing = lower(siteInput.value);
        if (suggestion && suggestion !== existing) {
          current = suggestion;
          hint.textContent = "추정 사이트: " + suggestion + " (클릭하여 적용)";
          hint.classList.remove("hidden");
        } else {
          current = "";
          hint.classList.add("hidden");
        }
      });
    }

    function apply() {
      if (current) {
        siteInput.value = current;
        hint.classList.add("hidden");
      }
    }

    urlInput.addEventListener("input", update);
    urlInput.addEventListener("blur", update);
    hint.addEventListener("click", apply);
    hint.addEventListener("keydown", function (event) {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        apply();
      }
    });
    hintBindings.push(update);
  }

  window.SiteSuggestions = {
    ensure: ensure,
    invalidate: invalidate,
    render: render,
    suggestFromUrl: suggestFromUrl,
    bindUrlHint: bindUrlHint,
    refreshHints: refreshHints
  };
})(window, document);
