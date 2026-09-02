(function () {
  'use strict';

  let currentUser = null;
  let channelsCache = [];
  let usersCache = [];
  let groupsCache = [];
  let currentDashboardChannelId = null;
  let searchDebounceTimer = null;
  let activeChannelConfiguration = null;
  let catalogEventSource = null;
  let catalogRefreshInFlight = false;
  let catalogRefreshQueued = false;

  // =========================================================
  // UTILIDADES
  // =========================================================

  function formatTime(seconds) {
    const value = Number(seconds);
    if (!Number.isFinite(value) || value < 0) return '00:00';

    const totalSecs = Math.floor(value);
    const hrs = Math.floor(totalSecs / 3600);
    const mins = Math.floor((totalSecs % 3600) / 60);
    const secs = totalSecs % 60;
    const pad = (n) => String(n).padStart(2, '0');

    if (hrs > 0) return `${hrs}:${pad(mins)}:${pad(secs)}`;
    return `${pad(mins)}:${pad(secs)}`;
  }

  function formatBytes(bytes) {
    const value = Number(bytes);
    if (!Number.isFinite(value) || value <= 0) return '0 MB';

    const mb = value / (1024 * 1024);
    if (mb >= 1024) return `${(mb / 1024).toFixed(2)} GB`;
    return `${mb.toFixed(1)} MB`;
  }

  function formatDate(isoStr) {
    if (!isoStr) return 'Nunca';

    const date = new Date(isoStr);
    if (Number.isNaN(date.getTime())) return String(isoStr);

    return date.toLocaleString('es-HN', {
      year: 'numeric',
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    });
  }

  function formatEpisodeDescriptor(item, compact = false) {
    const episode = Number(item?.episode_number) || 0;
    const hasSeason = item?.season_number !== null
      && item?.season_number !== undefined
      && Number(item.season_number) > 0;
    if (!hasSeason) return compact ? `E${episode}` : `Episodio ${episode}`;
    return compact
      ? `T${Number(item.season_number)}E${episode}`
      : `Temporada ${Number(item.season_number)}, Episodio ${episode}`;
  }

  function escapeHtml(value) {
    if (value === null || value === undefined) return '';

    return String(value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#039;');
  }

  async function readResponseData(response) {
    const contentType = response.headers.get('content-type') || '';

    if (contentType.includes('application/json')) {
      try {
        return await response.json();
      } catch {
        return null;
      }
    }

    try {
      return await response.text();
    } catch {
      return null;
    }
  }

  function getErrorMessage(data, fallback) {
    if (data && typeof data === 'object' && data.detail) {
      if (Array.isArray(data.detail)) {
        return data.detail
          .map((item) => item.msg || JSON.stringify(item))
          .join('\n');
      }

      return String(data.detail);
    }

    if (typeof data === 'string' && data.trim()) {
      return data;
    }

    return fallback;
  }

  function handleAuthFailure(response) {
    if (response.status === 401) {
      window.location.href = '/login?next=/admin';
      return true;
    }

    if (response.status === 403) {
      alert(
        'Acceso restringido: Se requieren permisos de administrador.'
      );

      window.location.href = '/';
      return true;
    }

    return false;
  }

  function setDashboardEmpty(message = 'Sin emisión activa') {
    document.getElementById('dash-media').textContent = message;
    document.getElementById('dash-episode').textContent = '—';
    document.getElementById('dash-progress').textContent =
      '00:00 / 00:00';
    document.getElementById('dash-next').textContent = '—';
  }

  function setChannelsLoading() {
    const tbody = document.getElementById('channels-tbody');

    if (tbody) {
      tbody.innerHTML = `
        <tr>
          <td
            colspan="6"
            style="text-align:center; color:var(--text-muted);"
          >
            Cargando canales...
          </td>
        </tr>
      `;
    }
  }

  // =========================================================
  // AUTENTICACIÓN E INICIALIZACIÓN
  // =========================================================

  async function initAuth() {
    try {
      const res = await fetch('/api/auth/me', {
        credentials: 'same-origin',
      });

      if (!res.ok) {
        window.location.href = '/login?next=/admin';
        return;
      }

      currentUser = await res.json();

      if (currentUser.role !== 'admin') {
        alert(
          'Acceso restringido: Se requieren permisos de administrador.'
        );

        window.location.href = '/';
        return;
      }

      document.getElementById(
        'current-username'
      ).textContent = `${currentUser.username} (${currentUser.role})`;

      // Primero cargamos los canales porque el Dashboard depende de ellos.
      await loadChannels({
        preserveSelection: true,
      });

      await Promise.all([
        loadDashboardStats(),
        loadUsers(),
        loadLibrary(),
        loadOptimizationProfile(),
        loadSystemUpdateStatus(),
      ]);
      await loadGroups();

      // If an optimization survived a page reload/navigation, reconnect the
      // panel to the in-memory job and resume its progress polling.
      await restoreActiveNormalizationJob();
      await restoreActiveOptimizationJob();

      await loadSelectedChannelState();
      connectCatalogEvents();
    } catch (err) {
      console.error(
        'Error inicializando panel administrativo:',
        err
      );

      window.location.href = '/login?next=/admin';
    }
  }

  // =========================================================
  // ACTUALIZACIÓN DEL SISTEMA
  // =========================================================

  const systemUpdateButton = document.getElementById('btn-system-update');
  const systemUpdateLabel = document.getElementById('system-update-label');

  function setSystemUpdateButton({
    label,
    title,
    enabled = false,
    checking = false,
    restarting = false,
  }) {
    if (!systemUpdateButton || !systemUpdateLabel) return;

    systemUpdateLabel.textContent = label;
    systemUpdateButton.title = title || label;
    systemUpdateButton.disabled = !enabled;
    systemUpdateButton.classList.toggle('update-available', enabled);
    systemUpdateButton.classList.toggle('checking', checking);
    systemUpdateButton.classList.toggle('restarting', restarting);
  }

  async function loadSystemUpdateStatus() {
    if (!systemUpdateButton) return;

    setSystemUpdateButton({
      label: 'Comprobando...',
      title: 'Consultando origin/main.',
      checking: true,
    });

    try {
      const res = await fetch('/api/admin/system/update', {
        credentials: 'same-origin',
        cache: 'no-store',
      });
      if (handleAuthFailure(res)) return;

      const data = await readResponseData(res);
      if (!res.ok) {
        throw new Error(getErrorMessage(data, 'No se pudo comprobar la actualización.'));
      }

      if (data.can_update) {
        setSystemUpdateButton({
          label: 'Actualizar sistema',
          title: data.message,
          enabled: true,
        });
        return;
      }

      const labels = {
        up_to_date: 'Sistema actualizado',
        local_ahead: 'Copia local adelantada',
        local_changes: data.update_available ? 'Cambios locales' : 'Sistema actualizado',
        diverged: 'Revisión manual necesaria',
      };
      setSystemUpdateButton({
        label: labels[data.state] || 'Actualización no disponible',
        title: data.message,
      });
    } catch (err) {
      console.error('Error comprobando actualizaciones:', err);
      setSystemUpdateButton({
        label: 'No se pudo comprobar',
        title: err.message || 'No se pudo contactar con origin/main.',
      });
    }
  }

  async function waitForSystemRestart() {
    await new Promise((resolve) => window.setTimeout(resolve, 2500));

    const deadline = Date.now() + 60000;
    while (Date.now() < deadline) {
      try {
        const res = await fetch(`/api/health?restart=${Date.now()}`, {
          cache: 'no-store',
        });
        if (res.ok) {
          window.location.reload();
          return;
        }
      } catch {
        // A connection failure is expected while the process is restarting.
      }
      await new Promise((resolve) => window.setTimeout(resolve, 1000));
    }

    setSystemUpdateButton({
      label: 'Recarga la página',
      title: 'El reinicio está tardando más de lo esperado. Recarga la página manualmente.',
    });
  }

  systemUpdateButton?.addEventListener('click', async () => {
    if (!confirm('¿Actualizar desde origin/main y reiniciar SimpliTV ahora?')) return;

    setSystemUpdateButton({
      label: 'Actualizando...',
      title: 'Aplicando git pull --ff-only origin main.',
      restarting: true,
    });

    try {
      const res = await fetch('/api/admin/system/update', {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
      });
      if (handleAuthFailure(res)) return;

      const data = await readResponseData(res);
      if (!res.ok) {
        throw new Error(getErrorMessage(data, 'No se pudo actualizar el sistema.'));
      }

      setSystemUpdateButton({
        label: 'Reiniciando...',
        title: data.message || 'Actualización aplicada. Reiniciando SimpliTV.',
        restarting: true,
      });
      await waitForSystemRestart();
    } catch (err) {
      alert(err.message || 'No se pudo actualizar el sistema.');
      await loadSystemUpdateStatus();
    }
  });

  // =========================================================
  // NAVEGACIÓN POR PESTAÑAS
  // =========================================================

  document.querySelectorAll('.nav-tab').forEach((tabBtn) => {
    tabBtn.addEventListener('click', async () => {
      document
        .querySelectorAll('.nav-tab')
        .forEach((b) => b.classList.remove('active'));

      document
        .querySelectorAll('.tab-content')
        .forEach((c) => c.classList.remove('active'));

      tabBtn.classList.add('active');

      const targetId = tabBtn.dataset.tab;
      const target = document.getElementById(targetId);

      if (target) {
        target.classList.add('active');
      }

      if (targetId === 'tab-dashboard') {
        await loadDashboardStats();

        if (channelsCache.length === 0) {
          await loadChannels({
            preserveSelection: true,
          });
        }

        await loadSelectedChannelState();
      }

      if (targetId === 'tab-channels') {
        await loadChannels({
          preserveSelection: true,
        });
      }

      if (targetId === 'tab-users') {
        await loadUsers();
      }

      if (targetId === 'tab-groups') {
        await Promise.all([loadUsers(), loadChannels({ preserveSelection: true })]);
        await loadGroups();
      }

      if (targetId === 'tab-library') {
        const searchInput =
          document.getElementById('library-search');

        await loadLibrary(
          searchInput ? searchInput.value.trim() : ''
        );
      }
    });
  });

  // =========================================================
  // 1. DASHBOARD
  // =========================================================

  async function loadDashboardStats() {
    try {
      const res = await fetch('/api/admin/stats', {
        credentials: 'same-origin',
      });

      if (handleAuthFailure(res)) {
        return;
      }

      if (!res.ok) {
        const data = await readResponseData(res);

        console.error(
          'No se pudieron cargar las estadísticas:',
          getErrorMessage(data, `HTTP ${res.status}`)
        );

        return;
      }

      const data = await res.json();

      document.getElementById(
        'stat-total-episodes'
      ).textContent = data.media?.total_episodes ?? 0;

      document.getElementById(
        'stat-unique-series'
      ).textContent = data.media?.unique_series ?? 0;

      document.getElementById(
        'stat-total-hours'
      ).textContent = `${data.media?.total_duration_hours ?? 0}h`;

      document.getElementById(
        'stat-total-users'
      ).textContent = data.users?.total_users ?? 0;

      document.getElementById(
        'stat-active-users'
      ).textContent = data.users?.active_users ?? 0;
    } catch (err) {
      console.error(
        'Error loading dashboard stats:',
        err
      );
    }
  }

  async function loadSelectedChannelState() {
    const selector =
      document.getElementById('admin-dash-channel');

    const channelId = Number(
      selector?.value || currentDashboardChannelId
    );

    if (
      !Number.isInteger(channelId) ||
      channelId <= 0
    ) {
      setDashboardEmpty(
        'No hay canales disponibles'
      );

      document.getElementById(
        'btn-skip-episode'
      ).disabled = true;

      return;
    }

    currentDashboardChannelId = channelId;

    await loadChannelNowPlaying(channelId);
  }

  async function loadChannelNowPlaying(channelId) {
    const skipButton =
      document.getElementById('btn-skip-episode');

    try {
      const res = await fetch(
        `/api/admin/channels/${channelId}/now-playing`,
        {
          credentials: 'same-origin',
          cache: 'no-store',
        }
      );

      if (handleAuthFailure(res)) {
        return;
      }

      if (res.status === 404) {
        const data = await readResponseData(res);

        const message = getErrorMessage(
          data,
          'Canal sin emisión activa'
        );

        setDashboardEmpty(message);

        skipButton.disabled = true;

        return;
      }

      if (!res.ok) {
        const data = await readResponseData(res);

        setDashboardEmpty(
          'No se pudo consultar la emisión'
        );

        skipButton.disabled = true;

        console.error(
          'Error consultando emisión:',
          getErrorMessage(
            data,
            `HTTP ${res.status}`
          )
        );

        return;
      }

      const state = await res.json();
      const ep = state.episode;

      if (!ep) {
        setDashboardEmpty(
          'Canal sin emisión activa'
        );

        skipButton.disabled = true;

        return;
      }

      document.getElementById(
        'dash-media'
      ).textContent = ep.media_type === 'movie'
        ? (ep.franchise || ep.media_title || '—')
        : (ep.media_title || '—');

      document.getElementById(
        'dash-episode'
      ).textContent = ep.media_type === 'movie'
        ? `Película${ep.franchise && ep.media_title ? ` — ${ep.media_title}` : ''}`
        : `${formatEpisodeDescriptor(ep)}${
            ep.episode_title ? ` — ${ep.episode_title}` : ''
          }`;

      document.getElementById(
        'dash-progress'
      ).textContent =
        `${formatTime(state.current_time)} / ` +
        `${formatTime(state.duration)}`;

      if (state.next_episode) {
        const next = state.next_episode;

        document.getElementById(
          'dash-next'
        ).textContent = next.media_type === 'movie'
          ? `${next.franchise ? `${next.franchise} — ` : ''}${next.media_title} (Película)`
          : `${next.media_title} (${formatEpisodeDescriptor(next, true)})${
              next.episode_title ? ` — ${next.episode_title}` : ''
            }`;
      } else {
        document.getElementById(
          'dash-next'
        ).textContent =
          'Final de cola / sin siguiente episodio';
      }

      skipButton.disabled = false;
    } catch (err) {
      console.error(
        'Error cargando estado del canal:',
        err
      );

      setDashboardEmpty(
        'Error de conexión con la emisión'
      );

      skipButton.disabled = true;
    }
  }

  const dashboardChannelSelector =
    document.getElementById(
      'admin-dash-channel'
    );

  if (dashboardChannelSelector) {
    dashboardChannelSelector.addEventListener(
      'change',
      async (e) => {
        currentDashboardChannelId = Number(
          e.target.value
        );

        await loadSelectedChannelState();
      }
    );
  }

  const skipMediaItemButton =
    document.getElementById(
      'btn-skip-episode'
    );

  if (skipMediaItemButton) {
    skipMediaItemButton.addEventListener(
      'click',
      async () => {
        const channelId = Number(
          document.getElementById(
            'admin-dash-channel'
          ).value
        );

        if (
          !Number.isInteger(channelId) ||
          channelId <= 0
        ) {
          alert(
            'Selecciona un canal válido.'
          );

          return;
        }

        if (
          !confirm(
            '¿Estás seguro de que deseas saltar al siguiente episodio inmediatamente?'
          )
        ) {
          return;
        }

        const button =
          document.getElementById(
            'btn-skip-episode'
          );

        const originalText =
          button.textContent;

        let stateReloaded = false;

        button.disabled = true;
        button.textContent = 'Saltando...';

        try {
          const res = await fetch(
            `/api/admin/channels/${channelId}/skip`,
            {
              method: 'POST',
              credentials: 'same-origin',
            }
          );

          if (handleAuthFailure(res)) {
            return;
          }

          const data =
            await readResponseData(res);

          if (!res.ok) {
            alert(
              getErrorMessage(
                data,
                'No se pudo saltar el episodio.'
              )
            );

            return;
          }

          const librarySearch =
            document.getElementById(
              'library-search'
            );

          await Promise.all([
            loadChannelNowPlaying(
              channelId
            ),
            loadDashboardStats(),
            loadLibrary(
              librarySearch
                ? librarySearch.value.trim()
                : ''
            ),
          ]);

          stateReloaded = true;
        } catch (err) {
          console.error(
            'Error al saltar episodio:',
            err
          );

          alert(
            'Error de conexión al intentar saltar el episodio.'
          );
        } finally {
          button.textContent =
            originalText;

          if (
            !stateReloaded &&
            channelsCache.length > 0
          ) {
            button.disabled = false;
          }
        }
      }
    );
  }

  // =========================================================
  // 2. GESTIÓN DE CANALES
  // =========================================================

  async function loadChannels({
    preserveSelection = true,
  } = {}) {
    const selector =
      document.getElementById(
        'admin-dash-channel'
      );

    const previousId =
      preserveSelection
        ? Number(
            selector?.value ||
              currentDashboardChannelId
          )
        : null;

    setChannelsLoading();

    try {
      const res = await fetch(
        '/api/admin/channels',
        {
          credentials: 'same-origin',
          cache: 'no-store',
        }
      );

      if (handleAuthFailure(res)) {
        return [];
      }

      if (!res.ok) {
        const data =
          await readResponseData(res);

        const message =
          getErrorMessage(
            data,
            'No se pudieron cargar los canales.'
          );

        const tbody =
          document.getElementById(
            'channels-tbody'
          );

        if (tbody) {
          tbody.innerHTML = `
            <tr>
              <td
                colspan="6"
                style="text-align:center; color:var(--danger);"
              >
                ${escapeHtml(message)}
              </td>
            </tr>
          `;
        }

        return [];
      }

      const channels =
        await res.json();

      channelsCache =
        Array.isArray(channels)
          ? channels
          : [];

      renderChannelsTable(
        channelsCache
      );

      populateDashboardChannelSelector(
        channelsCache,
        previousId
      );

      return channelsCache;
    } catch (err) {
      console.error(
        'Error loading channels:',
        err
      );

      channelsCache = [];

      const tbody =
        document.getElementById(
          'channels-tbody'
        );

      if (tbody) {
        tbody.innerHTML = `
          <tr>
            <td
              colspan="6"
              style="text-align:center; color:var(--danger);"
            >
              Error de conexión al cargar los canales.
            </td>
          </tr>
        `;
      }

      populateDashboardChannelSelector(
        [],
        null
      );

      return [];
    }
  }

  function populateDashboardChannelSelector(
    channels,
    preferredId
  ) {
    const selector =
      document.getElementById(
        'admin-dash-channel'
      );

    if (!selector) {
      return;
    }

    selector.innerHTML = '';

    if (!channels.length) {
      const option =
        document.createElement('option');

      option.value = '';
      option.textContent = 'Sin canales';

      selector.appendChild(option);

      selector.disabled = true;

      currentDashboardChannelId = null;

      const button =
        document.getElementById(
          'btn-skip-episode'
        );

      if (button) {
        button.disabled = true;
      }

      setDashboardEmpty(
        'No hay canales disponibles'
      );

      return;
    }

    selector.disabled = false;

    channels.forEach((channel) => {
      const option =
        document.createElement('option');

      option.value =
        String(channel.id);

      option.textContent =
        channel.name;

      selector.appendChild(option);
    });

    const preferredExists =
      channels.some(
        (channel) =>
          Number(channel.id) ===
          Number(preferredId)
      );

    const selectedId =
      preferredExists
        ? Number(preferredId)
        : Number(channels[0].id);

    selector.value =
      String(selectedId);

    currentDashboardChannelId =
      selectedId;
  }

  function contentLabel(type) {
    return type === 'movies' ? 'Películas' : 'Series';
  }

  function startModeLabel(mode) {
    if (mode === 'odd') return 'inicio impar';
    if (mode === 'even') return 'inicio par';
    return 'inicio cualquiera';
  }

  function renderChannelsTable(channels) {
    const tbody = document.getElementById('channels-tbody');
    if (!tbody) return;

    tbody.innerHTML = '';

    if (!channels.length) {
      tbody.innerHTML = `
        <tr>
          <td colspan="6" style="text-align:center; color:var(--text-muted);">
            No hay canales registrados.
          </td>
        </tr>`;
      return;
    }

    channels.forEach((channel) => {
      const defaults = (channel.schedule_default || [])
        .map(contentLabel)
        .join(' + ') || '—';
      const slotText = channel.schedule_slots
        ? `${channel.schedule_slots} franja(s)`
        : 'Sin franjas';
      const tr = document.createElement('tr');

      tr.innerHTML = `
        <td>${channel.id}</td>
        <td><strong>${escapeHtml(channel.name)}</strong>${channel.sensitive_content ? ' <span class="badge-tag">Sensible</span>' : ''}</td>
        <td>${escapeHtml(channel.folder_name || channel.name)}</td>
        <td><span class="portable-status">Portable</span></td>
        <td>${escapeHtml(defaults)} <span class="text-muted">• ${escapeHtml(slotText)}</span></td>
        <td>
          <button type="button" class="btn-sm btn-outline btn-config-channel" data-channel-id="${channel.id}">
            ⚙ Administrar
          </button>
        </td>`;

      tbody.appendChild(tr);
    });

    attachChannelActionListeners();
  }

  function attachChannelActionListeners() {
    document.querySelectorAll('.btn-config-channel').forEach((btn) => {
      btn.addEventListener('click', () => {
        const channelId = Number(btn.dataset.channelId);
        if (Number.isInteger(channelId) && channelId > 0) {
          openChannelConfigModal(channelId);
        }
      });
    });
  }

  function setPortableConfigError(elementId, message = '') {
    const box = document.getElementById(elementId);
    if (!box) return;
    box.textContent = message;
    box.classList.toggle('hidden', !message);
  }

  const scheduleWeekdays = [
    ['monday', 'Lun'], ['tuesday', 'Mar'], ['wednesday', 'Mié'], ['thursday', 'Jue'],
    ['friday', 'Vie'], ['saturday', 'Sáb'], ['sunday', 'Dom'],
  ];

  function playbackModeLabel(mode) {
    return mode === 'sequential' ? 'secuencial' : 'aleatorio';
  }

  function checkedProgrammingOptions(items, selectedValues, cssClass, valueKey, labelBuilder) {
    const selected = new Set(Array.isArray(selectedValues) ? selectedValues : []);
    const known = new Set();
    const options = [];

    items.forEach((item) => {
      const value = item[valueKey];
      known.add(value);
      options.push(`
        <label class="schedule-filter-option">
          <input type="checkbox" class="${cssClass}" value="${escapeHtml(value)}" ${selected.has(value) ? 'checked' : ''} />
          <span>${escapeHtml(labelBuilder(item))}</span>
        </label>`);
    });

    selected.forEach((value) => {
      if (known.has(value)) return;
      options.push(`
        <label class="schedule-filter-option schedule-filter-missing">
          <input type="checkbox" class="${cssClass}" value="${escapeHtml(value)}" checked />
          <span>${escapeHtml(value)} <small>(no encontrado actualmente)</small></span>
        </label>`);
    });

    return options.length
      ? options.join('')
      : '<span class="schedule-filter-empty">Sin contenido disponible.</span>';
  }

  function normalizeSlotProgramming(data) {
    if (data?.programming) {
      return {
        series: {
          mode: data.programming.series?.mode || 'all',
          items: Array.isArray(data.programming.series?.items) ? data.programming.series.items : [],
        },
        movies: {
          mode: data.programming.movies?.mode || 'all',
          franchises: Array.isArray(data.programming.movies?.franchises) ? data.programming.movies.franchises : [],
          movies: Array.isArray(data.programming.movies?.movies) ? data.programming.movies.movies : [],
        },
      };
    }

    // Compatibility with channels created before the unified programming model.
    const content = new Set(Array.isArray(data?.content) ? data.content : ['series', 'movies']);
    const seriesInclude = Array.isArray(data?.series_include) ? data.series_include : [];
    const seriesExclude = new Set(Array.isArray(data?.series_exclude) ? data.series_exclude : []);
    let seriesRule;
    if (!content.has('series')) {
      seriesRule = { mode: 'off', items: [] };
    } else if (seriesInclude.length) {
      const effective = seriesInclude.filter((value) => !seriesExclude.has(value));
      seriesRule = effective.length ? { mode: 'only', items: effective } : { mode: 'all', items: [] };
    } else if (seriesExclude.size) {
      seriesRule = { mode: 'except', items: Array.from(seriesExclude) };
    } else {
      seriesRule = { mode: 'all', items: [] };
    }

    const franchiseInclude = Array.isArray(data?.franchise_include) ? data.franchise_include : [];
    const movieInclude = Array.isArray(data?.movie_include) ? data.movie_include : [];
    let movieRule;
    if (!content.has('movies')) {
      movieRule = { mode: 'off', franchises: [], movies: [] };
    } else if (franchiseInclude.length || movieInclude.length) {
      movieRule = { mode: 'only', franchises: franchiseInclude, movies: movieInclude };
    } else {
      movieRule = { mode: 'all', franchises: [], movies: [] };
    }
    return { series: seriesRule, movies: movieRule };
  }

  function scheduleModeOptions(selected) {
    const options = [
      ['off', 'No emitir'],
      ['all', 'Todo'],
      ['only', 'Solo seleccionados'],
      ['except', 'Todo excepto seleccionados'],
    ];
    return options.map(([value, label]) =>
      `<option value="${value}" ${selected === value ? 'selected' : ''}>${label}</option>`
    ).join('');
  }

  function updateScheduleProgrammingVisibility(row) {
    if (!row) return;
    const seriesMode = row.querySelector('.schedule-series-mode')?.value || 'off';
    const moviesMode = row.querySelector('.schedule-movies-mode')?.value || 'off';
    const seriesList = row.querySelector('.schedule-series-selection');
    const moviesList = row.querySelector('.schedule-movies-selection');
    const weights = row.querySelector('.schedule-slot-weights');

    seriesList?.classList.toggle('hidden', !['only', 'except'].includes(seriesMode));
    moviesList?.classList.toggle('hidden', !['only', 'except'].includes(moviesMode));
    weights?.classList.toggle('hidden', seriesMode === 'off' || moviesMode === 'off');
  }

  function createScheduleSlot(slot = null) {
    const container = document.getElementById('schedule-slots-container');
    if (!container) return;

    const data = slot || {
      start: '18:00',
      end: '22:00',
      days: scheduleWeekdays.map(([value]) => value),
      programming: {
        series: { mode: 'all', items: [] },
        movies: { mode: 'all', franchises: [], movies: [] },
      },
      weights: { series: 1, movies: 1 },
    };
    const programming = normalizeSlotProgramming(data);
    const selectedDays = new Set(Array.isArray(data.days) && data.days.length
      ? data.days
      : scheduleWeekdays.map(([value]) => value));
    const series = Array.isArray(activeChannelConfiguration?.series) ? activeChannelConfiguration.series : [];
    const franchises = Array.isArray(activeChannelConfiguration?.franchises) ? activeChannelConfiguration.franchises : [];
    const looseMovies = Array.isArray(activeChannelConfiguration?.loose_movie_items) ? activeChannelConfiguration.loose_movie_items : [];

    const row = document.createElement('div');
    row.className = 'schedule-slot';
    row.innerHTML = `
      <div class="schedule-slot-main">
        <label class="schedule-time-field">
          <span>Desde</span>
          <input class="schedule-start" type="time" required value="${escapeHtml(data.start || '18:00')}" />
        </label>
        <label class="schedule-time-field">
          <span>Hasta</span>
          <input class="schedule-end" type="time" required value="${escapeHtml(data.end || '22:00')}" />
        </label>
        <div class="action-buttons">
          <button type="button" class="btn-sm btn-outline schedule-move-up" title="Mover arriba">↑</button>
          <button type="button" class="btn-sm btn-outline schedule-move-down" title="Mover abajo">↓</button>
          <button type="button" class="btn-sm btn-danger schedule-remove">Eliminar</button>
        </div>
      </div>

      <div class="schedule-slot-details">
        <div class="schedule-days-block">
          <span class="schedule-detail-label">Días</span>
          <div class="schedule-days">
            ${scheduleWeekdays.map(([value, label]) => `
              <label class="schedule-day">
                <input type="checkbox" class="schedule-day-input" value="${value}" ${selectedDays.has(value) ? 'checked' : ''} />
                <span>${label}</span>
              </label>`).join('')}
          </div>
          <small class="text-muted">En una franja que cruza medianoche, los días indican cuándo comienza la franja.</small>
        </div>

        <div class="schedule-programming-panel">
          <div class="schedule-programming-heading">
            <span class="schedule-detail-label">Programación de esta franja</span>
            <small class="text-muted">Cada tipo usa una sola regla. No existen combinaciones contradictorias de incluir y excluir.</small>
          </div>

          <div class="schedule-programming-grid">
            <div class="schedule-program-card">
              <label class="schedule-program-mode">
                <span>Series</span>
                <select class="schedule-series-mode">
                  ${scheduleModeOptions(programming.series.mode)}
                </select>
              </label>
              <div class="schedule-series-selection">
                <small class="text-muted">Marca las series a las que se aplica la regla elegida.</small>
                <div class="schedule-filter-list">
                  ${checkedProgrammingOptions(series, programming.series.items, 'schedule-series-item', 'relative_dir', (item) => item.name)}
                </div>
              </div>
            </div>

            <div class="schedule-program-card">
              <label class="schedule-program-mode">
                <span>Películas</span>
                <select class="schedule-movies-mode">
                  ${scheduleModeOptions(programming.movies.mode)}
                </select>
              </label>
              <div class="schedule-movies-selection">
                <small class="text-muted">La misma regla se aplica a las franquicias y películas sueltas seleccionadas.</small>
                <div class="schedule-filter-columns">
                  <div>
                    <span class="schedule-detail-label">Franquicias</span>
                    <div class="schedule-filter-list">
                      ${checkedProgrammingOptions(franchises, programming.movies.franchises, 'schedule-franchise-item', 'relative_dir', (item) => item.name)}
                    </div>
                  </div>
                  <div>
                    <span class="schedule-detail-label">Películas sueltas</span>
                    <div class="schedule-filter-list">
                      ${checkedProgrammingOptions(looseMovies, programming.movies.movies, 'schedule-movie-item', 'relative_path', (item) => item.name)}
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </div>

          <div class="schedule-weight-grid schedule-slot-weights">
            <label>
              <span>Peso de Series</span>
              <input class="schedule-weight-series" type="number" min="1" max="1000" step="1" value="${Number(data.weights?.series || 1)}" />
            </label>
            <label>
              <span>Peso de Películas</span>
              <input class="schedule-weight-movies" type="number" min="1" max="1000" step="1" value="${Number(data.weights?.movies || 1)}" />
            </label>
            <small class="text-muted schedule-weight-help">Los pesos solo se usan cuando Series y Películas están habilitadas simultáneamente.</small>
          </div>
        </div>
      </div>`;

    container.appendChild(row);
    updateScheduleProgrammingVisibility(row);
    renderScheduleEmptyState();
  }

  function renderScheduleEmptyState() {
    const container = document.getElementById('schedule-slots-container');
    if (!container) return;

    const rows = container.querySelectorAll('.schedule-slot');
    const existingEmpty = container.querySelector('.schedule-empty');
    if (rows.length === 0 && !existingEmpty) {
      const empty = document.createElement('div');
      empty.className = 'schedule-empty';
      empty.textContent = 'Sin franjas: el canal usará la programación predeterminada durante todo el día.';
      container.appendChild(empty);
    } else if (rows.length > 0 && existingEmpty) {
      existingEmpty.remove();
    }
  }

  function renderPortableContentLists(configData) {
    const seriesContainer = document.getElementById('config-series-list');
    const franchiseContainer = document.getElementById('config-franchises-list');
    const looseMoviesContainer = document.getElementById('config-loose-movies-list');
    const looseMoviesText = document.getElementById('config-loose-movies');

    if (seriesContainer) {
      const series = Array.isArray(configData.series) ? configData.series : [];
      seriesContainer.innerHTML = series.length
        ? series.map((item) => {
            const cfg = item.config || {};
            const perAiring = Number(cfg.episodes_per_airing || 1);
            const mode = cfg.start_episode?.mode || 'any';
            const playback = cfg.playback?.mode || 'random';
            const weight = Number(cfg.selection_weight || 1);
            return `
              <div class="portable-config-item">
                <div class="portable-config-summary">
                  <strong>${escapeHtml(item.name)}</strong>
                  <span class="portable-config-meta">${Number(item.episode_count || 0)} episodio(s)</span>
                  <span class="portable-config-rule">${perAiring} por emisión • ${escapeHtml(startModeLabel(mode))} • ${escapeHtml(playbackModeLabel(playback))} • peso ${weight}</span>
                </div>
                <button type="button" class="btn-sm btn-outline btn-config-series" data-relative-dir="${escapeHtml(item.relative_dir)}">Configurar</button>
              </div>`;
          }).join('')
        : '<div class="schedule-empty">Este canal todavía no contiene series.</div>';
    }

    if (franchiseContainer) {
      const franchises = Array.isArray(configData.franchises) ? configData.franchises : [];
      franchiseContainer.innerHTML = franchises.length
        ? franchises.map((item) => {
            const playback = item.config?.playback?.mode || 'random';
            const weight = Number(item.config?.selection_weight || 1);
            return `
              <div class="portable-config-item">
                <div class="portable-config-summary">
                  <strong>${escapeHtml(item.name)}</strong>
                  <span class="portable-config-meta">Carpeta: ${escapeHtml(item.folder_name)} • ${Number(item.movie_count || 0)} película(s)</span>
                  <span class="portable-config-rule">${escapeHtml(playbackModeLabel(playback))} • peso ${weight}</span>
                </div>
                <button type="button" class="btn-sm btn-outline btn-config-franchise" data-relative-dir="${escapeHtml(item.relative_dir)}">Configurar</button>
              </div>`;
          }).join('')
        : '<div class="schedule-empty">Este canal no contiene franquicias de películas.</div>';
    }

    const looseMovies = Array.isArray(configData.loose_movie_items) ? configData.loose_movie_items : [];
    if (looseMoviesContainer) {
      looseMoviesContainer.innerHTML = looseMovies.length
        ? looseMovies.map((item) => `
            <label class="loose-movie-weight-row">
              <span>
                <strong>${escapeHtml(item.name)}</strong>
                <small>${escapeHtml(item.relative_path)}</small>
              </span>
              <span class="loose-movie-weight-control">
                Peso
                <input type="number" class="config-loose-movie-weight" data-relative-path="${escapeHtml(item.relative_path)}" min="1" max="1000" step="1" value="${Number(item.weight || 1)}" />
              </span>
            </label>`).join('')
        : '<div class="schedule-empty">No hay películas sueltas en este canal.</div>';
    }
    if (looseMoviesText) {
      looseMoviesText.textContent = looseMovies.length
        ? 'El peso de cada película suelta se guarda en channel.yaml. Se aplica cuando compite con otras películas o franquicias.'
        : '';
    }

    document.querySelectorAll('.btn-config-series').forEach((btn) => {
      btn.addEventListener('click', () => openSeriesConfigModal(btn.dataset.relativeDir));
    });
    document.querySelectorAll('.btn-config-franchise').forEach((btn) => {
      btn.addEventListener('click', () => openFranchiseConfigModal(btn.dataset.relativeDir));
    });
  }

  function renderChannelConfiguration(configData) {
    activeChannelConfiguration = configData;
    const channelConfig = configData.channel || {};
    const schedule = channelConfig.schedule || {};

    document.getElementById('config-channel-id').value = configData.channel_id;
    document.getElementById('config-channel-name').textContent = channelConfig.name || '—';
    document.getElementById('config-channel-display-name').value = channelConfig.name || '';
    document.getElementById('config-default-series').checked = (schedule.default || []).includes('series');
    document.getElementById('config-default-movies').checked = (schedule.default || []).includes('movies');
    document.getElementById('config-default-weight-series').value = Number(schedule.default_weights?.series || 1);
    document.getElementById('config-default-weight-movies').value = Number(schedule.default_weights?.movies || 1);
    document.getElementById('config-sensitive-content').checked = Boolean(channelConfig.sensitive_content);

    const slotsContainer = document.getElementById('schedule-slots-container');
    if (slotsContainer) slotsContainer.innerHTML = '';
    (schedule.slots || []).forEach((slot) => createScheduleSlot(slot));
    renderScheduleEmptyState();
    renderPortableContentLists(configData);
  }

  async function openChannelConfigModal(channelId) {
    const modal = document.getElementById('modal-config-channel');
    if (!modal) return;

    activeChannelConfiguration = null;
    const channel = channelsCache.find((item) => Number(item.id) === Number(channelId));
    document.getElementById('config-channel-name').textContent = channel?.name || 'Cargando…';
    document.getElementById('config-series-list').innerHTML = '<div class="schedule-empty">Cargando series…</div>';
    document.getElementById('config-franchises-list').innerHTML = '<div class="schedule-empty">Cargando franquicias…</div>';
    document.getElementById('config-loose-movies-list').innerHTML = '<div class="schedule-empty">Cargando películas…</div>';
    document.getElementById('config-loose-movies').textContent = '';
    document.getElementById('schedule-slots-container').innerHTML = '<div class="schedule-empty">Cargando horario…</div>';
    setPortableConfigError('config-channel-error');
    document.getElementById('config-channel-success')?.classList.add('hidden');
    modal.classList.remove('hidden');

    try {
      const res = await fetch(`/api/admin/channels/${channelId}/configuration`, {
        credentials: 'same-origin',
        cache: 'no-store',
      });
      if (handleAuthFailure(res)) return;
      const data = await readResponseData(res);
      if (!res.ok) throw new Error(getErrorMessage(data, 'No se pudo cargar la configuración del canal.'));
      renderChannelConfiguration(data);
    } catch (err) {
      setPortableConfigError('config-channel-error', err.message || 'No se pudo cargar la configuración del canal.');
    }
  }

  const addScheduleSlotButton = document.getElementById('btn-add-schedule-slot');
  if (addScheduleSlotButton) {
    addScheduleSlotButton.addEventListener('click', () => createScheduleSlot());
  }

  const scheduleSlotsContainer = document.getElementById('schedule-slots-container');
  if (scheduleSlotsContainer) {
    scheduleSlotsContainer.addEventListener('click', (e) => {
      const row = e.target.closest('.schedule-slot');
      if (!row) return;
      if (e.target.closest('.schedule-remove')) {
        row.remove();
        renderScheduleEmptyState();
        return;
      }
      if (e.target.closest('.schedule-move-up')) {
        const previous = row.previousElementSibling;
        if (previous?.classList.contains('schedule-slot')) {
          scheduleSlotsContainer.insertBefore(row, previous);
        }
        return;
      }
      if (e.target.closest('.schedule-move-down')) {
        const next = row.nextElementSibling;
        if (next?.classList.contains('schedule-slot')) {
          scheduleSlotsContainer.insertBefore(next, row);
        }
      }
    });
  }

  if (scheduleSlotsContainer) {
    scheduleSlotsContainer.addEventListener('change', (e) => {
      if (!e.target.matches('.schedule-series-mode, .schedule-movies-mode')) return;
      updateScheduleProgrammingVisibility(e.target.closest('.schedule-slot'));
    });
  }

  function checkedValues(row, selector) {
    return Array.from(row.querySelectorAll(`${selector}:checked`)).map((input) => input.value);
  }

  function collectChannelSchedule() {
    const defaultContent = [];
    if (document.getElementById('config-default-series').checked) defaultContent.push('series');
    if (document.getElementById('config-default-movies').checked) defaultContent.push('movies');
    if (!defaultContent.length) {
      throw new Error('La programación predeterminada debe permitir series, películas o ambas.');
    }
    const defaultSeriesWeight = Number(document.getElementById('config-default-weight-series').value);
    const defaultMoviesWeight = Number(document.getElementById('config-default-weight-movies').value);
    if (!Number.isInteger(defaultSeriesWeight) || defaultSeriesWeight < 1 || defaultSeriesWeight > 1000 ||
        !Number.isInteger(defaultMoviesWeight) || defaultMoviesWeight < 1 || defaultMoviesWeight > 1000) {
      throw new Error('Los pesos predeterminados deben ser enteros entre 1 y 1000.');
    }

    const slots = [];
    document.querySelectorAll('#schedule-slots-container .schedule-slot').forEach((row, index) => {
      const start = row.querySelector('.schedule-start')?.value;
      const end = row.querySelector('.schedule-end')?.value;
      const days = checkedValues(row, '.schedule-day-input');
      const seriesMode = row.querySelector('.schedule-series-mode')?.value || 'off';
      const moviesMode = row.querySelector('.schedule-movies-mode')?.value || 'off';
      const seriesItems = checkedValues(row, '.schedule-series-item');
      const franchiseItems = checkedValues(row, '.schedule-franchise-item');
      const movieItems = checkedValues(row, '.schedule-movie-item');
      const seriesWeight = Number(row.querySelector('.schedule-weight-series')?.value || 1);
      const moviesWeight = Number(row.querySelector('.schedule-weight-movies')?.value || 1);

      if (!start || !end) throw new Error(`Completa la hora de inicio y fin de la franja ${index + 1}.`);
      if (!days.length) throw new Error(`La franja ${index + 1} debe aplicarse al menos a un día.`);
      if (seriesMode === 'off' && moviesMode === 'off') {
        throw new Error(`La franja ${index + 1} debe emitir Series, Películas o ambas.`);
      }
      if (['only', 'except'].includes(seriesMode) && !seriesItems.length) {
        throw new Error(`Selecciona al menos una serie para la regla de Series de la franja ${index + 1}.`);
      }
      if (['only', 'except'].includes(moviesMode) && !franchiseItems.length && !movieItems.length) {
        throw new Error(`Selecciona al menos una franquicia o película para la regla de Películas de la franja ${index + 1}.`);
      }
      if (!Number.isInteger(seriesWeight) || seriesWeight < 1 || seriesWeight > 1000 ||
          !Number.isInteger(moviesWeight) || moviesWeight < 1 || moviesWeight > 1000) {
        throw new Error(`Los pesos de la franja ${index + 1} deben ser enteros entre 1 y 1000.`);
      }

      slots.push({
        start,
        end,
        days,
        programming: {
          series: {
            mode: seriesMode,
            items: ['only', 'except'].includes(seriesMode) ? seriesItems : [],
          },
          movies: {
            mode: moviesMode,
            franchises: ['only', 'except'].includes(moviesMode) ? franchiseItems : [],
            movies: ['only', 'except'].includes(moviesMode) ? movieItems : [],
          },
        },
        weights: { series: seriesWeight, movies: moviesWeight },
      });
    });

    return {
      default: defaultContent,
      default_weights: { series: defaultSeriesWeight, movies: defaultMoviesWeight },
      slots,
    };
  }

  function collectLooseMovieWeights() {
    const result = {};
    document.querySelectorAll('.config-loose-movie-weight').forEach((input) => {
      const relativePath = input.dataset.relativePath;
      const weight = Number(input.value);
      if (!relativePath) return;
      if (!Number.isInteger(weight) || weight < 1 || weight > 1000) {
        throw new Error(`El peso de “${relativePath}” debe ser un entero entre 1 y 1000.`);
      }
      result[relativePath] = weight;
    });
    return result;
  }

  const configChannelForm = document.getElementById('form-config-channel');
  if (configChannelForm) {
    configChannelForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const channelId = Number(document.getElementById('config-channel-id').value);
      const name = document.getElementById('config-channel-display-name').value.trim();
      const sensitiveContent = document.getElementById('config-sensitive-content').checked;
      const submitButton = e.submitter;
      setPortableConfigError('config-channel-error');
      document.getElementById('config-channel-success')?.classList.add('hidden');

      if (!Number.isInteger(channelId) || channelId <= 0) {
        setPortableConfigError('config-channel-error', 'ID de canal inválido.');
        return;
      }
      if (!name) {
        setPortableConfigError('config-channel-error', 'El nombre del canal no puede estar vacío.');
        return;
      }

      let schedule;
      let looseMovieWeights;
      try {
        schedule = collectChannelSchedule();
        looseMovieWeights = collectLooseMovieWeights();
      } catch (err) {
        setPortableConfigError('config-channel-error', err.message);
        return;
      }

      if (submitButton) submitButton.disabled = true;
      try {
        const res = await fetch(`/api/admin/channels/${channelId}/configuration`, {
          method: 'PUT',
          credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            version: 1,
            name,
            sensitive_content: sensitiveContent,
            schedule,
            loose_movie_weights: looseMovieWeights,
          }),
        });
        if (handleAuthFailure(res)) return;
        const data = await readResponseData(res);
        if (!res.ok) throw new Error(getErrorMessage(data, 'No se pudo guardar la configuración del canal.'));

        renderChannelConfiguration(data);
        document.getElementById('config-channel-success')?.classList.remove('hidden');
        await loadChannels({ preserveSelection: true });
        await loadSelectedChannelState();
      } catch (err) {
        setPortableConfigError('config-channel-error', err.message || 'No se pudo guardar la configuración del canal.');
      } finally {
        if (submitButton) submitButton.disabled = false;
      }
    });
  }

  function openSeriesConfigModal(relativeDir) {
    if (!activeChannelConfiguration) return;
    const item = (activeChannelConfiguration.series || []).find((entry) => entry.relative_dir === relativeDir);
    if (!item) return;

    const cfg = item.config || {};
    document.getElementById('config-series-channel-id').value = activeChannelConfiguration.channel_id;
    document.getElementById('config-series-relative-dir').value = item.relative_dir;
    document.getElementById('config-series-name').textContent = item.name;
    document.getElementById('config-series-display-name').value = cfg.name || item.name;
    document.getElementById('config-series-meta').textContent = `${Number(item.episode_count || 0)} episodio(s) detectado(s)`;
    document.getElementById('config-series-episodes-per-airing').value = Number(cfg.episodes_per_airing || 1);
    document.getElementById('config-series-start-mode').value = cfg.start_episode?.mode || 'any';
    document.getElementById('config-series-playback-mode').value = cfg.playback?.mode || 'random';
    document.getElementById('config-series-selection-weight').value = Number(cfg.selection_weight || 1);
    setPortableConfigError('config-series-error');
    document.getElementById('modal-config-series').classList.remove('hidden');
  }

  const configSeriesForm = document.getElementById('form-config-series');
  if (configSeriesForm) {
    configSeriesForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const channelId = Number(document.getElementById('config-series-channel-id').value);
      const relativeDir = document.getElementById('config-series-relative-dir').value;
      const displayName = document.getElementById('config-series-display-name').value.trim();
      const episodesPerAiring = Number(document.getElementById('config-series-episodes-per-airing').value);
      const startMode = document.getElementById('config-series-start-mode').value;
      const playbackMode = document.getElementById('config-series-playback-mode').value;
      const selectionWeight = Number(document.getElementById('config-series-selection-weight').value);
      const submitButton = e.submitter;
      setPortableConfigError('config-series-error');

      if (!displayName) {
        setPortableConfigError('config-series-error', 'El nombre visible de la serie es obligatorio.');
        return;
      }

      if (!Number.isInteger(episodesPerAiring) || episodesPerAiring < 1 || episodesPerAiring > 100) {
        setPortableConfigError('config-series-error', 'Episodios por emisión debe estar entre 1 y 100.');
        return;
      }
      if (!['random', 'sequential'].includes(playbackMode)) {
        setPortableConfigError('config-series-error', 'Modo de reproducción inválido.');
        return;
      }
      if (!Number.isInteger(selectionWeight) || selectionWeight < 1 || selectionWeight > 1000) {
        setPortableConfigError('config-series-error', 'El peso debe estar entre 1 y 1000.');
        return;
      }

      if (submitButton) submitButton.disabled = true;
      try {
        const res = await fetch(`/api/admin/channels/${channelId}/series/configuration`, {
          method: 'PUT',
          credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            relative_dir: relativeDir,
            config: {
              version: 1,
              name: displayName,
              episodes_per_airing: episodesPerAiring,
              start_episode: { mode: startMode },
              playback: { mode: playbackMode },
              selection_weight: selectionWeight,
            },
          }),
        });
        if (handleAuthFailure(res)) return;
        const data = await readResponseData(res);
        if (!res.ok) throw new Error(getErrorMessage(data, 'No se pudo guardar la configuración de la serie.'));

        activeChannelConfiguration = data;
        renderPortableContentLists(data);
        document.getElementById('modal-config-series').classList.add('hidden');
        await loadSelectedChannelState();
      } catch (err) {
        setPortableConfigError('config-series-error', err.message || 'No se pudo guardar la configuración de la serie.');
      } finally {
        if (submitButton) submitButton.disabled = false;
      }
    });
  }

  function openFranchiseConfigModal(relativeDir) {
    if (!activeChannelConfiguration) return;
    const item = (activeChannelConfiguration.franchises || []).find((entry) => entry.relative_dir === relativeDir);
    if (!item) return;

    document.getElementById('config-franchise-channel-id').value = activeChannelConfiguration.channel_id;
    document.getElementById('config-franchise-relative-dir').value = item.relative_dir;
    document.getElementById('config-franchise-title-name').textContent = item.name;
    document.getElementById('config-franchise-meta').textContent = `${Number(item.movie_count || 0)} película(s) • carpeta ${item.folder_name}`;
    document.getElementById('config-franchise-name').value = item.config?.name || item.name;
    document.getElementById('config-franchise-playback-mode').value = item.config?.playback?.mode || 'random';
    document.getElementById('config-franchise-selection-weight').value = Number(item.config?.selection_weight || 1);
    setPortableConfigError('config-franchise-error');
    document.getElementById('modal-config-franchise').classList.remove('hidden');
  }

  const configFranchiseForm = document.getElementById('form-config-franchise');
  if (configFranchiseForm) {
    configFranchiseForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const channelId = Number(document.getElementById('config-franchise-channel-id').value);
      const relativeDir = document.getElementById('config-franchise-relative-dir').value;
      const name = document.getElementById('config-franchise-name').value.trim();
      const playbackMode = document.getElementById('config-franchise-playback-mode').value;
      const selectionWeight = Number(document.getElementById('config-franchise-selection-weight').value);
      const submitButton = e.submitter;
      setPortableConfigError('config-franchise-error');

      if (!name) {
        setPortableConfigError('config-franchise-error', 'El nombre de la franquicia no puede estar vacío.');
        return;
      }
      if (!['random', 'sequential'].includes(playbackMode)) {
        setPortableConfigError('config-franchise-error', 'Modo de reproducción inválido.');
        return;
      }
      if (!Number.isInteger(selectionWeight) || selectionWeight < 1 || selectionWeight > 1000) {
        setPortableConfigError('config-franchise-error', 'El peso debe estar entre 1 y 1000.');
        return;
      }

      if (submitButton) submitButton.disabled = true;
      try {
        const res = await fetch(`/api/admin/channels/${channelId}/franchises/configuration`, {
          method: 'PUT',
          credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            relative_dir: relativeDir,
            config: {
              version: 1,
              name,
              playback: { mode: playbackMode },
              selection_weight: selectionWeight,
            },
          }),
        });
        if (handleAuthFailure(res)) return;
        const data = await readResponseData(res);
        if (!res.ok) throw new Error(getErrorMessage(data, 'No se pudo guardar la franquicia.'));

        activeChannelConfiguration = data;
        renderPortableContentLists(data);
        document.getElementById('modal-config-franchise').classList.add('hidden');
        await loadLibrary(document.getElementById('library-search')?.value.trim() || '');
        await loadSelectedChannelState();
      } catch (err) {
        setPortableConfigError('config-franchise-error', err.message || 'No se pudo guardar la franquicia.');
      } finally {
        if (submitButton) submitButton.disabled = false;
      }
    });
  }

  // =========================================================
  // 3. GESTIÓN DE USUARIOS
  // =========================================================

  async function loadUsers() {
    try {
      const res = await fetch(
        '/api/admin/users',
        {
          credentials:
            'same-origin',
        }
      );

      if (handleAuthFailure(res)) {
        return;
      }

      if (!res.ok) {
        return;
      }

      const users =
        await res.json();

      usersCache = Array.isArray(users) ? users : [];

      const tbody =
        document.getElementById(
          'users-tbody'
        );

      if (!tbody) {
        return;
      }

      tbody.innerHTML = '';

      if (!users.length) {
        tbody.innerHTML = `
          <tr>
            <td
              colspan="7"
              style="text-align:center; color:var(--text-muted);"
            >
              No hay usuarios registrados.
            </td>
          </tr>
        `;

        return;
      }

      users.forEach((u) => {
        const tr =
          document.createElement(
            'tr'
          );

        const isSelf =
          currentUser &&
          Number(currentUser.id) ===
            Number(u.id);

        tr.innerHTML = `
          <td>${u.id}</td>

          <td>
            <strong>
              ${escapeHtml(
                u.username
              )}
            </strong>
            ${
              isSelf
                ? ' <span class="badge-self">(Tú)</span>'
                : ''
            }
          </td>

          <td>
            <select
              class="select-role"
              data-user-id="${u.id}"
              ${
                isSelf
                  ? 'disabled'
                  : ''
              }
            >
              <option
                value="user"
                ${
                  u.role === 'user'
                    ? 'selected'
                    : ''
                }
              >
                user
              </option>

              <option
                value="admin"
                ${
                  u.role === 'admin'
                    ? 'selected'
                    : ''
                }
              >
                admin
              </option>
            </select>
          </td>

          <td>
            <span
              class="badge-status ${
                u.is_active
                  ? 'active'
                  : 'inactive'
              }"
            >
              ${
                u.is_active
                  ? 'Activo'
                  : 'Inactivo'
              }
            </span>
          </td>

          <td>
            ${formatDate(
              u.created_at
            )}
          </td>

          <td>
            ${formatDate(
              u.last_login_at
            )}
          </td>

          <td class="action-buttons">

            <button
              class="btn-sm btn-outline btn-toggle-active"
              data-user-id="${u.id}"
              data-active="${u.is_active}"
              ${
                isSelf
                  ? 'disabled'
                  : ''
              }
            >
              ${
                u.is_active
                  ? 'Desactivar'
                  : 'Activar'
              }
            </button>

            <button
              class="btn-sm btn-outline btn-reset-pw"
              data-user-id="${u.id}"
              data-username="${escapeHtml(
                u.username
              )}"
            >
              🔑 Reset Pass
            </button>

            ${
              !isSelf
                ? `
                  <button
                    class="btn-sm btn-danger btn-delete-user"
                    data-user-id="${u.id}"
                    data-username="${escapeHtml(
                      u.username
                    )}"
                  >
                    🗑 Eliminar
                  </button>
                `
                : ''
            }

          </td>
        `;

        tbody.appendChild(tr);
      });

      attachUserActionListeners();
    } catch (err) {
      console.error(
        'Error loading users:',
        err
      );
    }
  }

  function attachUserActionListeners() {
    document
      .querySelectorAll(
        '.select-role'
      )
      .forEach((sel) => {
        sel.addEventListener(
          'change',
          async (e) => {
            const userId =
              e.target.dataset.userId;

            const newRole =
              e.target.value;

            try {
              const res =
                await fetch(
                  `/api/admin/users/${userId}`,
                  {
                    method: 'PATCH',
                    credentials:
                      'same-origin',
                    headers: {
                      'Content-Type':
                        'application/json',
                    },
                    body: JSON.stringify({
                      role: newRole,
                    }),
                  }
                );

              if (
                handleAuthFailure(
                  res
                )
              ) {
                return;
              }

              if (!res.ok) {
                const data =
                  await readResponseData(
                    res
                  );

                alert(
                  getErrorMessage(
                    data,
                    'Error al actualizar el rol.'
                  )
                );

                await loadUsers();
              }
            } catch (err) {
              console.error(
                'Error actualizando rol:',
                err
              );

              alert(
                'Error de conexión al actualizar el rol.'
              );

              await loadUsers();
            }
          }
        );
      });

    document
      .querySelectorAll(
        '.btn-toggle-active'
      )
      .forEach((btn) => {
        btn.addEventListener(
          'click',
          async () => {
            const userId =
              btn.dataset.userId;

            const isCurrentlyActive =
              btn.dataset.active ===
              'true';

            try {
              const res =
                await fetch(
                  `/api/admin/users/${userId}`,
                  {
                    method:
                      'PATCH',
                    credentials:
                      'same-origin',
                    headers: {
                      'Content-Type':
                        'application/json',
                    },
                    body: JSON.stringify({
                      is_active:
                        !isCurrentlyActive,
                    }),
                  }
                );

              if (
                handleAuthFailure(
                  res
                )
              ) {
                return;
              }

              if (res.ok) {
                await loadUsers();
                await loadDashboardStats();
              } else {
                const data =
                  await readResponseData(
                    res
                  );

                alert(
                  getErrorMessage(
                    data,
                    'Error al cambiar el estado del usuario.'
                  )
                );
              }
            } catch (err) {
              console.error(
                'Error cambiando estado del usuario:',
                err
              );

              alert(
                'Error de conexión.'
              );
            }
          }
        );
      });

    document
      .querySelectorAll(
        '.btn-reset-pw'
      )
      .forEach((btn) => {
        btn.addEventListener(
          'click',
          () => {
            document.getElementById(
              'reset-user-id'
            ).value =
              btn.dataset.userId;

            document.getElementById(
              'reset-target-user-label'
            ).textContent =
              `Usuario: ${btn.dataset.username}`;

            document.getElementById(
              'reset-new-password'
            ).value = '';

            document
              .getElementById(
                'reset-password-error'
              )
              .classList.add(
                'hidden'
              );

            document
              .getElementById(
                'modal-reset-password'
              )
              .classList.remove(
                'hidden'
              );
          }
        );
      });

    document
      .querySelectorAll(
        '.btn-delete-user'
      )
      .forEach((btn) => {
        btn.addEventListener(
          'click',
          async () => {
            const userId =
              btn.dataset.userId;

            const username =
              btn.dataset.username;

            if (
              !confirm(
                `¿Estás seguro de que deseas eliminar permanentemente al usuario '${username}'?`
              )
            ) {
              return;
            }

            try {
              const res =
                await fetch(
                  `/api/admin/users/${userId}`,
                  {
                    method:
                      'DELETE',
                    credentials:
                      'same-origin',
                  }
                );

              if (
                handleAuthFailure(
                  res
                )
              ) {
                return;
              }

              if (res.ok) {
                await loadUsers();
                await loadDashboardStats();
              } else {
                const data =
                  await readResponseData(
                    res
                  );

                alert(
                  getErrorMessage(
                    data,
                    'Error al eliminar el usuario.'
                  )
                );
              }
            } catch (err) {
              console.error(
                'Error eliminando usuario:',
                err
              );

              alert(
                'Error de conexión.'
              );
            }
          }
        );
      });
  }

  // =========================================================
  // MODALES DE USUARIO
  // =========================================================

  function updateCreateUserGroupField() {
    const roleSelect = document.getElementById('new-role');
    const groupField = document.getElementById('new-user-group-field');
    const groupSelect = document.getElementById('new-user-group');
    const help = document.getElementById('new-user-group-help');
    if (!roleSelect || !groupField || !groupSelect) return;

    const isViewer = roleSelect.value !== 'admin';
    groupField.classList.toggle('hidden', !isViewer);
    groupSelect.required = isViewer;
    groupSelect.disabled = !isViewer;

    if (isViewer && help) {
      help.textContent = groupsCache.length
        ? 'Los espectadores deben pertenecer a un grupo.'
        : 'No hay grupos disponibles. Crea un grupo de acceso antes de crear un espectador.';
    }
  }

  function populateCreateUserGroups() {
    const groupSelect = document.getElementById('new-user-group');
    if (!groupSelect) return;

    if (!groupsCache.length) {
      groupSelect.innerHTML = '<option value="">No hay grupos disponibles</option>';
    } else {
      groupSelect.innerHTML = [
        '<option value="">Selecciona un grupo...</option>',
        ...groupsCache.map((group) =>
          `<option value="${group.id}">${escapeHtml(group.name)}</option>`
        ),
      ].join('');
    }
    updateCreateUserGroupField();
  }

  const createUserRoleSelect = document.getElementById('new-role');
  if (createUserRoleSelect) {
    createUserRoleSelect.addEventListener('change', updateCreateUserGroupField);
  }

  const createUserButton =
    document.getElementById(
      'btn-open-create-user'
    );

  if (createUserButton) {
    createUserButton.addEventListener(
      'click',
      async () => {
        const form =
          document.getElementById(
            'form-create-user'
          );

        if (form) {
          form.reset();
        }

        await loadGroups();
        populateCreateUserGroups();

        const errorBox =
          document.getElementById(
            'create-user-error'
          );

        if (errorBox) {
          errorBox.classList.add(
            'hidden'
          );
        }

        const modal =
          document.getElementById(
            'modal-create-user'
          );

        if (modal) {
          modal.classList.remove(
            'hidden'
          );
        }
      }
    );
  }

  document
    .querySelectorAll(
      '.modal-close'
    )
    .forEach((btn) => {
      btn.addEventListener(
        'click',
        () => {
          const modal = btn.closest('.modal-backdrop');
          if (modal) {
            modal.classList.add('hidden');
          }
        }
      );
    });

  document
    .querySelectorAll(
      '.modal-backdrop'
    )
    .forEach((modal) => {
      modal.addEventListener(
        'click',
        (e) => {
          if (e.target === modal) {
            modal.classList.add(
              'hidden'
            );
          }
        }
      );
    });

  document.addEventListener(
    'keydown',
    (e) => {
      if (e.key === 'Escape') {
        document
          .querySelectorAll(
            '.modal-backdrop'
          )
          .forEach((modal) =>
            modal.classList.add(
              'hidden'
            )
          );
      }
    }
  );

  const createUserForm =
    document.getElementById(
      'form-create-user'
    );

  if (createUserForm) {
    createUserForm.addEventListener(
      'submit',
      async (e) => {
        e.preventDefault();

        const errBox =
          document.getElementById(
            'create-user-error'
          );

        if (errBox) {
          errBox.classList.add(
            'hidden'
          );
        }

        const username =
          document
            .getElementById(
              'new-username'
            )
            .value.trim();

        const password =
          document.getElementById(
            'new-password'
          ).value;

        const role =
          document.getElementById(
            'new-role'
          ).value;

        const groupValue = document.getElementById('new-user-group')?.value || '';
        const groupId = groupValue ? Number(groupValue) : null;

        if (role !== 'admin' && !groupId) {
          if (errBox) {
            errBox.textContent = groupsCache.length
              ? 'Selecciona un grupo de acceso para el espectador.'
              : 'Debes crear un grupo de acceso antes de crear un espectador.';
            errBox.classList.remove('hidden');
          }
          return;
        }

        try {
          const res =
            await fetch(
              '/api/admin/users',
              {
                method: 'POST',
                credentials:
                  'same-origin',
                headers: {
                  'Content-Type':
                    'application/json',
                },
                body: JSON.stringify({
                  username,
                  password,
                  role,
                  group_id: role === 'admin' ? null : groupId,
                }),
              }
            );

          if (
            handleAuthFailure(res)
          ) {
            return;
          }

          const data =
            await readResponseData(
              res
            );

          if (!res.ok) {
            if (errBox) {
              errBox.textContent =
                getErrorMessage(
                  data,
                  'Error al crear usuario.'
                );

              errBox.classList.remove(
                'hidden'
              );
            }

            return;
          }

          const modal =
            document.getElementById(
              'modal-create-user'
            );

          if (modal) {
            modal.classList.add(
              'hidden'
            );
          }

          await loadUsers();
          await loadGroups();
          await loadDashboardStats();
        } catch (err) {
          console.error(
            'Error creando usuario:',
            err
          );

          if (errBox) {
            errBox.textContent =
              'Error de conexión.';

            errBox.classList.remove(
              'hidden'
            );
          }
        }
      }
    );
  }

  const resetPasswordForm =
    document.getElementById(
      'form-reset-password'
    );

  if (resetPasswordForm) {
    resetPasswordForm.addEventListener(
      'submit',
      async (e) => {
        e.preventDefault();

        const errBox =
          document.getElementById(
            'reset-password-error'
          );

        if (errBox) {
          errBox.classList.add(
            'hidden'
          );
        }

        const userId =
          document.getElementById(
            'reset-user-id'
          ).value;

        const newPassword =
          document.getElementById(
            'reset-new-password'
          ).value;

        try {
          const res =
            await fetch(
              `/api/admin/users/${userId}/reset-password`,
              {
                method: 'POST',
                credentials:
                  'same-origin',
                headers: {
                  'Content-Type':
                    'application/json',
                },
                body: JSON.stringify({
                  new_password:
                    newPassword,
                }),
              }
            );

          if (
            handleAuthFailure(res)
          ) {
            return;
          }

          const data =
            await readResponseData(
              res
            );

          if (!res.ok) {
            if (errBox) {
              errBox.textContent =
                getErrorMessage(
                  data,
                  'Error al restablecer contraseña.'
                );

              errBox.classList.remove(
                'hidden'
              );
            }

            return;
          }

          alert(
            'Contraseña actualizada correctamente.'
          );

          const modal =
            document.getElementById(
              'modal-reset-password'
            );

          if (modal) {
            modal.classList.add(
              'hidden'
            );
          }
        } catch (err) {
          console.error(
            'Error restableciendo contraseña:',
            err
          );

          if (errBox) {
            errBox.textContent =
              'Error de conexión.';

            errBox.classList.remove(
              'hidden'
            );
          }
        }
      }
    );
  }

  // =========================================================
  // 3.5 OPTIMIZACIÓN DE BIBLIOTECA
  // =========================================================

  const btnAnalyzeNormalization = document.getElementById('btn-analyze-normalization');
  const btnNormalizeLibrary = document.getElementById('btn-normalize-library');
  const btnStopNormalization = document.getElementById('btn-stop-normalization');
  let normalizationPollTimer = null;
  let activeNormalizationJobId = null;

  function renderNormalizationAnalysis(analysis) {
    const summary = document.getElementById('normalization-summary');
    if (!summary) return;
    summary.classList.remove('hidden');
    document.getElementById('norm-total').textContent = analysis.total_files ?? 0;
    document.getElementById('norm-ready').textContent = analysis.ready ?? 0;
    document.getElementById('norm-convert').textContent = analysis.convert ?? 0;
    document.getElementById('norm-remux').textContent = analysis.remux ?? 0;
    document.getElementById('norm-transcode').textContent = analysis.transcode ?? 0;
    document.getElementById('norm-errors').textContent = analysis.errors ?? 0;
    document.getElementById('norm-protected').textContent = analysis.protected ?? 0;
    document.getElementById('norm-scan-time').textContent = `${Number(analysis.scan_seconds ?? 0).toFixed(2)} s`;
    if (btnNormalizeLibrary) btnNormalizeLibrary.disabled = !(analysis.convert > 0);
  }

  async function analyzeNormalizationLibrary() {
    if (!btnAnalyzeNormalization) return;
    const oldText = btnAnalyzeNormalization.textContent;
    btnAnalyzeNormalization.disabled = true;
    if (btnNormalizeLibrary) btnNormalizeLibrary.disabled = true;
    btnAnalyzeNormalization.textContent = 'Analizando...';
    try {
      const res = await fetch('/api/admin/library/normalization/analyze', { method: 'POST', credentials: 'same-origin' });
      if (handleAuthFailure(res)) return;
      const data = await readResponseData(res);
      if (!res.ok) throw new Error(getErrorMessage(data, 'No se pudo analizar la compatibilidad MP4.'));
      renderNormalizationAnalysis(data);
    } catch (err) {
      console.error('Error analyzing MP4 normalization:', err);
      alert(err.message || 'No se pudo analizar la compatibilidad MP4.');
    } finally {
      btnAnalyzeNormalization.disabled = false;
      btnAnalyzeNormalization.textContent = oldText;
    }
  }

  function renderNormalizationJob(job) {
    const panel = document.getElementById('normalization-progress');
    if (!panel) return;
    panel.classList.remove('hidden');
    const active = job.status === 'queued' || job.status === 'running';
    panel.dataset.active = active ? 'true' : 'false';
    if (btnAnalyzeNormalization) btnAnalyzeNormalization.disabled = active;
    if (btnNormalizeLibrary) btnNormalizeLibrary.disabled = active;
    activeNormalizationJobId = active ? job.id : null;
    if (btnStopNormalization) {
      btnStopNormalization.classList.toggle('hidden', !active);
      btnStopNormalization.disabled = !active;
    }
    const pct = Math.max(0, Math.min(100, Number(job.progress ?? 0)));
    document.getElementById('norm-job-bar').style.width = `${pct}%`;
    document.getElementById('norm-job-count').textContent = `${job.processed ?? 0} / ${job.total ?? 0}`;
    document.getElementById('norm-job-file').textContent = job.current_file || '—';
    document.getElementById('norm-job-result').textContent = `Remux: ${job.remuxed ?? 0} · Transcodificados: ${job.transcoded ?? 0} · Errores: ${job.errors ?? 0}`;
    const labels = {
      queued: 'En cola', running: `Convirtiendo ${pct.toFixed(0)}%`, completed: 'Conversión completada',
      failed: 'Conversión fallida', cancelled: 'Conversión cancelada'
    };
    document.getElementById('norm-job-status').textContent = labels[job.status] || job.status;
  }

  async function pollNormalizationJob(jobId) {
    if (normalizationPollTimer) clearTimeout(normalizationPollTimer);
    try {
      const res = await fetch(`/api/admin/library/normalization/${jobId}`, { credentials: 'same-origin' });
      if (handleAuthFailure(res)) return;
      const job = await readResponseData(res);
      if (!res.ok) throw new Error(getErrorMessage(job, 'No se pudo consultar la conversión.'));
      renderNormalizationJob(job);
      if (job.status === 'queued' || job.status === 'running') {
        normalizationPollTimer = setTimeout(() => pollNormalizationJob(jobId), 1000);
      } else {
        if (btnAnalyzeNormalization) btnAnalyzeNormalization.disabled = false;
        if (btnNormalizeLibrary) btnNormalizeLibrary.disabled = true;
        await Promise.all([loadLibrary(), loadDashboardStats()]);
      }
    } catch (err) {
      console.error('Error polling MP4 normalization:', err);
      if (btnAnalyzeNormalization) btnAnalyzeNormalization.disabled = false;
    }
  }

  async function restoreActiveNormalizationJob() {
    try {
      const res = await fetch('/api/admin/library/normalization/active', { credentials: 'same-origin' });
      if (handleAuthFailure(res)) return false;
      const job = await readResponseData(res);
      if (!res.ok) throw new Error(getErrorMessage(job, 'No se pudo consultar la conversión activa.'));
      if (!job || !job.id) return false;
      renderNormalizationJob(job);
      pollNormalizationJob(job.id);
      return true;
    } catch (err) {
      console.error('Error restoring MP4 normalization:', err);
      return false;
    }
  }

  async function startNormalizationLibrary() {
    if (!btnNormalizeLibrary) return;
    const proceed = window.confirm(
      'Los archivos compatibles se remuxearán sin pérdida. Los codecs incompatibles se transcodificarán a H.264/AAC.\n\n' +
      'El archivo original solo se elimina después de validar el MP4 resultante. ¿Continuar?'
    );
    if (!proceed) return;
    btnNormalizeLibrary.disabled = true;
    if (btnAnalyzeNormalization) btnAnalyzeNormalization.disabled = true;
    try {
      const res = await fetch('/api/admin/library/normalization', { method: 'POST', credentials: 'same-origin' });
      if (handleAuthFailure(res)) return;
      const job = await readResponseData(res);
      if (res.status === 409) {
        const restored = await restoreActiveNormalizationJob();
        if (restored) return;
      }
      if (!res.ok) throw new Error(getErrorMessage(job, 'No se pudo iniciar la conversión a MP4.'));
      renderNormalizationJob(job);
      pollNormalizationJob(job.id);
    } catch (err) {
      console.error('Error starting MP4 normalization:', err);
      alert(err.message || 'No se pudo iniciar la conversión a MP4.');
      if (btnAnalyzeNormalization) btnAnalyzeNormalization.disabled = false;
      btnNormalizeLibrary.disabled = false;
    }
  }


  async function stopNormalizationLibrary() {
    if (!activeNormalizationJobId || !btnStopNormalization) return;
    if (!window.confirm('¿Detener la conversión a MP4 actual? El archivo que se está procesando conservará su original.')) return;
    btnStopNormalization.disabled = true;
    btnStopNormalization.textContent = 'Deteniendo...';
    try {
      const res = await fetch(`/api/admin/library/normalization/${activeNormalizationJobId}/stop`, {
        method: 'POST', credentials: 'same-origin'
      });
      if (handleAuthFailure(res)) return;
      const job = await readResponseData(res);
      if (!res.ok) throw new Error(getErrorMessage(job, 'No se pudo detener la conversión.'));
      renderNormalizationJob(job);
      await Promise.all([loadLibrary(), loadDashboardStats()]);
    } catch (err) {
      console.error('Error stopping MP4 normalization:', err);
      alert(err.message || 'No se pudo detener la conversión.');
    } finally {
      btnStopNormalization.textContent = '⏹ Detener';
    }
  }

  if (btnAnalyzeNormalization) btnAnalyzeNormalization.addEventListener('click', analyzeNormalizationLibrary);
  if (btnNormalizeLibrary) btnNormalizeLibrary.addEventListener('click', startNormalizationLibrary);
  if (btnStopNormalization) btnStopNormalization.addEventListener('click', stopNormalizationLibrary);

  const btnAnalyzeLibrary = document.getElementById('btn-analyze-library');
  const btnOptimizeLibrary = document.getElementById('btn-optimize-library');
  const btnStopOptimization = document.getElementById('btn-stop-optimization');
  const btnSaveOptimizationProfile = document.getElementById('btn-save-optimization-profile');
  const optProfileResolution = document.getElementById('opt-profile-resolution');
  const optProfileCrf = document.getElementById('opt-profile-crf');
  const optProfileBitrate1080 = document.getElementById('opt-profile-bitrate-1080');
  const optProfileBitrate720 = document.getElementById('opt-profile-bitrate-720');
  const optProfileBitrateSd = document.getElementById('opt-profile-bitrate-sd');
  let optimizationPollTimer = null;
  let activeOptimizationJobId = null;
  let optimizationProfile = null;

  const optimizationResolutionHeights = { '1080p': 1080, '720p': 720, '480p': 480 };

  function updateResolutionWarning() {
    const warning = document.getElementById('opt-resolution-warning');
    if (!warning || !optProfileResolution) return;
    warning.classList.toggle('hidden', optProfileResolution.value === '1080p');
  }

  function setOptimizationProfileControlsDisabled(disabled) {
    [
      optProfileResolution, optProfileCrf, optProfileBitrate1080,
      optProfileBitrate720, optProfileBitrateSd, btnSaveOptimizationProfile
    ].forEach((el) => { if (el) el.disabled = disabled; });
  }

  function renderOptimizationProfile(profile) {
    optimizationProfile = profile;
    if (optProfileResolution) optProfileResolution.value = profile.resolution || '1080p';
    if (optProfileCrf) {
      const value = String(profile.crf ?? 24);
      if (![...optProfileCrf.options].some((o) => o.value === value)) {
        const option = document.createElement('option');
        option.value = value;
        option.textContent = `Personalizada — CRF ${value}`;
        optProfileCrf.appendChild(option);
      }
      optProfileCrf.value = value;
    }
    if (optProfileBitrate1080) optProfileBitrate1080.value = profile.bitrate_1080_mbps ?? 2.5;
    if (optProfileBitrate720) optProfileBitrate720.value = profile.bitrate_720_mbps ?? 1.5;
    if (optProfileBitrateSd) optProfileBitrateSd.value = profile.bitrate_sd_mbps ?? 0.9;
    updateResolutionWarning();
  }

  async function loadOptimizationProfile() {
    try {
      const res = await fetch('/api/admin/library/optimization/profile', { credentials: 'same-origin' });
      if (handleAuthFailure(res)) return;
      const profile = await readResponseData(res);
      if (!res.ok) throw new Error(getErrorMessage(profile, 'No se pudo cargar el perfil de optimización.'));
      renderOptimizationProfile(profile);
    } catch (err) {
      console.error('Error loading optimization profile:', err);
      const statusEl = document.getElementById('opt-profile-status');
      if (statusEl) statusEl.textContent = err.message || 'No se pudo cargar la configuración.';
    }
  }

  async function saveOptimizationProfile() {
    if (!btnSaveOptimizationProfile || !optProfileResolution || !optProfileCrf) return;

    const resolution = optProfileResolution.value;
    const crf = Number(optProfileCrf.value);
    const bitrate1080 = Number(optProfileBitrate1080?.value);
    const bitrate720 = Number(optProfileBitrate720?.value);
    const bitrateSd = Number(optProfileBitrateSd?.value);
    const statusEl = document.getElementById('opt-profile-status');

    if (![bitrate1080, bitrate720, bitrateSd].every((v) => Number.isFinite(v) && v > 0)) {
      if (statusEl) statusEl.textContent = 'Los bitrates deben ser números mayores que 0.';
      return;
    }

    const oldHeight = optimizationResolutionHeights[optimizationProfile?.resolution] || 1080;
    const newHeight = optimizationResolutionHeights[resolution] || 1080;
    let confirmResolutionLoss = false;

    if (newHeight < oldHeight) {
      confirmResolutionLoss = window.confirm(
        `Vas a reducir la resolución máxima de ${optimizationProfile?.resolution || '1080p'} a ${resolution}.\n\n` +
        'Los vídeos que superen ese límite podrán ser reemplazados por versiones de menor resolución. ' +
        'Esta pérdida NO se puede revertir aumentando después la configuración.\n\n' +
        '¿Guardar esta configuración?'
      );
      if (!confirmResolutionLoss) return;
    }

    const oldText = btnSaveOptimizationProfile.textContent;
    setOptimizationProfileControlsDisabled(true);
    btnSaveOptimizationProfile.textContent = 'Guardando...';
    if (statusEl) statusEl.textContent = '';

    try {
      const res = await fetch('/api/admin/library/optimization/profile', {
        method: 'PUT',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          resolution,
          crf,
          bitrate_1080_mbps: bitrate1080,
          bitrate_720_mbps: bitrate720,
          bitrate_sd_mbps: bitrateSd,
          confirm_resolution_loss: confirmResolutionLoss,
        }),
      });
      if (handleAuthFailure(res)) return;
      const profile = await readResponseData(res);
      if (!res.ok) throw new Error(getErrorMessage(profile, 'No se pudo guardar el perfil.'));

      renderOptimizationProfile(profile);
      const summary = document.getElementById('optimization-summary');
      if (summary) summary.classList.add('hidden');
      if (btnOptimizeLibrary) btnOptimizeLibrary.disabled = true;
      if (statusEl) statusEl.textContent = 'Configuración guardada. Vuelve a analizar la biblioteca para aplicar estas reglas.';
    } catch (err) {
      console.error('Error saving optimization profile:', err);
      if (statusEl) statusEl.textContent = err.message || 'No se pudo guardar la configuración.';
    } finally {
      const active = document.getElementById('optimization-progress')?.dataset.active === 'true';
      setOptimizationProfileControlsDisabled(active);
      btnSaveOptimizationProfile.textContent = oldText;
    }
  }

  function renderOptimizationAnalysis(analysis) {
    const summary = document.getElementById('optimization-summary');
    if (!summary) return;
    summary.classList.remove('hidden');
    document.getElementById('opt-total').textContent = analysis.total_files ?? 0;
    document.getElementById('opt-ok').textContent = analysis.ok ?? 0;
    document.getElementById('opt-candidates').textContent = analysis.optimize ?? 0;
    document.getElementById('opt-skipped').textContent =
      (analysis.not_worth ?? 0) + (analysis.protected ?? 0);
    document.getElementById('opt-errors').textContent = analysis.errors ?? 0;
    document.getElementById('opt-current-size').textContent = formatBytes(analysis.total_size ?? 0);
    document.getElementById('opt-estimated-size').textContent = formatBytes(analysis.estimated_size ?? 0);
    document.getElementById('opt-savings').textContent = formatBytes(analysis.estimated_savings ?? 0);
    document.getElementById('opt-scan-time').textContent = `${Number(analysis.scan_seconds ?? 0).toFixed(2)} s`;
    if (btnOptimizeLibrary) btnOptimizeLibrary.disabled = !(analysis.optimize > 0);
  }

  async function analyzeOptimizationLibrary() {
    if (!btnAnalyzeLibrary) return;
    const oldText = btnAnalyzeLibrary.textContent;
    btnAnalyzeLibrary.disabled = true;
    if (btnOptimizeLibrary) btnOptimizeLibrary.disabled = true;
    btnAnalyzeLibrary.textContent = 'Analizando...';
    try {
      const res = await fetch('/api/admin/library/optimization/analyze', {
        method: 'POST', credentials: 'same-origin'
      });
      if (handleAuthFailure(res)) return;
      const data = await readResponseData(res);
      if (!res.ok) throw new Error(getErrorMessage(data, 'No se pudo analizar la biblioteca.'));
      renderOptimizationAnalysis(data);
    } catch (err) {
      console.error('Error analyzing library optimization:', err);
      alert(err.message || 'No se pudo analizar la biblioteca.');
    } finally {
      btnAnalyzeLibrary.disabled = false;
      btnAnalyzeLibrary.textContent = oldText;
    }
  }

  function renderOptimizationJob(job) {
    const panel = document.getElementById('optimization-progress');
    if (!panel) return;
    panel.classList.remove('hidden');

    const isActive = job.status === 'queued' || job.status === 'running';
    panel.dataset.active = isActive ? 'true' : 'false';
    if (btnAnalyzeLibrary) btnAnalyzeLibrary.disabled = isActive;
    if (btnOptimizeLibrary) btnOptimizeLibrary.disabled = isActive;
    setOptimizationProfileControlsDisabled(isActive);
    activeOptimizationJobId = isActive ? job.id : null;
    if (btnStopOptimization) {
      btnStopOptimization.classList.toggle('hidden', !isActive);
      btnStopOptimization.disabled = !isActive;
    }

    const pct = Math.max(0, Math.min(100, Number(job.progress ?? 0)));
    document.getElementById('opt-job-bar').style.width = `${pct}%`;
    document.getElementById('opt-job-count').textContent = `${job.processed ?? 0} / ${job.total ?? 0}`;
    document.getElementById('opt-job-file').textContent = job.current_file || '—';
    document.getElementById('opt-job-saved').textContent = `Ahorrado: ${formatBytes(job.bytes_saved ?? 0)}`;
    const labels = {
      queued: 'En cola', running: `Optimizando ${pct.toFixed(0)}%`, completed: 'Optimización completada',
      failed: 'Optimización fallida', cancelled: 'Optimización cancelada'
    };
    document.getElementById('opt-job-status').textContent = labels[job.status] || job.status;
  }

  async function pollOptimizationJob(jobId) {
    if (optimizationPollTimer) clearTimeout(optimizationPollTimer);
    try {
      const res = await fetch(`/api/admin/library/optimization/${jobId}`, { credentials: 'same-origin' });
      if (handleAuthFailure(res)) return;
      const job = await readResponseData(res);
      if (!res.ok) throw new Error(getErrorMessage(job, 'No se pudo consultar el trabajo.'));
      renderOptimizationJob(job);
      if (job.status === 'queued' || job.status === 'running') {
        optimizationPollTimer = setTimeout(() => pollOptimizationJob(jobId), 1000);
      } else {
        if (btnAnalyzeLibrary) btnAnalyzeLibrary.disabled = false;
        if (btnOptimizeLibrary) btnOptimizeLibrary.disabled = true;
        setOptimizationProfileControlsDisabled(false);
        await Promise.all([loadLibrary(), loadDashboardStats(), loadOptimizationProfile()]);
      }
    } catch (err) {
      console.error('Error polling optimization:', err);
      if (btnAnalyzeLibrary) btnAnalyzeLibrary.disabled = false;
    }
  }

  async function restoreActiveOptimizationJob() {
    try {
      const res = await fetch('/api/admin/library/optimization/active', {
        credentials: 'same-origin'
      });
      if (handleAuthFailure(res)) return false;

      const job = await readResponseData(res);
      if (!res.ok) {
        throw new Error(getErrorMessage(job, 'No se pudo consultar la optimización activa.'));
      }

      if (!job || !job.id) return false;

      renderOptimizationJob(job);
      pollOptimizationJob(job.id);
      return true;
    } catch (err) {
      console.error('Error restoring active optimization:', err);
      return false;
    }
  }

  async function startOptimizationLibrary() {
    if (!btnOptimizeLibrary) return;

    if (optimizationProfile?.resolution && optimizationProfile.resolution !== '1080p') {
      const proceed = window.confirm(
        `El perfil actual limita la resolución a ${optimizationProfile.resolution}.\n\n` +
        'Los vídeos de mayor resolución que sean candidatos serán reemplazados por versiones reducidas. ' +
        'Subir la configuración a 1080p más adelante NO restaurará el detalle perdido.\n\n' +
        '¿Iniciar la optimización?'
      );
      if (!proceed) return;
    }

    btnOptimizeLibrary.disabled = true;
    if (btnAnalyzeLibrary) btnAnalyzeLibrary.disabled = true;
    try {
      const res = await fetch('/api/admin/library/optimization', {
        method: 'POST', credentials: 'same-origin'
      });
      if (handleAuthFailure(res)) return;
      const job = await readResponseData(res);

      if (res.status === 409) {
        const restored = await restoreActiveOptimizationJob();
        if (restored) return;
      }

      if (!res.ok) throw new Error(getErrorMessage(job, 'No se pudo iniciar la optimización.'));
      renderOptimizationJob(job);
      pollOptimizationJob(job.id);
    } catch (err) {
      console.error('Error starting optimization:', err);
      alert(err.message || 'No se pudo iniciar la optimización.');
      if (btnAnalyzeLibrary) btnAnalyzeLibrary.disabled = false;
      btnOptimizeLibrary.disabled = false;
    }
  }


  async function stopOptimizationLibrary() {
    if (!activeOptimizationJobId || !btnStopOptimization) return;
    if (!window.confirm('¿Detener la optimización actual? El archivo que se está procesando conservará su versión original.')) return;
    btnStopOptimization.disabled = true;
    btnStopOptimization.textContent = 'Deteniendo...';
    try {
      const res = await fetch(`/api/admin/library/optimization/${activeOptimizationJobId}/stop`, {
        method: 'POST', credentials: 'same-origin'
      });
      if (handleAuthFailure(res)) return;
      const job = await readResponseData(res);
      if (!res.ok) throw new Error(getErrorMessage(job, 'No se pudo detener la optimización.'));
      renderOptimizationJob(job);
      setOptimizationProfileControlsDisabled(false);
      await Promise.all([loadLibrary(), loadDashboardStats(), loadOptimizationProfile()]);
    } catch (err) {
      console.error('Error stopping optimization:', err);
      alert(err.message || 'No se pudo detener la optimización.');
    } finally {
      btnStopOptimization.textContent = '⏹ Detener';
    }
  }

  if (btnAnalyzeLibrary) btnAnalyzeLibrary.addEventListener('click', analyzeOptimizationLibrary);
  if (btnOptimizeLibrary) btnOptimizeLibrary.addEventListener('click', startOptimizationLibrary);
  if (btnStopOptimization) btnStopOptimization.addEventListener('click', stopOptimizationLibrary);
  if (btnSaveOptimizationProfile) btnSaveOptimizationProfile.addEventListener('click', saveOptimizationProfile);
  if (optProfileResolution) optProfileResolution.addEventListener('change', updateResolutionWarning);

  // =========================================================
  // 4. BIBLIOTECA MULTIMEDIA
  // =========================================================

  const librarySearch =
    document.getElementById(
      'library-search'
    );

  if (librarySearch) {
    librarySearch.addEventListener(
      'input',
      (e) => {
        clearTimeout(
          searchDebounceTimer
        );

        searchDebounceTimer =
          setTimeout(() => {
            loadLibrary(
              e.target.value.trim()
            );
          }, 300);
      }
    );
  }

  let libraryItemsCache = [];
  let libraryViewMode = 'tree';

  function libraryChannelName(item) {
    if (item.channel_name) return item.channel_name;
    const channel = channelsCache.find((entry) => Number(entry.id) === Number(item.channel_id));
    return channel?.name || `Canal ${item.channel_id ?? '—'}`;
  }

  function libraryFormat(item) {
    const mimeType = item.mime_type || 'desconocido';
    return mimeType.includes('/') ? mimeType.split('/')[1] : mimeType;
  }

  function libraryMediaItemLabel(item) {
    if (item.media_type === 'movie') return 'Película';
    const hasSeason = item.season_number !== null
      && item.season_number !== undefined
      && Number(item.season_number) > 0;
    return hasSeason
      ? `T${Number(item.season_number)} · E${item.episode_number}`
      : `E${item.episode_number}`;
  }

  function libraryMetaLine(item) {
    return [
      formatTime(item.duration),
      libraryFormat(item).toUpperCase(),
      formatBytes(item.file_size),
      `${item.play_count ?? 0} reproducciones`,
    ].join(' · ');
  }

  function groupLibrary(items) {
    const channels = new Map();

    items.forEach((item) => {
      const channelName = libraryChannelName(item);
      if (!channels.has(channelName)) {
        channels.set(channelName, {
          name: channelName,
          series: new Map(),
          franchises: new Map(),
          standaloneMovies: [],
          count: 0,
          duration: 0,
          size: 0,
        });
      }

      const channel = channels.get(channelName);
      channel.count += 1;
      channel.duration += Number(item.duration) || 0;
      channel.size += Number(item.file_size) || 0;

      if (item.media_type === 'movie') {
        if (item.franchise) {
          if (!channel.franchises.has(item.franchise)) {
            channel.franchises.set(item.franchise, []);
          }
          channel.franchises.get(item.franchise).push(item);
        } else {
          channel.standaloneMovies.push(item);
        }
        return;
      }

      const seriesName = item.media_title || 'Serie sin nombre';
      if (!channel.series.has(seriesName)) {
        channel.series.set(seriesName, new Map());
      }
      const season = Number(item.season_number) || 0;
      if (!channel.series.get(seriesName).has(season)) {
        channel.series.get(seriesName).set(season, []);
      }
      channel.series.get(seriesName).get(season).push(item);
    });

    return Array.from(channels.values()).sort((a, b) =>
      a.name.localeCompare(b.name, undefined, { numeric: true, sensitivity: 'base' })
    );
  }

  function libraryLeaf(item) {
    const title = item.media_type === 'movie'
      ? (item.media_title || item.episode_title || 'Película')
      : (item.episode_title || `Episodio ${item.episode_number}`);

    return `
      <div class="library-leaf">
        <div class="library-leaf-main">
          <span class="library-leaf-icon">${item.media_type === 'movie' ? '🎬' : '▶'}</span>
          <div class="library-leaf-copy">
            <div class="library-leaf-title">
              ${item.media_type === 'movie' ? '' : `<span class="library-episode-code">${escapeHtml(libraryMediaItemLabel(item))}</span>`}
              <span>${escapeHtml(title)}</span>
            </div>
            <div class="library-leaf-meta">${escapeHtml(libraryMetaLine(item))}</div>
          </div>
        </div>
        <div class="library-leaf-last">
          <span class="text-muted">Última emisión</span>
          <span>${escapeHtml(formatDate(item.last_played_at))}</span>
        </div>
      </div>
    `;
  }

  function renderLibraryTree(items) {
    const container = document.getElementById('library-content');
    if (!container) return;

    const channels = groupLibrary(items);
    if (!channels.length) {
      container.innerHTML = '<div class="library-empty">No se encontró contenido en la biblioteca.</div>';
      return;
    }

    container.innerHTML = channels.map((channel, channelIndex) => {
      const seriesBlocks = Array.from(channel.series.entries())
        .sort(([a], [b]) => a.localeCompare(b, undefined, { numeric: true, sensitivity: 'base' }))
        .map(([seriesName, seasons]) => {
          const episodeCount = Array.from(seasons.values()).reduce((sum, eps) => sum + eps.length, 0);
          const seasonCount = Array.from(seasons.keys()).filter((season) => season > 0).length;
          const seasonBlocks = Array.from(seasons.entries())
            .sort(([a], [b]) => a - b)
            .map(([season, episodes]) => {
              const leaves = episodes
                .slice()
                .sort((a, b) => Number(a.episode_number) - Number(b.episode_number))
                .map(libraryLeaf)
                .join('');
              if (season === 0) {
                return `<div class="library-node-body">${leaves}</div>`;
              }
              return `
                <details class="library-node library-season-node">
                  <summary>
                    <span class="library-node-title"><span class="library-node-icon">📁</span>Temporada ${season}</span>
                    <span class="library-node-count">${episodes.length} episodio${episodes.length === 1 ? '' : 's'}</span>
                  </summary>
                  <div class="library-node-body">${leaves}</div>
                </details>
              `;
            }).join('');

          const seriesMeta = seasonCount > 0
            ? `${seasonCount} temp. · ${episodeCount} ep.`
            : `${episodeCount} ep.`;

          return `
            <details class="library-node library-series-node">
              <summary>
                <span class="library-node-title"><span class="library-node-icon">📺</span>${escapeHtml(seriesName)}</span>
                <span class="library-node-count">${seriesMeta}</span>
              </summary>
              <div class="library-node-body">${seasonBlocks}</div>
            </details>
          `;
        }).join('');

      const franchiseBlocks = Array.from(channel.franchises.entries())
        .sort(([a], [b]) => a.localeCompare(b, undefined, { numeric: true, sensitivity: 'base' }))
        .map(([franchise, movies]) => `
          <details class="library-node library-franchise-node">
            <summary>
              <span class="library-node-title"><span class="library-node-icon">🎞️</span>${escapeHtml(franchise)}</span>
              <span class="library-node-count">${movies.length} película${movies.length === 1 ? '' : 's'}</span>
            </summary>
            <div class="library-node-body">
              ${movies
                .slice()
                .sort((a, b) => String(a.media_title || '').localeCompare(String(b.media_title || ''), undefined, { numeric: true, sensitivity: 'base' }))
                .map(libraryLeaf)
                .join('')}
            </div>
          </details>
        `).join('');

      const standaloneMovies = channel.standaloneMovies.length
        ? `
          <details class="library-node library-franchise-node">
            <summary>
              <span class="library-node-title"><span class="library-node-icon">🎬</span>Películas sueltas</span>
              <span class="library-node-count">${channel.standaloneMovies.length}</span>
            </summary>
            <div class="library-node-body">
              ${channel.standaloneMovies
                .slice()
                .sort((a, b) => String(a.media_title || '').localeCompare(String(b.media_title || ''), undefined, { numeric: true, sensitivity: 'base' }))
                .map(libraryLeaf)
                .join('')}
            </div>
          </details>
        `
        : '';

      const seriesCount = channel.series.size;
      const movieCount = Array.from(channel.franchises.values()).reduce((sum, movies) => sum + movies.length, 0) + channel.standaloneMovies.length;

      return `
        <details class="library-channel" ${channels.length === 1 || channelIndex === 0 ? 'open' : ''}>
          <summary class="library-channel-summary">
            <div class="library-channel-heading">
              <span class="library-channel-icon">📡</span>
              <div>
                <strong>${escapeHtml(channel.name)}</strong>
                <span>${seriesCount} series · ${movieCount} películas · ${channel.count} archivos</span>
              </div>
            </div>
            <div class="library-channel-meta">
              <span>${formatTime(channel.duration)} totales</span>
              <span>${formatBytes(channel.size)}</span>
            </div>
          </summary>
          <div class="library-channel-body">
            ${seriesBlocks ? `
              <section class="library-type-section">
                <div class="library-type-heading"><span>📺 Series</span><span>${seriesCount}</span></div>
                ${seriesBlocks}
              </section>
            ` : ''}
            ${(franchiseBlocks || standaloneMovies) ? `
              <section class="library-type-section">
                <div class="library-type-heading"><span>🎬 Películas</span><span>${movieCount}</span></div>
                ${franchiseBlocks}${standaloneMovies}
              </section>
            ` : ''}
          </div>
        </details>
      `;
    }).join('');
  }

  function renderLibraryFiles(items) {
    const container = document.getElementById('library-content');
    if (!container) return;

    if (!items.length) {
      container.innerHTML = '<div class="library-empty">No se encontró contenido en la biblioteca.</div>';
      return;
    }

    container.innerHTML = `
      <div class="library-file-list">
        ${items.map((item) => `
          <div class="library-file-row">
            <div class="library-file-kind">${item.media_type === 'movie' ? '🎬' : '▶'}</div>
            <div class="library-file-copy">
              <strong>${escapeHtml(item.media_title || item.episode_title || 'Sin título')}</strong>
              <span>
                ${escapeHtml(libraryChannelName(item))}
                ${item.media_type === 'movie'
                  ? (item.franchise ? ` · ${escapeHtml(item.franchise)}` : ' · Película')
                  : ` · ${escapeHtml(libraryMediaItemLabel(item))}${item.episode_title ? ` · ${escapeHtml(item.episode_title)}` : ''}`}
              </span>
            </div>
            <div class="library-file-meta">${escapeHtml(libraryMetaLine(item))}</div>
          </div>
        `).join('')}
      </div>
    `;
  }

  function renderLibrarySummary(items) {
    const summary = document.getElementById('library-summary');
    if (!summary) return;

    const series = new Set();
    const franchises = new Set();
    let movies = 0;
    let episodes = 0;
    let duration = 0;
    let size = 0;

    items.forEach((item) => {
      duration += Number(item.duration) || 0;
      size += Number(item.file_size) || 0;
      if (item.media_type === 'movie') {
        movies += 1;
        if (item.franchise) franchises.add(`${libraryChannelName(item)}::${item.franchise}`);
      } else {
        episodes += 1;
        series.add(`${libraryChannelName(item)}::${item.media_title}`);
      }
    });

    summary.innerHTML = `
      <div class="library-summary-card"><strong>${items.length}</strong><span>Archivos</span></div>
      <div class="library-summary-card"><strong>${series.size}</strong><span>Series</span></div>
      <div class="library-summary-card"><strong>${episodes}</strong><span>Episodios</span></div>
      <div class="library-summary-card"><strong>${movies}</strong><span>Películas</span></div>
      <div class="library-summary-card"><strong>${franchises.size}</strong><span>Franquicias</span></div>
      <div class="library-summary-card library-summary-wide"><strong>${formatTime(duration)}</strong><span>Duración total</span></div>
      <div class="library-summary-card library-summary-wide"><strong>${formatBytes(size)}</strong><span>Almacenamiento</span></div>
    `;
  }

  function renderLibrary(items = libraryItemsCache) {
    renderLibrarySummary(items);
    if (libraryViewMode === 'files') renderLibraryFiles(items);
    else renderLibraryTree(items);

    document.getElementById('library-view-tree')?.classList.toggle('active', libraryViewMode === 'tree');
    document.getElementById('library-view-files')?.classList.toggle('active', libraryViewMode === 'files');
  }

  async function loadLibrary(query = '') {
    try {
      const url = query
        ? `/api/admin/library?q=${encodeURIComponent(query)}`
        : '/api/admin/library';

      const res = await fetch(url, { credentials: 'same-origin' });
      if (handleAuthFailure(res)) return;
      if (!res.ok) return;

      libraryItemsCache = await res.json();
      renderLibrary(libraryItemsCache);
    } catch (err) {
      console.error('Error loading library:', err);
      const container = document.getElementById('library-content');
      if (container) container.innerHTML = '<div class="library-empty">No se pudo cargar la biblioteca.</div>';
    }
  }

  document.getElementById('library-view-tree')?.addEventListener('click', () => {
    libraryViewMode = 'tree';
    renderLibrary();
  });

  document.getElementById('library-view-files')?.addEventListener('click', () => {
    libraryViewMode = 'files';
    renderLibrary();
  });


  // =========================================================
  // GRUPOS DE ACCESO
  // =========================================================

  async function loadGroups() {
    const tbody = document.getElementById('groups-tbody');
    if (!tbody) return [];

    try {
      const res = await fetch('/api/admin/groups', { credentials: 'same-origin' });
      if (handleAuthFailure(res)) return [];
      const data = await readResponseData(res);
      if (!res.ok) throw new Error(getErrorMessage(data, `HTTP ${res.status}`));

      groupsCache = Array.isArray(data) ? data : [];
      tbody.innerHTML = '';

      if (!groupsCache.length) {
        tbody.innerHTML = '<tr><td colspan="5" style="text-align:center; color:var(--text-muted);">No hay grupos. Debes crear al menos uno antes de crear espectadores.</td></tr>';
        return groupsCache;
      }

      const usersById = new Map(usersCache.map((u) => [Number(u.id), u.username]));
      const channelsById = new Map(channelsCache.map((c) => [Number(c.id), c.name]));

      groupsCache.forEach((group) => {
        const userNames = (group.user_ids || []).map((id) => usersById.get(Number(id)) || `#${id}`);
        const channelNames = (group.channel_ids || []).map((id) => channelsById.get(Number(id)) || `#${id}`);
        const tr = document.createElement('tr');
        tr.innerHTML = `
          <td>${group.id}</td>
          <td><strong>${escapeHtml(group.name)}</strong></td>
          <td>${userNames.length ? userNames.map(escapeHtml).join(', ') : '<span class="text-muted">Sin usuarios</span>'}</td>
          <td>${channelNames.length ? channelNames.map(escapeHtml).join(', ') : '<span class="text-muted">Ninguno</span>'}</td>
          <td>
            <div class="action-buttons">
              <button type="button" class="btn-sm btn-outline btn-edit-group" data-group-id="${group.id}">Editar</button>
              <button type="button" class="btn-sm btn-danger btn-delete-group" data-group-id="${group.id}" data-group-name="${escapeHtml(group.name)}">Eliminar</button>
            </div>
          </td>`;
        tbody.appendChild(tr);
      });

      tbody.querySelectorAll('.btn-edit-group').forEach((btn) => {
        btn.addEventListener('click', () => {
          const group = groupsCache.find((g) => Number(g.id) === Number(btn.dataset.groupId));
          if (group) openGroupModal(group);
        });
      });

      tbody.querySelectorAll('.btn-delete-group').forEach((btn) => {
        btn.addEventListener('click', async () => {
          if (!confirm(`¿Eliminar el grupo '${btn.dataset.groupName}'? No se permitirá si deja a algún espectador sin grupo.`)) return;
          try {
            const res = await fetch(`/api/admin/groups/${btn.dataset.groupId}`, { method: 'DELETE', credentials: 'same-origin' });
            if (handleAuthFailure(res)) return;
            const data = await readResponseData(res);
            if (!res.ok) throw new Error(getErrorMessage(data, 'No se pudo eliminar el grupo.'));
            await loadGroups();
          } catch (err) {
            alert(err.message || 'No se pudo eliminar el grupo.');
          }
        });
      });

      return groupsCache;
    } catch (err) {
      console.error('Error loading access groups:', err);
      tbody.innerHTML = '<tr><td colspan="5" style="text-align:center; color:var(--danger-color);">Error cargando grupos.</td></tr>';
      return [];
    }
  }

  function renderAccessChecks(containerId, items, selectedIds, kind) {
    const container = document.getElementById(containerId);
    if (!container) return;
    const selected = new Set((selectedIds || []).map(Number));

    if (!items.length) {
      container.innerHTML = '<span class="text-muted">No hay elementos disponibles.</span>';
      return;
    }

    container.innerHTML = items.map((item) => {
      const label = kind === 'user'
        ? `${item.username}${item.role === 'admin' ? ' (admin)' : ''}`
        : item.name;
      return `<label class="access-check-item">
        <input type="checkbox" value="${item.id}" ${selected.has(Number(item.id)) ? 'checked' : ''} />
        <span>${escapeHtml(label)}</span>
      </label>`;
    }).join('');
  }

  function openGroupModal(group = null) {
    const modal = document.getElementById('modal-access-group');
    const idInput = document.getElementById('access-group-id');
    const nameInput = document.getElementById('access-group-name');
    const title = document.getElementById('access-group-title');
    const errorBox = document.getElementById('access-group-error');
    if (!modal || !idInput || !nameInput) return;

    idInput.value = group ? group.id : '';
    nameInput.value = group ? group.name : '';
    if (title) title.textContent = group ? `Editar Grupo: ${group.name}` : 'Crear Grupo de Acceso';
    if (errorBox) errorBox.classList.add('hidden');

    renderAccessChecks('access-group-users', usersCache, group?.user_ids || [], 'user');
    renderAccessChecks('access-group-channels', channelsCache, group?.channel_ids || [], 'channel');
    modal.classList.remove('hidden');
  }

  const openCreateGroupButton = document.getElementById('btn-open-create-group');
  if (openCreateGroupButton) {
    openCreateGroupButton.addEventListener('click', async () => {
      await Promise.all([loadUsers(), loadChannels({ preserveSelection: true })]);
      openGroupModal();
    });
  }

  const accessGroupForm = document.getElementById('form-access-group');
  if (accessGroupForm) {
    accessGroupForm.addEventListener('submit', async (e) => {
      e.preventDefault();
      const id = document.getElementById('access-group-id').value;
      const name = document.getElementById('access-group-name').value.trim();
      const errorBox = document.getElementById('access-group-error');
      if (errorBox) errorBox.classList.add('hidden');

      const userIds = Array.from(document.querySelectorAll('#access-group-users input:checked')).map((el) => Number(el.value));
      const channelIds = Array.from(document.querySelectorAll('#access-group-channels input:checked')).map((el) => Number(el.value));

      try {
        const res = await fetch(id ? `/api/admin/groups/${id}` : '/api/admin/groups', {
          method: id ? 'PATCH' : 'POST',
          credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name, user_ids: userIds, channel_ids: channelIds }),
        });
        if (handleAuthFailure(res)) return;
        const data = await readResponseData(res);
        if (!res.ok) throw new Error(getErrorMessage(data, 'No se pudo guardar el grupo.'));

        document.getElementById('modal-access-group').classList.add('hidden');
        await loadGroups();
      } catch (err) {
        if (errorBox) {
          errorBox.textContent = err.message || 'No se pudo guardar el grupo.';
          errorBox.classList.remove('hidden');
        }
      }
    });
  }

  async function refreshAdminCatalog() {
    if (catalogRefreshInFlight) {
      catalogRefreshQueued = true;
      return;
    }

    catalogRefreshInFlight = true;
    try {
      do {
        catalogRefreshQueued = false;
        const configuredChannelId = Number(activeChannelConfiguration?.channel_id);
        await loadChannels({ preserveSelection: true });

        if (configuredChannelId && !channelsCache.some(
          (channel) => Number(channel.id) === configuredChannelId
        )) {
          activeChannelConfiguration = null;
          ['modal-config-channel', 'modal-config-series', 'modal-config-franchise']
            .forEach((id) => document.getElementById(id)?.classList.add('hidden'));
        }

        const search = document.getElementById('library-search')?.value.trim() || '';
        await Promise.all([
          loadDashboardStats(),
          loadLibrary(search),
          loadGroups(),
        ]);
        await loadSelectedChannelState();
      } while (catalogRefreshQueued);
    } finally {
      catalogRefreshInFlight = false;
    }
  }

  function connectCatalogEvents() {
    if (catalogEventSource) catalogEventSource.close();
    if (typeof EventSource === 'undefined') return;

    catalogEventSource = new EventSource('/api/channels/catalog-events');
    catalogEventSource.addEventListener('catalog-update', refreshAdminCatalog);
    catalogEventSource.onerror = () => {
      // EventSource reconnects automatically and receives the latest revision.
      console.warn('Flujo del catálogo desconectado; esperando reconexión.');
    };
  }

  // =========================================================
  // CERRAR SESIÓN
  // =========================================================

  const logoutButton =
    document.getElementById(
      'btn-admin-logout'
    );

  if (logoutButton) {
    logoutButton.addEventListener(
      'click',
      async () => {
        try {
          if (catalogEventSource) catalogEventSource.close();
          await fetch(
            '/api/auth/logout',
            {
              method: 'POST',
              credentials:
                'same-origin',
            }
          );
        } finally {
          window.location.href =
            '/login';
        }
      }
    );
  }

  // =========================================================
  // BOOT
  // =========================================================

  initAuth();

  // Keep the indicator current when the administration panel remains open.
  window.setInterval(() => {
    if (!systemUpdateButton?.classList.contains('restarting')) {
      loadSystemUpdateStatus();
    }
  }, 5 * 60 * 1000);
})();
