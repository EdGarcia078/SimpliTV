with open("app/static/index.html", "r") as f:
    content = f.read()

content = content.replace(
"""          <div class="channel-badge">
            <span class="live-dot"></span>
            <span class="channel-name">SIMPLITV EN VIVO</span>
          </div>""",
"""          <div class="channel-badge">
            <span class="live-dot"></span>
            <select id="channel-selector" class="channel-selector" style="display: none;">
            </select>
            <span id="channel-name-display" class="channel-name">SIMPLITV EN VIVO</span>
          </div>"""
)

with open("app/static/index.html", "w") as f:
    f.write(content)
