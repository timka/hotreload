NOTE
----

This is a resurrection of no longer available https://github.com/imjuls/hotreload

Source taken from https://testpypi.python.org/pypi/hotreload/0.1.0

This is obsolete. Use jurigged instead
https://github.com/breuleux/jurigged

=========
hotreload
=========

``hotreload`` is an automatic code reloader for Python modules.

The purpose of ``hotreload`` is to enable rapid prototyping without the need to
restart the Python interpreter after making code changes. It can be imported as
a library or run as a command line program.

``hotreload`` requires Python 3.4+.




Usage
-----
``hotreload.watch(path)`` starts a background thread which monitors changes to
all modules residing in the specified directory (or parent directory, if
a filename is given). When a change is detected, the module is reloaded
automatically. ::

    import hotreload
    observer = hotreload.watch(__file__)

Alternatively, the ``hotreload`` command line program can run a script
directly.

Usage: ``hotreload [options] file [...]``

-h, --help  show this help message and exit
-i          inspect interactively after running script

Hot swapping
~~~~~~~~~~~~
By default, when a module is reloaded, existing references to functions and
class instances will not be updated. ``hotreload`` provides a ``@hotswap``
decorator which performs this task.

Use the ``@hotswap`` decorator on classes and functions which should be
hot-swapped::

    from hotreload import hotswap

    @hotswap
    class Foo:
        def __reinit__(self):
            # Optional method.
            print('Re-initializing', self)

    @hotswap
    def adder(x, y):
        return x + y

    # Without hot swapping, these references would become out of date:
    add = adder
    foo = Foo()

To enable hot swapping of *all* top-level classes and functions in a module,
specify::

    __hotswap__ = True

This uses an ``import`` hook to automatically apply the ``@hotswap`` decorator
where needed.

Working with ``asyncio``
~~~~~~~~~~~~~~~~~~~~~~~~
``hotreload`` can integrate with an `asyncio`_ event loop, delegating module
reloads to the thread on which the loop is running::

    import asyncio
    hotreload.watch(__file__, loop=asyncio.get_event_loop())


Installation
------------
``hotreload`` is available on PyPI::

    pip install hotreload

``hotreload`` tries to use the `watchdog`_ package if it is available, and
falls back to a polling observer if it isn't. To install this optional
dependency along with hotreload, use::

    pip install hotreload[watchdog]

License
-------
``hotreload`` is dedicated to the Public Domain under the `CC0 1.0`_ license.

.. _asyncio: https://docs.python.org/3/library/asyncio.html
.. _watchdog: http://pythonhosted.org/watchdog/
.. _CC0 1.0: https://creativecommons.org/publicdomain/zero/1.0/
