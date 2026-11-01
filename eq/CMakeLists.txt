cmake_minimum_required(VERSION 3.15)
project(touchseq-eq)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()

# The same DPF checkout the XYFX and monitor plugins use (git submodule)
set(DPF_DIR "${CMAKE_SOURCE_DIR}/../xyfx/dpf" CACHE PATH "DPF checkout")
if(NOT EXISTS "${DPF_DIR}/distrho/DistrhoPlugin.hpp")
  message(FATAL_ERROR "DPF not found at ${DPF_DIR} - run: git submodule update --init --recursive")
endif()
add_subdirectory(${DPF_DIR} dpf)

dpf_add_plugin(touchseq-eq
  TARGETS lv2 clap
  FILES_DSP EQPlugin.cpp)
target_include_directories(touchseq-eq PUBLIC ".")

# DSP test: ./eq-test
add_executable(eq-test test.cpp)
target_compile_options(eq-test PRIVATE -UNDEBUG)  # its checks are asserts
