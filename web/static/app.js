const state = {
  semanticLayer: null,
  activeTab: "metrics",
  maxLength: 300,
  chartCounter: 0,
  charts: [],
};

const els = {
  providerLabel: document.getElementById("provider-label"),
  quotaPill: document.getElementById("quota-pill"),
  kpiPremio: document.getElementById("kpi-premio"),
  kpiApolices: document.getElementById("kpi-apolices"),
  kpiRenovacao: document.getElementById("kpi-renovacao"),
  kpiSinistralidade: document.getElementById("kpi-sinistralidade"),
  form: document.getElementById("ask-form"),
  input: document.getElementById("question-input"),
  submitBtn: document.getElementById("ask-submit"),
  btnLabel: document.querySelector("#ask-submit .btn-label"),
  spinner: document.querySelector("#ask-submit .spinner"),
  charCounter: document.getElementById("char-counter"),
  errorBanner: document.getElementById("error-banner"),
  resultsFeed: document.getElementById("results-feed"),
  emptyState: document.getElementById("empty-state"),
  chips: document.getElementById("example-chips"),
  drawer: document.getElementById("semantic-drawer"),
  openSemanticBtn: document.getElementById("open-semantic-btn"),
  closeSemanticBtn: document.getElementById("close-semantic-btn"),
  closeSemanticBackdrop: document.getElementById("close-semantic-backdrop"),
  drawerContent: document.getElementById("drawer-content"),
};

function fmtBRL(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "--";
  return new Intl.NumberFormat("pt-BR", {
    style: "currency",
    currency: "BRL",
    maximumFractionDigits: 0,
  }).format(Number(value));
}

function fmtInt(value) {
  if (value === null || value === undefined) return "--";
  return new Intl.NumberFormat("pt-BR").format(Number(value));
}

function fmtPct(value) {
  if (value === null || value === undefined) return "--";
  return `${Number(value).toFixed(1).replace(".", ",")}%`;
}

