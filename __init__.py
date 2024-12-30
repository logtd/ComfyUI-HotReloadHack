"""
"""

import os
import sys
import time
import atexit
import hashlib
import logging
import requests
import threading
import importlib
from collections import defaultdict

from watchdog.observers.polling import PollingObserver as Observer
from watchdog.events import FileSystemEventHandler
from aiohttp import web

import folder_paths
from nodes import load_custom_node
from comfy_execution import caching

# ==============================================================================
# === GLOBALS ===
# ==============================================================================

RELOADED_CLASS_TYPES: dict = {}  # Stores types of classes that have been reloaded.
CUSTOM_NODE_ROOT: list[str] = folder_paths.folder_names_and_paths["custom_nodes"][0]  # Custom Node root directory list.

# Set of modules to exclude from reloading.
EXCLUDE_MODULES: set[str] = {'ComfyUI-Manager', 'ComfyUI-HotReloadHack'}
if (HOTRELOAD_EXCLUDE := os.getenv("HOTRELOAD_EXCLUDE", None)) is not None:
    EXCLUDE_MODULES.update(x for x in HOTRELOAD_EXCLUDE.split(',') if x)

# Set of modules to observe exclusively for changes.
HOTRELOAD_OBSERVE_ONLY: set[str] = set(x for x in os.getenv("HOTRELOAD_OBSERVE_ONLY", '').split(',') if x)

# File extensions to watch for changes.
HOTRELOAD_EXTENSIONS: set[str] = set(x.strip() for x in os.getenv("HOTRELOAD_EXTENSIONS", '.py,.json,.yaml').split(',') if x)

# Time to wait before reloading after detecting a file change, default is 1.0 second.
try:
    DEBOUNCE_TIME: float = float(os.getenv("HOTRELOAD_DEBOUNCE_TIME", 1.0))
except ValueError:
    DEBOUNCE_TIME = 1.0

# ==============================================================================
# === SUPPORT FUNCTIONS ===
# ==============================================================================

def hash_file(file_path: str) -> str:
    """
    Computes the MD5 hash of a file's contents.

    :param file_path: The path to the file.
    :return: The MD5 hash as a hexadecimal string, or None if an error occurs.
    """
    try:
        with open(file_path, 'rb') as f:
            return hashlib.md5(f.read()).hexdigest()
    except Exception as e:
        logging.error(f"Error reading file {file_path}: {e}")
        return None

def is_hidden_file_windows(file_path: str) -> bool:
    """
    Check if a given file or directory is hidden on Windows.

    :param file_path: Path to the file or directory.
    :return: True if the file or directory is hidden, False otherwise.
    """
    try:
        import ctypes
        attribute = ctypes.windll.kernel32.GetFileAttributesW(file_path)
        if attribute == -1:
            return False
        return attribute & 0x2 != 0  # FILE_ATTRIBUTE_HIDDEN is 0x2
    except Exception as e:
        logging.error(f"Error checking if file is hidden on Windows: {e}")
        return False

def is_hidden_file(file_path: str) -> bool:
    """
    Check if a given file or any of its parent directories is hidden.

    Works across all major operating systems (Windows, Linux, macOS).

    :param file_path: Path to the file or directory to check.
    :return: True if the file or any parent directory is hidden, False otherwise.
    """
    file_path = os.path.abspath(file_path)

    # Windows-specific hidden file check
    if sys.platform.startswith('win'):
        while file_path and file_path != os.path.dirname(file_path):
            if is_hidden_file_windows(file_path):
                return True
            file_path = os.path.dirname(file_path)
    else:
        # Unix-like systems check
        while file_path and file_path != os.path.dirname(file_path):
            if os.path.basename(file_path).startswith('.'):
                return True
            file_path = os.path.dirname(file_path)

    return False

def dfs(item_list: list, searches: set) -> bool:
    """
    Performs a depth-first search to find items in a list.

    :param item_list: The list of items to search through.
    :param searches: The set of search items to look for.
    :return: True if any search item is found, False otherwise.
    """
    for item in item_list:
        if isinstance(item, (frozenset, tuple)) and dfs(item, searches):
            return True
        elif item in searches:
            return True
    return False

# ==============================================================================
# === CLASS DEFINITION ===
# ==============================================================================

