(() => {
  'use strict';

  let deferredInstallPrompt = null;
  let installedThisSession = false;

  const standaloneMedia = typeof window.matchMedia === 'function'
    ? window.matchMedia('(display-mode: standalone)')
    : null;

  function isStandalone() {
    return Boolean(
      installedThisSession ||
      standaloneMedia?.matches ||
      window.navigator?.standalone === true
    );
  }

  function detectPlatform() {
    const userAgent = String(window.navigator?.userAgent || '');
    const platform = String(window.navigator?.platform || '');
    const touchPoints = Number(window.navigator?.maxTouchPoints || 0);
    const isIOS = /iPad|iPhone|iPod/i.test(userAgent) ||
      (platform === 'MacIntel' && touchPoints > 1);

    if (isIOS) return 'ios';
    if (/Android/i.test(userAgent)) return 'android';
    return 'desktop';
  }

  function getState() {
    const standalone = isStandalone();
    return Object.freeze({
      secureContext: Boolean(window.isSecureContext),
      serviceWorkerSupported: 'serviceWorker' in navigator,
      standalone,
      canPromptInstall: Boolean(deferredInstallPrompt) && !standalone,
      platform: detectPlatform(),
    });
  }

  function emitStateChange() {
    window.dispatchEvent(new CustomEvent('simplitv:pwa-statechange', {
      detail: getState(),
    }));
  }

  async function promptInstall() {
    const state = getState();

    if (state.standalone) {
      return { status: 'installed', outcome: 'accepted' };
    }
    if (!state.secureContext) {
      return { status: 'unavailable', reason: 'insecure-context' };
    }
    if (!state.serviceWorkerSupported) {
      return { status: 'unavailable', reason: 'unsupported-browser' };
    }
    if (!deferredInstallPrompt) {
      return { status: 'manual', reason: 'browser-install-ui' };
    }

    // Consume each browser prompt once. If the user dismisses it, Chromium may
    // emit a new beforeinstallprompt later; we never nag or invoke it ourselves.
    const promptEvent = deferredInstallPrompt;
    deferredInstallPrompt = null;
    emitStateChange();

    try {
      await promptEvent.prompt();
      const choice = await promptEvent.userChoice;
      const outcome = choice?.outcome === 'accepted' ? 'accepted' : 'dismissed';
      return { status: outcome, outcome };
    } catch {
      return { status: 'manual', reason: 'prompt-failed' };
    } finally {
      emitStateChange();
    }
  }

  // Expose a tiny read-only integration surface for authenticated UIs. The PWA
  // remains progressive enhancement: no application code depends on this API.
  window.SimpliTVPWA = Object.freeze({
    getState,
    promptInstall,
  });

  window.addEventListener('beforeinstallprompt', (event) => {
    // Prevent browser-driven install banners/prompts. SimpliTV only asks after
    // an explicit user action from Cuenta -> Instalar SimpliTV.
    event.preventDefault();
    deferredInstallPrompt = event;
    emitStateChange();
  });

  window.addEventListener('appinstalled', () => {
    deferredInstallPrompt = null;
    installedThisSession = true;
    emitStateChange();
  });

  if (standaloneMedia) {
    const onDisplayModeChange = () => emitStateChange();
    if (typeof standaloneMedia.addEventListener === 'function') {
      standaloneMedia.addEventListener('change', onDisplayModeChange);
    } else if (typeof standaloneMedia.addListener === 'function') {
      standaloneMedia.addListener(onDisplayModeChange);
    }
  }

  // Service workers require a secure context. Keeping registration a no-op on
  // plain LAN HTTP preserves SimpliTV's normal browser mode without errors.
  if (window.isSecureContext && 'serviceWorker' in navigator) {
    window.addEventListener('load', async () => {
      try {
        const registration = await navigator.serviceWorker.register('/service-worker.js', {
          scope: '/',
          updateViaCache: 'none',
        });

        // Keep installed clients aligned with SimpliTV's deployment watcher.
        if (registration.active) {
          registration.update().catch(() => {});
        }
      } catch {
        // Registration failure must never prevent the regular web client.
      } finally {
        emitStateChange();
      }
    }, { once: true });
  }

  // The UI may already be listening because player.js is loaded at the end of
  // the document while this script is deferred from the generated <head>.
  emitStateChange();
})();
