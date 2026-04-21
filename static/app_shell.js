(function () {
  var context = window.__APP_CONTEXT__ || {};
  var pageContent = document.getElementById("page-content");

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
      }, 120);
    });
  }

  function navLink(item, endpoint) {
    var endpoints = Array.isArray(item.endpoints) ? item.endpoints : [];
    var isActive = endpoints.indexOf(endpoint) !== -1;
    return (
      '<a href="' +
      item.href +
      '" class="' +
      (isActive ? "active" : "") +
      '">' +
      item.label +
      "</a>"
    );
  }

  function mountShell() {
    if (!context.loggedIn) return;

    var navItems = Array.isArray(context.navItems) ? context.navItems : [];
    var contentItems = navItems.filter(function (item) {
      return item.label !== "Log Out";
    });
    var logoutItem = navItems.find(function (item) {
      return item.label === "Log Out";
    });

    var headerTarget = document.getElementById("react-shell-header");
    if (!headerTarget) return;

    var linksHtml = contentItems
      .map(function (item) {
        return navLink(item, context.endpoint);
      })
      .join("");

    var accountHtml =
      '<div class="shell-account">' +
      '<span>' + (context.role || "").toUpperCase() + "</span>" +
      '<strong>' + (context.username || "") + "</strong>" +
      (logoutItem
        ? '<a href="' + logoutItem.href + '">Log Out</a>'
        : "") +
      "</div>";

    headerTarget.innerHTML =
      '<div class="shell-header">' +
      '<div class="shell-header-inner">' +
      '<div class="shell-brand">' +
      (context.logoUrl
        ? '<img src="' + context.logoUrl + '" alt="Modesto Karate logo" />'
        : "") +
      '<div class="shell-brand-title">Modesto\'s Karate Academies</div>' +
      "</div>" +
      accountHtml +
      "</div>" +
      '<nav class="shell-nav" aria-label="Main navigation">' +
      linksHtml +
      "</nav>" +
      "</div>";
  }

  function wireFormStates() {
    document.addEventListener("submit", function (event) {
      var form = event.target;
      if (!form || form.tagName !== "FORM") return;
      form.classList.add("is-submitting");
    });
  }

  wireLinkTransitions();
  mountShell();
  wireFormStates();
})();
