/**
 * Google SecOps Ingestion Intelligence Dashboard
 * Dynamic Charting, Table Filtering, Reconciliation, and API integration.
 */

let currentPeriod = 'daily';
let volumeChartInstance = null;
let donutChartInstance = null;
let rawLogSources = [];

// Utility formatting functions
function formatBytes(bytes) {
  if (bytes === 0 || !bytes) return '0 B';
  const k = 1000;
  const sizes = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
  const i = Math.floor(Math.log(bytes) / Math.log(k));
  return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
}

function formatNumber(num) {
  if (!num) return '0';
  if (num >= 1e9) return (num / 1e9).toFixed(2) + 'B';
  if (num >= 1e6) return (num / 1e6).toFixed(2) + 'M';
  if (num >= 1e3) return (num / 1e3).toFixed(2) + 'K';
  return num.toLocaleString();
}

// Initialize on DOM load
document.addEventListener('DOMContentLoaded', () => {
  setupEventListeners();
  loadDashboardData(currentPeriod);
});

function setupEventListeners() {
  // Timeframe selector pills
  document.querySelectorAll('.timeframe-btn').forEach(btn => {
    btn.addEventListener('click', (e) => {
      document.querySelectorAll('.timeframe-btn').forEach(b => b.classList.remove('active'));
      e.target.classList.add('active');
      currentPeriod = e.target.getAttribute('data-period');
      loadDashboardData(currentPeriod);
    });
  });

  // Tab navigation
  document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', (e) => {
      const tabTarget = btn.getAttribute('data-tab');
      document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
      document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
      btn.classList.add('active');
      const activeContent = document.getElementById(tabTarget);
      if (activeContent) activeContent.classList.add('active');

      if (tabTarget === 'tabReconcile') {
        loadReconciliationData();
      } else if (tabTarget === 'tabBigQuery') {
        loadBigQueryData();
      }
    });
  });

  // Refresh button
  document.getElementById('btnRefresh').addEventListener('click', async () => {
    const btn = document.getElementById('btnRefresh');
    btn.disabled = true;
    btn.innerHTML = `<span class="pulse-dot"></span> Fetching...`;
    try {
      await fetch('/api/refresh', { method: 'POST' });
      await loadDashboardData(currentPeriod);
    } finally {
      btn.disabled = false;
      btn.innerHTML = `
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M23 4v6h-6M1 20v-6h6M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/>
        </svg> Refresh`;
    }
  });

  // Search filter
  document.getElementById('searchTable').addEventListener('input', (e) => {
    filterSourcesTable(e.target.value);
  });

  // Re-run reconcile button
  document.getElementById('btnRunReconcile').addEventListener('click', () => {
    loadReconciliationData();
  });

  // Export Buttons
  document.getElementById('btnExportCsv').addEventListener('click', () => {
    window.location.href = `/api/export/csv?period=${currentPeriod}`;
  });

  document.getElementById('btnExportJson').addEventListener('click', () => {
    window.location.href = `/api/export/json?period=${currentPeriod}`;
  });

  // Settings Modal controls
  const modal = document.getElementById('settingsModal');
  document.getElementById('btnSettings').addEventListener('click', () => {
    modal.classList.add('active');
  });

  document.getElementById('btnCloseModal').addEventListener('click', () => {
    modal.classList.remove('active');
  });

  document.getElementById('btnCancelSettings').addEventListener('click', () => {
    modal.classList.remove('active');
  });

  document.getElementById('btnSaveSettings').addEventListener('click', async () => {
    const projectId = document.getElementById('inputProjectId').value.trim();
    const credsPath = document.getElementById('inputCredsPath').value.trim();
    const mockMode = document.getElementById('checkMockMode').checked;

    await fetch('/api/settings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        project_id: projectId,
        credentials_path: credsPath,
        mock_mode: mockMode,
      }),
    });

    modal.classList.remove('active');
    document.getElementById('currentProjectText').textContent = projectId;
    document.getElementById('footerProject').textContent = projectId;

    const modeBadge = document.getElementById('modeBadge');
    const modeText = document.getElementById('modeText');
    if (mockMode) {
      modeBadge.className = 'badge badge-mock';
      modeText.textContent = 'MOCK / DEMO';
    } else {
      modeBadge.className = 'badge badge-live';
      modeText.textContent = 'GCP LIVE';
    }

    loadDashboardData(currentPeriod);
  });
}

