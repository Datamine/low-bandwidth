const elements = {
  collector: document.querySelector("#collector-name"),
  sampleWindow: document.querySelector("#sample-window"),
  lastRefresh: document.querySelector("#last-refresh"),
  recipes: document.querySelector("#recipes"),
  notices: document.querySelector("#notices"),
  processTable: document.querySelector("#process-table"),
  history: document.querySelector("#history"),
  search: document.querySelector("#search"),
};

let latestStatus = null;
let inFlight = false;

function formatBytes(value) {
  if (value < 1024) return `${value} B`;
  if (value < 1024 ** 2) return `${(value / 1024).toFixed(1)} KiB`;
  if (value < 1024 ** 3) return `${(value / 1024 ** 2).toFixed(1)} MiB`;
  return `${(value / 1024 ** 3).toFixed(2)} GiB`;
}

function formatRate(value) {
  return `${formatBytes(value)}/s`;
}

function formatPorts(ports) {
  return ports && ports.length ? ports.join(", ") : "-";
}

function relativeTimestamp(unixSeconds) {
  const delta = Math.max(0, Math.round(Date.now() / 1000 - unixSeconds));
  if (delta < 2) return "just now";
  if (delta < 60) return `${delta}s ago`;
  const minutes = Math.round(delta / 60);
  return `${minutes}m ago`;
}

function button(label, className, onClick) {
  const node = document.createElement("button");
  node.textContent = label;
  if (className) node.className = className;
  node.addEventListener("click", onClick);
  return node;
}

async function postJSON(path, payload) {
  if (inFlight) return;
  inFlight = true;
  try {
    const response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const result = await response.json();
    await refresh();
    return result;
  } finally {
    inFlight = false;
  }
}

async function refresh() {
  const response = await fetch("/api/status");
  latestStatus = await response.json();
  render();
}

function render() {
  if (!latestStatus) return;
  const { snapshot, recipes, history } = latestStatus;
  elements.collector.textContent = snapshot.collector;
  elements.sampleWindow.textContent = snapshot.averaging_window_seconds
    ? `${snapshot.sample_seconds}s sample / ${snapshot.averaging_window_seconds}s avg`
    : `${snapshot.sample_seconds}s`;
  elements.lastRefresh.textContent = relativeTimestamp(snapshot.collected_at);
  elements.notices.textContent = snapshot.notices.join(" ");
  renderRecipes(recipes, snapshot.platform);
  renderProcesses(snapshot.processes);
  renderHistory(history);
}

function renderRecipes(recipes, platform) {
  elements.recipes.replaceChildren();
  if (!recipes.length) {
    const empty = document.createElement("p");
    empty.className = "notices";
    empty.textContent = `No built-in presets for ${platform}. Platform-specific presets only appear where they map to real services.`;
    elements.recipes.append(empty);
    return;
  }
  for (const recipe of recipes) {
    const card = document.createElement("article");
    card.className = "recipe-card";

    const title = document.createElement("h3");
    title.textContent = recipe.title;
    card.append(title);

    const summary = document.createElement("p");
    summary.textContent = recipe.summary;
    card.append(summary);

    const instructions = document.createElement("p");
    instructions.innerHTML = `<code>${recipe.command_preview}</code><br>${recipe.instructions}`;
    card.append(instructions);

    const meta = document.createElement("div");
    meta.className = "recipe-meta";
    meta.append(pill(recipe.temporary ? "Temporary" : "Persistent"));
    meta.append(pill(recipe.admin_required ? "Admin likely" : "No admin", recipe.admin_required ? "alert" : "good"));
    if (recipe.disruptive) meta.append(pill("Disruptive", "alert"));
    card.append(meta);

    card.append(
      button("Run preset", "", async () => {
        await postJSON("/api/recipe-action", { recipe_id: recipe.recipe_id });
      }),
    );
    elements.recipes.append(card);
  }
}

function renderProcesses(processes) {
  const query = elements.search.value.trim().toLowerCase();
  elements.processTable.replaceChildren();

  const filtered = processes.filter((process) => {
    if (!query) return true;
    return [process.display_name, process.name, process.command || ""].some((value) =>
      value.toLowerCase().includes(query),
    );
  });

  if (!filtered.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 10;
    cell.textContent = query ? "Nothing matched the current filter." : "No process traffic found in the rolling average window.";
    row.append(cell);
    elements.processTable.append(row);
    return;
  }

  for (const process of filtered) {
    const row = document.createElement("tr");

    const nameCell = document.createElement("td");
    nameCell.className = "process-name";
    const strong = document.createElement("strong");
    strong.textContent = process.display_name;
    nameCell.append(strong);
    const small = document.createElement("small");
    small.textContent = process.command || process.name;
    nameCell.append(small);
    row.append(nameCell);

    row.append(cell(String(process.pid ?? "-")));
    row.append(cell(formatPorts(process.ports)));
    row.append(cell(formatBytes(process.download_bytes)));
    row.append(cell(formatBytes(process.upload_bytes)));
    row.append(cell(formatBytes(process.total_bytes)));
    row.append(cell(formatRate(process.instant_total_rate_bps)));
    row.append(cell(formatRate(process.total_rate_bps)));
    row.append(cell(process.is_background ? "Background" : "Foreground"));

    const actionCell = document.createElement("td");
    const actionRow = document.createElement("div");
    actionRow.className = "action-row";
    if (process.pid) {
      actionRow.append(
        button("Stop", "secondary", async () => {
          await postJSON("/api/process-action", { pid: process.pid, action: "terminate" });
        }),
      );
      actionRow.append(
        button("Force stop", "danger", async () => {
          await postJSON("/api/process-action", { pid: process.pid, action: "kill" });
        }),
      );
    }

    for (const recipeId of process.recipe_ids) {
      const recipe = latestStatus.recipes.find((candidate) => candidate.recipe_id === recipeId);
      if (!recipe) continue;
      actionRow.append(
        button(recipe.title, "secondary", async () => {
          await postJSON("/api/recipe-action", { recipe_id: recipeId });
        }),
      );
    }
    actionCell.append(actionRow);
    row.append(actionCell);
    elements.processTable.append(row);
  }
}

function renderHistory(history) {
  elements.history.replaceChildren();
  if (!history.length) {
    const empty = document.createElement("p");
    empty.className = "notices";
    empty.textContent = "No actions yet.";
    elements.history.append(empty);
    return;
  }

  for (const item of history) {
    const card = document.createElement("article");
    card.className = `history-item ${item.ok ? "ok" : "fail"}`;
    const title = document.createElement("h3");
    title.textContent = item.title;
    card.append(title);
    const detail = document.createElement("p");
    detail.textContent = item.detail;
    card.append(detail);
    const meta = document.createElement("div");
    meta.className = "history-meta";
    meta.append(pill(relativeTimestamp(item.timestamp), item.ok ? "good" : "alert"));
    if (item.command) meta.append(pill(item.command));
    card.append(meta);
    elements.history.append(card);
  }
}

function pill(text, className = "") {
  const node = document.createElement("span");
  node.className = `pill ${className}`.trim();
  node.textContent = text;
  return node;
}

function cell(text) {
  const node = document.createElement("td");
  node.textContent = text;
  return node;
}

elements.search.addEventListener("input", render);

await refresh();
setInterval(refresh, 5000);
