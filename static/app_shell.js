(function () {
  var context = window.__APP_CONTEXT__ || {};
  var pageContent = document.getElementById("page-content");

  function animatePageEnter() {
    if (!pageContent) return;
    pageContent.classList.remove("is-leaving");
    pageContent.classList.add("is-entering");
    window.setTimeout(function () {
      pageContent.classList.remove("is-entering");
    }, 420);
  }

  function wireLinkTransitions() {
    document.addEventListener("click", function (event) {
      var link = event.target.closest("a[href]");
      if (!link) return;

      var href = link.getAttribute("href");
      var isInternal = href && href.startsWith("/");
      var isModified = event.metaKey || event.ctrlKey || event.shiftKey || event.altKey;
      var opensNewTab = link.target === "_blank";
      var isHashLink = href && href.startsWith("#");
      var isDownload = link.hasAttribute("download");

      if (!isInternal || isModified || opensNewTab || isHashLink || isDownload) {
        return;
      }

      event.preventDefault();
      if (pageContent) {
        pageContent.classList.add("is-leaving");
      }
      window.setTimeout(function () {
        window.location.href = href;
      }, 180);
    });
  }

  function navItemHtml(item, endpoint, asTopLink) {
    var endpoints = Array.isArray(item.endpoints) ? item.endpoints : [];
    var isActive = endpoints.indexOf(endpoint) !== -1;

    if (asTopLink) {
      var topClass = isActive
        ? "border-brand-200 bg-brand-50 text-brand-700"
        : "border-slate-200 bg-white text-slate-700 hover:border-slate-300 hover:bg-slate-50";
      return (
        '<a href="' +
        item.href +
        '" class="rounded-lg border px-2.5 py-1.5 text-xs font-semibold transition sm:text-sm ' +
        topClass +
        '">' +
        item.label +
        "</a>"
      );
    }

    var sideClass = isActive
      ? "border-brand-200 bg-brand-50 shadow-soft"
      : "border-slate-200 bg-white hover:-translate-y-0.5 hover:border-slate-300 hover:bg-slate-50";
    var labelClass = isActive ? "text-brand-700" : "text-slate-900";
    var hintClass = isActive ? "text-brand-600" : "text-slate-500";

    return (
      '<a href="' +
      item.href +
      '" class="group block rounded-xl border px-3 py-2 transition ' +
      sideClass +
      '">' +
      '<p class="text-sm font-semibold ' +
      labelClass +
      '">' +
      item.label +
      "</p>" +
      '<p class="text-xs ' +
      hintClass +
      '">' +
      item.hint +
      "</p>" +
      "</a>"
    );
  }

  function buildHeaderHtml() {
    var role = context.role || "user";
    var roleLabel = role.charAt(0).toUpperCase() + role.slice(1);
    var navItems = Array.isArray(context.navItems) ? context.navItems : [];
    var topItems = navItems.filter(function (item) {
      return item.label !== "Log Out";
    });
    var logoutItem = navItems.find(function (item) {
      return item.label === "Log Out";
    });

    var topLinksHtml = topItems
      .map(function (item) {
        return navItemHtml(item, context.endpoint, true);
      })
      .join("");

    var logoHtml = context.logoUrl
      ? '<img src="' +
        context.logoUrl +
        '" alt="Modesto Karate logo" class="h-14 w-auto rounded-md border border-slate-200 bg-white p-1 sm:h-16" />'
      : "";

    var accountMenuHtml = "";
    if (logoutItem) {
      accountMenuHtml =
        '<div id="account-menu-wrap" class="relative">' +
        '<button type="button" id="account-menu-toggle" class="rounded-xl border border-slate-200 bg-slate-50 px-3 py-2 text-right transition hover:bg-slate-100">' +
        '<p class="text-xs font-semibold uppercase tracking-wide text-slate-500">' +
        roleLabel +
        "</p>" +
        '<p class="text-sm font-bold text-slate-800">' +
        (context.username || "") +
        "</p>" +
        "</button>" +
        '<div id="account-menu-panel" class="hidden absolute right-0 z-50 mt-2 min-w-[9.5rem] rounded-lg border border-slate-200 bg-white p-1.5 shadow-lg">' +
        '<a href="' +
        logoutItem.href +
        '" class="block rounded-md px-3 py-2 text-sm font-semibold text-slate-700 transition hover:bg-slate-100">Log Out</a>' +
        "</div>" +
        "</div>";
    } else {
      accountMenuHtml =
        '<div class="rounded-xl border border-slate-200 bg-slate-50 px-3 py-2 text-right">' +
        '<p class="text-xs font-semibold uppercase tracking-wide text-slate-500">' +
        roleLabel +
        "</p>" +
        '<p class="text-sm font-bold text-slate-800">' +
        (context.username || "") +
        "</p>" +
        "</div>";
    }

    return (
      '<div class="sticky top-0 z-40 border-b border-slate-200/70 bg-white/90 backdrop-blur">' +
      '<div class="mx-auto flex w-full max-w-7xl items-center justify-between gap-3 px-4 py-3 sm:px-6 lg:px-8">' +
      '<button type="button" id="mobile-nav-open" class="inline-flex h-10 w-10 items-center justify-center rounded-xl border border-slate-200 bg-white text-slate-700 transition hover:bg-slate-50 lg:hidden" aria-label="Open navigation menu">' +
      '<span class="text-lg">☰</span>' +
      "</button>" +
      '<div class="flex items-center gap-3">' +
      "<div>" +
      '<p class="text-xs font-semibold uppercase tracking-[0.18em] text-brand-600">Modesto\'s Karate Academies</p>' +
      '<h1 class="truncate text-base font-extrabold text-slate-900 sm:text-lg">Academy Portal</h1>' +
      "</div>" +
      logoHtml +
      "</div>" +
      accountMenuHtml +
      "</div>" +
      '<div class="mx-auto w-full max-w-7xl border-t border-slate-200/70 px-4 py-2 sm:px-6 lg:px-8">' +
      '<div class="flex flex-wrap items-center gap-2">' +
      topLinksHtml +
      "</div>" +
      "</div>"
    );
  }

  function buildSidebarHtml() {
    var navItems = (Array.isArray(context.navItems) ? context.navItems : []).filter(function (item) {
      return item.label !== "Log Out";
    });
    var links = navItems
      .map(function (item) {
        return navItemHtml(item, context.endpoint, false);
      })
      .join("");

    return (
      '<div class="rounded-2xl border border-slate-200/80 bg-white/95 p-4 shadow-soft">' +
      '<p class="mb-3 text-xs font-semibold uppercase tracking-[0.18em] text-slate-500">Navigation</p>' +
      '<nav class="space-y-2" aria-label="Main navigation">' +
      links +
      "</nav>" +
      "</div>"
    );
  }

  function buildMobileMenuHtml() {
    var navItems = (Array.isArray(context.navItems) ? context.navItems : []).filter(function (item) {
      return item.label !== "Log Out";
    });
    var links = navItems
      .map(function (item) {
        return navItemHtml(item, context.endpoint, false);
      })
      .join("");

    return (
      '<div id="mobile-nav-overlay" class="fixed inset-0 z-50 hidden bg-slate-900/30 p-4 backdrop-blur-sm lg:hidden">' +
      '<div class="mx-auto mt-2 max-w-md rounded-2xl border border-slate-200 bg-white p-4 shadow-soft">' +
      '<div class="mb-4 flex items-center justify-between">' +
      '<p class="text-sm font-bold text-slate-900">Quick Navigation</p>' +
      '<button type="button" id="mobile-nav-close" class="rounded-lg border border-slate-200 px-2 py-1 text-sm text-slate-700">Close</button>' +
      "</div>" +
      '<nav class="space-y-2" aria-label="Mobile navigation">' +
      links +
      "</nav>" +
      "</div>" +
      "</div>"
    );
  }

  function wireMobileMenu() {
    var openBtn = document.getElementById("mobile-nav-open");
    var overlay = document.getElementById("mobile-nav-overlay");
    var closeBtn = document.getElementById("mobile-nav-close");

    if (!openBtn || !overlay) return;

    openBtn.addEventListener("click", function () {
      overlay.classList.remove("hidden");
    });

    function closeMenu() {
      overlay.classList.add("hidden");
    }

    if (closeBtn) {
      closeBtn.addEventListener("click", closeMenu);
    }

    overlay.addEventListener("click", function (event) {
      if (event.target === overlay) {
        closeMenu();
      }
    });

    window.addEventListener("resize", closeMenu);
  }

  function wireAccountMenu() {
    var toggleBtn = document.getElementById("account-menu-toggle");
    var panel = document.getElementById("account-menu-panel");
    var wrap = document.getElementById("account-menu-wrap");

    if (!toggleBtn || !panel || !wrap) return;

    toggleBtn.addEventListener("click", function (event) {
      event.preventDefault();
      panel.classList.toggle("hidden");
    });

    document.addEventListener("click", function (event) {
      if (!wrap.contains(event.target)) {
        panel.classList.add("hidden");
      }
    });
  }

  function mountShell() {
    if (!context.loggedIn) return;

    var headerTarget = document.getElementById("react-shell-header");
    if (headerTarget) {
      headerTarget.innerHTML = buildHeaderHtml() + buildMobileMenuHtml();
    }

    var sideTarget = document.getElementById("react-shell-sidebar");
    if (sideTarget) {
      sideTarget.innerHTML = buildSidebarHtml();
    }

    wireMobileMenu();
    wireAccountMenu();
  }

  function wireFormStates() {
    document.addEventListener("submit", function (event) {
      var form = event.target;
      if (!form || form.tagName !== "FORM") return;
      form.classList.add("is-submitting");
      var submitBtn = form.querySelector('button[type="submit"], input[type="submit"]');
      if (submitBtn && !submitBtn.dataset.busyLabel) {
        submitBtn.dataset.busyLabel = submitBtn.textContent || submitBtn.value || "";
      }
    });
  }

  animatePageEnter();
  wireLinkTransitions();
  mountShell();
  wireFormStates();
})();