async function loadDashboardData(period) {
  const titles = {
    daily: 'Daily Ingestion Summary (Last 24 Hours)',
    weekly: 'Weekly Ingestion Summary (Last 7 Days)',
    monthly: 'Monthly Ingestion Summary (Last 30 Days)',
  };
  document.getElementById('viewTitle').textContent = titles[period] || 'SecOps Ingestion Summary';

  try {
    // 1. Fetch Summary
    const summaryRes = await fetch(`/api/ingestion/summary?period=${period}`);
    const summary = await summaryRes.json();
    renderKpiSummary(summary);

    // 2. Fetch Breakdown
    const breakdownRes = await fetch(`/api/ingestion/breakdown?period=${period}`);
    const breakdown = await breakdownRes.json();
    rawLogSources = breakdown.log_types || [];
    renderSourcesTable(rawLogSources);
    renderHealthCards(rawLogSources);

    // 3. Fetch Timeseries for Charts
    const timeseriesRes = await fetch(`/api/ingestion/timeseries?period=${period}`);
    const timeseries = await timeseriesRes.json();
    renderCharts(timeseries.timeline, rawLogSources);

    // If reconcile or bigquery tab is already active, reload them too
    const activeTab = document.querySelector('.tab-btn.active');
    if (activeTab && activeTab.getAttribute('data-tab') === 'tabReconcile') {
      loadReconciliationData();
    } else if (activeTab && activeTab.getAttribute('data-tab') === 'tabBigQuery') {
      loadBigQueryData();
    }
  } catch (err) {
    console.error('Error fetching dashboard data:', err);
  }
}

function renderKpiSummary(summary) {
  document.getElementById('kpiVolume').textContent = formatBytes(summary.total_bytes);
  document.getElementById('kpiVolumeMb').textContent = `${summary.total_size_mb.toLocaleString()} MB`;
  document.getElementById('kpiActiveLogTypes').textContent = `${summary.active_log_types_count} Active Log Types`;

  document.getElementById('kpiRecords').textContent = formatNumber(summary.total_records);
  document.getElementById('kpiEvents').textContent = formatNumber(summary.total_normalized_events);
  
  const normRatio = summary.overall_norm_ratio || 0.0;
  document.getElementById('kpiNormRatio').textContent = `${normRatio.toFixed(1)}%`;

  const bar = document.getElementById('normProgressBar');
  bar.style.width = `${Math.min(normRatio, 100)}%`;
  if (normRatio >= 95) {
    bar.style.background = 'linear-gradient(90deg, #10b981, #06b6d4)';
  } else if (normRatio >= 80) {
    bar.style.background = 'linear-gradient(90deg, #f59e0b, #fbbf24)';
  } else {
    bar.style.background = 'linear-gradient(90deg, #ef4444, #f87171)';
  }

  document.getElementById('kpiHealthBreakdown').textContent = 
    `${summary.healthy_sources_count} Healthy • ${summary.warning_sources_count} Warning • ${summary.critical_sources_count} Critical`;

  document.getElementById('viewIntervalText').textContent = 
    `Time Window: ${summary.start_time} to ${summary.end_time}`;
}

