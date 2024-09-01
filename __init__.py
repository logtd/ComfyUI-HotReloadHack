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

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from aiohttp import web

import folder_paths
from nodes import load_custom_node
from comfy_execution import caching

# ==============================================================================
# === GLOBALS ===
# ==============================================================================

RELOADED_CLASS_TYPES = {}
CUSTOM_NODE_ROOT = folder_paths.folder_names_and_paths["custom_nodes"][0]

EXCLUDE_MODULES = ['ComfyUI-Manager', 'ComfyUI-HotReloadHack']
if (HOTRELOAD_EXCLUDE := os.getenv("HOTRELOAD_EXCLUDE", None)) is not None:
    EXCLUDE_MODULES.extend(x for x in HOTRELOAD_EXCLUDE.split(',') if len(x))
EXCLUDE_MODULES = set(EXCLUDE_MODULES)

HOTRELOAD_OBSERVE_ONLY = os.getenv("HOTRELOAD_OBSERVE_ONLY", '')
HOTRELOAD_OBSERVE_ONLY = set(x for x in HOTRELOAD_OBSERVE_ONLY.split(',') if len(x))

HOTRELOAD_EXTENSIONS = os.getenv("HOTRELOAD_EXTENSIONS", '.py, .json, .yaml')
HOTRELOAD_EXTENSIONS = set(x for x in HOTRELOAD_EXTENSIONS.split(',') if len(x))

DEBOUNCE_TIME = os.getenv("HOTRELOAD_EXTENSIONS", 1.0) # seconds

# ==============================================================================
# === SUPPORT ===
# ==============================================================================

def hash_file(file_path):
    try:
        with open(file_path, 'rb') as f:
            return hashlib.md5(f.read()).hexdigest()
    except Exception as e:
        logging.error(f"Error reading file {file_path}: {e}")
        return None

def is_hidden_file_windows(file_path: str):
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
        # FILE_ATTRIBUTE_HIDDEN is 0x2
        return attribute & 0x2 != 0
    except Exception as e:
        print(f"Error checking if file is hidden: {e}")
        return False

def is_hidden_file(file_path: str):
    """
    Check if a given file or any of its parent directories is hidden.

    Works across all major operating systems (Windows, Linux, macOS).

    :param file_path: Path to the file or directory to check.
    :return: True if the file or any parent directory is hidden, False otherwise.
    """
    file_path = os.path.abspath(file_path)

    # Windows-specific hidden file check
    if sys.platform.startswith('win'):
        if is_hidden_file_windows(file_path):
            return True

        # Check parent directories
        # Until reaching the root directory
        while file_path != os.path.dirname(file_path):
            file_path = os.path.dirname(file_path)
            if is_hidden_file_windows(file_path):
                return True

    else:
        # Unix-like systems check
        # Check if the file itself or any parent directory is hidden
        # Until reaching the root directory
        while file_path != os.path.dirname(file_path):
            if os.path.basename(file_path).startswith('.'):
                return True
            file_path = os.path.dirname(file_path)

    return False

def dfs(item_list, searches):
    """Performs a depth-first search to find items in the list."""
    for item in item_list:
        if (isinstance(item, frozenset) or isinstance(item, tuple)) and dfs(item, searches):
            return True
        elif item in searches:
            return True
    return False

# ==============================================================================
# === CLASS ===
# ==============================================================================

