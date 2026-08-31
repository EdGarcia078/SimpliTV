with open("app/static/admin.html", "r") as f:
    content = f.read()

content = content.replace(
"""      <button class="nav-tab active" data-tab="tab-dashboard">Dashboard & Emisión</button>""",
"""      <button class="nav-tab active" data-tab="tab-dashboard">Dashboard & Emisión</button>
      <button class="nav-tab" data-tab="tab-channels">Gestión de Canales</button>"""
)

channels_tab = """
      <!-- 1.5 TAB: CHANNELS -->
      <section id="tab-channels" class="tab-content">
        <div class="section-toolbar">
          <h2>Gestión de Canales</h2>
        </div>
        <div class="table-responsive">
          <table class="admin-table">
            <thead>
              <tr>
                <th>ID</th>
                <th>Nombre del Canal</th>
                <th>Batch Size</th>
                <th>Empezar en Par</th>
                <th>Loop Continuo</th>
                <th>Acciones</th>
              </tr>
            </thead>
            <tbody id="channels-tbody">
              <!-- Loaded via JS -->
            </tbody>
          </table>
        </div>
      </section>
"""

content = content.replace(
"""      <!-- 2. TAB: USERS -->""",
channels_tab + """\n      <!-- 2. TAB: USERS -->"""
)

# Also add modal for channel config
channel_modal = """
  <!-- Modal: Configurar Canal -->
  <div id="modal-config-channel" class="modal-backdrop hidden">
    <div class="modal-card">
      <h3>Configurar Canal: <span id="config-channel-name"></span></h3>
      <form id="form-config-channel">
        <input type="hidden" id="config-channel-id" />
        <div class="form-group">
          <label for="config-batch-size">Batch Size (Episodios por Show)</label>
          <input type="number" id="config-batch-size" required min="1" max="100" />
        </div>
        <div class="form-group">
          <label for="config-start-even">Empezar desde Episodio Par</label>
          <select id="config-start-even">
            <option value="true">Sí</option>
            <option value="false">No</option>
          </select>
        </div>
        <div class="form-group">
          <label for="config-loop">Loop Continuo</label>
          <select id="config-loop">
            <option value="true">Sí</option>
            <option value="false">No</option>
          </select>
        </div>
        <div id="config-channel-error" class="alert-error hidden"></div>
        <div class="modal-actions">
          <button type="button" class="btn-outline modal-close">Cancelar</button>
          <button type="submit" class="btn-primary">Guardar Configuración</button>
        </div>
      </form>
    </div>
  </div>
"""

content = content.replace(
"""  <script src="/static/js/admin.js"></script>""",
channel_modal + """\n  <script src="/static/js/admin.js"></script>"""
)

# And in Dashboard, add channel selector for skipping/viewing
content = content.replace(
"""            <div class="card-header">
              <h3>📡 Emisión Global del Canal</h3>
              <button id="btn-skip-episode" class="btn-accent btn-sm">⏭ Saltar Episodio</button>
            </div>""",
"""            <div class="card-header">
              <h3>📡 Emisión Global del Canal</h3>
              <select id="admin-dash-channel" class="channel-selector" style="margin-right: auto; margin-left: 1rem;"></select>
              <button id="btn-skip-episode" class="btn-accent btn-sm">⏭ Saltar Episodio</button>
            </div>"""
)

with open("app/static/admin.html", "w") as f:
    f.write(content)
