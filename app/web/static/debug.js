(function () {
  "use strict";

  function parseInitialDebug() {
    var el = document.getElementById("initial-debug");
    if (!el) return null;
    try {
      return JSON.parse(el.textContent);
    } catch (e) {
      return null;
    }
  }

  function fetchSources() {
    return fetch("/api/sources", { cache: "no-store" }).then(function (res) {
      return res.json();
    });
  }

  function formatClock(isoString) {
    if (!isoString) return null;
    var date = new Date(isoString);
    return date.toLocaleString("nb-NO", {
      day: "2-digit",
      month: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      timeZone: "Europe/Oslo",
    });
  }

  function formatAge(ageSeconds) {
    if (ageSeconds === null || ageSeconds === undefined) return "never";
    var minutes = Math.round(ageSeconds / 60);
    if (minutes < 1) return "just now";
    if (minutes < 60) return minutes + "m ago";
    var hours = Math.floor(minutes / 60);
    if (hours < 24) return hours + "h ago";
    return Math.floor(hours / 24) + "d ago";
  }

  function makeEl(tag, className, text) {
    var node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function renderRun(run) {
    var row = makeEl("li", "run-row" + (run.ok === false ? " run-failed" : ""));
    row.appendChild(makeEl("span", "run-started", formatClock(run.started_at)));
    row.appendChild(makeEl("span", "run-outcome", run.ok === null ? "in progress" : (run.ok ? "ok" : "failed")));
    if (run.duration_ms != null) {
      row.appendChild(makeEl("span", "run-duration", run.duration_ms + "ms"));
    }
    if (run.error) {
      row.appendChild(makeEl("span", "run-error", run.error));
    }
    return row;
  }

  function renderSourceRow(source) {
    var neverSucceeded = !source.last_ok_at;
    var row = makeEl("li", "source-row" + (neverSucceeded ? " never-succeeded" : ""));

    var head = makeEl("div", "source-head");
    head.appendChild(makeEl("span", "source-name", source.name));
    head.appendChild(makeEl("span", "source-interval", source.interval_s + "s interval"));
    head.appendChild(makeEl("span", "source-last-ok",
      neverSucceeded ? "never succeeded" : "succeeded " + formatAge(source.age_s)));
    head.appendChild(makeEl("span", "source-failures", source.consecutive_failures + " consecutive failures"));
    if (source.last_duration_ms != null) {
      head.appendChild(makeEl("span", "source-duration", "last took " + source.last_duration_ms + "ms"));
    }

    var button = makeEl("button", "run-now-btn", "Run now");
    button.type = "button";
    var msg = makeEl("span", "run-msg", "");
    button.addEventListener("click", function () {
      button.disabled = true;
      msg.textContent = "running…";
      fetch("/api/sources/" + encodeURIComponent(source.name) + "/run", { method: "POST" })
        .then(function (res) {
          if (res.status === 409) {
            msg.textContent = "already running";
            return null;
          }
          if (!res.ok) {
            msg.textContent = "failed to trigger";
            return null;
          }
          return fetchSources();
        })
        .then(function (sources) {
          if (sources) {
            renderSources(sources);
          }
        })
        .catch(function () {
          msg.textContent = "failed to trigger";
        })
        .finally(function () {
          button.disabled = false;
        });
    });
    head.appendChild(button);
    head.appendChild(msg);
    row.appendChild(head);

    if (source.last_error) {
      row.appendChild(makeEl("p", "source-error", source.last_error));
    }

    var details = document.createElement("details");
    details.appendChild(makeEl("summary", null, "recent runs (" + source.recent_runs.length + ")"));
    var runList = makeEl("ul", "runs");
    source.recent_runs.forEach(function (run) {
      runList.appendChild(renderRun(run));
    });
    details.appendChild(runList);
    row.appendChild(details);

    return row;
  }

  function renderSources(sources) {
    var container = document.getElementById("sources");
    container.innerHTML = "";
    if (!sources.length) {
      container.appendChild(makeEl("p", "empty", "No sources registered yet."));
      return;
    }
    var list = makeEl("ul", "source-list");
    sources.forEach(function (source) {
      list.appendChild(renderSourceRow(source));
    });
    container.appendChild(list);
  }

  function renderJobs(retention) {
    var container = document.getElementById("jobs");
    container.innerHTML = "";
    var neverSucceeded = !retention.last_ok_at;
    var row = makeEl("li", "source-row" + (neverSucceeded ? " never-succeeded" : ""));
    var head = makeEl("div", "source-head");
    head.appendChild(makeEl("span", "source-name", "retention"));
    head.appendChild(makeEl("span", "source-last-ok",
      neverSucceeded ? "never succeeded" : "succeeded " + formatClock(retention.last_ok_at)));
    head.appendChild(makeEl("span", "source-failures", retention.consecutive_failures + " consecutive failures"));
    if (retention.last_duration_ms != null) {
      head.appendChild(makeEl("span", "source-duration", "last took " + retention.last_duration_ms + "ms"));
    }
    row.appendChild(head);
    if (retention.last_error) {
      row.appendChild(makeEl("p", "source-error", retention.last_error));
    }
    var list = makeEl("ul", "source-list");
    list.appendChild(row);
    container.appendChild(list);
  }

  var root = document.getElementById("debug-app");
  root.innerHTML =
    '<section><h2>Sources</h2><div id="sources"></div></section>' +
    '<section><h2>Jobs</h2><div id="jobs"></div></section>';

  var initial = parseInitialDebug();
  if (initial) {
    renderSources(initial.sources);
    renderJobs(initial.retention);
  } else {
    fetchSources().then(renderSources);
  }
})();
