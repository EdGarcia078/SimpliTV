import asyncio
import logging
import threading
import time
from pathlib import Path
from typing import Callable, Dict, Optional, Set
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer
from sqlmodel import Session

from app.core.config import settings
from app.db.session import engine
from app.services.scanner import scan_library, upsert_episode_file, remove_episode_by_path
from app.services.channel import channel_engine

logger = logging.getLogger(__name__)


def is_file_stable(file_path: Path, min_check_interval: float = 0.4) -> bool:
    """
    Check if a file has finished copying/writing.
    Verifies that:
    1. File exists and is a regular file.
    2. File can be opened in binary read mode.
    3. File size is consistent across check interval.
    """
    if not file_path.exists() or not file_path.is_file():
        return False

    try:
        size1 = file_path.stat().st_size
        if size1 == 0:
            time.sleep(min_check_interval)
            return file_path.exists() and file_path.stat().st_size > 0

        with open(file_path, "rb") as f:
            f.read(min(1024, size1))

        time.sleep(min_check_interval)
        size2 = file_path.stat().st_size

        return size1 == size2
    except (OSError, PermissionError):
        return False


class MediaFileEventHandler(FileSystemEventHandler):
    """Event handler that captures relevant media filesystem changes with debouncing."""

    def __init__(self, watcher: "MediaWatcher"):
        super().__init__()
        self.watcher = watcher

    def _is_relevant(self, path_str: str, is_dir: bool) -> bool:
        if is_dir:
            return False
        path = Path(path_str)
        if path.name.casefold() in {"channel.yaml", "series.yaml", "franchise.yaml"}:
            return True
        return ".optimizing" not in path.stem and ".converting" not in path.stem and path.suffix.lower() in settings.SUPPORTED_EXTENSIONS

    def on_created(self, event: FileSystemEvent):
        if self._is_relevant(event.src_path, event.is_directory):
            self.watcher.queue_change(Path(event.src_path), is_deletion=False)

    def on_modified(self, event: FileSystemEvent):
        if self._is_relevant(event.src_path, event.is_directory):
            self.watcher.queue_change(Path(event.src_path), is_deletion=False)

    def on_deleted(self, event: FileSystemEvent):
        if self._is_relevant(event.src_path, event.is_directory):
            self.watcher.queue_change(Path(event.src_path), is_deletion=True)

    def on_moved(self, event: FileSystemEvent):
        if self._is_relevant(event.src_path, event.is_directory):
            self.watcher.queue_change(Path(event.src_path), is_deletion=True)
        if hasattr(event, "dest_path"):
            if self._is_relevant(event.dest_path, event.is_directory):
                self.watcher.queue_change(Path(event.dest_path), is_deletion=False)


class MediaWatcher:
    """
    Asynchronous filesystem watcher for dynamic media library updates.
    Features:
    - Debounce and coalescing of high-frequency filesystem events.
    - Copy stability verification before running ffprobe.
    - Idempotent SQLite indexing.
    - Non-disruptive integration with ChannelEngine.
    """

    def __init__(
        self,
        media_dir: Optional[Path] = None,
        debounce_seconds: float = 1.0,
        session_factory: Optional[Callable[[], Session]] = None,
    ):
        self.media_dir = (media_dir or settings.resolved_media_dir).resolve()
        self.debounce_seconds = debounce_seconds
        self.session_factory = session_factory or (lambda: Session(engine))

        self._observer: Optional[Observer] = None
        self._handler = MediaFileEventHandler(self)
        self._pending_changes: Dict[Path, float] = {}
        self._pending_deletions: Set[Path] = set()
        self._lock = threading.Lock()

        self._worker_task: Optional[asyncio.Task] = None
        self._running: bool = False

    def queue_change(self, file_path: Path, is_deletion: bool = False) -> None:
        """Queue a file path event with the current timestamp."""
        path = file_path.resolve()
        with self._lock:
            self._pending_changes[path] = time.time()
            if is_deletion:
                self._pending_deletions.add(path)
            else:
                self._pending_deletions.discard(path)

    async def _process_pending_worker(self) -> None:
        """Background coroutine that drains and processes debounced events."""
        while self._running:
            try:
                await asyncio.sleep(0.3)
                ready_events = []
                now = time.time()

                with self._lock:
                    for path, event_time in list(self._pending_changes.items()):
                        if now - event_time >= self.debounce_seconds:
                            is_del = path in self._pending_deletions
                            ready_events.append((path, is_del))
                            del self._pending_changes[path]
                            self._pending_deletions.discard(path)

                if not ready_events:
                    continue

                with self.session_factory() as session:
                    library_updated = False
                    config_changed = False

                    for path, is_del in ready_events:
                        if path.name.casefold() in {"channel.yaml", "series.yaml", "franchise.yaml"}:
                            config_changed = True
                            continue

                        if is_del:
                            deleted = remove_episode_by_path(session, path, self.media_dir)
                            if deleted:
                                library_updated = True
                        else:
                            # Verify copy stability
                            if is_file_stable(path):
                                ep = await upsert_episode_file(session, path, self.media_dir)
                                if ep:
                                    library_updated = True
                            else:
                                # Re-queue if file is still unstable / copying
                                logger.debug(f"File {path.name} is still copying/unstable; re-queueing.")
                                with self._lock:
                                    self._pending_changes[path] = time.time()

                    if config_changed:
                        # Config files are small and infrequently edited. A full
                        # idempotent scan keeps generated defaults, display names,
                        # and the DB index coherent without adding a second config
                        # synchronization path.
                        await scan_library(session, self.media_dir)
                        library_updated = True

                    if library_updated:
                        await channel_engine.notify_library_changed(session)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(f"Error in MediaWatcher worker: {exc}", exc_info=True)

    def start(self, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        """Start the watchdog observer and async debounce processor."""
        if self._running:
            return

        self._running = True
        self.media_dir.mkdir(parents=True, exist_ok=True)

        # Start filesystem observer
        self._observer = Observer()
        self._observer.schedule(self._handler, str(self.media_dir), recursive=True)
        self._observer.daemon = True
        self._observer.start()

        # Start async worker task
        active_loop = loop or asyncio.get_event_loop()
        self._worker_task = active_loop.create_task(self._process_pending_worker())
        logger.info(f"MediaWatcher started watching: {self.media_dir}")

    def stop(self) -> None:
        """Stop watcher and release observer threads and worker tasks."""
        self._running = False

        if self._worker_task and not self._worker_task.done():
            self._worker_task.cancel()
            self._worker_task = None

        if self._observer:
            try:
                self._observer.stop()
                self._observer.join(timeout=2.0)
            except Exception:
                pass
            self._observer = None

        with self._lock:
            self._pending_changes.clear()
            self._pending_deletions.clear()

        logger.info("MediaWatcher stopped cleanly.")


media_watcher = MediaWatcher()
