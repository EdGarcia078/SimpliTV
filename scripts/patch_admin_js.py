with open("app/static/js/admin.js", "r") as f:
    content = f.read()

content = content.replace(
"""  const btnSkipMediaItem = document.getElementById('btn-skip-episode');""",
"""  const btnSkipMediaItem = document.getElementById('btn-skip-episode');
  const adminDashChannel = document.getElementById('admin-dash-channel');
  let currentAdminChannelId = 1;"""
)

# Replace loadStats logic to fetch /api/channels first and then stats for that channel
# Wait, loadStats uses /api/admin/stats which currently returns global stats but also channel state.
# We should decouple it or fetch channel state from /api/channels/${currentAdminChannelId}/now-playing
content = content.replace(
"""  async function loadStats() {
    try {
      const res = await fetch('/api/admin/stats');
      if (!res.ok) return;
      const data = await res.json();

      // Quick Stats
      document.getElementById('stat-total-episodes').textContent = data.media.total_episodes;
      document.getElementById('stat-unique-series').textContent = data.media.unique_series;
      document.getElementById('stat-total-hours').textContent = data.media.total_duration_hours;
      document.getElementById('stat-total-users').textContent = data.users.total_users;
      document.getElementById('stat-active-users').textContent = data.users.active_users;

      // Broadcast State
      if (data.channel) {
        const c = data.channel;
        document.getElementById('dash-media').textContent = c.episode.media_title;
        document.getElementById('dash-episode').textContent = `T${c.episode.season_number} • E${c.episode.episode_number}`;
        document.getElementById('dash-progress').textContent = `${formatTime(c.current_time)} / ${formatTime(c.duration)}`;
        if (c.next_episode) {
          document.getElementById('dash-next').textContent = `${c.next_episode.media_title} (T${c.next_episode.season_number}E${c.next_episode.episode_number})`;
        } else {
          document.getElementById('dash-next').textContent = '—';
        }
      }
    } catch (err) {
      console.error('Error loading stats:', err);
    }
  }""",
"""  async function loadStats() {
    try {
      const res = await fetch('/api/admin/stats');
      if (!res.ok) return;
      const data = await res.json();

      // Quick Stats
      document.getElementById('stat-total-episodes').textContent = data.media.total_episodes;
      document.getElementById('stat-unique-series').textContent = data.media.unique_series;
      document.getElementById('stat-total-hours').textContent = data.media.total_duration_hours;
      document.getElementById('stat-total-users').textContent = data.users.total_users;
      document.getElementById('stat-active-users').textContent = data.users.active_users;

      // Ensure channel list is loaded
      await fetchAdminChannels();
      
      if (!currentAdminChannelId) return;

      // Broadcast State
      const stRes = await fetch(`/api/channels/${currentAdminChannelId}/now-playing`);
      if (stRes.ok) {
        const c = await stRes.json();
        document.getElementById('dash-media').textContent = c.episode.media_title;
        document.getElementById('dash-episode').textContent = `T${c.episode.season_number} • E${c.episode.episode_number}`;
        document.getElementById('dash-progress').textContent = `${formatTime(c.current_time)} / ${formatTime(c.duration)}`;
        if (c.next_episode) {
          document.getElementById('dash-next').textContent = `${c.next_episode.media_title} (T${c.next_episode.season_number}E${c.next_episode.episode_number})`;
        } else {
          document.getElementById('dash-next').textContent = '—';
        }
      }
    } catch (err) {
      console.error('Error loading stats:', err);
    }
  }"""
)

