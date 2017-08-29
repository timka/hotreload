# -*- coding: utf-8 -*-
#
# hotreload.py - Automatic code reloading with hot-swappable classes.
#
# Written in 2016 by Julian Schleemann (julian.schleemann@gmail.com)
#
# To the extent possible under law, the author(s) have dedicated all
# copyright and related and neighboring rights to this software to
# the public domain worldwide. This software is distributed without any
# warranty.
#
# You should have received a copy of the CC0 Public Domain Dedication
# along with this software.
#
# If not, see <http://creativecommons.org/publicdomain/zero/1.0/>.

"""
hotreload
=========

Automatic code reloading with hot swapping.
"""

import os
import ast
import sys
import time
import types
import logging
import weakref
import importlib
import threading
import traceback

from os import path
from functools import wraps
from collections import deque, OrderedDict
from importlib.abc import MetaPathFinder
from importlib.machinery import ModuleSpec, PathFinder
from importlib.machinery import BuiltinImporter, SourceFileLoader

__version__ = '0.1.0'

log = logging.getLogger('hotreload')

#: Name of "magic" flag; if a module sets this to True, all top-level
#: functions and classes are hotswapped automatically.
HOTSWAP_MAGIC = '__hotswap__'

#: Specifies that the traceback of an exception raised by a failed module
#: (re)load should only include frames originating from the reloaded module,
#: i.e. no importlib or hotreload internals.
TRIM_IMPORT_ERROR_TRACEBACK = True

#: Mapping of paths to observers.
observers = {}


class Registry(object):
    def __init__(self):
        self.functions = weakref.WeakValueDictionary()
        self.classes = weakref.WeakValueDictionary()
        self.instances = weakref.WeakKeyDictionary()


registry = Registry()


def hotswap(obj):
    """
    Decorator for hot-swappable functions and classes.
    """
    if isinstance(obj, types.FunctionType):
        return hotswap_function(obj)
    elif isinstance(obj, type):
        obj.__hotswap__ = True
        return hotswap_class(obj)
    else:
        raise TypeError('{!r} type cannot be hotswapped'.format(
            type(obj).__name__))


def hotswap_function(func):
    functions = registry.functions
    qualname = func.__qualname__  # Py 3.3.
    module = func.__module__
    key = (module, qualname)

    try:
        f = functions[key]
    except KeyError:
        # First time defining this function.
        return functions.setdefault(key, func)

    log.debug('Redefining function %r in %s', qualname, module)

    f.__code__ = func.__code__
    return f


def hotswap_class(cls):
    classes = registry.classes
    instances = registry.instances
    qualname = cls.__qualname__
    module = cls.__module__
    bases = cls.__bases__
    attrs = cls.__dict__
    key = (module, qualname)

    __new__ = cls.__new__

    @wraps(__new__)
    def __new_instance__(cls, *args, **kwargs):
        # object.__new__ doesn't take any arguments.
        if __new__ is object.__new__:
            obj = __new__(cls)
        else:
            obj = __new__(cls, *args, **kwargs)
        try:
            instances[c].add(obj)
        except KeyError:
            instances[c] = weakref.WeakSet((obj,))
        return obj

    cls.__new__ = __new_instance__

    if key not in classes:
        # First time defining this class.
        return classes.setdefault(key, cls)

    log.debug('Redefining class %r in %s', qualname, module)

    c = classes[key]
    c.__bases__ = bases

    # We cannot directly set the items in the class dict with
    # `c.__dict__[key] = value` or `c.__dict__.update()`. The only way around
    # that is to use standard attribute access.
    #
    # HOLD MY BEER: Using type.__setattr__ and type.__delattr__ completely
    # bypasses any metaclass logic behind setting and deleting attributes, but
    # the idea is that since we are merely "copying" from an already correctly
    # metaclass'd class, this should pose no problems per se.
    #
    # Of course, it can break other things in subtle ways. For example,
    # hotswapping a standard lib `enum.Enum` will update its class-level
    # attributes, but any existing singleton member instances in the wild that
    # were obtained before the reload will not be touched. This should be noted
    # in the docs.

    existing_attrs = tuple(c.__dict__)

    for attr, value in attrs.items():
        if attr == '__dict__':
            continue
        type.__setattr__(c, attr, value)

    for attr in existing_attrs:
        if attr not in attrs:
            type.__delattr__(c, attr)

    try:
        __reinit__ = c.__reinit__
    except AttributeError:
        pass
    else:
        for instance in instances.get(c, ()):
            __reinit__(instance)

    # XXX: Do we need something similar for class-level re-init...
    # maybe __redefine__? Or calling the metaclass' __reinit__?

    return c


