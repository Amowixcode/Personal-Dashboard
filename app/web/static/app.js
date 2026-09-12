(function () {
  "use strict";

  var POLL_INTERVAL_MS = 30000;
  var pollTimer = null;

  function parseInitialSummary() {
    var el = document.getElementById("initial-summary");
    if (!el) return null;
    try {
      return JSON.parse(el.textContent);
    } catch (e) {
      return null;
    }
  }

  function fetchSummary() {
    return fetch("/summary", { cache: "no-store" }).then(function (res) {
      return res.json();
    });
  }

  function formatClock(isoString) {
    if (!isoString) return null;
    var date = new Date(isoString);
    return date.toLocaleTimeString("nb-NO", {
      hour: "2-digit",
      minute: "2-digit",
      timeZone: "Europe/Oslo",
    });
  }

  function formatCountdown(isoString) {
    if (!isoString) return null;
    var diffMs = new Date(isoString).getTime() - Date.now();
    var pastDue = diffMs < 0;
    var minutes = Math.round(Math.abs(diffMs) / 60000);
    var hours = Math.floor(minutes / 60);
    var mins = minutes % 60;
    var text = hours > 0 ? hours + "h " + mins + "m" : mins + "m";
    return pastDue ? "overdue by " + text : "in " + text;
  }

  function makeEl(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function renderItemList(container, items) {
    container.innerHTML = "";
    if (!items.length) {
      container.appendChild(makeEl("p", "empty", "Nothing here."));
      return;
    }
    var list = makeEl("ul", "items");
    items.forEach(function (item) {
      var row = makeEl("li", "item" + (item.actionable ? " actionable" : ""));
      row.appendChild(makeEl("span", "item-title", item.title));
      if (item.due_at) {
        row.appendChild(makeEl("span", "item-due", formatCountdown(item.due_at)));
      }
      list.appendChild(row);
    });
    container.appendChild(list);
  }

  function renderSections(container, sections) {
    container.innerHTML = "";
    if (!sections.length) {
      container.appendChild(makeEl("p", "empty", "No sources registered yet."));
      return;
    }
    var list = makeEl("ul", "sections");
    sections.forEach(function (section) {
      var row = makeEl("li", "section status-" + section.status);
      row.appendChild(makeEl("span", "section-label", section.label));
      row.appendChild(makeEl("span", "section-message", section.message));
      if (section.as_of) {
        row.appendChild(makeEl("span", "section-as-of", "as of " + formatClock(section.as_of)));
      }
      list.appendChild(row);
    });
    container.appendChild(list);
  }

  function render(summary) {
    document.getElementById("generated-at").textContent =
      "as of " + formatClock(summary.generated_at);

    renderItemList(document.getElementById("today"), summary.today);
    renderItemList(document.getElementById("this-week"), summary.this_week);
    renderSections(document.getElementById("sections"), summary.sections);

    var banner = document.getElementById("stale-banner");
    if (summary.stale_sources.length) {
      banner.hidden = false;
      banner.textContent = summary.stale_sources.length + " source(s) need attention";
    } else {
      banner.hidden = true;
    }
  }

  function refresh() {
    fetchSummary().then(render).catch(function () {
      // Whatever is already on screen stays visible, greyed by its own
      // age -- a failed refresh never blocks or spins the page.
    });
  }

  function startPolling() {
    if (pollTimer) return;
    pollTimer = setInterval(refresh, POLL_INTERVAL_MS);
  }

  function stopPolling() {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  }

  document.addEventListener("visibilitychange", function () {
    if (document.hidden) {
      stopPolling();
    } else {
      refresh();
      startPolling();
    }
  });

  var appRoot = document.getElementById("app");
  appRoot.innerHTML =
    '<section><h2>Today</h2><div id="today"></div></section>' +
    '<section><h2>This week</h2><div id="this-week"></div></section>' +
    '<section><h2>Sources</h2><p id="stale-banner" class="stale-banner" hidden></p><div id="sections"></div></section>';

  var initial = parseInitialSummary();
  if (initial) {
    render(initial);
  } else {
    refresh();
  }
  if (!document.hidden) {
    startPolling();
  }
})();
