(() => {
  'use strict';

  const deploymentMeta = document.querySelector('meta[name="simplitv-deployment"]');
  const loadedDeployment = deploymentMeta?.content?.trim();
  if (!loadedDeployment) return;

  const VISIBLE_INTERVAL_MS = 30000;
  const HIDDEN_INTERVAL_MS = 60000;
  let timer = null;
  let requestInFlight = false;
  let reloading = false;

  function scheduleNextCheck() {
    window.clearTimeout(timer);
    const delay = document.hidden ? HIDDEN_INTERVAL_MS : VISIBLE_INTERVAL_MS;
    timer = window.setTimeout(checkDeployment, delay);
  }

  async function checkDeployment() {
    if (requestInFlight || reloading) {
      scheduleNextCheck();
      return;
    }

    requestInFlight = true;
    try {
      const response = await fetch('/api/health', {
        cache: 'no-store',
        credentials: 'same-origin',
      });
      if (!response.ok) return;

      const data = await response.json();
      if (data.deployment_id && data.deployment_id !== loadedDeployment) {
        reloading = true;
        window.location.reload();
        return;
      }
    } catch {
      // A failed request is expected while SimpliTV is restarting.  The next
      // check will detect the new process once it is available again.
    } finally {
      requestInFlight = false;
      if (!reloading) scheduleNextCheck();
    }
  }

  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) checkDeployment();
    else scheduleNextCheck();
  });
  window.addEventListener('focus', checkDeployment);
  window.addEventListener('pageshow', (event) => {
    if (event.persisted) checkDeployment();
  });

  scheduleNextCheck();
})();