class DebouncedHotReloader(FileSystemEventHandler):
    """Hot reloader with debouncing mechanism to reload modules on file changes."""

    def __init__(self, delay: float = 1.0):
        """
        Initialize the DebouncedHotReloader.

        :param delay: Delay in seconds before reloading modules after detecting a change.
        """
        self.__delay: float = delay
        self.__last_modified: defaultdict[str, float] = defaultdict(float)
        self.__reload_timers: dict[str, threading.Timer] = {}
        self.__hashes: dict[str, str] = {}
        self.__lock: threading.Lock = threading.Lock()

    def __reload(self, module_name: str) -> web.Response:
        """
        Reloads all relevant modules and clears caches.

        :param module_name: The name of the module to reload.
        :return: A web response indicating success or failure.
        """
        with self.__lock:
            reload_modules: list[str] = [
                mod_name for mod_name in sys.modules.keys()
                if module_name in mod_name and mod_name != module_name
            ]

            # Unload dependent modules first
            for reload_mod in reload_modules:
                del sys.modules[reload_mod]

            # Unload the main module
            if module_name in sys.modules:
                del sys.modules[module_name]

            module_path_init: str = os.path.join(CUSTOM_NODE_ROOT[0], module_name, '__init__.py')
            spec = importlib.util.spec_from_file_location(module_name, module_path_init)
            module = importlib.util.module_from_spec(spec)

            try:
                sys.modules[module_name] = module
                spec.loader.exec_module(module)
                for key in module.NODE_CLASS_MAPPINGS.keys():
                    RELOADED_CLASS_TYPES[key] = 3
            except Exception as e:
                logging.error(f"Failed to reload module {module_name}: {e}")
                return web.Response(text='FAILED')

            module_path: str = os.path.join(CUSTOM_NODE_ROOT[0], module_name)
            load_custom_node(module_path)
            return web.Response(text='OK')

    def on_modified(self, event):
        """Handles file modification events."""
        if event.is_directory:
            return

        file_path: str = event.src_path

        if not any(ext == '*' for ext in HOTRELOAD_EXTENSIONS):
            if not any(file_path.endswith(ext) for ext in HOTRELOAD_EXTENSIONS):
                return

        if is_hidden_file(file_path):
            return

        relative_path: str = os.path.relpath(file_path, CUSTOM_NODE_ROOT[0])
        root_dir: str = relative_path.split(os.path.sep)[0]

        if HOTRELOAD_OBSERVE_ONLY and root_dir not in HOTRELOAD_OBSERVE_ONLY:
            return
        elif root_dir in EXCLUDE_MODULES:
            return

        current_hash: str = hash_file(file_path)
        if current_hash == self.__hashes.get(file_path):
            logging.debug(f"File {file_path} triggered event but content hasn't changed. Ignoring.")
            return

        self.__hashes[file_path] = current_hash
        self.schedule_reload(root_dir)

    def schedule_reload(self, module_name: str):
        """
        Schedules a reload of the given module after a delay.

        :param module_name: The name of the module to reload.
        """
        current_time: float = time.time()
        self.__last_modified[module_name] = current_time

        with self.__lock:
            if module_name in self.__reload_timers:
                self.__reload_timers[module_name].cancel()

            timer = threading.Timer(self.__delay, self.check_and_reload, args=[module_name, current_time])
            self.__reload_timers[module_name] = timer
            timer.start()

    def check_and_reload(self, module_name: str, scheduled_time: float):
        """
        Checks the timestamp and reloads the module if needed.

        :param module_name: The name of the module to check.
        :param scheduled_time: The scheduled time for the reload.
        """
        with self.__lock:
            if self.__last_modified[module_name] != scheduled_time:
                return

        try:
            self.__reload(module_name)
            logging.info(f'[ComfyUI-HotReloadHack] Reloaded module {module_name}')
        except requests.RequestException as e:
            logging.error(f"Error calling reload for module {module_name}: {e}")
        except Exception as e:
            logging.exception(f"[ComfyUI-HotReloadHack] {e}")

class HotReloaderService:
    """Service to manage the hot reloading of modules."""

    def __init__(self, delay: float = 1.0):
        """
        Initialize the HotReloaderService.

        :param delay: Delay in seconds before reloading modules after detecting a change.
        """
        self.__observer: Observer = None
        self.__reloader: DebouncedHotReloader = DebouncedHotReloader(delay)

    def start(self):
        """Start observing for file changes."""
        self.__observer = Observer()
        self.__observer.schedule(self.__reloader, CUSTOM_NODE_ROOT[0], recursive=True)
        self.__observer.start()

    def stop(self):
        """Stop observing for file changes."""
        if self.__observer:
            self.__observer.stop()
            self.__observer.join()

# ==============================================================================
# === MONKEY PATCHING ===
# ==============================================================================

def monkeypatch():
    """Apply necessary monkey patches for hot reloading."""

    original_set_prompt = caching.BasicCache.set_prompt

    def set_prompt(self, dynprompt, node_ids, is_changed_cache):
        """
        Custom set_prompt function to handle cache clearing for hot reloading.

        :param dynprompt: Dynamic prompt to set.
        :param node_ids: Node IDs to process.
        :param is_changed_cache: Boolean flag indicating if cache has changed.
        """
        if not hasattr(self, 'cache_key_set'):
            RELOADED_CLASS_TYPES.clear()
            return original_set_prompt(self, dynprompt, node_ids, is_changed_cache)

        found_keys = []
        for key, item_list in self.cache_key_set.keys.items():
            if dfs(item_list, RELOADED_CLASS_TYPES):
                found_keys.append(key)

        if len(found_keys):
            for value_key in list(RELOADED_CLASS_TYPES.keys()):
                RELOADED_CLASS_TYPES[value_key] -= 1
                if RELOADED_CLASS_TYPES[value_key] == 0:
                    del RELOADED_CLASS_TYPES[value_key]

        for key in found_keys:
            cache_key = self.cache_key_set.get_data_key(key)
            if cache_key and cache_key in self.cache:
                del self.cache[cache_key]
                del self.cache_key_set.keys[key]
                del self.cache_key_set.subcache_keys[key]
        return original_set_prompt(self, dynprompt, node_ids, is_changed_cache)

    caching.HierarchicalCache.set_prompt = set_prompt

def setup():
    """Sets up the hot reload system."""
    logging.info("[ComfyUI-HotReloadHack] Monkey patching comfy_execution.caching.BasicCache")
    monkeypatch()
    logging.info("[ComfyUI-HotReloadHack] Starting Hot Reloader")
    hot_reloader_service = HotReloaderService(delay=DEBOUNCE_TIME)
    atexit.register(hot_reloader_service.stop)
    hot_reloader_service.start()

setup()

NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}
