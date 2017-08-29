import re
import ast

from os import path
from setuptools import setup


def _read(filename):
    with open(path.join(path.dirname(__file__), filename)) as infile:
        return infile.read()


version = str(ast.literal_eval(
    re.search(r'__version__\s*=\s*(.*)', _read('hotreload.py')).group(1)))

setup(
    name='hotreload',
    version=version,
    url='https://github.com/timka/hotreload',
    description='automatic code reloader with hot swapping',
    long_description=_read('README.rst'),
    author='Julian Schleemann',
    author_email='julian.schleemann@gmail.com',
    license='CC0',
    platforms='ALL',
    keywords='reload reloading hotswap',
    py_modules=['hotreload'],
    extras_require={'watchdog': ['watchdog>=0.8']},
    entry_points={'console_scripts': ['hotreload=hotreload:main']},
    classifiers=[
        'Development Status :: 3 - Alpha',
        'Environment :: Console',
        'Intended Audience :: Developers',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.4',
        'Programming Language :: Python :: 3.5',
        'Programming Language :: Python :: 3 :: Only',
        'Topic :: System :: Filesystems',
        'Topic :: System :: Monitoring',
        'Topic :: Utilities'
    ]
)
