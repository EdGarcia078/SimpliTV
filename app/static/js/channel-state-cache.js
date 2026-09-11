/* Small, memory-only cache. It never stores media bytes or authorization. */
class ChannelStateCache {
  constructor(now = () => performance.now()) {
    this.now = now;
    this.entries = new Map();
  }
  clear() { this.entries.clear(); }
  put(id, state) {
    if (!state?.episode?.stream_url || !Number.isFinite(state.current_time) ||
        !Number.isFinite(state.duration) || state.duration <= 0) return;
    this.entries.delete(Number(id));
    this.entries.set(Number(id), { state, at: this.now() });
    while (this.entries.size > 8) this.entries.delete(this.entries.keys().next().value);
  }
  get(id) {
    const entry = this.entries.get(Number(id));
    if (!entry) return null;
    const age = (this.now() - entry.at) / 1000;
    const offset = entry.state.current_time + age;
    if (age < 0 || age >= 20 || entry.state.duration - offset <= 15) return null;
    return { ...entry.state, current_time: offset,
      remaining_time: entry.state.duration - offset };
  }
}
if (typeof module !== 'undefined') module.exports = ChannelStateCache;
