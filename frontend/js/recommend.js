(function (window, document) {
  "use strict";

  var state = { data: null, timer: null };

  function byId(id) {
    return document.getElementById(id);
  }

  function esc(text) {
    return String(text == null ? "" : text)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function compact(value) {
    var number = Number(value) || 0;
    if (number >= 1000000) {
      return (number / 1000000).toFixed(1).replace(/\.0$/, "") + "M";
    }
    if (number >= 1000) {
      return (number / 1000).toFixed(1).replace(/\.0$/, "") + "k";
    }
    return String(number);
  }

  function fmtDate(iso) {
    if (!iso) {
      return "날짜 없음";
    }
    return String(iso).replace("T", " ").slice(0, 16);
  }

  function fmtGap(hours) {
    if (hours == null) {
      return "간격 정보 없음";
    }
    if (hours < 24) {
      return "중앙 간격 " + Math.round(hours) + "시간";
    }
    return "중앙 간격 " + (hours / 24).toFixed(1) + "일";
  }

  function request(path, options) {
    var config = { method: (options && options.method) || "GET" };
    if (options && options.body != null) {
      config.headers = { "Content-Type": "application/json" };
      config.body = JSON.stringify(options.body);
    }
    return fetch(window.location.origin + path, config).then(function (response) {
      return response.json().catch(function () {
        return {};
      }).then(function (data) {
        if (!response.ok) {
          throw new Error(data.detail || data.error || ("요청 실패: " + response.status));
        }
        return data;
      });
    });
  }

  // ------------------------------------------------------------------ render
  function metricsChips(post) {
    if (!post.has_engagement) {
      return '<span class="rec-chip">반응 지표 없음</span>';
    }
    var chips = [];
    if (post.likes) {
      chips.push('<span class="rec-chip">♥ ' + compact(post.likes) + "</span>");
    }
    if (post.retweets) {
      chips.push('<span class="rec-chip">RT ' + compact(post.retweets) + "</span>");
    }
    if (post.bookmarks) {
      chips.push('<span class="rec-chip">★ ' + compact(post.bookmarks) + "</span>");
    }
    if (post.views) {
      chips.push('<span class="rec-chip">조회 ' + compact(post.views) + "</span>");
    }
    if (!chips.length) {
      chips.push('<span class="rec-chip">반응 0</span>');
    }
    return chips.join("");
  }

  function postCard(post, index, kind) {
    var rank = "";
    if (kind === "popular" && index < 10) {
      rank = '<span class="rec-rank">' + (index + 1) + "</span>";
    }
    var gauge = "";
    if (kind === "popular" && post.score != null) {
      gauge = '<div class="rec-gauge"><span style="width:' +
        Math.round(Math.max(0, Math.min(1, post.score)) * 100) + '%"></span></div>';
    }
    return '<div class="rec-card-wrap">' + rank +
      '<article class="rec-card" data-post="' + post.id + '">' +
      '<img class="rec-thumb" loading="lazy" src="' + esc(post.thumbnail) +
      '" alt="' + esc(post.uploader_name || post.post_id) + '"' +
      ' onerror="this.style.opacity=0.15">' +
      '<div class="rec-card-body">' +
      '<span class="rec-name">' + esc(post.uploader_name || post.uploader || "-") + "</span>" +
      '<span class="rec-sub">' + esc(post.site) + " · " + esc(fmtDate(post.posted_at)) +
      (post.media_count > 1 ? " · 미디어 " + post.media_count : "") + "</span>" +
      '<div class="rec-metrics">' + metricsChips(post) + "</div>" +
      gauge +
      "</div></article></div>";
  }

  function uploaderCard(item) {
    var badge = item.is_rare
      ? '<span class="rec-chip rare">희소</span>'
      : '<span class="rec-chip">' + item.posts_in_range + "건</span>";
    return '<article class="rec-uploader" data-uploader="' +
      esc(item.uploader) + '" data-site="' + esc(item.site) + '">' +
      '<img loading="lazy" src="' + esc(item.thumbnail) + '" alt="" onerror="this.style.opacity=0.15">' +
      '<div class="rec-uploader-info">' +
      "<strong>" + esc(item.name || item.uploader) + "</strong>" +
      '<span class="muted rec-sub">' + esc(item.site) + " · " + item.posts_in_range + "건 · " +
      esc(fmtGap(item.gap_median_hours)) + "</span>" +
      '<div class="rec-metrics">' + badge +
      '<span class="rec-chip">희소도 ' + Math.round((item.rarity || 0) * 100) + "</span></div>" +
      "</div></article>";
  }

  function emptyBox(text) {
    return '<p class="rec-empty">' + text + "</p>";
  }

  function render() {
    var data = state.data;
    if (!data) {
      return;
    }
    var stats = data.stats || {};

    byId("recentMeta").textContent = (data.recent || []).length + "건";
    byId("popularMeta").textContent = (data.popular || []).length + "건";
    byId("rareMeta").textContent = (data.rare_uploaders || []).length + "명";

    byId("recentGrid").innerHTML = (data.recent || []).length
      ? data.recent.map(function (post, index) {
          return postCard(post, index, "recent");
        }).join("")
      : emptyBox("이 기간에 색인된 게시물이 없습니다. <b>색인 갱신</b>을 눌러보세요.");

    byId("popularGrid").innerHTML = (data.popular || []).length
      ? data.popular.map(function (post, index) {
          return postCard(post, index, "popular");
        }).join("")
      : emptyBox(
          "반응 지표(좋아요/리트윗 등)가 있는 게시물이 없습니다.<br>" +
          "수집 시 gallery-dl 메타데이터가 함께 저장되면 자동으로 채워집니다. " +
          "(이 기간 " + (stats.window_posts || 0) + "건 중 지표 보유 " +
          (stats.window_with_engagement || 0) + "건)"
        );

    byId("rareGrid").innerHTML = (data.rare_uploaders || []).length
      ? data.rare_uploaders.map(uploaderCard).join("")
      : emptyBox("이 기간에 업로더 정보가 없습니다.");

    var rangeLabel = data.days > 0 ? "최근 " + data.days + "일" : "전체 기간";
    byId("recStatus").textContent =
      rangeLabel + " · 색인 " + (stats.indexed_posts || 0) + "건 / 소스 " +
      (stats.indexed_sources || 0) + "개" +
      (stats.last_indexed_at ? " · 마지막 색인 " + fmtDate(stats.last_indexed_at) : "");

    var select = byId("recSite");
    var current = select.value;
    var options = ['<option value="">전체 사이트</option>'];
    (data.sites || []).forEach(function (site) {
      options.push('<option value="' + esc(site) + '">' + esc(site) + "</option>");
    });
    select.innerHTML = options.join("");
    select.value = current;
  }

  // ------------------------------------------------------------------- modal
  function openModal(postId) {
    var data = state.data || {};
    var pool = (data.recent || []).concat(data.popular || []);
    var post = null;
    for (var i = 0; i < pool.length; i += 1) {
      if (String(pool[i].id) === String(postId)) {
        post = pool[i];
        break;
      }
    }
    if (!post) {
      (data.rare_uploaders || []).forEach(function (item) {
        if (item.best && String(item.best.id) === String(postId)) {
          post = item.best;
        }
      });
    }
    if (!post) {
      return;
    }
    byId("recModalImage").src = post.thumbnail + "?full=1";
    byId("recModalImage").alt = post.uploader_name || post.post_id;
    var rows = [
      ["업로더", post.uploader_name || post.uploader],
      ["사이트", post.site],
      ["게시일", fmtDate(post.posted_at)],
      ["게시물 ID", post.post_id],
      ["미디어", post.media_count + "개"],
      ["좋아요", post.has_engagement ? compact(post.likes) : "-"],
      ["리트윗", post.has_engagement ? compact(post.retweets) : "-"],
      ["북마크", post.has_engagement ? compact(post.bookmarks) : "-"],
      ["조회", post.has_engagement ? compact(post.views) : "-"],
      ["지표 출처", post.origin === "metadata" ? "메타데이터" : "파일명/mtime"],
    ];
    var body = "<h3>" + esc(post.uploader_name || post.uploader || "게시물") + "</h3>";
    if (post.title) {
      body += '<p class="muted">' + esc(String(post.title).slice(0, 400)) + "</p>";
    }
    rows.forEach(function (pair) {
      body += '<div class="rec-kv"><b>' + esc(pair[0]) + "</b><span>" + esc(pair[1]) + "</span></div>";
    });
    if (post.tags && post.tags.length) {
      body += '<div class="rec-kv"><b>태그</b><span>' +
        esc(post.tags.slice(0, 20).join(", ")) + "</span></div>";
    }
    if (post.url) {
      body += '<a class="rec-link" href="' + esc(post.url) +
        '" target="_blank" rel="noopener noreferrer">원문 열기 ↗</a>';
    }
    byId("recModalBody").innerHTML = body;
    byId("recModal").classList.remove("hidden");
  }

  function closeModal() {
    byId("recModal").classList.add("hidden");
    byId("recModalImage").src = "";
  }

  // -------------------------------------------------------------------- load
  function load() {
    var days = byId("recDays").value;
    var site = byId("recSite").value;
    var query = byId("recQuery").value.trim();
    var params = ["days=" + encodeURIComponent(days), "limit=48"];
    if (site) {
      params.push("site=" + encodeURIComponent(site));
    }
    if (query) {
      params.push("q=" + encodeURIComponent(query));
    }
    if (byId("recSensitive").checked) {
      params.push("include_sensitive=1");
    }
    byId("recStatus").textContent = "불러오는 중…";
    return request("/api/recommendations?" + params.join("&"))
      .then(function (data) {
        state.data = data;
        render();
      })
      .catch(function (error) {
        byId("recStatus").textContent = error.message || "불러오기 실패";
      });
  }

  function reindex() {
    var button = byId("recIndex");
    button.disabled = true;
    byId("recStatus").textContent = "색인 갱신 중… (파일 수에 따라 시간이 걸립니다)";
    request("/api/recommendations/refresh", { method: "POST", body: {} })
      .then(function (result) {
        var summary = (result && result.result) || {};
        byId("recStatus").textContent =
          "색인 완료 · 신규 " + (summary.created || 0) + " / 갱신 " + (summary.updated || 0);
        return load();
      })
      .catch(function (error) {
        byId("recStatus").textContent = error.message || "색인 실패";
      })
      .finally(function () {
        button.disabled = false;
      });
  }

  function bind() {
    byId("recDays").addEventListener("change", load);
    byId("recSite").addEventListener("change", load);
    byId("recSensitive").addEventListener("change", load);
    byId("recReload").addEventListener("click", load);
    byId("recIndex").addEventListener("click", reindex);
    byId("recQuery").addEventListener("input", function () {
      if (state.timer) {
        window.clearTimeout(state.timer);
      }
      state.timer = window.setTimeout(load, 350);
    });

    document.addEventListener("click", function (event) {
      var card = event.target.closest ? event.target.closest(".rec-card") : null;
      if (card) {
        openModal(card.dataset.post);
        return;
      }
      var uploader = event.target.closest ? event.target.closest(".rec-uploader") : null;
      if (uploader) {
        byId("recQuery").value = uploader.dataset.uploader || "";
        load();
        return;
      }
      if (event.target === byId("recModal")) {
        closeModal();
      }
    });

    byId("recModalClose").addEventListener("click", closeModal);
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape") {
        closeModal();
      }
    });
  }

  bind();
  load();
})(window, document);
