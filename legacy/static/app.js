const REFRESH_MS = 60_000;

function fmt(n) {
  return n === null || n === undefined ? "—" : n.toFixed(2);
}

async function loadSession() {
  try {
    const res = await fetch("/api/session");
    const data = await res.json();

    document.getElementById("price").textContent = data.price ? fmt(data.price) : "—";
    document.getElementById("updated").textContent = "Updated " + (data.updated_at || "—");

    renderEntries(data.entries || []);
    renderLevels(data.pivots, data.opening_range);
  } catch (e) {
    document.getElementById("entries").innerHTML =
      '<div class="placeholder">Couldn\'t reach market data. Retrying…</div>';
  }
}

function renderEntries(entries) {
  const el = document.getElementById("entries");
  if (!entries.length) {
    el.innerHTML = '<div class="placeholder">No entries yet — waiting on the opening range to form (9:30–9:45 AM ET).</div>';
    return;
  }
  el.innerHTML = entries.map(e => `
    <div class="entry">
      <span class="entry-side ${e.side.toLowerCase()}">${e.side}</span>
      <div class="entry-main">
        <div class="entry-label">${e.label}</div>
        <div class="entry-note">${e.note}</div>
      </div>
      <div class="entry-nums">
        <div class="entry-trigger">${fmt(e.trigger)}</div>
        <div class="entry-stop">stop ${fmt(e.stop)}</div>
      </div>
    </div>
  `).join("");
}

function renderLevels(pivots, orb) {
  const el = document.getElementById("levels");
  const rows = [];

  if (orb) {
    rows.push(["Opening Range High", orb.formed ? fmt(orb.high) : "forming…"]);
    rows.push(["Opening Range Low", orb.formed ? fmt(orb.low) : "forming…"]);
  }
  if (pivots) {
    rows.push(["R2", fmt(pivots.r2)]);
    rows.push(["R1", fmt(pivots.r1)]);
    rows.push(["Pivot", fmt(pivots.pivot)]);
    rows.push(["S1", fmt(pivots.s1)]);
    rows.push(["S2", fmt(pivots.s2)]);
  }

  el.innerHTML = rows.length
    ? rows.map(([name, val]) => `
        <div class="level-row">
          <span class="level-name">${name}</span>
          <span class="level-value">${val}</span>
        </div>
      `).join("")
    : '<div class="placeholder">No levels available.</div>';
}

async function loadNews() {
  try {
    const res = await fetch("/api/news");
    const data = await res.json();
    const el = document.getElementById("news");

    if (!data.items || !data.items.length) {
      el.innerHTML = '<div class="placeholder">No relevant headlines right now.</div>';
      return;
    }

    el.innerHTML = data.items.map(item => `
      <a class="news-item" href="${item.link}" target="_blank" rel="noopener">
        <div class="news-title">${item.title}</div>
        <div class="news-meta">${item.source}${item.published ? " · " + item.published : ""}</div>
      </a>
    `).join("");
  } catch (e) {
    document.getElementById("news").innerHTML =
      '<div class="placeholder">Couldn\'t load news. Retrying…</div>';
  }
}

function refresh() {
  loadSession();
  loadNews();
}

refresh();
setInterval(refresh, REFRESH_MS);
