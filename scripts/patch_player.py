with open("app/static/js/player.js", "r") as f:
    content = f.read()

content = content.replace(
"""  const btnAdminLink = document.getElementById('btn-admin-link');""",
"""  const btnAdminLink = document.getElementById('btn-admin-link');
  const channelSelector = document.getElementById('channel-selector');
  const channelNameDisplay = document.getElementById('channel-name-display');"""
)

content = content.replace(
"""  let currentUser = null;""",
"""  let currentUser = null;
  let currentChannelId = 1;"""
)

content = content.replace(
"""      const response = await fetch('/api/channel/now-playing');""",
"""      const response = await fetch(`/api/channels/${currentChannelId}/now-playing`);"""
)

content = content.replace(
"""  // Check Auth & Profile
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
  }""",
"""  // Check Auth & Profile
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
      const res = await fetch('/api/channels');
      if (res.ok) {
        const channels = await res.json();
        if (channels.length > 0) {
          currentChannelId = channels[0].id;
          channelSelector.innerHTML = '';
          channels.forEach(ch => {
            const opt = document.createElement('option');
            opt.value = ch.id;
            opt.textContent = ch.name;
            channelSelector.appendChild(opt);
          });
          channelSelector.style.display = 'inline-block';
          channelNameDisplay.style.display = 'none';
        }
      }
    } catch (err) {
      console.error('Error loading channels', err);
    }
  }

  if (channelSelector) {
      channelSelector.addEventListener('change', (e) => {
        currentChannelId = e.target.value;
        currentMediaItemId = null;
        video.pause();
        video.src = '';
        syncWithChannel(true);
      });
  }"""
)

content = content.replace(
"""  // Initial Boot
  checkAuth().then((authenticated) => {
    if (authenticated) {
      syncWithChannel(true);
    }
  });""",
"""  // Initial Boot
  checkAuth().then(async (authenticated) => {
    if (authenticated) {
      await loadChannels();
      syncWithChannel(true);
    }
  });"""
)

with open("app/static/js/player.js", "w") as f:
    f.write(content)
