cmake_minimum_required(VERSION 3.15)
project(touchseq-xyfx)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()

# DPF (https://github.com/DISTRHO/DPF) is a git submodule: git submodule update --init --recursive
set(DPF_DIR "${CMAKE_SOURCE_DIR}/dpf" CACHE PATH "DPF checkout")
if(NOT EXISTS "${DPF_DIR}/distrho/DistrhoPlugin.hpp")
  message(FATAL_ERROR "DPF not found at ${DPF_DIR} - run: git submodule update --init --recursive")
endif()
add_subdirectory(${DPF_DIR} dpf)

dpf_add_plugin(touchseq-xyfx
  TARGETS lv2 clap
  FILES_DSP XYFXPlugin.cpp)
target_include_directories(touchseq-xyfx PUBLIC ".")

# DSP test: ./xyfx-test
add_executable(xyfx-test test.cpp)
target_compile_options(xyfx-test PRIVATE -UNDEBUG)  # its checks are asserts
