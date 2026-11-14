"""pytest configuration: make the built pylob module importable.

CMake writes pylob.cpython-*.so into this directory. pytest puts the directory
of a conftest.py on sys.path, so tests can `import pylob` without installing it.
"""