class ReloadHandler:

    def __init__(self, loop=None):
        self.loop = loop

    def on_modified(self, event):
        if event.is_directory:
            return
        filename = path.abspath(event.src_path)
        for module in list(sys.modules.values()):
            spec = getattr(module, '__spec__', None)
            if not spec:
                continue
            if spec.has_location:
                location = path.abspath(spec.origin)
            else:
                continue
            if location == filename:
                self.on_module_modified(module)
                return

    def on_module_modified(self, module):
        log.info('Reloading module %r', module.__name__)
        try:
            importlib.reload(module)
        except Exception as e:
            log.error(
                'An exception occurred while reloading the module'
                '\n\n' + format_trimmed_exception(e))

try:
    import watchdog
except ImportError:
    watchdog = None

if watchdog:

    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    from watchdog.events import (
        EVENT_TYPE_CREATED, EVENT_TYPE_MODIFIED, EVENT_TYPE_DELETED)

    class CollapsingDispatcherMixin:
        """
        This is a slight modification of watchdog's dispatch mechanism. It
        buffers events in separate per-file queues. When an event is fired,
        rather than being handled immediately, it is postponed for a brief
        period (`_event_delay`). Within this window, further incoming events
        associated with this file path are collapsed:

        - MODIFIED events cancel all pending MODIFIED events, effectively
          combining them into a single MODIFIED event. Any DELETED events are
          cancelled as well. Collapsing multiple MODIFIED events is of special
          interest to us because we only want to reload a module file once per
          "atomic" write.

        - CREATED and DELETED events cancel each other out. Some programs will
          delete the old file before writing out its current contents, or
          create temporary files which are deleted almost instantly. We don't
          want to know about either of these cases.
        """
        _event_delay = 0.2  # 200ms

        def dispatch_events(self, event_queue, timeout):
            try:
                event_buffer = self._event_buffer
            except AttributeError:
                event_buffer = self._event_buffer = {}

            now = time.time()

            # Process buffered events.
            for (src_path, watch), buffered in tuple(event_buffer.items()):
                while buffered:
                    event = buffered[0]
                    if getattr(event, '_cancelled', False):
                        buffered.pop()
                        event_queue.task_done()
                        continue
                    event_time = getattr(event, '_time', 0)
                    if now >= event_time:
                        # The first event fired for a file path invalidates all
                        # the others.
                        event_buffer.pop((src_path, watch))
                        self._dispatch_event(event_queue, watch, event)
                        break
                    else:
                        timeout = min(timeout, event_time - now)
                        break

            # Process new event.
            event, watch = event_queue.get(block=True, timeout=timeout)
            src_path = event.src_path

            if event.is_directory:
                self._dispatch_event(event_queue, watch, event)
            else:
                try:
                    buffered = event_buffer[src_path, watch]
                except KeyError:
                    buffered = event_buffer[src_path, watch] = deque()
                if not event.is_directory:
                    event_type = event.event_type
                    # Collapse events.
                    cancel = self._cancel
                    if event_type is EVENT_TYPE_MODIFIED:
                        cancel(buffered,
                               (EVENT_TYPE_DELETED, EVENT_TYPE_MODIFIED))
                    elif event_type is EVENT_TYPE_CREATED:
                        if cancel(buffered, (EVENT_TYPE_DELETED,)):
                            event = None
                    elif event_type is EVENT_TYPE_DELETED:
                        if cancel(buffered, (EVENT_TYPE_CREATED,)):
                            event = None
                        cancel(buffered, (EVENT_TYPE_MODIFIED,))

                if event:
                    event._time = now + self._event_delay
                    buffered.appendleft(event)
                else:
                    event_queue.task_done()

        @staticmethod
        def _cancel(l, event_types):
            n = 0
            for event in l:
                if event.event_type in event_types:
                    event._cancelled = True
                    n += 1
            return n

        def _dispatch_event(self, event_queue, watch, event):
            with self._lock:
                for handler in list(self._handlers.get(watch, [])):
                    if handler in self._handlers.get(watch, []):
                        handler.dispatch(event)
                event_queue.task_done()

    class WatchdogObserver(CollapsingDispatcherMixin, Observer):
        pass

    class WatchdogReloadHandler(ReloadHandler, FileSystemEventHandler):
        pass

    Handler = WatchdogReloadHandler
    Observer = WatchdogObserver

