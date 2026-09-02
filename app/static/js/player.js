(function () {
  'use strict';

  // DOM Elements
  const video = document.getElementById('tv-player');
  const osdOverlay = document.getElementById('osd-overlay');
  const mediaTitle = document.getElementById('media-title');
  const seasonMediaItem = document.getElementById('season-episode');
  const episodeTitle = document.getElementById('episode-title');
  const nextBox = document.getElementById('next-box');
  const nextMediaTitle = document.getElementById('next-media-title');
  const nextMediaItemDetail = document.getElementById('next-episode-detail');
  const progressBar = document.getElementById('progress-bar');
  const timeCurrent = document.getElementById('time-current');
  const timeDuration = document.getElementById('time-duration');
  const timeRemaining = document.getElementById('time-remaining');
  const btnFullscreen = document.getElementById('btn-fullscreen');
  const btnSync = document.getElementById('btn-sync');
  const btnMute = document.getElementById('btn-mute');
  const btnMuteIcon = document.getElementById('btn-mute-icon');
  const volumeSlider = document.getElementById('volume-slider');
  const btnChannelPrev = document.getElementById('btn-channel-prev');
  const btnChannelNext = document.getElementById('btn-channel-next');
  const btnAdminLink = document.getElementById('btn-admin-link');
  const channelSelector = document.getElementById('channel-selector');
  const channelNameDisplay = document.getElementById('channel-name-display');
  const btnAccount = document.getElementById('btn-account');
  const viewerSettings = document.getElementById('viewer-settings');
  const btnCloseSettings = document.getElementById('btn-close-settings');
  const formProfileSettings = document.getElementById('form-profile-settings');
  const settingsUsername = document.getElementById('settings-username');
  const settingsCurrentPassword = document.getElementById('settings-current-password');
  const settingsNewPassword = document.getElementById('settings-new-password');
  const settingsConfirmPassword = document.getElementById('settings-confirm-password');
  const profileSettingsMessage = document.getElementById('profile-settings-message');
  const formUnlockChannelSettings = document.getElementById('form-unlock-channel-settings');
  const settingsChannelPassword = document.getElementById('settings-channel-password');
  const channelSettingsUnlockMessage = document.getElementById('channel-settings-unlock-message');
  const channelSettingsContent = document.getElementById('channel-settings-content');
  const blockedChannelsList = document.getElementById('blocked-channels-list');
  const channelSettingsMessage = document.getElementById('channel-settings-message');
  const btnSensitiveToggle = document.getElementById('btn-sensitive-toggle');
  const settingsLogout = document.getElementById('settings-logout');
  const unmuteBanner = document.getElementById('unmute-banner');
  const unmuteBtn = document.getElementById('unmute-btn');
  const emptyState = document.getElementById('empty-state');
  const emptyStateTitle = document.getElementById('empty-state-title');
  const emptyStateMessage = document.getElementById('empty-state-message');
  const emptyStateSubtitle = document.getElementById('empty-state-subtitle');
  const actionFeedback = document.getElementById('action-feedback');
  const actionFeedbackIcon = document.getElementById('action-feedback-icon');
  const actionFeedbackLabel = document.getElementById('action-feedback-label');

  // State
  let currentUser = null;
  let currentChannelId = null;
  let currentMediaItemId = null;
  let serverTotalDuration = 0;
  let clientFetchTimestamp = null;
  let initialServerOffset = 0;
  let nextMediaItemTimer = null;
  let periodicSyncTimer = null;
  let emptyStatePollTimer = null;
  let osdTimer = null;
  let isFetchingState = false;
  let syncRequestedWhileFetching = false;
  let channelEventSource = null;
  let accessEventSource = null;
  let catalogEventSource = null;
  let accessRefreshInFlight = false;
  let accessRefreshQueued = false;
  let actionFeedbackTimer = null;
  let preferencesPassword = null;
  let viewerPreferences = null;

  const VOLUME_STEP = 0.05;

  function preferenceKey(name) {
    if (!currentUser || currentUser.id == null) return null;
    return `simpliTV.player.${currentUser.id}.${name}`;
  }

  function readPreference(name) {
    const key = preferenceKey(name);
    if (!key) return null;
    try {
      return localStorage.getItem(key);
    } catch {
      return null;
    }
  }

  function writePreference(name, value) {
    const key = preferenceKey(name);
    if (!key) return;
    try {
      localStorage.setItem(key, String(value));
    } catch {
      // Playback must keep working even when storage is unavailable.
    }
  }

  function restoreAudioPreferences() {
    const savedVolume = Number.parseFloat(readPreference('volume'));
    if (Number.isFinite(savedVolume)) {
      video.volume = Math.min(1, Math.max(0, savedVolume));
    }

    const savedMuted = readPreference('muted');
    if (savedMuted === 'true' || savedMuted === 'false') {
      video.muted = savedMuted === 'true';
    }

    if (volumeSlider) {
      volumeSlider.value = String(Math.round(video.volume * 100));
    }
    updateMuteButton();
  }

  // Format seconds to mm:ss or hh:mm:ss
  function formatTime(seconds) {
    if (isNaN(seconds) || seconds < 0) return '00:00';
    const totalSecs = Math.floor(seconds);
    const hrs = Math.floor(totalSecs / 3600);
    const mins = Math.floor((totalSecs % 3600) / 60);
    const secs = totalSecs % 60;
    const pad = (n) => String(n).padStart(2, '0');

    if (hrs > 0) {
      return `${hrs}:${pad(mins)}:${pad(secs)}`;
    }
    return `${pad(mins)}:${pad(secs)}`;
  }

  function setEmptyStateMode(mode) {
    if (mode === 'no-channels') {
      if (emptyStateTitle) emptyStateTitle.textContent = 'Sin canales disponibles';
      if (emptyStateMessage) emptyStateMessage.textContent = 'No hay canales visibles para tu usuario en este momento.';
      if (emptyStateSubtitle) emptyStateSubtitle.textContent = 'Puedes revisar tus preferencias de canales desde el botón de cuenta.';
      return;
    }
    if (emptyStateTitle) emptyStateTitle.textContent = 'SimpliTV';
    if (emptyStateMessage) emptyStateMessage.textContent = 'No se encontraron episodios disponibles para este canal.';
    if (emptyStateSubtitle) emptyStateSubtitle.textContent = 'El canal se actualizará automáticamente cuando haya contenido disponible.';
  }

  function formatMediaForOsd(item) {
    if (item?.media_type === 'movie') {
      return {
        main: item.franchise || item.media_title || 'Película',
        detail: 'Película',
        subtitle: item.franchise && item.media_title
          ? `— ${item.media_title}`
          : ''
      };
    }

    return {
      main: item?.media_title || '—',
      detail: item?.season_number == null || Number(item.season_number) <= 0
        ? `Episodio ${item?.episode_number ?? 0}`
        : `T${item.season_number} • E${item?.episode_number ?? 0}`,
      subtitle: item?.episode_title ? `— ${item.episode_title}` : ''
    };
  }

  // Calculate current expected server position based on wall-clock time
  function getExpectedServerOffset() {
    if (!clientFetchTimestamp) return 0;
    const elapsedSinceFetch = (Date.now() - clientFetchTimestamp) / 1000;
    const expected = initialServerOffset + elapsedSinceFetch;
    return Math.min(serverTotalDuration, Math.max(0, expected));
  }

  // OSD Inactivity auto-hide
  function showOSD() {
    osdOverlay.classList.remove('hidden');
    document.body.style.cursor = 'default';
    clearTimeout(osdTimer);
    osdTimer = setTimeout(() => {
      if (!video.paused) {
        osdOverlay.classList.add('hidden');
        document.body.style.cursor = 'none';
      }
    }, 3500);
  }

  const FEEDBACK_ICONS = {
    muted: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M3 9v6h4l5 5V4L7 9H3zm13.59 3L19 14.41 20.41 13 18 10.59 20.41 8.17 19 6.76l-2.41 2.41-2.42-2.41-1.41 1.41L15.17 10.59 12.76 13l1.41 1.41z"/></svg>',
    volume: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M3 9v6h4l5 5V4L7 9H3zm13.5 3c0-1.77-1.02-3.29-2.5-4.03v8.05c1.48-.73 2.5-2.25 2.5-4.02zM14 3.23v2.06c2.89.86 5 3.54 5 6.71s-2.11 5.85-5 6.71v2.06c4.01-.91 7-4.49 7-8.77s-2.99-7.86-7-8.77z"/></svg>',
    previous: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M15.41 7.41 14 6l-6 6 6 6 1.41-1.41L10.83 12z"/></svg>',
    next: '<svg viewBox="0 0 24 24" fill="currentColor"><path d="M8.59 16.59 10 18l6-6-6-6-1.41 1.41L13.17 12z"/></svg>'
  };

  function showActionFeedback(icon, label) {
    if (!actionFeedback || !actionFeedbackIcon || !actionFeedbackLabel) return;

    clearTimeout(actionFeedbackTimer);
    actionFeedbackIcon.innerHTML = FEEDBACK_ICONS[icon] || '';
    actionFeedbackLabel.textContent = label || '';

    // Restart the transition even if actions happen in quick succession.
    actionFeedback.classList.remove('visible');
    void actionFeedback.offsetWidth;
    actionFeedback.classList.add('visible');

    actionFeedbackTimer = setTimeout(() => {
      actionFeedback.classList.remove('visible');
    }, 1000);
  }

  function updateMuteButton() {
    if (!btnMute || !btnMuteIcon) return;

    btnMute.setAttribute('aria-label', video.muted ? 'Activar audio' : 'Silenciar audio');
    btnMute.title = video.muted ? 'Activar audio (M)' : 'Silenciar audio (M)';
    btnMuteIcon.innerHTML = video.muted
      ? '<path d="M3 9v6h4l5 5V4L7 9H3zm13.59 3L19 14.41 20.41 13 18 10.59 20.41 8.17 19 6.76l-2.41 2.41-2.42-2.41-1.41 1.41L15.17 10.59 12.76 13l1.41 1.41z"/>'
      : '<path d="M3 9v6h4l5 5V4L7 9H3zm13.5 3c0-1.77-1.02-3.29-2.5-4.03v8.05c1.48-.73 2.5-2.25 2.5-4.02zM14 3.23v2.06c2.89.86 5 3.54 5 6.71s-2.11 5.85-5 6.71v2.06c4.01-.91 7-4.49 7-8.77s-2.99-7.86-7-8.77z"/>';
  }

  function setMuted(muted, showFeedback = true, persist = true) {
    video.muted = muted;
    if (persist) {
      writePreference('muted', muted);
    }
    if (!muted) {
      unmuteBanner.classList.add('hidden');
    }
    updateMuteButton();

    if (showFeedback) {
      showActionFeedback(muted ? 'muted' : 'volume', muted ? 'Silenciado' : 'Audio activado');
    }
  }

  function toggleMute() {
    setMuted(!video.muted, true);
  }

  function setVolume(value, showFeedback = true) {
    const normalized = Math.min(1, Math.max(0, value));
    video.volume = normalized;
    writePreference('volume', normalized.toFixed(2));

    if (volumeSlider) {
      volumeSlider.value = String(Math.round(normalized * 100));
    }

    if (showFeedback) {
      showActionFeedback('volume', `Volumen ${Math.round(normalized * 100)}%`);
    }
  }

  function changeVolume(direction) {
    setVolume(video.volume + direction * VOLUME_STEP, true);
  }

  function changeChannel(direction) {
    if (!channelSelector || channelSelector.options.length === 0) return;

    const total = channelSelector.options.length;
    const currentIndex = channelSelector.selectedIndex >= 0 ? channelSelector.selectedIndex : 0;
    const targetIndex = (currentIndex + direction + total) % total;

    channelSelector.selectedIndex = targetIndex;
    const option = channelSelector.options[targetIndex];
    showActionFeedback(direction < 0 ? 'previous' : 'next', option ? option.textContent : 'Canal');
    channelSelector.dispatchEvent(new Event('change'));
  }

  // Check Auth & Profile
  async function checkAuth() {
    try {
      const res = await fetch('/api/auth/me');
      if (res.status === 401 || !res.ok) {
        window.location.href = '/login';
        return false;
      }
      currentUser = await res.json();
      if (currentUser.role === 'admin' && btnAdminLink) {
        btnAdminLink.classList.remove('hidden');
      }
      return true;
    } catch {
      window.location.href = '/login';
      return false;
    }
  }

  async function loadChannels() {
    try {
      const res = await fetch('/api/channels', { cache: 'no-store' });
      if (res.status === 401) {
        window.location.href = '/login';
        return [];
      }
      if (!res.ok) {
        throw new Error(`HTTP ${res.status}`);
      }

      const channels = await res.json();
      channelSelector.innerHTML = '';

      if (!channels.length) {
        currentChannelId = null;
        channelSelector.style.display = 'none';
        channelNameDisplay.style.display = 'inline';
        channelNameDisplay.textContent = 'SIN CANALES DISPONIBLES';
        setEmptyStateMode('no-channels');
        return [];
      }

      channels.forEach(ch => {
        const opt = document.createElement('option');
        opt.value = ch.id;
        opt.textContent = ch.name;
        channelSelector.appendChild(opt);
      });

      const currentChannel = channels.find(ch => Number(ch.id) === Number(currentChannelId));
      const savedChannelId = readPreference('channelId');
      const savedChannel = channels.find(ch => String(ch.id) === savedChannelId);
      const selectedChannel = currentChannel || savedChannel || channels[0];
      currentChannelId = Number(selectedChannel.id);
      channelSelector.value = String(currentChannelId);
      writePreference('channelId', currentChannelId);

      channelSelector.style.display = 'inline-block';
      channelNameDisplay.style.display = 'none';
      return channels;
    } catch (err) {
      console.error('Error loading channels', err);
      return [];
    }
  }

  async function refreshPlayerChannels(showInterface = true) {
    const previousChannelId = currentChannelId == null ? null : Number(currentChannelId);
    const channels = await loadChannels();

    if (!channels.length || currentChannelId == null) {
      if (channelEventSource) {
        channelEventSource.close();
        channelEventSource = null;
      }
      currentMediaItemId = null;
      video.pause();
      video.removeAttribute('src');
      video.load();
      emptyState.classList.remove('hidden');
      osdOverlay.classList.remove('hidden');
      return;
    }

    const channelChanged = previousChannelId !== Number(currentChannelId);
    if (channelChanged) {
      currentMediaItemId = null;
      video.pause();
      video.removeAttribute('src');
      video.load();
    }
    connectChannelEvents();
    await syncWithChannel(channelChanged, showInterface);
  }

  if (channelSelector) {
      channelSelector.addEventListener('change', (e) => {
        currentChannelId = Number(e.target.value);
        writePreference('channelId', currentChannelId);
        currentMediaItemId = null;
        video.pause();
        video.src = '';
        connectChannelEvents();
        syncWithChannel(true);
      });
  }

  // Fetch Channel State and Synchronize
  async function syncWithChannel(forceSeek = false, showInterface = false) {
    if (!currentChannelId) return;
    if (isFetchingState) {
      syncRequestedWhileFetching = true;
      return;
    }
    isFetchingState = true;
    const requestedChannelId = Number(currentChannelId);

    try {
      const response = await fetch(`/api/channels/${requestedChannelId}/now-playing`, { cache: 'no-store' });

      // The selected channel may have changed while this request was in flight
      // (especially after a realtime access revocation). Never let a stale
      // response restore playback for a channel the account has just lost.
      if (requestedChannelId !== Number(currentChannelId)) {
        return;
      }

      if (response.status === 401) {
        window.location.href = '/login';
        return;
      }

      if (response.status === 403) {
        // Preferences may have changed in another tab/session. Rebuild the
        // selector from the server-authoritative visible channel list instead
        // of leaving a stale forbidden channel selected.
        isFetchingState = false;
        await refreshPlayerChannels();
        return;
      }

      if (response.status === 404) {
        currentMediaItemId = null;
        video.pause();
        video.removeAttribute('src');
        video.load();
        setEmptyStateMode('no-signal');
        emptyState.classList.remove('hidden');
        osdOverlay.classList.remove('hidden');
        mediaTitle.textContent = '—';
        seasonMediaItem.textContent = '—';
        episodeTitle.textContent = '';
        nextBox.classList.add('hidden');
        isFetchingState = false;

        // Auto poll until watcher detects files
        clearTimeout(emptyStatePollTimer);
        emptyStatePollTimer = setTimeout(() => {
          syncWithChannel(true);
        }, 3500);
        return;
      }

      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`);
      }

      clearTimeout(emptyStatePollTimer);
      const state = await response.json();
      if (requestedChannelId !== Number(currentChannelId)) {
        return;
      }
      emptyState.classList.add('hidden');

      const ep = state.episode;
      const episodeChanged = currentMediaItemId !== ep.id;
      currentMediaItemId = ep.id;
      serverTotalDuration = state.duration;
      initialServerOffset = state.current_time;
      clientFetchTimestamp = Date.now();

      // Update OSD info
      const currentDisplay = formatMediaForOsd(ep);
      mediaTitle.textContent = currentDisplay.main;
      seasonMediaItem.textContent = currentDisplay.detail;
      episodeTitle.textContent = currentDisplay.subtitle;

      // Update Next MediaItem info
      if (state.next_episode) {
        const nextDisplay = formatMediaForOsd(state.next_episode);
        nextMediaTitle.textContent = nextDisplay.main;
        nextMediaItemDetail.textContent = `${nextDisplay.detail}${
          nextDisplay.subtitle ? ` ${nextDisplay.subtitle}` : ''
        }`;
        nextBox.classList.remove('hidden');
      } else {
        nextBox.classList.add('hidden');
      }

      // Schedule next episode transition when remaining time expires
      clearTimeout(nextMediaItemTimer);
      const remainingMs = Math.max(1000, state.remaining_time * 1000 + 400);
      nextMediaItemTimer = setTimeout(() => {
        syncWithChannel(true);
      }, remainingMs);

      // Handle video source loading or seeking
      if (episodeChanged) {
        video.src = ep.stream_url;
        video.load();

        const onLoaded = async () => {
          video.removeEventListener('loadedmetadata', onLoaded);
          const target = getExpectedServerOffset();
          if (target > 0) {
            video.currentTime = target;
          }
          try {
            await video.play();
          } catch (err) {
            console.warn('Autoplay unmuted blocked. Falling back to muted autoplay.', err);
            setMuted(true, false, false);
            await video.play();
            unmuteBanner.classList.remove('hidden');
          }
        };
        video.addEventListener('loadedmetadata', onLoaded);
      } else if (forceSeek) {
        const target = getExpectedServerOffset();
        applyDriftCorrection(target, true);
      }

      if (showInterface) {
        showOSD();
      }
    } catch (err) {
      console.error('Channel synchronization error:', err);
    } finally {
      isFetchingState = false;
      if (syncRequestedWhileFetching) {
        syncRequestedWhileFetching = false;
        // Run after the current promise unwinds so an event received during a
        // fetch cannot be lost. Administrative skips therefore always win.
        setTimeout(() => syncWithChannel(true), 0);
      }
    }
  }

  // Listen for server-side channel transitions (including administrator skips).
  function connectChannelEvents() {
    if (channelEventSource) {
      channelEventSource.close();
      channelEventSource = null;
    }

    if (typeof EventSource === 'undefined' || !currentChannelId) {
      return;
    }

    const source = new EventSource(`/api/channels/${currentChannelId}/events`);
    channelEventSource = source;

    source.addEventListener('channel-update', () => {
      // Do not trust event payloads as playback state. Re-fetch the canonical
      // state and force a seek/source change immediately.
      syncWithChannel(true);
    });

    source.onerror = () => {
      // EventSource reconnects automatically. The regular health sync below is
      // retained as a fallback in case a proxy/server does not support SSE.
      console.warn('Channel event stream disconnected; waiting for reconnect.');
    };
  }

  async function refreshUnlockedViewerPreferences() {
    if (!preferencesPassword || !channelSettingsContent || channelSettingsContent.classList.contains('hidden')) {
      return;
    }

    try {
      const response = await fetch('/api/auth/preferences', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ current_password: preferencesPassword }),
        cache: 'no-store',
      });
      const data = await readJsonResponse(response);
      if (response.status === 401) {
        lockChannelPreferences();
        setSettingsMessage(channelSettingsUnlockMessage, 'Vuelve a verificar tu contraseña.');
        return;
      }
      if (response.ok) {
        renderViewerPreferences(data);
      }
    } catch (err) {
      console.warn('Could not refresh unlocked viewer preferences:', err);
    }
  }

  async function handleRealtimeAccessUpdate() {
    // Coalesce bursts (for example an admin changing membership and grants in the
    // same save) so the player never runs overlapping selector/state rebuilds.
    if (accessRefreshInFlight) {
      accessRefreshQueued = true;
      return;
    }

    accessRefreshInFlight = true;
    try {
      do {
        accessRefreshQueued = false;
        await refreshPlayerChannels(false);
        await refreshUnlockedViewerPreferences();
      } while (accessRefreshQueued);
    } finally {
      accessRefreshInFlight = false;
    }
  }

  function connectAccessEvents() {
    if (accessEventSource) {
      accessEventSource.close();
      accessEventSource = null;
    }

    if (typeof EventSource === 'undefined') return;

    const source = new EventSource('/api/auth/access-events');
    accessEventSource = source;

    source.addEventListener('access-update', () => {
      handleRealtimeAccessUpdate();
    });

    source.addEventListener('session-invalid', () => {
      source.close();
      if (channelEventSource) channelEventSource.close();
      if (catalogEventSource) catalogEventSource.close();
      video.pause();
      video.removeAttribute('src');
      video.load();
      window.location.href = '/login';
    });

    source.onerror = () => {
      // EventSource reconnects automatically. The server emits the current access
      // revision immediately after every reconnect, so missed permission changes
      // are recovered without waiting for the 30-second playback health check.
      console.warn('Account access event stream disconnected; waiting for reconnect.');
    };
  }

  function connectCatalogEvents() {
    if (catalogEventSource) catalogEventSource.close();
    if (typeof EventSource === 'undefined') return;

    catalogEventSource = new EventSource('/api/channels/catalog-events');
    catalogEventSource.addEventListener('catalog-update', () => {
      handleRealtimeAccessUpdate();
    });
    catalogEventSource.onerror = () => {
      // Reconnection is automatic and starts with the current revision.
      console.warn('Library catalog event stream disconnected; waiting for reconnect.');
    };
  }

  // Drift Correction Logic (Small: ignore, Moderate: playbackRate, Large: seek)
  function applyDriftCorrection(expectedTime, immediateSeek = false) {
    if (video.readyState < 2) return;

    const actual = video.currentTime;
    const diff = expectedTime - actual;
    const absDiff = Math.abs(diff);

    if (immediateSeek || absDiff > 8.0) {
      // Large drift or explicit seek request -> jump directly
      video.currentTime = expectedTime;
      video.playbackRate = 1.0;
    } else if (absDiff >= 2.0 && absDiff <= 8.0) {
      // Moderate drift -> gentle speed correction without audible glitch
      if (diff > 0) {
        video.playbackRate = 1.06;
      } else {
        video.playbackRate = 0.94;
      }
    } else {
      // Small drift (< 2s) -> normal speed
      if (video.playbackRate !== 1.0) {
        video.playbackRate = 1.0;
      }
    }
  }

  // Timeupdate handler for progress bar and OSD display
  video.addEventListener('timeupdate', () => {
    const expected = getExpectedServerOffset();
    const current = video.currentTime;
    const duration = serverTotalDuration || video.duration || 1;

    const pct = (current / duration) * 100;
    progressBar.style.width = `${Math.min(100, Math.max(0, pct))}%`;
    timeCurrent.textContent = formatTime(current);
    timeDuration.textContent = formatTime(duration);

    const remaining = Math.max(0, duration - current);
    timeRemaining.textContent = formatTime(remaining);

    applyDriftCorrection(expected, false);
  });

  // When episode naturally ends
  video.addEventListener('ended', () => {
    syncWithChannel(true);
  });

  // After buffering or stall, resynchronize to live broadcast position
  video.addEventListener('playing', () => {
    const expected = getExpectedServerOffset();
    const diff = Math.abs(expected - video.currentTime);
    if (diff > 3.0) {
      applyDriftCorrection(expected, true);
    }
  });

  function isViewerSettingsOpen() {
    return viewerSettings && !viewerSettings.classList.contains('hidden');
  }

  function setSettingsMessage(element, message = '', type = 'error') {
    if (!element) return;
    element.textContent = message;
    element.classList.toggle('hidden', !message);
    element.classList.toggle('success', Boolean(message) && type === 'success');
  }

  function lockChannelPreferences() {
    preferencesPassword = null;
    viewerPreferences = null;
    if (settingsChannelPassword) settingsChannelPassword.value = '';
    if (formUnlockChannelSettings) formUnlockChannelSettings.classList.remove('hidden');
    if (channelSettingsContent) channelSettingsContent.classList.add('hidden');
    if (blockedChannelsList) blockedChannelsList.innerHTML = '';
    setSettingsMessage(channelSettingsUnlockMessage);
    setSettingsMessage(channelSettingsMessage);
  }

  function openViewerSettings() {
    if (!viewerSettings) return;
    clearTimeout(osdTimer);
    if (settingsUsername && currentUser) settingsUsername.value = currentUser.username || '';
    if (settingsCurrentPassword) settingsCurrentPassword.value = '';
    if (settingsNewPassword) settingsNewPassword.value = '';
    if (settingsConfirmPassword) settingsConfirmPassword.value = '';
    setSettingsMessage(profileSettingsMessage);
    lockChannelPreferences();
    viewerSettings.classList.remove('hidden');
    document.body.style.cursor = 'default';
  }

  function closeViewerSettings() {
    if (!viewerSettings) return;
    viewerSettings.classList.add('hidden');
    lockChannelPreferences();
    showOSD();
  }

  async function readJsonResponse(response) {
    try {
      return await response.json();
    } catch {
      return {};
    }
  }

  function renderViewerPreferences(data) {
    viewerPreferences = data;
    if (!blockedChannelsList || !btnSensitiveToggle) return;

    const channels = Array.isArray(data.channels) ? data.channels : [];
    if (!channels.length) {
      blockedChannelsList.innerHTML = '<div class="blocked-channels-empty">No hay canales disponibles en esta vista.</div>';
    } else {
      blockedChannelsList.innerHTML = '';
      channels.forEach((channel) => {
        const row = document.createElement('label');
        row.className = 'blocked-channel-row';

        const name = document.createElement('span');
        name.className = 'blocked-channel-name';
        name.textContent = channel.name;

        const control = document.createElement('span');
        control.className = 'blocked-channel-control';
        const text = document.createElement('span');
        text.textContent = 'Bloquear';
        const checkbox = document.createElement('input');
        checkbox.type = 'checkbox';
        checkbox.className = 'blocked-channel-checkbox';
        checkbox.dataset.channelId = String(channel.id);
        checkbox.checked = Boolean(channel.blocked);
        checkbox.addEventListener('change', saveBlockedChannels);

        control.appendChild(text);
        control.appendChild(checkbox);
        row.appendChild(name);
        row.appendChild(control);
        blockedChannelsList.appendChild(row);
      });
    }

    const sensitiveEnabled = Boolean(data.sensitive_content_enabled);
    btnSensitiveToggle.classList.toggle('active', sensitiveEnabled);
    btnSensitiveToggle.setAttribute('aria-pressed', String(sensitiveEnabled));
  }

  async function updateViewerPreferences(patch) {
    if (!preferencesPassword) return null;
    setSettingsMessage(channelSettingsMessage);

    const controls = blockedChannelsList
      ? blockedChannelsList.querySelectorAll('input, button')
      : [];
    controls.forEach((control) => { control.disabled = true; });
    if (btnSensitiveToggle) btnSensitiveToggle.disabled = true;

    try {
      const response = await fetch('/api/auth/preferences', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ current_password: preferencesPassword, ...patch }),
      });
      const data = await readJsonResponse(response);
      if (!response.ok) {
        if (response.status === 401) {
          lockChannelPreferences();
          setSettingsMessage(channelSettingsUnlockMessage, data.detail || 'Vuelve a verificar tu contraseña.');
          return null;
        }
        throw new Error(data.detail || 'No se pudieron guardar las preferencias.');
      }

      renderViewerPreferences(data);
      setSettingsMessage(channelSettingsMessage, 'Preferencias actualizadas.', 'success');
      await refreshPlayerChannels();
      return data;
    } catch (err) {
      setSettingsMessage(channelSettingsMessage, err.message || 'No se pudieron guardar las preferencias.');
      return null;
    } finally {
      if (blockedChannelsList) {
        blockedChannelsList.querySelectorAll('input, button').forEach((control) => {
          control.disabled = false;
        });
      }
      if (btnSensitiveToggle) btnSensitiveToggle.disabled = false;
    }
  }

  async function saveBlockedChannels() {
    if (!blockedChannelsList) return;
    const blockedIds = Array.from(
      blockedChannelsList.querySelectorAll('.blocked-channel-checkbox:checked')
    ).map((input) => Number(input.dataset.channelId));
    await updateViewerPreferences({ blocked_channel_ids: blockedIds });
  }

  if (formProfileSettings) {
    formProfileSettings.addEventListener('submit', async (e) => {
      e.preventDefault();
      setSettingsMessage(profileSettingsMessage);

      const username = settingsUsername.value.trim();
      const currentPassword = settingsCurrentPassword.value;
      const newPassword = settingsNewPassword.value;
      const confirmPassword = settingsConfirmPassword.value;

      if (newPassword !== confirmPassword) {
        setSettingsMessage(profileSettingsMessage, 'Las nuevas contraseñas no coinciden.');
        return;
      }
      if (newPassword && newPassword.length < 6) {
        setSettingsMessage(profileSettingsMessage, 'La nueva contraseña debe tener al menos 6 caracteres.');
        return;
      }

      const submitButton = e.submitter;
      if (submitButton) submitButton.disabled = true;
      try {
        const response = await fetch('/api/auth/me', {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            current_password: currentPassword,
            username,
            new_password: newPassword || null,
          }),
        });
        const data = await readJsonResponse(response);
        if (!response.ok) throw new Error(data.detail || 'No se pudo actualizar la cuenta.');

        currentUser = data;
        settingsUsername.value = data.username;
        settingsCurrentPassword.value = '';
        settingsNewPassword.value = '';
        settingsConfirmPassword.value = '';
        if (newPassword) lockChannelPreferences();
        setSettingsMessage(profileSettingsMessage, 'Cuenta actualizada.', 'success');
      } catch (err) {
        setSettingsMessage(profileSettingsMessage, err.message || 'No se pudo actualizar la cuenta.');
      } finally {
        if (submitButton) submitButton.disabled = false;
      }
    });
  }

  if (formUnlockChannelSettings) {
    formUnlockChannelSettings.addEventListener('submit', async (e) => {
      e.preventDefault();
      setSettingsMessage(channelSettingsUnlockMessage);
      const password = settingsChannelPassword.value;
      const submitButton = e.submitter;
      if (submitButton) submitButton.disabled = true;

      try {
        const response = await fetch('/api/auth/preferences', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ current_password: password }),
        });
        const data = await readJsonResponse(response);
        if (!response.ok) throw new Error(data.detail || 'No se pudo abrir esta sección.');

        preferencesPassword = password;
        settingsChannelPassword.value = '';
        formUnlockChannelSettings.classList.add('hidden');
        channelSettingsContent.classList.remove('hidden');
        renderViewerPreferences(data);
      } catch (err) {
        setSettingsMessage(channelSettingsUnlockMessage, err.message || 'No se pudo abrir esta sección.');
      } finally {
        if (submitButton) submitButton.disabled = false;
      }
    });
  }

  if (btnSensitiveToggle) {
    btnSensitiveToggle.addEventListener('click', async () => {
      const nextValue = !Boolean(viewerPreferences?.sensitive_content_enabled);
      await updateViewerPreferences({ sensitive_content_enabled: nextValue });
    });
  }

  if (btnCloseSettings) btnCloseSettings.addEventListener('click', closeViewerSettings);
  if (viewerSettings) {
    viewerSettings.addEventListener('click', (e) => {
      if (e.target === viewerSettings) closeViewerSettings();
    });
  }
  if (settingsLogout) {
    settingsLogout.addEventListener('click', async () => {
      await fetch('/api/auth/logout', { method: 'POST' });
      window.location.href = '/login';
    });
  }

  // Unmute Handler
  function handleUnmute() {
    setMuted(false, true);
  }

  unmuteBtn.addEventListener('click', handleUnmute);
  document.addEventListener('click', (e) => {
    if (isViewerSettingsOpen()) return;
    if (video.muted && !unmuteBanner.classList.contains('hidden')) {
      handleUnmute();
    }
  });

  // Fullscreen + automatic landscape orientation on mobile/tablet.
  // Screen Orientation API support depends on the browser; failures are
  // intentionally non-fatal so fullscreen continues to work everywhere.
  function isMobileFullscreenDevice() {
    return navigator.maxTouchPoints > 0 ||
      (window.matchMedia && window.matchMedia('(pointer: coarse)').matches);
  }

  async function lockLandscapeOrientation() {
    if (!isMobileFullscreenDevice()) return false;

    try {
      if (screen.orientation && typeof screen.orientation.lock === 'function') {
        await screen.orientation.lock('landscape');
        return true;
      }

      // Legacy Android/Windows implementations.
      const legacyLock = screen.lockOrientation ||
        screen.mozLockOrientation ||
        screen.msLockOrientation;
      if (typeof legacyLock === 'function') {
        return Boolean(legacyLock.call(screen, 'landscape'));
      }
    } catch (err) {
      // Safari/iOS and some embedded browsers reject orientation locking.
      console.debug('Landscape orientation lock is not available:', err);
    }

    return false;
  }

  function unlockFullscreenOrientation() {
    if (!isMobileFullscreenDevice()) return;

    try {
      if (screen.orientation && typeof screen.orientation.unlock === 'function') {
        screen.orientation.unlock();
        return;
      }

      const legacyUnlock = screen.unlockOrientation ||
        screen.mozUnlockOrientation ||
        screen.msUnlockOrientation;
      if (typeof legacyUnlock === 'function') {
        legacyUnlock.call(screen);
      }
    } catch (err) {
      console.debug('Orientation unlock is not available:', err);
    }
  }

  async function enterAppFullscreen() {
    const target = document.documentElement;

    if (target.requestFullscreen) {
      try {
        await target.requestFullscreen();
        await lockLandscapeOrientation();
        return;
      } catch (err) {
        console.error('Error enabling fullscreen:', err);
      }
    }

    // iPhone/iPad fallback: use Safari's native video fullscreen when the
    // page Fullscreen API is unavailable or denied. Orientation locking is
    // controlled by iOS in this mode, but playback still enters fullscreen.
    if (typeof video.webkitEnterFullscreen === 'function') {
      try {
        video.webkitEnterFullscreen();
        await lockLandscapeOrientation();
        return;
      } catch (err) {
        console.error('Error enabling native video fullscreen:', err);
      }
    }

    if (typeof video.webkitRequestFullscreen === 'function') {
      try {
        video.webkitRequestFullscreen();
        await lockLandscapeOrientation();
      } catch (err) {
        console.error('Error enabling WebKit fullscreen:', err);
      }
    }
  }

  async function exitAppFullscreen() {
    unlockFullscreenOrientation();

    if (document.fullscreenElement && document.exitFullscreen) {
      try {
        await document.exitFullscreen();
        return;
      } catch (err) {
        console.error('Error exiting fullscreen:', err);
      }
    }

    if (video.webkitDisplayingFullscreen && typeof video.webkitExitFullscreen === 'function') {
      try {
        video.webkitExitFullscreen();
      } catch (err) {
        console.error('Error exiting native video fullscreen:', err);
      }
    }
  }

  function toggleFullscreen() {
    const isNativeVideoFullscreen = Boolean(video.webkitDisplayingFullscreen);
    if (!document.fullscreenElement && !isNativeVideoFullscreen) {
      enterAppFullscreen();
    } else {
      exitAppFullscreen();
    }
  }

  // Lock only after fullscreen is active: Chromium requires this ordering.
  document.addEventListener('fullscreenchange', () => {
    if (document.fullscreenElement) {
      lockLandscapeOrientation();
    } else {
      unlockFullscreenOrientation();
    }
  });

  // Safari native video fullscreen lifecycle.
  video.addEventListener('webkitbeginfullscreen', lockLandscapeOrientation);
  video.addEventListener('webkitendfullscreen', unlockFullscreenOrientation);

  btnFullscreen.addEventListener('click', toggleFullscreen);
  btnMute.addEventListener('click', (e) => {
    e.stopPropagation();
    toggleMute();
    showOSD();
  });
  if (volumeSlider) {
    volumeSlider.addEventListener('input', (e) => {
      e.stopPropagation();
      setVolume(Number(e.target.value) / 100, true);
      showOSD();
    });
    volumeSlider.addEventListener('click', (e) => e.stopPropagation());
  }
  btnChannelPrev.addEventListener('click', (e) => {
    e.stopPropagation();
    changeChannel(-1);
    showOSD();
  });
  btnChannelNext.addEventListener('click', (e) => {
    e.stopPropagation();
    changeChannel(1);
    showOSD();
  });

  updateMuteButton();

  // Sync button in OSD
  btnSync.addEventListener('click', () => {
    syncWithChannel(true, true);
  });

  if (btnAccount) {
    btnAccount.addEventListener('click', (e) => {
      e.stopPropagation();
      openViewerSettings();
    });
  }

  // Mouse activity shows OSD
  window.addEventListener('mousemove', showOSD);
  window.addEventListener('touchstart', showOSD);

// Keyboard Shortcuts
window.addEventListener('keydown', (e) => {

  if (isViewerSettingsOpen()) {
    if (e.key === 'Escape') {
      e.preventDefault();
      closeViewerSettings();
    }
    return;
  }

  // Evitar que los atajos funcionen mientras se escribe
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') {
    return;
  }

  switch (e.key.toLowerCase()) {

    case '1':
      changeChannel(-1);
      showOSD();
      break;

    case '2':
      changeChannel(1);
      showOSD();
      break;

    case 'arrowleft':
      changeChannel(-1);
      showOSD();
      break;

    case 'arrowright':
      changeChannel(1);
      showOSD();
      break;

    case 'arrowup':
      e.preventDefault();
      changeVolume(1);
      showOSD();
      break;

    case 'arrowdown':
      e.preventDefault();
      changeVolume(-1);
      showOSD();
      break;

    case 'f':
      toggleFullscreen();
      break;

    case 'm':
      toggleMute();
      showOSD();
      break;

    case ' ':
      e.preventDefault();

      if (video.paused) {
        syncWithChannel(true);
      } else {
        video.pause();
      }

      showOSD();
      break;

    case 's':
      // Resync manual: mostrar el OSD porque fue una acción del usuario.
      syncWithChannel(true, true);
      break;

    case 'i':
      // Toggle OSD
      osdOverlay.classList.toggle('hidden');
      break;
  }
});

  // Periodic health/sync check (every 30 seconds)
  periodicSyncTimer = setInterval(() => {
    if (!document.hidden && !video.paused) {
      // Fallback health check. Normally channel changes arrive instantly by SSE,
      // but this also recovers clients behind proxies that interrupt event streams.
      syncWithChannel(false);
    }
  }, 30000);

  // Initial Boot
  checkAuth().then(async (authenticated) => {
    if (authenticated) {
      restoreAudioPreferences();
      const channels = await loadChannels();
      if (channels.length > 0 && currentChannelId) {
        connectChannelEvents();
        syncWithChannel(true);
      } else {
        setEmptyStateMode('no-channels');
        emptyState.classList.remove('hidden');
        osdOverlay.classList.remove('hidden');
      }
      connectAccessEvents();
      connectCatalogEvents();
    }
  });
})();