class DebouncedHotReloader(FileSystemEventHandler):
    """Hot reloader with debouncing mechanism to reload modules on file changes."""

    def __init__(self, delay=1.0):
        self.__delay = delay
        self.__last_modified = defaultdict(float)
        self.__reload_timers = {}
        self.__hashes = {}
        # To prevent race conditions
        self.__lock = threading.Lock()

    def __reload(self, module_name: str):
        """Reloads all relevant modules and clears caches."""
        with self.__lock:
            reload_modules = [mod_name for mod_name in sys.modules.keys() if module_name in mod_name and mod_name != module_name]
            for reload_mod in reload_modules:
                del sys.modules[reload_mod]

            if module_name in sys.modules:
                del sys.modules[module_name]

            module_path_init = os.path.join(CUSTOM_NODE_ROOT[0], module_name, '__init__.py')
            spec = importlib.util.spec_from_file_location(module_name, module_path_init)
            module = importlib.util.module_from_spec(spec)

            try:
                sys.modules[module_name] = module
                spec.loader.exec_module(module)
                for key in module.NODE_CLASS_MAPPINGS.keys():
                    RELOADED_CLASS_TYPES[key] = 3  # 3 caches to rule them all
            except Exception as e:
                logging.error(f"Failed to reload module {module_name}: {e}")
                return web.Response(text='FAILED')

            module_path = os.path.join(CUSTOM_NODE_ROOT[0], module_name)
            load_custom_node(module_path)
            return web.Response(text='OK')

    def on_modified(self, event):
        """Handles file modification events."""
        if event.is_directory:
            return

        file_path = event.src_path

        # * is all extensions otherwise, only the ones we are watching
        if not any(x=='*' for x in HOTRELOAD_EXTENSIONS):
            if not all(file_path.endswith(x) for x in HOTRELOAD_EXTENSIONS):
                return

        if self.__is_hidden(file_path):
            return

        relative_path = os.path.relpath(file_path, CUSTOM_NODE_ROOT[0])
        root_dir = relative_path.split(os.path.sep)[0]

        if len(HOTRELOAD_OBSERVE_ONLY):
            if root_dir not in HOTRELOAD_OBSERVE_ONLY:
                return
        elif root_dir in EXCLUDE_MODULES:
            return

        current_hash = hash_file(file_path)
        if current_hash == self.__hashes.get(file_path):
            logging.debug(f"Python file {file_path} triggered event but content hasn't changed. Ignoring.")
            return

        self.__hashes[file_path] = current_hash
        self.schedule_reload(root_dir)

    def schedule_reload(self, module_name):
        """Schedules a reload of the given module after a delay."""
        current_time = time.time()
        self.__last_modified[module_name] = current_time

        with self.__lock:
            if module_name in self.__reload_timers:
                self.__reload_timers[module_name].cancel()

            timer = threading.Timer(self.__delay, self.check_and_reload, args=[module_name, current_time])
            self.__reload_timers[module_name] = timer
            timer.start()

    def check_and_reload(self, module_name, scheduled_time):
        """Checks the timestamp and reloads the module if needed."""
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

    def __init__(self, delay=1.0):
        self.__observer = None
        self.__watch_thread = None
        self.__stop_event = threading.Event()
        self.__delay = delay

    def start(self):
        """Starts the hot reloader service."""
        if self.__observer is not None:
            return

        event_handler = DebouncedHotReloader(delay=self.__delay)
        self.__observer = Observer()
        self.__observer.schedule(event_handler, CUSTOM_NODE_ROOT[0], recursive=True)

        self.__watch_thread = threading.Thread(target=self._run_observer)
        self.__watch_thread.start()
        logging.info(f"Hot Reloader started monitoring {CUSTOM_NODE_ROOT[0]} with a {self.__delay} second delay")

    def stop(self):
        """Stops the hot reloader service."""
        if self.__observer is None:
            return

        logging.info("Stopping Hot Reloader...")
        self.__stop_event.set()
        self.__observer.stop()
        self.__watch_thread.join()
        self.__observer = None
        self.__watch_thread = None
        logging.info("Hot Reloader stopped")

    def _run_observer(self):
        """Runs the observer in a separate thread."""
        self.__observer.start()
        try:
            while not self.__stop_event.is_set():
                self.__stop_event.wait(1)
        except Exception as e:
            logging.error(f"Observer encountered an error: {e}")
        finally:
            self.__observer.stop()
            self.__observer.join()

# ==============================================================================
# === ...A PURSUING JELLY WHICH RISES ABOVE THE UNCLEAN FROTH... ===
# ==============================================================================

def monkeypatch():
    """Monkey patches the cache system to support hot reloading."""
    original_set_prompt = caching.BasicCache.set_prompt

    def set_prompt(self, dynprompt, node_ids, is_changed_cache):
        """Overrides set_prompt to clear and reload cache when necessary."""
        if not hasattr(self, 'cache_key_set'):
            RELOADED_CLASS_TYPES.clear()
            return original_set_prompt(self, dynprompt, node_ids, is_changed_cache)

        found_keys = []
        for key, item_list in self.cache_key_set.keys.items():
            if dfs(item_list, RELOADED_CLASS_TYPES):
                found_keys.append(key)

        if found_keys:
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

# ==============================================================================
# === INITIALIZATION ===
# ==============================================================================

monkeypatch()

hot_reloader = HotReloaderService(delay=DEBOUNCE_TIME)
hot_reloader.start()

atexit.register(lambda: hot_reloader.stop())

NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}