else:  # watchdog is not available

    class PollingModuleObserver(threading.Thread):

        interval = 2  # seconds

        def __init__(self):
            super(PollingModuleObserver, self).__init__()
            self.handler = None
            self._stop = False
            self.daemon = True

        def run(self):
            last_mtime = {}
            root_path = self.path

            while not self._stop:
                for name, module in tuple(sys.modules.items()):
                    spec = getattr(module, '__spec__')
                    if spec is None or not spec.has_location:
                        continue
                    filename = path.abspath(spec.origin)
                    if not filename.startswith(root_path):
                        continue
                    try:
                        mtime = os.stat(filename).st_mtime
                    except OSError:
                        pass
                    else:
                        if mtime > last_mtime.setdefault(module, mtime):
                            last_mtime[module] = mtime
                            self.handler.on_module_modified(module)
                time.sleep(self.interval)

        def schedule(self, handler, path, recursive):
            self.handler = handler
            self.path = path
            self.recursive = recursive

        def stop(self):
            self._stop = True

    Handler = ReloadHandler
    Observer = PollingModuleObserver


def format_trimmed_exception(exc):
    """
    Format exception to exclude "noise" from hotreload and importlib.
    """
    tb = traceback.extract_tb(exc.__traceback__)
    tb = [fs for fs in tb if _filter_tb(fs)]
    lines = ['Traceback (most recent call last):\n']
    lines.extend(traceback.format_list(tb))
    lines.extend(traceback.format_exception_only(type(exc), exc))
    return ''.join(lines).rstrip()


def _filter_tb(fs):
    if not TRIM_IMPORT_ERROR_TRACEBACK:
        return True
    # Return False for frame summaries to discard. Currently we are filtering
    # out importlib internals and hotreload itself.
    filename = fs.filename
    if filename == importlib.__file__:
        return False
    elif filename.startswith('<frozen importlib'):
        return False
    elif filename == __file__:
        return False
    return True


_loop_class_handlers = OrderedDict()


def register_handler(loop_class, handler_class):
    _loop_class_handlers[loop_class] = handler_class

# Specialized Handlers (only asyncio for now):

try:
    import asyncio
except ImportError:
    pass
else:
    class AsyncioReloadHandler(Handler):
        """
        Handle events in an asyncio event loop.
        """
        def __init__(self, loop=None):
            import asyncio
            if loop is None:
                loop = asyncio.get_event_loop()
            super(AsyncioReloadHandler, self).__init__(loop=loop)

        def dispatch(self, event):
            self.loop.call_soon_threadsafe(
                super(AsyncioReloadHandler, self).dispatch, event)

    register_handler(asyncio.AbstractEventLoop, AsyncioReloadHandler)


def watch(dirpath, loop=None):
    """
    Monitor the specified directory, reloading modules in it when they are
    changed.

    Returns a stoppable thread.
    """
    if loop is not None:
        for base_class, handler_class in _loop_class_handlers.items():
            if isinstance(loop, base_class):
                break
        else:
            raise TypeError(
                'No suitable handler class was found for {!r}'.format(loop))
    else:
        handler_class = Handler

    dirpath = path.realpath(dirpath)
    if path.isfile(dirpath):
        # Watch the parent directory.
        dirpath = path.split(dirpath)[0]
    try:
        observer = observers[dirpath]
    except KeyError:
        pass
    else:
        log.debug('Already watching %s', dirpath)
        return observer

    log.info('Watching %s', dirpath)

    observer = observers[dirpath] = Observer()
    observer.schedule(handler_class(loop), dirpath, recursive=True)
    observer.start()
    return observer


class AutoDecorate(ast.NodeTransformer):
    """
    Modifies the AST to decorate top-level functions and classes with the
    @hotswap decorator.

    This only happens if the HOTSWAP_MAGIC flag is found and set to True.
    """

    # Note: ast.copy_location and ast.fix_missing_locations are needed to fill
    # in a newly generated AST node's `lineno` field which links it to the
    # corresponding line in the source code; compile() complains if it's
    # missing.

    def __init__(self):
        self.decorate = set()
        # Normal variables can't begin with a number. Using this name prevents
        # any collisions with existing names.
        self.name_hotswap = '0hotswap'

    def visit_Module(self, module):
        # Look for magic `__hotswap__ = True` statement.
        magic_assigns = [
            node for node in module.body
            if isinstance(node, ast.Assign) and
            any(t.id == HOTSWAP_MAGIC for t in node.targets)]

        if magic_assigns:
            magic = magic_assigns[-1]
            if not (isinstance(magic.value, ast.NameConstant) and
                    magic.value.value is True):
                return module  # Assigned some value other than True.
        else:
            return module  # Not found.

        # Import the hotswap decorator.
        import_ = ast.ImportFrom(
            module='hotreload',
            names=[ast.alias(name='hotswap', asname=self.name_hotswap)],
            level=0)
        module.body.insert(0, ast.fix_missing_locations(import_))

        # Mark top-level classes and functions for decoration.
        self.decorate.update(
            node for node in module.body
            if isinstance(node, ast.ClassDef))
        self.decorate.update(
            node for node in module.body
            if isinstance(node, ast.FunctionDef))

        return self.generic_visit(module)

    def visit_FunctionDef(self, fdef):
        # It *would* be nice to check here if the @hotswap decorator is already
        # present, but if we wanted to be thorough, that would require
        # recursively resolving each ast.Name reference in the decorator list.
        # But since @hotswap is idempotent, it's easier to just not worry about
        # it.
        if fdef in self.decorate:
            name = ast.copy_location(
                ast.Name(id=self.name_hotswap, ctx=ast.Load()), fdef)
            # Decorators are chained top to bottom, making this the outermost:
            fdef.decorator_list.insert(0, name)
        return fdef

    visit_ClassDef = visit_FunctionDef  # Works the same way.


