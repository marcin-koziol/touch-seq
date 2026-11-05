cmake_minimum_required(VERSION 3.15)
project(touchseq-sampler)

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

dpf_add_plugin(touchseq-sampler
  TARGETS lv2 clap
  FILES_DSP SamplerPlugin.cpp SamplerCore.cpp)
target_include_directories(touchseq-sampler PUBLIC ".")

# DSP test: ./sampler-test
add_executable(sampler-test test.cpp SamplerCore.cpp)
target_include_directories(sampler-test PRIVATE ".")
target_compile_options(sampler-test PRIVATE -UNDEBUG)  # its checks are asserts
