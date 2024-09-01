import hashlib
import sys
import os
import threading
import requests
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import importlib
from nodes import load_custom_node
import folder_paths
import logging
import atexit
from collections import defaultdict
import time
import importlib

from aiohttp import web
from server import PromptServer

from nodes import load_custom_node
from comfy_execution import caching


from comfy.cli_args import args


reloaded_class_types = {}


def dfs(item_list, searches):
    for item in item_list:
        if (isinstance(item, frozenset) or isinstance(item, tuple)) and dfs(item, searches):
            return True
        elif item in searches:
            return True
    return False

def monkeypatch():
    # i'm not proud, but i can hot reload
    original_set_prompt = caching.BasicCache.set_prompt
    def set_prompt(self, dynprompt, node_ids, is_changed_cache):
        if not hasattr(self, 'cache_key_set'):
            reloaded_class_types.clear()
            return original_set_prompt(self, dynprompt, node_ids, is_changed_cache)
        
        found_keys = []
        for key, item_list in self.cache_key_set.keys.items():
            if dfs(item_list, reloaded_class_types):
                found_keys.append(key)

        if len(found_keys):
            for value_key in list(reloaded_class_types.keys()):
                reloaded_class_types[value_key] -= 1
                if reloaded_class_types[value_key] == 0:
                    del reloaded_class_types[value_key]

        
        for key in found_keys:
            cache_key = self.cache_key_set.get_data_key(key)
            if cache_key and cache_key in self.cache:
                del self.cache[cache_key]
                del self.cache_key_set.keys[key]
                del self.cache_key_set.subcache_keys[key]
        return original_set_prompt(self, dynprompt, node_ids, is_changed_cache)
    
    caching.HierarchicalCache.set_prompt = set_prompt

monkeypatch()

routes = PromptServer.instance.routes


def del_module(module_name):
    if module_name in sys.modules:
        del sys.modules[module_name]


def reload_module(module_name):
    module_path = os.path.join('custom_nodes', module_name, '__init__.py')
    if module_name in sys.modules:
        del sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    for key in module.NODE_CLASS_MAPPINGS.keys():
        reloaded_class_types[key] = 3 # 3 caches to rule them all


def reload(module_name):
    reload_modules = [mod_name for mod_name in sys.modules.keys() if module_name in mod_name]
    for reload_mod in reload_modules:
        del_module(reload_mod)
    reload_module(module_name)
    load_custom_node('custom_nodes/' + module_name)
    return web.Response(text='OK')


EXCLUDED_REPOS = set(['ComfyUI-Manager', 'ComfyUI-HotReloadHack'])
INCLUDED_FILE_TYPES = set(['.py', '.json', '.yaml'])


class DebouncedHotReloader(FileSystemEventHandler):
    def __init__(self, delay=1.0):
        self.delay = delay
        self.last_modified = defaultdict(float)
        self.reload_timers = {}
        self.file_hashes = {}

    def on_modified(self, event):
        if not event.is_directory:
            file_path = event.src_path

            _, file_extension = os.path.splitext(file_path)
            if file_extension not in INCLUDED_FILE_TYPES:
                return
            
            if self.is_hidden(file_path):
                return
            
            current_hash = self.get_file_hash(file_path)
            if current_hash == self.file_hashes.get(file_path):
                logging.debug(f"Python file {file_path} triggered event but content hasn't changed. Ignoring.")
                return
            
            self.file_hashes[file_path] = current_hash
            
            root_dir = self.get_root_directory(file_path)
            if root_dir not in EXCLUDED_REPOS:
                self.schedule_reload(root_dir)

    def is_hidden(self, file_path):
        # Check if file or any parent directory is hidden
        path = os.path.abspath(file_path)
        while path != os.path.dirname(path):  # Stop at the root directory
            if os.path.basename(path).startswith('.'):
                return True
            path = os.path.dirname(path)
        return False

    def get_file_hash(self, file_path):
        try:
            with open(file_path, 'rb') as f:
                return hashlib.md5(f.read()).hexdigest()
        except Exception as e:
            logging.error(f"Error reading file {file_path}: {e}")
            return None

    def get_root_directory(self, file_path):
        custom_nodes_dir = os.path.abspath('custom_nodes')
        relative_path = os.path.relpath(file_path, custom_nodes_dir)
        root_dir = relative_path.split(os.path.sep)[0]
        return root_dir

    def schedule_reload(self, module_name):
        current_time = time.time()
        self.last_modified[module_name] = current_time

        if module_name in self.reload_timers:
            self.reload_timers[module_name].cancel()

        timer = threading.Timer(self.delay, self.check_and_reload, args=[module_name, current_time])
        self.reload_timers[module_name] = timer
        timer.start()

    def check_and_reload(self, module_name, scheduled_time):
        if self.last_modified[module_name] == scheduled_time:
            self.call_reload(module_name)

    def call_reload(self, module_name):
        try:
            reload(module_name)
            logging.info(f'[ComfyUI-HotReloadHack] Reloaded module {module_name}')
        except requests.RequestException as e:
            logging.error(f"Error calling reload for module {module_name}: {e}")


class HotReloaderService:
    def __init__(self, delay=1.0):
        self.observer = None
        self.watch_thread = None
        self.stop_event = threading.Event()
        self.delay = delay

    def start(self):
        if self.observer is not None:
            return

        path = folder_paths.get_folder_paths('custom_nodes')[0]
        event_handler = DebouncedHotReloader(delay=self.delay)
        self.observer = Observer()
        self.observer.schedule(event_handler, path, recursive=True)
        
        self.watch_thread = threading.Thread(target=self._run_observer)
        self.watch_thread.start()
        logging.info(f"Hot Reloader started monitoring {path} with a {self.delay} second delay")

    def stop(self):
        if self.observer is None:
            return

        logging.info("Stopping Hot Reloader...")
        self.stop_event.set()
        self.observer.stop()
        self.watch_thread.join()
        self.observer = None
        self.watch_thread = None
        logging.info("Hot Reloader stopped")

    def _run_observer(self):
        self.observer.start()
        try:
            while not self.stop_event.is_set():
                self.stop_event.wait(1)
        finally:
            self.observer.stop()
            self.observer.join()


debounce_time = 1.0 # seconds
hot_reloader = HotReloaderService(delay=debounce_time)
hot_reloader.start()

atexit.register(lambda: hot_reloader.stop())


NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}