class HotswapLoader(importlib.machinery.SourceFileLoader):
    """
    Applies `AutoDecorate` modifications to the source code's AST before
    compiling it.
    """
    def source_to_code(self, data, path, *, optimize=-1):
        tree = compile(data, path, mode='exec', flags=ast.PyCF_ONLY_AST)
        tree = AutoDecorate().visit(tree)
        return compile(tree, path, mode='exec')


class HotswapFinder(PathFinder):
    """
    Overrides the default importlib PathFinder to use the `HotswapLoader`
    instead of `SourceFileLoader` to load source files.
    """
    # find_spec was introduced in Python 3.4. Before that, find_module would
    # have been the entry point.
    @classmethod
    def find_spec(cls, fullname, path, target=None):
        spec = super(HotswapFinder, cls).find_spec(fullname, path, target)
        if not spec:
            return
        if isinstance(spec.loader, SourceFileLoader):
            spec.loader.__class__ = HotswapLoader
            # e.g. extension modules etc.
        return spec


class MainModuleFinder(MetaPathFinder):
    """
    Makes the __main__ module reloadable.
    """
    @staticmethod
    def find_spec(fullname, path, target=None):
        __main__ = sys.modules.get('__main__', None)
        if target is __main__:
            if sys.argv:
                filename = sys.argv[0]
                spec = ModuleSpec(
                    name='__main__',
                    loader=HotswapLoader('__main__', filename),
                    origin=filename)
                spec.has_location = True
            else:
                spec = ModuleSpec(name='__main__', loader=BuiltinImporter)
            return spec


def main():
    """
    Entry point for the `hotreload` command line program.
    """
    import argparse
    parser = argparse.ArgumentParser(
        prog='hotreload',
        description='Run Python script with hotreload.')
    parser.add_argument('-i', dest='interactive', action='store_const',
                        const=True, default=False,
                        help='inspect interactively after running script')
    parser.add_argument('filename', metavar='file', type=str, nargs=1,
                        help='program read from script file')
    parser.add_argument('args', metavar='arg ...', type=str, nargs='?',
                        help='arguments passed to program in sys.argv[1:]')
    args, _ = parser.parse_known_args()

    filename = args.filename[0]

    if not path.exists(filename):
        print("hotreload: can't open '%s': no such file" % filename,
              file=sys.stderr)
        return

    # Remove the args to hotreload.
    del sys.argv[0:2 if not args.interactive else 3]
    # Put the script's filename back as the first argument (as expected).
    sys.argv.insert(0, filename)

    print('hotreload: running script %s' % filename, file=sys.stderr)

    # Ensure the script gets its own namespace rather than inheriting this one.
    del sys.modules['__main__']

    # Important: specifying '__main__' here sets the module's __name__
    # to '__main__', which is how scripts identify themselves.
    loader = HotswapLoader('__main__', filename)

    try:
        module = loader.load_module()
    except Exception as e:
        print(format_trimmed_exception(e), file=sys.stderr)
        return

    module.__spec__ = MainModuleFinder.find_spec('__main__', None, module)
    assert module.__spec__

    watch(filename)

    if args.interactive and not sys.flags.interactive:
        import code
        code.interact(banner='', local=module.__dict__)

try:
    SENTINEL  # In case hotreload itself gets reloaded.
except NameError:
    SENTINEL = True
    sys.meta_path.insert(0, HotswapFinder)
    sys.meta_path.append(MainModuleFinder)

__all__ = ['watch', 'hotswap']

if __name__ == '__main__':
    # In case a script imports hotreload -- we don't need two copies of the
    # same module.
    __name__ = 'hotreload'
    sys.modules['hotreload'] = sys.modules['__main__']
    main()
else:
    import __main__
    __main__.__spec__ = MainModuleFinder.find_spec('__main__', None, __main__)