function escapeHtml(text) {
  return String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function renderMarkdownBasic(mdText) {
  const lines = String(mdText || "").split(/\r?\n/);
  let html = "";
  let inList = false;

  for (const raw of lines) {
    const line = raw.trim();
    if (!line) {
      if (inList) {
        html += "</ul>";
        inList = false;
      }
      continue;
    }
    const formatted = escapeHtml(line)
      .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
      .replace(/`(.+?)`/g, "<code>$1</code>");

    if (/^[-*•]\s+/.test(line)) {
      if (!inList) {
        html += "<ul>";
        inList = true;
      }
      html += `<li>${formatted.replace(/^[-*•]\s+/, "")}</li>`;
    } else {
      if (inList) {
        html += "</ul>";
        inList = false;
      }
      html += `<p style="margin:0 0 6px 0;">${formatted}</p>`;
    }
  }
  if (inList) html += "</ul>";
  return html;
}

function updateQuotaPill(rl) {
  if (!rl || !els.quotaPill) return;
  const total = `${rl.remaining_ip}/${rl.max_requests} total`;
  els.quotaPill.textContent =
    rl.per_minute_ip !== undefined
      ? `Cota: ${rl.remaining_minute_ip}/${rl.per_minute_ip} por min · ${total}`
      : `Cota: ${total}`;
}

function showError(msg) {
  els.errorBanner.textContent = msg;
  els.errorBanner.classList.remove("hidden");
}

function clearError() {
  els.errorBanner.textContent = "";
  els.errorBanner.classList.add("hidden");
}

function setLoading(isLoading) {
  els.submitBtn.disabled = isLoading;
  els.input.disabled = isLoading;
  els.btnLabel.textContent = isLoading ? "Analisando..." : "Analisar";
  els.spinner.classList.toggle("hidden", !isLoading);
}

function downloadCSV(columns, rows, filename) {
  const csvLines = [columns.join(",")];
  for (const row of rows) {
    const vals = columns.map((c) => {
      const raw = row[c] === null || row[c] === undefined ? "" : String(row[c]);
      return raw.includes(",") || raw.includes('"') ? `"${raw.replace(/"/g, '""')}"` : raw;
    });
    csvLines.push(vals.join(","));
  }
  const blob = new Blob([csvLines.join("\n")], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

const CHART_THEMES = {
  dark: {
    palette: ["#38bdf8", "#2dd4bf", "#fbbf24", "#fb7185", "#a3e635", "#60a5fa"],
    ink: "#e4e4ea",
    muted: "#8d8d98",
    grid: "rgba(255,255,255,0.08)",
    hoverBg: "#17171c",
  },
  light: {
    palette: ["#0c5cab", "#0d9488", "#d97706", "#e11d48", "#65a30d", "#2563eb"],
    ink: "#0f172a",
    muted: "#64748b",
    grid: "rgba(15,23,42,0.10)",
    hoverBg: "#ffffff",
  },
};

let CHART_PALETTE = CHART_THEMES.dark.palette;
let CHART_INK = CHART_THEMES.dark.ink;
let CHART_MUTED = CHART_THEMES.dark.muted;
let CHART_GRID = CHART_THEMES.dark.grid;
let CHART_HOVER_BG = CHART_THEMES.dark.hoverBg;

function currentTheme() {
  return document.documentElement.getAttribute("data-theme") === "light" ? "light" : "dark";
}

function refreshChartTheme() {
  const t = CHART_THEMES[currentTheme()];
  CHART_PALETTE = t.palette;
  CHART_INK = t.ink;
  CHART_MUTED = t.muted;
  CHART_GRID = t.grid;
  CHART_HOVER_BG = t.hoverBg;
}

function applyTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  try { localStorage.setItem("theme", theme); } catch (e) { /* sem persistência */ }
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute("content", theme === "light" ? "#f6f8fb" : "#09090b");
  refreshChartTheme();
  if (window.Plotly) {
    for (const item of state.charts) {
      const el = document.getElementById(item.id);
      if (!el) continue;
      const themed = themeFigure(item.fig);
      window.Plotly.react(item.id, themed.data, themed.layout);
    }
  }
}

refreshChartTheme();

function styleAxis(axis) {
  const base = axis || {};
  const title = typeof base.title === "string" ? { text: base.title } : base.title || {};
  return Object.assign({}, base, {
    gridcolor: CHART_GRID,
    linecolor: CHART_GRID,
    zerolinecolor: CHART_GRID,
    tickfont: { color: CHART_MUTED, size: 12 },
    title: Object.assign({}, title, { font: { color: CHART_MUTED, size: 12 } }),
  });
}

function themeFigure(fig) {
  const srcLayout = Object.assign({}, fig.layout || {});
  delete srcLayout.template;
  const srcTitle = typeof srcLayout.title === "string" ? { text: srcLayout.title } : srcLayout.title || {};

  const data = (fig.data || []).map((trace, i) => {
    const color = CHART_PALETTE[i % CHART_PALETTE.length];
    const t = Object.assign({}, trace);
    t.marker = Object.assign({}, trace.marker || {});
    if (typeof t.marker.color !== "object") t.marker.color = color;
    if (trace.line || trace.type === "scatter") {
      t.line = Object.assign({}, trace.line || {}, { color, width: 3 });
    }
    t.textfont = { color: CHART_INK, size: 12 };
    if (trace.type === "bar") {
      t.textposition = "outside";
      t.cliponaxis = false;
    }
    return t;
  });

  const layout = Object.assign({}, srcLayout, {
    autosize: true,
    margin: { t: 56, r: 56, b: 56, l: 64 },
    font: { family: "IBM Plex Sans, sans-serif", color: CHART_INK },
    paper_bgcolor: "rgba(0,0,0,0)",
    plot_bgcolor: "rgba(0,0,0,0)",
    colorway: CHART_PALETTE,
    title: Object.assign({}, srcTitle, { x: 0.02, font: { color: CHART_INK, size: 15 } }),
    xaxis: styleAxis(srcLayout.xaxis),
    yaxis: styleAxis(srcLayout.yaxis),
    legend: Object.assign({}, srcLayout.legend || {}, { font: { color: CHART_MUTED } }),
    hoverlabel: { bgcolor: CHART_HOVER_BG, bordercolor: CHART_PALETTE[1], font: { color: CHART_INK } },
  });
  return { data, layout };
}

function renderResultCard(data) {
  if (els.emptyState) els.emptyState.classList.add("hidden");

  const card = document.createElement("article");
  card.className = "result-card";

  const metricsHtml = (data.matched_metrics || [])
    .map((m) => `<span class="metric-badge">Camada Semântica: ${escapeHtml(m)}</span>`)
    .join("");

  const chartId = `plotly-chart-${++state.chartCounter}`;
  const hasChart = Boolean(data.chart);

  // Detect numeric columns for right alignment
  const numericCols = new Set();
  for (const col of data.columns || []) {
    const sample = (data.rows || []).find((r) => r[col] !== null && r[col] !== undefined);
    if (sample && typeof sample[col] === "number") numericCols.add(col);
  }

  const headHtml = (data.columns || [])
    .map((c) => `<th class="${numericCols.has(c) ? "num" : ""}">${escapeHtml(c)}</th>`)
    .join("");

  const bodyHtml = (data.rows || [])
    .slice(0, 50)
    .map((row) => {
      const cells = (data.columns || [])
        .map((c) => {
          const val = row[c];
          let display = val === null || val === undefined ? "" : String(val);
          if (typeof val === "number") {
            display = Number.isInteger(val)
              ? fmtInt(val)
              : val.toLocaleString("pt-BR", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
          }
          return `<td class="${numericCols.has(c) ? "num" : ""}">${escapeHtml(display)}</td>`;
        })
        .join("");
      return `<tr>${cells}</tr>`;
    })
    .join("");

  card.innerHTML = `
    <div class="result-header">
      <div>
        <h2 class="result-question">${escapeHtml(data.question)}</h2>
        ${metricsHtml ? `<div class="metric-badges">${metricsHtml}</div>` : ""}
      </div>
      <span class="pill pill-muted">${escapeHtml(data.provider)} · ${data.attempts} tentativa(s)</span>
    </div>

    <div class="interpretation-box">
      ${renderMarkdownBasic(data.interpretation)}
    </div>

    ${hasChart ? `<div id="${chartId}" class="chart-container"></div>` : ""}

    <div class="table-toolbar">
      <span class="table-meta">Tabela analítica (${data.row_count} linha(s))</span>
      <button type="button" class="btn-outline csv-btn">Exportar CSV</button>
    </div>

    <div class="table-wrap">
      <table>
        <thead><tr>${headHtml}</tr></thead>
        <tbody>${bodyHtml}</tbody>
      </table>
    </div>

    <details class="sql-details">
      <summary>
        <span>Ver SQL validado (AST + LIMIT)</span>
        <button type="button" class="copy-btn">Copiar SQL</button>
      </summary>
      <pre><code>${escapeHtml(data.sql_validated)}</code></pre>
    </details>
  `;

  els.resultsFeed.prepend(card);

  // Render Plotly chart (tema escuro Enterprise)
  if (hasChart && window.Plotly) {
    const themed = themeFigure(data.chart);
    state.charts.push({ id: chartId, fig: data.chart });
    window.Plotly.newPlot(chartId, themed.data, themed.layout, {
      responsive: true,
      displaylogo: false,
    });
  }

  // Bind CSV export
  card.querySelector(".csv-btn").addEventListener("click", () => {
    downloadCSV(data.columns || [], data.rows || [], "analise_seguros.csv");
  });

  // Bind Copy SQL
  const copyBtn = card.querySelector(".copy-btn");
  copyBtn.addEventListener("click", async (ev) => {
    ev.preventDefault();
    ev.stopPropagation();
    try {
      await navigator.clipboard.writeText(data.sql_validated);
      copyBtn.textContent = "Copiado!";
      setTimeout(() => { copyBtn.textContent = "Copiar SQL"; }, 1600);
    } catch {
      copyBtn.textContent = "Erro ao copiar";
    }
  });
}

function renderDrawer() {
  if (!state.semanticLayer) return;
  if (state.activeTab === "metrics") {
    els.drawerContent.innerHTML = state.semanticLayer.metrics
      .map(
        (m) => `
        <div class="semantic-item">
          <h3>${escapeHtml(m.label)} <span class="muted">${escapeHtml(m.name)}</span></h3>
          <p>${escapeHtml(m.description)}</p>
          <pre><code>${escapeHtml(m.formula_sql)}</code></pre>
          ${m.caveats ? `<div class="semantic-caveat"><strong>Regra:</strong> ${escapeHtml(m.caveats)}</div>` : ""}
        </div>`
      )
      .join("");
  } else {
    els.drawerContent.innerHTML = state.semanticLayer.tables
      .map(
        (t) => `
        <div class="semantic-item">
          <h3>${escapeHtml(t.name)}</h3>
          <p>${escapeHtml(t.description)} · <strong>Grão:</strong> ${escapeHtml(t.grain)}</p>
          <p class="semantic-cols">
            ${t.columns.map((c) => `${escapeHtml(c.name)} (${escapeHtml(c.type)})`).join(" · ")}
          </p>
        </div>`
      )
      .join("");
  }
}

async function loadOverview() {
  try {
    const res = await fetch("/api/overview");
    if (!res.ok) throw new Error("Falha ao carregar visão geral.");
    const data = await res.json();

    els.providerLabel.textContent = `${data.provider.toUpperCase()} · ${data.model}`;
    updateQuotaPill(data.rate_limit);

    state.maxLength = data.max_question_length || 300;
    els.input.maxLength = state.maxLength;
    els.charCounter.textContent = `0 / ${state.maxLength}`;

    els.kpiPremio.textContent = fmtBRL(data.kpis.premio_emitido_total);
    els.kpiApolices.textContent = fmtInt(data.kpis.total_apolices);
    els.kpiRenovacao.textContent = fmtPct(data.kpis.taxa_renovacao_pct);
    els.kpiSinistralidade.textContent = fmtPct(data.kpis.sinistralidade_pct);

    state.semanticLayer = data.semantic_layer;
    renderDrawer();
  } catch (err) {
    showError(err.message || "Erro ao conectar ao servidor.");
  }
}

async function submitQuestion(question) {
  const clean = question.trim();
  if (!clean) return;
  clearError();
  setLoading(true);
  try {
    const res = await fetch("/api/ask", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question: clean }),
    });
    const data = await res.json();
    if (!res.ok) {
      throw new Error(data.detail || "Não foi possível processar sua pergunta.");
    }
    updateQuotaPill(data.rate_limit);
    renderResultCard(data);
    els.input.value = "";
    els.charCounter.textContent = `0 / ${state.maxLength}`;
  } catch (err) {
    showError(err.message || "Erro inesperado ao consultar o agente.");
  } finally {
    setLoading(false);
  }
}