# btnSkipMediaItem click
content = content.replace(
"""  btnSkipMediaItem.addEventListener('click', async () => {
    try {
      const res = await fetch('/api/admin/channel/skip', { method: 'POST' });
      if (res.ok) {
        loadStats();
      }
    } catch (err) {
      console.error('Skip failed:', err);
    }
  });""",
"""  btnSkipMediaItem.addEventListener('click', async () => {
    if (!currentAdminChannelId) return;
    try {
      const res = await fetch(`/api/admin/channels/${currentAdminChannelId}/skip`, { method: 'POST' });
      if (res.ok) {
        loadStats();
      }
    } catch (err) {
      console.error('Skip failed:', err);
    }
  });

  if (adminDashChannel) {
    adminDashChannel.addEventListener('change', (e) => {
        currentAdminChannelId = e.target.value;
        loadStats();
    });
  }

  let channelsData = [];
  async function fetchAdminChannels() {
    try {
      const res = await fetch('/api/channels');
      if (res.ok) {
        channelsData = await res.json();
        if (!currentAdminChannelId && channelsData.length > 0) {
            currentAdminChannelId = channelsData[0].id;
        }
        
        // update dashboard selector
        if (adminDashChannel) {
            adminDashChannel.innerHTML = '';
            channelsData.forEach(ch => {
                const opt = document.createElement('option');
                opt.value = ch.id;
                opt.textContent = ch.name;
                if (ch.id == currentAdminChannelId) opt.selected = true;
                adminDashChannel.appendChild(opt);
            });
        }
        
        // update channels tab table
        const tbody = document.getElementById('channels-tbody');
        if (tbody) {
            tbody.innerHTML = '';
            channelsData.forEach(ch => {
                const tr = document.createElement('tr');
                tr.innerHTML = `
                  <td>${ch.id}</td>
                  <td><strong>${escapeHtml(ch.name)}</strong></td>
                  <td>${ch.batch_size}</td>
                  <td>${ch.start_from_even ? 'Sí' : 'No'}</td>
                  <td>${ch.loop ? 'Sí' : 'No'}</td>
                  <td>
                    <button class="btn-sm btn-outline btn-config-channel" data-id="${ch.id}" data-name="${escapeHtml(ch.name)}">⚙️ Configurar</button>
                  </td>
                `;
                tbody.appendChild(tr);
            });
            
            document.querySelectorAll('.btn-config-channel').forEach(btn => {
                btn.addEventListener('click', () => {
                    const id = parseInt(btn.dataset.id);
                    const ch = channelsData.find(c => c.id === id);
                    if (!ch) return;
                    
                    document.getElementById('config-channel-id').value = id;
                    document.getElementById('config-channel-name').textContent = ch.name;
                    document.getElementById('config-batch-size').value = ch.batch_size;
                    document.getElementById('config-start-even').value = ch.start_from_even.toString();
                    document.getElementById('config-loop').value = ch.loop.toString();
                    document.getElementById('modal-config-channel').classList.remove('hidden');
                });
            });
        }
      }
    } catch (err) {
      console.error(err);
    }
  }

  // Handle Channel Config Form
  const formConfigChannel = document.getElementById('form-config-channel');
  if (formConfigChannel) {
      formConfigChannel.addEventListener('submit', async (e) => {
        e.preventDefault();
        const id = document.getElementById('config-channel-id').value;
        const batch_size = parseInt(document.getElementById('config-batch-size').value);
        const start_from_even = document.getElementById('config-start-even').value === 'true';
        const loop = document.getElementById('config-loop').value === 'true';
        
        try {
          const res = await fetch(`/api/admin/channels/${id}/settings`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ batch_size, start_from_even, loop }),
          });
          if (res.ok) {
            document.getElementById('modal-config-channel').classList.add('hidden');
            fetchAdminChannels();
          } else {
             const data = await res.json();
             alert(data.detail || 'Error saving settings');
          }
        } catch (err) {
            console.error(err);
        }
      });
  }
"""
)

# And make sure fetchAdminChannels is called when switching tabs
content = content.replace(
"""      if (tabId === 'tab-users') loadUsers();
      if (tabId === 'tab-library') loadLibrary();""",
"""      if (tabId === 'tab-users') loadUsers();
      if (tabId === 'tab-library') loadLibrary();
      if (tabId === 'tab-channels') fetchAdminChannels();"""
)

with open("app/static/js/admin.js", "w") as f:
    f.write(content)
