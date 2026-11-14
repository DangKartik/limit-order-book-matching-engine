cmake_minimum_required(VERSION 3.25)   # 3.25 for FetchContent's SYSTEM option
project(lob VERSION 0.1.0 LANGUAGES CXX)

# C++20, strictly: no silent fallback to an older standard, no GNU extensions.
set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

# Writes build/compile_commands.json so editors (clangd, VS Code) understand the project.
set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

# Default to an optimised build. A build with no type set gets NO optimisation
# flags at all, which is a classic way to produce meaningless benchmark numbers.
if(NOT CMAKE_BUILD_TYPE AND NOT CMAKE_CONFIGURATION_TYPES)
  set(CMAKE_BUILD_TYPE Release CACHE STRING "Build type" FORCE)
endif()

option(LOB_BUILD_TESTS "Build the unit tests" ON)
option(LOB_BUILD_BENCH "Build the benchmark harness" ON)
option(LOB_SANITIZE "Build with AddressSanitizer + UndefinedBehaviorSanitizer" OFF)

# Sanitizers instrument the code to catch memory errors (use-after-free,
# out-of-bounds) and undefined behaviour (signed overflow, bad casts) at runtime.
# C++ has no garbage collector or bounds checks, so this is the safety net that
# Java/Python give you for free. Must be set before any target is created so
# that every target (including GoogleTest) is compiled and linked with it.
if(LOB_SANITIZE)
  add_compile_options(-fsanitize=address,undefined -fno-omit-frame-pointer -fno-sanitize-recover=undefined)
  add_link_options(-fsanitize=address,undefined)
endif()

# An INTERFACE target carries no code, only settings. Linking to it applies our
# warning flags to our own targets without forcing them on third-party code.
add_library(lob_warnings INTERFACE)
target_compile_options(lob_warnings INTERFACE -Wall -Wextra -Wpedantic -Wconversion -Wshadow)

# The engine: a header-only library with no I/O (it is a class template, so its
# code lives in headers; see include/lob/order_book.hpp). An INTERFACE target
# compiles nothing itself; it just tells anything that links to it where the
# headers are. Tests, benchmarks and the Python bindings all link to this target.
add_library(lob INTERFACE)
target_include_directories(lob INTERFACE include)

if(LOB_BUILD_TESTS)
  enable_testing()
  add_subdirectory(tests)
endif()

if(LOB_BUILD_BENCH)
  add_subdirectory(bench)
endif()