// Event Listeners
els.form.addEventListener("submit", (e) => {
  e.preventDefault();
  submitQuestion(els.input.value);
});

els.input.addEventListener("input", () => {
  els.charCounter.textContent = `${els.input.value.length} / ${state.maxLength}`;
});

els.chips.addEventListener("click", (e) => {
  const btn = e.target.closest(".chip");
  if (!btn) return;
  const q = btn.getAttribute("data-q");
  els.input.value = q;
  els.charCounter.textContent = `${q.length} / ${state.maxLength}`;
  submitQuestion(q);
});

function toggleDrawer(open) {
  els.drawer.classList.toggle("hidden", !open);
  els.drawer.setAttribute("aria-hidden", String(!open));
}

els.openSemanticBtn.addEventListener("click", () => toggleDrawer(true));
els.closeSemanticBtn.addEventListener("click", () => toggleDrawer(false));
els.closeSemanticBackdrop.addEventListener("click", () => toggleDrawer(false));

document.querySelectorAll(".tab-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    state.activeTab = btn.getAttribute("data-tab");
    renderDrawer();
  });
});

document.getElementById("theme-toggle").addEventListener("click", () => {
  applyTheme(currentTheme() === "light" ? "dark" : "light");
});

document.addEventListener("DOMContentLoaded", loadOverview);