function renderCharts(timeline, logTypes) {
  if (typeof Chart === 'undefined') {
    console.warn('Chart.js not loaded, skipping chart render.');
    return;
  }

  // --- 1. Volume Trend Line Chart ---
  const ctxVolume = document.getElementById('volumeTrendChart').getContext('2d');
  if (volumeChartInstance) volumeChartInstance.destroy();

  const labels = (timeline || []).map(pt => {
    const d = new Date(pt.timestamp);
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) + (timeline.length > 24 ? ' ' + (d.getMonth()+1)+'/'+d.getDate() : '');
  });
  const dataMb = (timeline || []).map(pt => pt.size_mb);

  volumeChartInstance = new Chart(ctxVolume, {
    type: 'line',
    data: {
      labels: labels,
      datasets: [{
        label: 'Ingested Volume (MB)',
        data: dataMb,
        borderColor: '#3b82f6',
        backgroundColor: 'rgba(59, 130, 246, 0.12)',
        borderWidth: 2,
        fill: true,
        tension: 0.35,
        pointRadius: labels.length > 40 ? 0 : 2,
        pointHoverRadius: 5,
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { display: false },
        tooltip: {
          mode: 'index',
          intersect: false,
          callbacks: {
            label: (ctx) => ` Volume: ${ctx.parsed.y.toLocaleString()} MB`
          }
        }
      },
      scales: {
        x: {
          grid: { color: 'rgba(255, 255, 255, 0.05)' },
          ticks: { color: '#94a3b8', maxTicksLimit: 8 }
        },
        y: {
          grid: { color: 'rgba(255, 255, 255, 0.05)' },
          ticks: { color: '#94a3b8' }
        }
      }
    }
  });

  // --- 2. Share by Log Type Donut Chart ---
  const ctxDonut = document.getElementById('logTypeDonutChart').getContext('2d');
  if (donutChartInstance) donutChartInstance.destroy();

  const topSources = (logTypes || []).slice(0, 6);
  const otherSources = (logTypes || []).slice(6);
  const otherMb = otherSources.reduce((acc, curr) => acc + curr.size_mb, 0);

  const donutLabels = topSources.map(s => s.log_type);
  const donutData = topSources.map(s => s.size_mb);
  if (otherMb > 0) {
    donutLabels.push('OTHER');
    donutData.push(otherMb);
  }

  const palette = ['#3b82f6', '#8b5cf6', '#06b6d4', '#10b981', '#f59e0b', '#ec4899', '#64748b'];

  donutChartInstance = new Chart(ctxDonut, {
    type: 'doughnut',
    data: {
      labels: donutLabels,
      datasets: [{
        data: donutData,
        backgroundColor: palette.slice(0, donutLabels.length),
        borderColor: '#182234',
        borderWidth: 2,
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: {
          position: 'right',
          labels: { color: '#cbd5e1', boxWidth: 12, font: { size: 11 } }
        },
        tooltip: {
          callbacks: {
            label: (ctx) => ` ${ctx.label}: ${ctx.parsed.toLocaleString()} MB`
          }
        }
      },
      cutout: '65%'
    }
  });
}

function renderSourcesTable(sources) {
  const tbody = document.getElementById('sourcesTableBody');
  tbody.innerHTML = '';

  if (!sources || sources.length === 0) {
    tbody.innerHTML = `<tr><td colspan="9" style="text-align: center; padding: 2rem; color: var(--text-dim);">No log sources returned for this timeframe.</td></tr>`;
    document.getElementById('tableCountText').textContent = '0 log sources';
    return;
  }

  document.getElementById('tableCountText').textContent = `Showing ${sources.length} active log sources`;

  sources.forEach(src => {
    const tr = document.createElement('tr');
    
    // Status badge class
    let statusClass = 'status-healthy';
    if (src.health_status === 'DEGRADED') statusClass = 'status-degraded';
    else if (src.health_status === 'INACTIVE') statusClass = 'status-inactive';
    else if (src.health_status.includes('FAIL') || src.health_status.includes('CRITICAL')) statusClass = 'status-critical';

    const collectorsHtml = (src.collectors && src.collectors.length > 0)
      ? src.collectors.map(c => `<span class="collector-pill" title="Collector ID">${c}</span>`).join('')
      : '<span style="color:var(--text-dim); font-size:0.75rem;">direct / gcp feed</span>';

    tr.innerHTML = `
      <td>
        <span class="log-type-tag">${src.log_type}</span>
      </td>
      <td>${collectorsHtml}</td>
      <td style="text-align: right; font-weight: 600;">${src.size_mb.toLocaleString()}</td>
      <td style="text-align: right; color: var(--text-muted);">${src.size_gb.toFixed(3)}</td>
      <td style="text-align: right;">${formatNumber(src.record_count)}</td>
      <td style="text-align: right; color: #34d399;">${formatNumber(src.normalized_event_count)}</td>
      <td style="text-align: right; font-weight: 700;">${src.normalization_rate_pct.toFixed(1)}%</td>
      <td style="text-align: right; font-family: var(--font-mono); font-size: 0.8rem; color: var(--text-muted);">${src.avg_bytes_per_record} B</td>
      <td>
        <span class="status-indicator ${statusClass}">
          ${src.health_status}
        </span>
      </td>
    `;
    tbody.appendChild(tr);
  });
}

function filterSourcesTable(query) {
  const q = query.toLowerCase().trim();
  if (!q) {
    renderSourcesTable(rawLogSources);
    return;
  }
  const filtered = rawLogSources.filter(s => {
    const matchesType = s.log_type.toLowerCase().includes(q);
    const matchesColls = (s.collectors || []).some(c => c.toLowerCase().includes(q));
    const matchesStatus = s.health_status.toLowerCase().includes(q);
    return matchesType || matchesColls || matchesStatus;
  });
  renderSourcesTable(filtered);
}

function renderHealthCards(sources) {
  const container = document.getElementById('healthAlertsContainer');
  container.innerHTML = '';

  const issues = sources.filter(s => s.health_status !== 'HEALTHY');
  if (issues.length === 0) {
    container.innerHTML = `
      <div class="kpi-card" style="grid-column: 1 / -1; padding: 2rem; text-align: center;">
        <div style="font-size: 1.1rem; font-weight: 700; color: #10b981; margin-bottom: 0.5rem;">All Ingestion Sources Operating Normally</div>
        <p style="color: var(--text-muted); font-size: 0.85rem;">All active log types show high normalization efficiency (&gt;95%) and steady traffic.</p>
      </div>`;
    return;
  }

  issues.forEach(src => {
    const card = document.createElement('div');
    card.className = 'kpi-card';
    card.style.borderLeft = `4px solid ${src.health_color === 'red' ? '#ef4444' : '#f59e0b'}`;

    card.innerHTML = `
      <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.5rem;">
        <span class="log-type-tag">${src.log_type}</span>
        <span class="status-indicator ${src.health_color === 'red' ? 'status-critical' : 'status-degraded'}">${src.health_status}</span>
      </div>
      <div style="font-size: 0.85rem; color: #fff; margin-bottom: 0.5rem;">
        Efficiency: <strong>${src.normalization_rate_pct}%</strong> (${formatNumber(src.normalized_event_count)} / ${formatNumber(src.record_count)} records)
      </div>
      <div style="font-size: 0.8rem; color: var(--text-muted); line-height: 1.4;">
        ${src.recommendation}
      </div>
    `;
    container.appendChild(card);
  });
}

async function loadReconciliationData() {
  const tbody = document.getElementById('reconcileTableBody');
  tbody.innerHTML = `<tr><td colspan="7" style="text-align: center; padding: 2rem; color: var(--text-muted);"><span class="pulse-dot"></span> Calculating 30m sum vs rollup window...</td></tr>`;

  try {
    const res = await fetch(`/api/ingestion/reconcile?period=${currentPeriod}`);
    const data = await res.json();
    const rows = data.reconciliation || [];

    tbody.innerHTML = '';
    if (rows.length === 0) {
      tbody.innerHTML = `<tr><td colspan="7" style="text-align: center; padding: 2rem; color: var(--text-dim);">No reconciliation series returned.</td></tr>`;
      return;
    }

    rows.forEach(r => {
      const tr = document.createElement('tr');
      const isSumWinner = r.winner.includes('30m');
      const winnerClass = isSumWinner ? 'winner-sum' : 'winner-rollup';

      tr.innerHTML = `
        <td><span class="log-type-tag">${r.log_type}</span></td>
        <td><span class="collector-pill">${r.collector_id}</span></td>
        <td style="text-align: right; font-family: var(--font-mono);">${formatBytes(r.sum_30m)}</td>
        <td style="text-align: right; font-family: var(--font-mono);">${formatBytes(r.rollup)}</td>
        <td style="text-align: right; font-family: var(--font-mono); font-weight: 700; color: #60a5fa;">${formatBytes(r.official_max)}</td>
        <td style="text-align: right; font-size: 0.8rem; color: ${r.variance >= 0 ? '#34d399' : '#f87171'};">${r.variance_pct > 0 ? '+' : ''}${r.variance_pct}%</td>
        <td><span class="winner-pill ${winnerClass}">${r.winner}</span></td>
      `;
      tbody.appendChild(tr);
    });
  } catch (err) {
    console.error('Error fetching reconciliation:', err);
    tbody.innerHTML = `<tr><td colspan="7" style="text-align: center; padding: 2rem; color: #ef4444;">Failed to execute reconciliation audit: ${err.message}</td></tr>`;
  }
}

async function loadBigQueryData() {
  const tbody = document.getElementById('bqTableBody');
  tbody.innerHTML = `<tr><td colspan="9" style="text-align: center; padding: 2rem; color: var(--text-muted);"><span class="pulse-dot"></span> Mapping Cloud Monitoring metrics to BigQuery datalake schema...</td></tr>`;

  try {
    const res = await fetch(`/api/ingestion/bigquery-compat?period=${currentPeriod}`);
    const data = await res.json();
    const rows = data.rows || [];

    tbody.innerHTML = '';
    if (rows.length === 0) {
      tbody.innerHTML = `<tr><td colspan="9" style="text-align: center; padding: 2rem; color: var(--text-dim);">No rows available.</td></tr>`;
      return;
    }

    rows.forEach(r => {
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td><span class="log-type-tag">${r.log_type}</span></td>
        <td style="font-family: var(--font-mono); font-size: 0.75rem; color: var(--text-muted);">${r.collector_ids}</td>
        <td style="font-size: 0.75rem;">${r.input_types}</td>
        <td style="text-align: right; font-weight: 600;">${r.size_mb.toLocaleString()}</td>
        <td style="text-align: right;">${formatNumber(r.event_count)}</td>
        <td style="text-align: right; color: #34d399;">${formatNumber(r.normalized_events)}</td>
        <td style="text-align: right; color: ${r.error_events > 0 ? '#ef4444' : 'var(--text-dim)'};">${formatNumber(r.error_events)}</td>
        <td style="font-size: 0.75rem; color: var(--text-dim);">${r.drop_reason_codes}</td>
        <td><span class="status-indicator ${r.health_status === 'HEALTHY' ? 'status-healthy' : 'status-degraded'}">${r.health_status}</span></td>
      `;
      tbody.appendChild(tr);
    });
  } catch (err) {
    console.error('Error fetching BigQuery format:', err);
    tbody.innerHTML = `<tr><td colspan="9" style="text-align: center; padding: 2rem; color: #ef4444;">Failed loading BigQuery view: ${err.message}</td></tr>`;
  }
}
