cmake_minimum_required(VERSION 3.15)
project(touchseq-space)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()

# The same DPF checkout the XYFX plugin uses (git submodule: git submodule update --init --recursive)
set(DPF_DIR "${CMAKE_SOURCE_DIR}/../xyfx/dpf" CACHE PATH "DPF checkout")
if(NOT EXISTS "${DPF_DIR}/distrho/DistrhoPlugin.hpp")
  message(FATAL_ERROR "DPF not found at ${DPF_DIR} - run: git submodule update --init --recursive")
endif()
add_subdirectory(${DPF_DIR} dpf)

dpf_add_plugin(touchseq-space
  TARGETS lv2 clap
  FILES_DSP SpacePlugin.cpp)
target_include_directories(touchseq-space PUBLIC ".")

# DSP test: ./space-test
add_executable(space-test test.cpp)
target_compile_options(space-test PRIVATE -UNDEBUG)  # its checks are asserts
