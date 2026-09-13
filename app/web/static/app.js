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

  function performItemAction(url, method, body) {
    return fetch(url, {
      method: method,
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    })
      .then(refresh)
      .catch(function () {
        // Same policy as refresh() itself: a failed action leaves the
        // screen as it is rather than erroring out loud.
      });
  }

  function renderItemList(container, items, focusId) {
    container.innerHTML = "";
    if (!items.length) {
      container.appendChild(makeEl("p", "empty", "Nothing here."));
      return;
    }
    var list = makeEl("ul", "items");
    items.forEach(function (item) {
      var row = makeEl("li", "item" + (item.actionable ? " actionable" : ""));

      var checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.className = "item-check";
      checkbox.addEventListener("change", function () {
        performItemAction("/api/items/" + item.id, "PATCH", {
          completed_at: checkbox.checked ? new Date().toISOString() : null,
        });
      });
      row.appendChild(checkbox);

      row.appendChild(makeEl("span", "item-title", item.title));
      if (item.due_at) {
        row.appendChild(makeEl("span", "item-due", formatCountdown(item.due_at)));
      }

      var isFocus = focusId !== null && focusId === item.id;
      var star = makeEl("button", "item-star" + (isFocus ? " active" : ""), "★");
      star.type = "button";
      star.title = "Mark as today's focus";
      star.addEventListener("click", function () {
        performItemAction("/api/items/" + item.id, "PATCH", { focus: !isFocus });
      });
      row.appendChild(star);

      var dismiss = makeEl("button", "item-dismiss", "✕");
      dismiss.type = "button";
      dismiss.title = "Remove";
      dismiss.addEventListener("click", function () {
        performItemAction("/api/items/" + item.id, "DELETE");
      });
      row.appendChild(dismiss);

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

    var focusId = summary.focus ? summary.focus.id : null;
    renderItemList(document.getElementById("today"), summary.today, focusId);
    renderItemList(document.getElementById("this-week"), summary.this_week, focusId);
    renderSections(document.getElementById("sections"), summary.sections);

    var focusLine = document.getElementById("focus-line");
    if (summary.focus) {
      focusLine.hidden = false;
      focusLine.textContent = "Focus: " + summary.focus.title;
    } else {
      focusLine.hidden = true;
    }

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

  // --- Command field: "/" focuses it from anywhere; Enter either jumps
  // (">target") or creates a task (anything else). Out of scope beyond
  // today/tomorrow/YYYY-MM-DD: no other natural-language date parsing.

  var JUMP_TARGETS = {
    today: "today",
    week: "this-week",
    "this week": "this-week",
    "this-week": "this-week",
    sources: "sections",
  };

  var EXPLICIT_DATE_RE = /^\d{4}-\d{2}-\d{2}$/;

  function osloDateParts(date) {
    var fmt = new Intl.DateTimeFormat("en-CA", {
      timeZone: "Europe/Oslo",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
    });
    var parts = {};
    fmt.formatToParts(date).forEach(function (p) {
      parts[p.type] = p.value;
    });
    return { year: +parts.year, month: +parts.month, day: +parts.day };
  }

  function addDaysToDateParts(parts, days) {
    // Noon UTC anchor: a plain calendar-day add with no DST edge cases,
    // since we only care about the Y-M-D that comes out.
    var anchor = new Date(Date.UTC(parts.year, parts.month - 1, parts.day, 12, 0, 0));
    anchor.setUTCDate(anchor.getUTCDate() + days);
    return {
      year: anchor.getUTCFullYear(),
      month: anchor.getUTCMonth() + 1,
      day: anchor.getUTCDate(),
    };
  }

  function osloWallTimeToUtcIso(year, month, day, hour, minute) {
    var asIfUtc = new Date(Date.UTC(year, month - 1, day, hour, minute, 0));
    var osloStr = asIfUtc.toLocaleString("en-US", { timeZone: "Europe/Oslo" });
    var utcStr = asIfUtc.toLocaleString("en-US", { timeZone: "UTC" });
    var offsetMs = new Date(osloStr).getTime() - new Date(utcStr).getTime();
    return new Date(asIfUtc.getTime() - offsetMs).toISOString().replace(/\.\d{3}Z$/, "Z");
  }

  function extractDueDate(text) {
    var words = text.split(/\s+/);
    for (var i = 0; i < words.length; i++) {
      var word = words[i];
      var lower = word.toLowerCase();
      var parts = null;
      if (lower === "today") {
        parts = osloDateParts(new Date());
      } else if (lower === "tomorrow") {
        parts = addDaysToDateParts(osloDateParts(new Date()), 1);
      } else if (EXPLICIT_DATE_RE.test(word)) {
        var bits = word.split("-");
        parts = { year: +bits[0], month: +bits[1], day: +bits[2] };
      }
      if (parts) {
        words.splice(i, 1);
        return {
          title: words.join(" ").trim(),
          dueAt: osloWallTimeToUtcIso(parts.year, parts.month, parts.day, 23, 59),
        };
      }
    }
    return { title: text, dueAt: null };
  }

  function handleCommandSubmit() {
    var field = document.getElementById("command-field");
    var raw = field.value.trim();
    if (!raw) return;

    if (raw.charAt(0) === ">") {
      var targetId = JUMP_TARGETS[raw.slice(1).trim().toLowerCase()];
      var targetEl = targetId && document.getElementById(targetId);
      if (targetEl) {
        targetEl.scrollIntoView({ behavior: "smooth" });
        field.value = "";
      }
      return;
    }

    var parsed = extractDueDate(raw);
    if (!parsed.title) return;

    fetch("/api/items", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        kind: "task",
        title: parsed.title,
        section: "today",
        due_at: parsed.dueAt,
        actionable: true,
      }),
    })
      .then(function (res) {
        if (res.ok) {
          field.value = "";
          refresh();
        }
      })
      .catch(function () {
        // Leave the field's text in place so nothing typed is lost.
      });
  }

  function isTextInput(el) {
    return !!el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.isContentEditable);
  }

  document.addEventListener("keydown", function (e) {
    if (e.key !== "/" || isTextInput(document.activeElement)) return;
    e.preventDefault();
    document.getElementById("command-field").focus();
  });

  document.getElementById("command-field").addEventListener("keydown", function (e) {
    if (e.key === "Enter") handleCommandSubmit();
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
