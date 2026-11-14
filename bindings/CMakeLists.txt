# pybind11 generates the glue between C++ and CPython. Like GoogleTest it is
# fetched at a pinned version rather than installed system-wide.
include(FetchContent)
FetchContent_Declare(
  pybind11
  GIT_REPOSITORY https://github.com/pybind/pybind11.git
  GIT_TAG        v3.0.4
  GIT_SHALLOW    TRUE
  SYSTEM
)
# Use CMake's modern FindPython, which honours -DPython_EXECUTABLE=... so the
# module is built for the interpreter in the project's virtual environment.
set(PYBIND11_FINDPYTHON ON)
FetchContent_MakeAvailable(pybind11)

# Builds a shared library that CPython can import: pylob.cpython-<ver>-darwin.so
pybind11_add_module(pylob pylob.cpp)
target_link_libraries(pylob PRIVATE lob lob_warnings)

# Put the built module in python/, next to the Python code that imports it.
set_target_properties(pylob PROPERTIES LIBRARY_OUTPUT_DIRECTORY ${CMAKE_SOURCE_DIR}/python)
