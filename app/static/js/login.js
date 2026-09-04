(() => {
  'use strict';

  function safeInternalDestination(candidate, fallback) {
    if (!candidate || !candidate.startsWith('/') || candidate.startsWith('//')) {
      return fallback;
    }
    try {
      const resolved = new URL(candidate, window.location.origin);
      if (resolved.origin !== window.location.origin) return fallback;
      return `${resolved.pathname}${resolved.search}${resolved.hash}`;
    } catch {
      return fallback;
    }
  }

  const form = document.getElementById('login-form');
  if (!form) return;

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const errBox = document.getElementById('login-error');
    const btn = document.getElementById('btn-submit');
    const username = document.getElementById('username').value.trim();
    const password = document.getElementById('password').value;

    errBox.classList.add('hidden');
    btn.disabled = true;
    btn.textContent = 'Verificando...';

    try {
      const res = await fetch('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify({ username, password }),
      });
      const data = await res.json();

      if (!res.ok) {
        errBox.textContent = data.detail || 'Usuario o contraseña incorrectos.';
        errBox.classList.remove('hidden');
        return;
      }

      if (data.must_change_password) {
        window.location.href = '/change-password';
        return;
      }

      const fallback = data.role === 'admin' ? '/admin' : '/';
      const candidate = new URLSearchParams(window.location.search).get('next');
      window.location.href = safeInternalDestination(candidate, fallback);
    } catch {
      errBox.textContent = 'Error de conexión con el servidor.';
      errBox.classList.remove('hidden');
    } finally {
      btn.disabled = false;
      btn.textContent = 'Iniciar Sesión';
    }
  });
})();
