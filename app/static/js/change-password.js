(() => {
  'use strict';

  const form = document.getElementById('password-change-form');
  if (!form) return;

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const errorBox = document.getElementById('password-change-error');
    const button = document.getElementById('btn-change-required-password');
    const currentPassword = document.getElementById('current-password').value;
    const newPassword = document.getElementById('new-password-required').value;
    const confirmation = document.getElementById('confirm-password-required').value;

    errorBox.classList.add('hidden');
    if (newPassword !== confirmation) {
      errorBox.textContent = 'Las nuevas contraseñas no coinciden.';
      errorBox.classList.remove('hidden');
      return;
    }
    if (newPassword.length < 12) {
      errorBox.textContent = 'La nueva contraseña debe tener al menos 12 caracteres.';
      errorBox.classList.remove('hidden');
      return;
    }

    button.disabled = true;
    button.textContent = 'Guardando...';
    try {
      const response = await fetch('/api/auth/change-default-password', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin',
        body: JSON.stringify({
          current_password: currentPassword,
          new_password: newPassword,
        }),
      });
      const data = await response.json();
      if (!response.ok) {
        errorBox.textContent = data.detail || 'No se pudo cambiar la contraseña.';
        errorBox.classList.remove('hidden');
        return;
      }
      window.location.href = data.role === 'admin' ? '/admin' : '/';
    } catch {
      errorBox.textContent = 'Error de conexión con el servidor.';
      errorBox.classList.remove('hidden');
    } finally {
      button.disabled = false;
      button.textContent = 'Guardar contraseña';
    }
  });
})();
