(function (window, document) {
  "use strict";

  var popover = null;
  var activeIcon = null;
  var pinned = false;

  function byId(id) {
    return document.getElementById(id);
  }

  function show(icon) {
    var text = icon.getAttribute("data-help");
    if (!text) {
      return;
    }
    popover = byId("helpPopover");
    if (!popover) {
      return;
    }
    popover.textContent = text;
    popover.classList.remove("hidden");
    activeIcon = icon;
    position(icon);
  }

  function position(icon) {
    if (!popover) {
      return;
    }
    var rect = icon.getBoundingClientRect();
    var width = popover.offsetWidth;
    var height = popover.offsetHeight;
    var vw = window.innerWidth;
    var vh = window.innerHeight;
    var x = rect.left + rect.width / 2 - width / 2;
    var y = rect.bottom + 8;
    if (y + height > vh - 8) {
      y = rect.top - height - 8;
    }
    x = Math.max(8, Math.min(x, vw - width - 8));
    y = Math.max(8, y);
    popover.style.left = x + "px";
    popover.style.top = y + "px";
  }

  function hide() {
    if (popover) {
      popover.classList.add("hidden");
    }
    activeIcon = null;
    pinned = false;
  }

  function init() {
    document.addEventListener("click", function (event) {
      var icon = event.target.closest ? event.target.closest(".help-icon") : null;
      if (icon) {
        event.preventDefault();
        event.stopPropagation();
        if (activeIcon === icon && pinned) {
          hide();
        } else {
          show(icon);
          pinned = true;
        }
        return;
      }
      if (!event.target.closest || !event.target.closest("#helpPopover")) {
        hide();
      }
    }, true);

    document.addEventListener("keydown", function (event) {
      var icon = event.target.closest ? event.target.closest(".help-icon") : null;
      if (icon && (event.key === "Enter" || event.key === " ")) {
        event.preventDefault();
        show(icon);
      } else if (event.key === "Escape") {
        hide();
      }
    });

    document.addEventListener("mouseover", function (event) {
      if (event.pointerType === "touch") {
        return;
      }
      var icon = event.target.closest ? event.target.closest(".help-icon") : null;
      if (icon) {
        show(icon);
      }
    });

    document.addEventListener("mouseout", function (event) {
      if (pinned) {
        return;
      }
      var icon = event.target.closest ? event.target.closest(".help-icon") : null;
      if (icon && activeIcon === icon) {
        hide();
      }
    });

    window.addEventListener("scroll", hide, true);
    window.addEventListener("resize", hide);
  }

  window.Help = { init: init, hide: hide };
})(window, document);
