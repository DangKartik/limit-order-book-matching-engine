# FetchContent downloads and builds GoogleTest as part of our own build, pinned
# to an exact release, so there is nothing to install system-wide and every
# machine builds against the same version. Needs network access the first time.
include(FetchContent)
FetchContent_Declare(
  googletest
  GIT_REPOSITORY https://github.com/google/googletest.git
  GIT_TAG        v1.17.0
  GIT_SHALLOW    TRUE
  # SYSTEM marks GoogleTest's headers as third-party, so our strict warning
  # flags do not report issues inside code we do not own.
  SYSTEM
)
set(INSTALL_GTEST OFF CACHE BOOL "" FORCE)
set(BUILD_GMOCK OFF CACHE BOOL "" FORCE)   # we only use plain GoogleTest, not the mocking library
FetchContent_MakeAvailable(googletest)

add_executable(lob_tests
  order_book_test.cpp
  differential_test.cpp
  order_pool_test.cpp
)
target_link_libraries(lob_tests PRIVATE lob lob_warnings GTest::gtest_main)

# Registers each TEST() with CTest so `ctest` can list and run them individually.
include(GoogleTest)
gtest_discover_tests(lob_tests)
