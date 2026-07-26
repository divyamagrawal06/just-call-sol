(() => {
  "use strict";

  const apiBase = document.querySelector('meta[name="hotline-api-base"]').content;
  const stateGrid = document.getElementById("state-grid");
  const eventsContainer = document.getElementById("events");
  const eventTotal = document.getElementById("event-total");
  const lastUpdated = document.getElementById("last-updated");
  const health = document.querySelector(".health");
  const healthLabel = document.getElementById("health-label");

  const displayName = (value) => String(value).replaceAll("_", " ");

  const element = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) {
      node.className = className;
    }
    if (text !== undefined) {
      node.textContent = String(text);
    }
    return node;
  };

  const relativeTime = (isoValue) => {
    const date = new Date(isoValue);
    if (Number.isNaN(date.valueOf())) {
      return "time unavailable";
    }
    const seconds = Math.round((date.valueOf() - Date.now()) / 1000);
    const formatter = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });
    if (Math.abs(seconds) < 60) {
      return formatter.format(seconds, "second");
    }
    const minutes = Math.round(seconds / 60);
    if (Math.abs(minutes) < 60) {
      return formatter.format(minutes, "minute");
    }
    const hours = Math.round(minutes / 60);
    if (Math.abs(hours) < 24) {
      return formatter.format(hours, "hour");
    }
    return formatter.format(Math.round(hours / 24), "day");
  };

  const renderStates = (stateCounts) => {
    stateGrid.replaceChildren();
    for (const [state, count] of Object.entries(stateCounts)) {
      const card = element("article", "state-card");
      card.append(element("strong", "", count));
      card.append(element("span", "", displayName(state)));
      stateGrid.append(card);
    }
  };

  const renderTimeline = (entries) => {
    const container = element("div", "timeline");
    if (entries.length === 0) {
      container.append(element("p", "empty-state", "No timeline entries yet."));
      return container;
    }
    for (const entry of entries) {
      const row = element("div", "timeline-row");
      const state = element("div", "timeline-state");
      state.append(element("span", "", displayName(entry.kind)));
      if (entry.from_state || entry.to_state) {
        const transition = element(
          "strong",
          "",
          ` · ${displayName(entry.from_state || "—")} → ${displayName(entry.to_state || "—")}`,
        );
        state.append(transition);
      }
      const detailValues = Object.values(entry.detail || {});
      if (detailValues.length > 0) {
        state.append(element("strong", "", ` · ${detailValues.join(" · ")}`));
      }
      const time = element("time", "", relativeTime(entry.occurred_at));
      time.dateTime = entry.occurred_at;
      row.append(state, time);
      container.append(row);
    }
    return container;
  };

  const renderEvents = (events) => {
    eventsContainer.replaceChildren();
    eventTotal.textContent = `${events.length} ${events.length === 1 ? "event" : "events"}`;
    if (events.length === 0) {
      eventsContainer.append(element("p", "empty-state", "No escalation events recorded."));
      return;
    }

    for (const event of events) {
      const card = element("article", "event");
      const main = element("div", "event-main");
      const heading = element("div", "event-heading");
      heading.append(element("h3", "", event.summary));

      const tags = element("div", "event-tags");
      const severity = element("span", "tag", event.severity);
      severity.dataset.severity = event.severity;
      const state = element("span", "tag", displayName(event.state));
      state.dataset.state = event.state;
      tags.append(severity, state, element("span", "tag", displayName(event.kind)));
      heading.append(tags);

      const metadata = element("div", "event-meta");
      metadata.append(
        element("span", "", event.agent_type),
        element("span", "", event.source),
        element("span", "", relativeTime(event.detected_at)),
      );
      main.append(heading, metadata);
      card.append(main, renderTimeline(event.timeline));
      eventsContainer.append(card);
    }
  };

  const setHealth = (status, label) => {
    health.dataset.status = status;
    healthLabel.textContent = label;
  };

  const refresh = async () => {
    try {
      const response = await fetch(`${apiBase}/snapshot`, {
        cache: "no-store",
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
      if (!response.ok) {
        throw new Error("dashboard data unavailable");
      }
      const snapshot = await response.json();
      renderStates(snapshot.state_counts);
      renderEvents(snapshot.events);
      lastUpdated.textContent = `Updated ${relativeTime(snapshot.generated_at)}`;
      setHealth("ok", "Store connected");
    } catch {
      setHealth("degraded", "Store unavailable");
      lastUpdated.textContent = "Refresh failed";
    }
  };

  refresh();
  window.setInterval(refresh, 5000);
})();
