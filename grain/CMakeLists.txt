cmake_minimum_required(VERSION 3.15)
project(touchseq-grain)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
if(NOT CMAKE_BUILD_TYPE)
  set(CMAKE_BUILD_TYPE Release)
endif()

# The same DPF checkout the other plugins use (git submodule: git submodule update --init --recursive)
set(DPF_DIR "${CMAKE_SOURCE_DIR}/../xyfx/dpf" CACHE PATH "DPF checkout")
if(NOT EXISTS "${DPF_DIR}/distrho/DistrhoPlugin.hpp")
  message(FATAL_ERROR "DPF not found at ${DPF_DIR} - run: git submodule update --init --recursive")
endif()
add_subdirectory(${DPF_DIR} dpf)

# The WAV reader is the sampler's (public domain / MIT-0): the source picker is the sampler's pad map.
dpf_add_plugin(touchseq-grain
  TARGETS lv2 clap
  FILES_DSP GrainPlugin.cpp GrainCore.cpp)
target_include_directories(touchseq-grain PUBLIC "." "${CMAKE_SOURCE_DIR}/../sampler")

# DSP test: ./grain-test [demo.wav]
add_executable(grain-test test.cpp GrainCore.cpp)
target_include_directories(grain-test PRIVATE "." "${CMAKE_SOURCE_DIR}/../sampler")
target_compile_options(grain-test PRIVATE -UNDEBUG)  # its checks are asserts
