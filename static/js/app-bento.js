/**
 * Google SecOps Ingestion Intelligence - Cyber Bento Engine
 * Alpine.js Reactive State Controller with Chart.js Integration
 */

// Configure high-contrast global defaults for Chart.js in dark theme
if (typeof Chart !== 'undefined') {
  Chart.defaults.color = '#f1f5f9';
  Chart.defaults.borderColor = 'rgba(255, 255, 255, 0.08)';
  Chart.defaults.font.family = 'Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif';
}

function secopsDashboard() {
  return {
    // Core State
    period: 'daily',
    loading: false,
    refreshing: false,
    collecting: false,
    autoRefresh: true,
    countdown: 30,
    countdownInterval: null,
    
    // UI Navigation
    activeTab: 'sources',
    chartMetric: 'both', // 'volume', 'records', 'both'
    cachedTsData: null,
    searchQuery: '',
    healthFilter: 'all', // 'all', 'optimal', 'warning', 'degraded'
    sortBy: 'volume',
    sortDesc: true,

    // Status & Diagnostics
    status: {
      projectId: 'Loading...',
      mockMode: false,
      authMethod: 'Initializing...',
      status: 'connecting',
      sslEnabled: window.location.protocol === 'https:'
    },

    // Metrics & Summary Data
    summary: {
      totalBytes: 0,
      totalSizeGb: 0,
      totalSizeMb: 0,
      totalRecords: 0,
      totalNormalizedEvents: 0,
      normRatioPct: 100,
      activeSources: 0,
      healthyCount: 0,
      errorCount: 0,
      unparsedRecords: 0,
      periodTitle: 'Daily (Last 24 Hours)',
      intervalStr: '',
      latencyMs: 42
    },

    // Animated Numbers for smooth counter effect
    anim: {
      volumeGb: 0,
      records: 0,
      events: 0,
      ratio: 0
    },

    // Table Datasets
    sources: [],
    reconcileRows: [],
    bigqueryRows: [],

    // Chart.js Instances
    volumeChart: null,
    donutChart: null,

    // Modals & Drawers
    settingsModalOpen: false,
    inspectorModalOpen: false,
    settingsForm: {
      projectId: '',
      mockMode: false
    },

    // Toast Notifications
    toast: {
      show: false,
      message: '',
      type: 'info',
      timeout: null
    },

    // Initialization
    async init() {
      await this.fetchStatus();
      await this.loadAllData();
      this.startAutoRefresh();
      
      // Handle visibility changes to conserve resources
      document.addEventListener('visibilitychange', () => {
        if (document.hidden) {
          this.pauseAutoRefresh();
        } else {
          this.startAutoRefresh();
        }
      });
    },

    showToast(message, type = 'info') {
      if (this.toast.timeout) clearTimeout(this.toast.timeout);
      this.toast.message = message;
      this.toast.type = type;
      this.toast.show = true;
      this.toast.timeout = setTimeout(() => {
        this.toast.show = false;
      }, 4000);
    },

    startAutoRefresh() {
      this.pauseAutoRefresh();
      this.countdown = 30;
      this.countdownInterval = setInterval(() => {
        if (!this.autoRefresh || this.loading || this.refreshing) return;
        this.countdown--;
        if (this.countdown <= 0) {
          this.countdown = 30;
          this.refreshDashboard(false);
        }
      }, 1000);
    },

    pauseAutoRefresh() {
      if (this.countdownInterval) clearInterval(this.countdownInterval);
    },

    toggleAutoRefresh() {
      this.autoRefresh = !this.autoRefresh;
      if (this.autoRefresh) {
        this.startAutoRefresh();
        this.showToast('Auto-refresh active (30s interval)', 'info');
      } else {
        this.pauseAutoRefresh();
        this.showToast('Auto-refresh paused', 'warning');
      }
    },

    // API: Fetch Service Status
    async fetchStatus() {
      try {
        const res = await fetch('/api/status');
        if (res.ok) {
          const data = await res.json();
          this.status.projectId = data.project_id || 'Unknown';
          this.status.mockMode = !!data.mock_mode;
          this.status.authMethod = data.auth_method || 'ADC / Service Account';
          this.status.status = data.status || 'connected';
          this.status.sslEnabled = window.location.protocol === 'https:';

          this.settingsForm.projectId = this.status.projectId;
          this.settingsForm.mockMode = this.status.mockMode;
        }
      } catch (err) {
        console.error('Failed to load status:', err);
      }
    },

    // Period Switcher
    async setPeriod(p) {
      if (this.period === p) return;
      this.period = p;
      this.countdown = 30;
      await this.loadAllData();
      this.showToast(`Switched view to ${this.period.toUpperCase()}`, 'info');
    },

    // Primary Data Loader
    async loadAllData() {
      this.loading = true;
      const startTime = performance.now();

      try {
        const [sumRes, breakRes, tsRes] = await Promise.all([
          fetch(`/api/ingestion/summary?period=${this.period}`),
          fetch(`/api/ingestion/breakdown?period=${this.period}`),
          fetch(`/api/ingestion/timeseries?period=${this.period}`)
        ]);

        if (sumRes.ok) {
          const sumData = await sumRes.json();
          this.updateSummary(sumData);
        }

        if (breakRes.ok) {
          const breakData = await breakRes.json();
          const rawList = breakData.log_types || breakData.log_sources || [];
          this.sources = rawList.map(s => {
            const records = s.record_count || 0;
            const normEvents = s.normalized_event_count ?? s.normalized_events ?? 0;
            const ratio = s.normalization_rate_pct ?? (records > 0 ? (normEvents / records) * 100 : 100);
            const collectorsList = Array.isArray(s.collectors) ? s.collectors.join(', ') : (s.collector_ids || '');
            
            let health = 'optimal';
            if (s.health_status) {
              if (s.health_status === 'HEALTHY') health = 'optimal';
              else if (s.health_status === 'CRITICAL_DROPS') health = 'degraded';
              else health = 'warning';
            } else {
              if ((s.error_events || 0) > 0 || ratio < 80) health = 'degraded';
              else if (ratio < 95) health = 'warning';
            }

            return {
              ...s,
              normalized_events: normEvents,
              collector_ids: collectorsList,
              ratio_pct: Math.min(ratio, 100),
              health: health
            };
          });

          // Calculate Health counts if not explicitly populated
          if (!this.summary.healthyCount && !this.summary.errorCount) {
            this.summary.healthyCount = this.sources.filter(s => s.health === 'optimal').length;
            this.summary.errorCount = this.sources.filter(s => s.health !== 'optimal').length;
          }

          this.renderDonutChart(this.sources);
        }

        if (tsRes.ok) {
          this.cachedTsData = await tsRes.json();
          this.renderVolumeChart(this.cachedTsData);
        }

        const endTime = performance.now();
        this.summary.latencyMs = Math.round(endTime - startTime);

        // Lazily refresh active secondary tab if open
        if (this.activeTab === 'reconciliation') this.fetchReconcile();
        if (this.activeTab === 'bigquery') this.fetchBigQuery();

      } catch (err) {
        console.error('Data load error:', err);
        this.showToast('Failed to load metric stream', 'danger');
      } finally {
        this.loading = false;
      }
    },

    updateSummary(data) {
      this.summary.totalBytes = data.total_bytes || 0;
      this.summary.totalSizeGb = data.total_size_gb || 0;
      this.summary.totalSizeMb = data.total_size_mb || 0;
      this.summary.totalRecords = data.total_records || 0;
      this.summary.totalNormalizedEvents = data.total_normalized_events || 0;
      this.summary.normRatioPct = data.overall_norm_ratio ?? data.overall_norm_ratio_pct ?? 100;
      this.summary.activeSources = data.active_log_types_count ?? data.active_sources ?? 0;
      this.summary.healthyCount = data.healthy_sources_count ?? this.summary.healthyCount;
      this.summary.errorCount = (data.critical_sources_count || 0) + (data.warning_sources_count || 0);
      this.summary.unparsedRecords = Math.max(0, this.summary.totalRecords - this.summary.totalNormalizedEvents);
      this.summary.periodTitle = data.period || this.period;

      if (data.start_time && data.end_time) {
        const start = new Date(data.start_time).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
        const end = new Date(data.end_time).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
        this.summary.intervalStr = `${start} — ${end} UTC`;
      } else if (data.interval) {
        const start = new Date(data.interval.start).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
        const end = new Date(data.interval.end).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
        this.summary.intervalStr = `${start} — ${end} UTC`;
      }

      // Trigger animated counter transitions
      this.animateNumber('volumeGb', this.summary.totalSizeGb, 2);
      this.animateNumber('records', this.summary.totalRecords, 0);
      this.animateNumber('events', this.summary.totalNormalizedEvents, 0);
      this.animateNumber('ratio', Math.min(this.summary.normRatioPct, 100), 1);
    },

    // Animated Counter Effect
    animateNumber(key, targetValue, decimals = 0) {
      const startValue = this.anim[key] || 0;
      const duration = 600; // ms
      const startTime = performance.now();

      const step = (currentTime) => {
        const elapsed = currentTime - startTime;
        const progress = Math.min(elapsed / duration, 1);
        // Ease-out cubic
        const ease = 1 - Math.pow(1 - progress, 3);
        const current = startValue + (targetValue - startValue) * ease;
        this.anim[key] = parseFloat(current.toFixed(decimals));

        if (progress < 1) {
          requestAnimationFrame(step);
        } else {
          this.anim[key] = targetValue;
        }
      };

      requestAnimationFrame(step);
    },

    // Refresh Cache on Demand
    async refreshDashboard(manual = true) {
      if (manual) this.refreshing = true;
      try {
        await fetch('/api/refresh', { method: 'POST' });
        await this.loadAllData();
        if (manual) this.showToast('Telemetry refreshed from Cloud Monitoring', 'success');
      } catch (err) {
        if (manual) this.showToast('Refresh failed', 'danger');
      } finally {
        this.refreshing = false;
        this.countdown = 30;
      }
    },

    // Trigger Automated Telemetry Collector
    async triggerCollect() {
      this.collecting = true;
      this.showToast('Triggering Cloud Monitoring collection...', 'info');
      try {
        const res = await fetch('/api/collect', { method: 'POST' });
        if (res.ok) {
          const data = await res.json();
          await this.loadAllData();
          this.showToast('Collection completed & cache populated!', 'success');
        } else {
          this.showToast('Collection returned error status', 'danger');
        }
      } catch (err) {
        this.showToast('Collection request failed', 'danger');
      } finally {
        this.collecting = false;
      }
    },

    // Tab Switching
    setTab(t) {
      this.activeTab = t;
      if (t === 'reconciliation' && this.reconcileRows.length === 0) {
        this.fetchReconcile();
      } else if (t === 'bigquery' && this.bigqueryRows.length === 0) {
        this.fetchBigQuery();
      }
    },

    async fetchReconcile() {
      try {
        const res = await fetch(`/api/ingestion/reconcile?period=${this.period}`);
        if (res.ok) {
          const data = await res.json();
          this.reconcileRows = data.reconciliation || [];
        }
      } catch (e) {
        console.error('Reconciliation fetch error:', e);
      }
    },

    async fetchBigQuery() {
      try {
        const res = await fetch(`/api/ingestion/bigquery-compat?period=${this.period}`);
        if (res.ok) {
          const data = await res.json();
          this.bigqueryRows = data.rows || [];
        }
      } catch (e) {
        console.error('BigQuery fetch error:', e);
      }
    },

    setChartMetric(m) {
      this.chartMetric = m;
      if (this.cachedTsData) {
        this.renderVolumeChart(this.cachedTsData);
      }
    },

    // Chart.js Render: Ingestion Volume & Records Trend
    renderVolumeChart(tsData) {
      const ctx = document.getElementById('bentoVolumeChart');
      if (!ctx) return;

      if (this.volumeChart) {
        this.volumeChart.destroy();
        this.volumeChart = null;
      }

      if (!tsData) return;

      let points = [];
      if (tsData.timeline && Array.isArray(tsData.timeline)) {
        points = tsData.timeline;
      } else if (tsData.timestamps && Array.isArray(tsData.timestamps)) {
        points = tsData.timestamps.map((t, idx) => ({
          timestamp: t,
          bytes: (tsData.bytes_timeseries && tsData.bytes_timeseries[idx]) || 0,
          records: (tsData.records_timeseries && tsData.records_timeseries[idx]) || 0
        }));
      }

      const labels = points.map(pt => {
        const d = new Date(pt.timestamp);
        if (isNaN(d.getTime())) return pt.timestamp || '';
        if (this.period === 'daily') return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
        if (this.period === 'weekly') return d.toLocaleDateString([], { weekday: 'short', hour: '2-digit' });
        return d.toLocaleDateString([], { month: 'short', day: 'numeric' });
      });

      const volumeGbData = points.map(pt => parseFloat(((pt.bytes || 0) / 1e9).toFixed(3)));
      const recordsData = points.map(pt => pt.records || 0);

      const showVolume = this.chartMetric === 'volume' || this.chartMetric === 'both';
      const showRecords = this.chartMetric === 'records' || this.chartMetric === 'both';

      const datasets = [];

      if (showVolume) {
        datasets.push({
          label: 'Ingestion Volume (GB)',
          data: volumeGbData,
          borderColor: '#00f2fe',
          backgroundColor: (context) => {
            const chart = context.chart;
            const { ctx, chartArea } = chart;
            if (!chartArea) return 'rgba(0, 242, 254, 0.1)';
            const gradient = ctx.createLinearGradient(0, chartArea.top, 0, chartArea.bottom);
            gradient.addColorStop(0, 'rgba(0, 242, 254, 0.35)');
            gradient.addColorStop(1, 'rgba(0, 242, 254, 0.00)');
            return gradient;
          },
          borderWidth: 2.5,
          tension: 0.35,
          fill: true,
          pointRadius: labels.length > 30 ? 0 : 3,
          pointHoverRadius: 6,
          pointBackgroundColor: '#00f2fe',
          yAxisID: 'yVolume'
        });
      }

      if (showRecords) {
        datasets.push({
          label: 'Raw Records',
          data: recordsData,
          borderColor: '#a855f7',
          backgroundColor: (context) => {
            const chart = context.chart;
            const { ctx, chartArea } = chart;
            if (!chartArea) return 'rgba(168, 85, 247, 0.1)';
            const gradient = ctx.createLinearGradient(0, chartArea.top, 0, chartArea.bottom);
            gradient.addColorStop(0, 'rgba(168, 85, 247, 0.25)');
            gradient.addColorStop(1, 'rgba(168, 85, 247, 0.00)');
            return gradient;
          },
          borderWidth: 2,
          tension: 0.35,
          fill: true,
          pointRadius: labels.length > 30 ? 0 : 3,
          pointHoverRadius: 6,
          pointBackgroundColor: '#a855f7',
          yAxisID: 'yRecords'
        });
      }

      this.volumeChart = new Chart(ctx, {
        type: 'line',
        data: {
          labels: labels,
          datasets: datasets
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          interaction: {
            mode: 'index',
            intersect: false
          },
          plugins: {
            legend: {
              display: true,
              position: 'top',
              align: 'end',
              labels: {
                color: '#f8fafc',
                boxWidth: 10,
                boxHeight: 10,
                usePointStyle: true,
                pointStyle: 'circle',
                font: { family: 'Inter', size: 11, weight: '600' }
              }
            },
            tooltip: {
              backgroundColor: 'rgba(8, 12, 20, 0.95)',
              titleColor: '#f8fafc',
              bodyColor: '#cbd5e1',
              borderColor: 'rgba(0, 242, 254, 0.4)',
              borderWidth: 1,
              padding: 12,
              cornerRadius: 8,
              boxPadding: 6,
              titleFont: { family: 'Inter', size: 12, weight: '600' },
              bodyFont: { family: 'JetBrains Mono', size: 12 },
              callbacks: {
                label: function(context) {
                  let label = context.dataset.label || '';
                  if (label) label += ': ';
                  if (context.dataset.yAxisID === 'yVolume') {
                    label += context.parsed.y.toFixed(3) + ' GB';
                  } else {
                    label += context.parsed.y.toLocaleString() + ' logs';
                  }
                  return label;
                }
              }
            }
          },
          scales: {
            x: {
              grid: { color: 'rgba(255, 255, 255, 0.04)' },
              ticks: { color: '#94a3b8', font: { family: 'Inter', size: 10 }, maxRotation: 0 }
            },
            ...(showVolume ? {
              yVolume: {
                position: 'left',
                grid: { color: 'rgba(255, 255, 255, 0.04)' },
                ticks: {
                  color: '#38bdf8',
                  font: { family: 'JetBrains Mono', size: 10 },
                  callback: (v) => v + ' GB'
                }
              }
            } : {}),
            ...(showRecords ? {
              yRecords: {
                position: showVolume ? 'right' : 'left',
                grid: { drawOnChartArea: !showVolume, color: 'rgba(255, 255, 255, 0.04)' },
                ticks: {
                  color: '#c084fc',
                  font: { family: 'JetBrains Mono', size: 10 },
                  callback: (v) => {
                    if (v >= 1e6) return (v / 1e6).toFixed(1) + 'M';
                    if (v >= 1e3) return (v / 1e3).toFixed(0) + 'K';
                    return v.toLocaleString();
                  }
                }
              }
            } : {})
          }
        }
      });
    },

    // Chart.js Render: Volume Share Donut
    renderDonutChart(sources) {
      const ctx = document.getElementById('bentoDonutChart');
      if (!ctx) return;

      if (this.donutChart) {
        this.donutChart.destroy();
        this.donutChart = null;
      }

      if (!sources || sources.length === 0) return;

      const topSources = [...sources]
        .sort((a, b) => (b.bytes_count || 0) - (a.bytes_count || 0))
        .slice(0, 6);

      const labels = topSources.map(s => s.log_type);
      const data = topSources.map(s => parseFloat(((s.bytes_count || 0) / 1e9).toFixed(3)));

      const cyberColors = [
        '#00f2fe',
        '#a855f7',
        '#10b981',
        '#f59e0b',
        '#3b82f6',
        '#f43f5e'
      ];

      this.donutChart = new Chart(ctx, {
        type: 'doughnut',
        data: {
          labels: labels,
          datasets: [{
            data: data,
            backgroundColor: cyberColors.slice(0, labels.length),
            borderColor: '#080c14',
            borderWidth: 2,
            hoverOffset: 6
          }]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          cutout: '72%',
          plugins: {
            legend: {
              position: 'right',
              labels: {
                color: '#f8fafc',
                boxWidth: 10,
                boxHeight: 10,
                usePointStyle: true,
                pointStyle: 'circle',
                padding: 10,
                font: { family: 'Inter', size: 11, weight: '500' },
                generateLabels: function(chart) {
                  const chartData = chart.data;
                  if (chartData.labels.length && chartData.datasets.length) {
                    return chartData.labels.map((label, i) => {
                      const val = chartData.datasets[0].data[i];
                      const shortLabel = label.length > 14 ? label.slice(0, 14) + '…' : label;
                      const displayVal = val >= 0.01 ? `${val} GB` : `${(val * 1000).toFixed(1)} MB`;
                      return {
                        text: `${shortLabel} (${displayVal})`,
                        fillStyle: chartData.datasets[0].backgroundColor[i],
                        strokeStyle: chartData.datasets[0].borderColor,
                        fontColor: '#f8fafc',
                        lineWidth: 1,
                        hidden: false,
                        index: i
                      };
                    });
                  }
                  return [];
                }
              }
            },
            tooltip: {
              backgroundColor: 'rgba(8, 12, 20, 0.95)',
              titleColor: '#f8fafc',
              bodyColor: '#cbd5e1',
              borderColor: 'rgba(0, 242, 254, 0.4)',
              borderWidth: 1,
              padding: 10,
              cornerRadius: 6,
              titleFont: { family: 'Inter', size: 12, weight: '600' },
              bodyFont: { family: 'JetBrains Mono', size: 11 },
              callbacks: {
                label: function(context) {
                  const val = context.parsed;
                  const total = context.dataset.data.reduce((a, b) => a + b, 0);
                  const pct = total > 0 ? ((val / total) * 100).toFixed(1) : 0;
                  const displayVal = val >= 0.01 ? `${val} GB` : `${(val * 1000).toFixed(1)} MB`;
                  return ` ${context.label}: ${displayVal} (${pct}%)`;
                }
              }
            }
          }
        }
      });
    },

    // Filtered & Sorted Table Rows (Computed)
    get filteredSources() {
      let list = this.sources.filter(s => {
        const matchesSearch = !this.searchQuery || 
          s.log_type.toLowerCase().includes(this.searchQuery.toLowerCase()) ||
          (s.collector_ids || '').toLowerCase().includes(this.searchQuery.toLowerCase());
        
        const matchesHealth = 
          this.healthFilter === 'all' ||
          (this.healthFilter === 'optimal' && s.health === 'optimal') ||
          (this.healthFilter === 'warning' && s.health === 'warning') ||
          (this.healthFilter === 'degraded' && s.health === 'degraded');

        return matchesSearch && matchesHealth;
      });

      list.sort((a, b) => {
        let valA = a[this.sortBy];
        let valB = b[this.sortBy];

        if (this.sortBy === 'volume') {
          valA = a.bytes_count;
          valB = b.bytes_count;
        } else if (this.sortBy === 'records') {
          valA = a.record_count;
          valB = b.record_count;
        } else if (this.sortBy === 'events') {
          valA = a.normalized_events;
          valB = b.normalized_events;
        } else if (this.sortBy === 'errors') {
          valA = a.error_events;
          valB = b.error_events;
        } else if (this.sortBy === 'ratio') {
          valA = a.ratio_pct;
          valB = b.ratio_pct;
        } else if (this.sortBy === 'name') {
          valA = a.log_type.toLowerCase();
          valB = b.log_type.toLowerCase();
        }

        if (valA < valB) return this.sortDesc ? 1 : -1;
        if (valA > valB) return this.sortDesc ? -1 : 1;
        return 0;
      });

      return list;
    },

    toggleSort(col) {
      if (this.sortBy === col) {
        this.sortDesc = !this.sortDesc;
      } else {
        this.sortBy = col;
        this.sortDesc = true;
      }
    },

    // Save Settings
    //
    // /api/settings mutates runtime state, so it is gated behind ADMIN_SECRET on
    // the server. The token is prompted for per-save and deliberately never
    // persisted to localStorage or sessionStorage.
    async saveSettings() {
      const token = window.prompt('Admin token (ADMIN_SECRET) required to change settings:');
      if (!token) {
        this.showToast('Settings unchanged - no admin token supplied', 'danger');
        return;
      }

      try {
        const res = await fetch('/api/settings', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'Authorization': `Bearer ${token}`
          },
          body: JSON.stringify({
            project_id: this.settingsForm.projectId,
            mock_mode: this.settingsForm.mockMode
          })
        });

        if (res.ok) {
          const data = await res.json();
          this.status.projectId = data.project_id;
          this.status.mockMode = data.mock_mode;
          this.settingsModalOpen = false;
          this.showToast('Settings updated successfully', 'success');
          await this.loadAllData();
        } else if (res.status === 503) {
          this.showToast('Settings are disabled: ADMIN_SECRET is not configured on the server', 'danger');
        } else if (res.status === 401) {
          this.showToast('Rejected: invalid admin token', 'danger');
        } else {
          const body = await res.json().catch(() => ({}));
          this.showToast(body.error || 'Failed to update settings', 'danger');
        }
      } catch (err) {
        this.showToast('Error saving settings', 'danger');
      }
    },

    // Formatting Helpers
    formatBytes(bytes) {
      if (!bytes || bytes === 0) return '0 B';
      const k = 1000;
      const sizes = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
      const i = Math.floor(Math.log(bytes) / Math.log(k));
      return parseFloat((bytes / Math.pow(k, i)).toFixed(2)) + ' ' + sizes[i];
    },

    formatNumber(num) {
      if (!num) return '0';
      if (num >= 1e9) return (num / 1e9).toFixed(2) + 'B';
      if (num >= 1e6) return (num / 1e6).toFixed(2) + 'M';
      if (num >= 1e3) return (num / 1e3).toFixed(1) + 'K';
      return num.toLocaleString();
    },

    // Export Helpers
    exportCsv() {
      window.location.href = `/api/export/csv?period=${this.period}`;
    },

    exportJson() {
      window.location.href = `/api/export/json?period=${this.period}`;
    }
  };
}
