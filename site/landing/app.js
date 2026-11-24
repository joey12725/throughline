(function () {
  "use strict";
  var root = document.documentElement;
  var themeMeta = document.querySelector('meta[name="theme-color"]');

  // Theme toggle: OS preference by default, remembered choice when set.
  var themeBtn = document.getElementById("theme");
  var darkMq = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)");
  function currentTheme() {
    var t = root.getAttribute("data-theme");
    if (t) return t;
    return darkMq && darkMq.matches ? "dark" : "light";
  }
  function syncThemeColor() {
    if (themeMeta) themeMeta.setAttribute("content", currentTheme() === "dark" ? "#111317" : "#f7f4ee");
  }
  syncThemeColor();
  if (darkMq && darkMq.addEventListener) darkMq.addEventListener("change", syncThemeColor);
  if (themeBtn) {
    themeBtn.addEventListener("click", function () {
      var next = currentTheme() === "dark" ? "light" : "dark";
      root.setAttribute("data-theme", next);
      try { localStorage.setItem("tl-theme", next); } catch (e) {}
      syncThemeColor();
    });
  }

  // Nav border once scrolled.
  var nav = document.getElementById("nav");
  function onScroll() { if (nav) nav.classList.toggle("is-scrolled", window.scrollY > 8); }
  window.addEventListener("scroll", onScroll, { passive: true });
  onScroll();

  // Example-question tabs (WAI-ARIA tabs pattern).
  var tabs = Array.prototype.slice.call(document.querySelectorAll(".ask-tab"));
  function select(tab, focus, fromClick) {
    var activePanel = null;
    tabs.forEach(function (t) {
      var on = t === tab;
      t.setAttribute("aria-selected", on ? "true" : "false");
      t.tabIndex = on ? 0 : -1;
      var panel = document.getElementById(t.getAttribute("aria-controls"));
      if (panel) {
        panel.hidden = !on;
        if (on) activePanel = panel;
      }
    });
    if (focus) tab.focus();
    if (fromClick && activePanel && window.innerWidth <= 1020) {
      var rect = activePanel.getBoundingClientRect();
      if (rect.top > window.innerHeight - 120 || rect.bottom < 80) {
        activePanel.scrollIntoView({ behavior: "smooth", block: "nearest" });
      }
    }
  }
  tabs.forEach(function (tab, i) {
    tab.addEventListener("click", function () { select(tab, false, true); });
    tab.addEventListener("keydown", function (e) {
      var k = e.key, n = null;
      if (k === "ArrowDown" || k === "ArrowRight") n = tabs[(i + 1) % tabs.length];
      else if (k === "ArrowUp" || k === "ArrowLeft") n = tabs[(i - 1 + tabs.length) % tabs.length];
      else if (k === "Home") n = tabs[0];
      else if (k === "End") n = tabs[tabs.length - 1];
      if (n) { e.preventDefault(); select(n, true, false); }
    });
  });

  // Copy install commands (without prompts or comments), with fallback for non-HTTPS contexts.
  var copy = document.getElementById("copy");
  var cmds = document.getElementById("cmds");
  if (copy && cmds) {
    copy.addEventListener("click", function () {
      var text = cmds.innerText.split("\n")
        .filter(function (l) { return l.trim() && l.trim().charAt(0) !== "#"; })
        .map(function (l) { return l.replace(/^\$ /, ""); })
        .join("\n");
      var done = function () {
        copy.textContent = "Copied";
        setTimeout(function () { copy.textContent = "Copy"; }, 1600);
      };
      var fallback = function () {
        var ta = document.createElement("textarea");
        ta.value = text;
        ta.setAttribute("readonly", "");
        ta.style.position = "fixed";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.select();
        try { if (document.execCommand("copy")) done(); } catch (e) {}
        document.body.removeChild(ta);
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done, fallback);
      } else {
        fallback();
      }
    });
  }

  // Screenshot lightbox with body scroll lock and focus return.
  var lb = document.getElementById("lightbox");
  if (lb && typeof lb.showModal === "function") {
    var lbImg = lb.querySelector("img");
    var lastTrigger = null;
    document.querySelectorAll("button.shot").forEach(function (b) {
      b.addEventListener("click", function () {
        var img = b.querySelector("img");
        lastTrigger = b;
        lbImg.src = b.getAttribute("data-full") || img.src;
        lbImg.alt = img.alt;
        lb.showModal();
      });
    });
    lb.addEventListener("close", function () {
      if (lastTrigger && typeof lastTrigger.focus === "function") lastTrigger.focus();
    });
    document.getElementById("lb-close").addEventListener("click", function () { lb.close(); });
    lb.addEventListener("click", function (e) { if (e.target === lb) lb.close(); });
  }
})();
